"""Mini chat transports: Claude's CLI and Codex's local stdio app-server.

Only Mini-owned conversation ids are resumed. Prompts are stdin/JSON data,
never shell commands. Provider CLIs handle the user's existing sign-in.
"""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import shutil
import re
import subprocess
import threading
import time
import uuid

import psutil
from PySide6.QtCore import QObject, Signal

from . import __version__
from .common import data_path, load_json, save_json


def friendly_error(provider, text):
    text = str(text)
    lower = text.lower()
    if 'hit your weekly limit' in lower or 'hit your session limit' in lower:
        scope = '本周额度' if 'weekly' in lower else '当前时段额度'
        when = ''
        match = re.search(r'resets\s+([A-Za-z]+)\s+(\d+),\s*(\d+)(?::(\d+))?\s*(am|pm)', text, re.I)
        if match:
            month,day,hour,minute,ampm = match.groups()
            months = {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}
            h = int(hour) % 12 + (12 if ampm.lower()=='pm' else 0)
            when = f"，{months.get(month[:3].lower(),month)} 月 {day} 日 {h:02d}:{int(minute or 0):02d} 重置"
        return f"{provider.capitalize()} {scope}已用完{when}。额度恢复后可以继续发送。"
    return text


def cli_path(provider):
    explicit = os.environ.get('CLAUDE_PET_' + provider.upper() + '_BIN')
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which(provider + '.exe' if os.name == 'nt' else provider)
    if found:
        return found
    home = Path.home()
    candidates = [home / '.local' / 'bin' / (provider + ('.exe' if os.name == 'nt' else ''))]
    if provider == 'codex' and os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA') or home / 'AppData' / 'Local') / 'OpenAI' / 'Codex' / 'bin'
        candidates += sorted(base.glob('*/codex.exe'), key=lambda p:p.stat().st_mtime, reverse=True)
    for path in candidates:
        if path.is_file():
            return str(path)
    raise RuntimeError(f'没找到 {provider} 命令，请先安装并登录对应 CLI。')


def process_options(cwd):
    env = dict(os.environ)
    env.pop('CLAUDECODE', None)
    return dict(cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def stop_process(proc):
    """Stop this transport and only its descendants, never other assistant apps."""
    if proc is None or proc.poll() is not None:
        return
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try: child.terminate()
            except psutil.Error: pass
        parent.terminate()
        _, alive = psutil.wait_procs(children + [parent], timeout=1)
        for child in alive:
            try: child.kill()
            except psutil.Error: pass
    except psutil.Error:
        pass


class ClaudeTransport:
    def __init__(self, emit):
        self.emit = emit
        self.process = None
        self.cancelled = threading.Event()
        self.thread = None

    @staticmethod
    def command(executable, session_id):
        args = [executable, '-p', '--output-format', 'stream-json', '--verbose',
                '--include-partial-messages']
        if session_id:
            args += ['--resume', session_id]
        return args

    def send(self, text, cwd, session_id):
        self.cancelled.clear()
        self.thread = threading.Thread(target=self._run, args=(text,cwd,session_id), daemon=True)
        self.thread.start()

    def _run(self, text, cwd, session_id):
        errors = deque(maxlen=25)
        final = False
        result_obj = None
        streamed = False
        try:
            proc = subprocess.Popen(self.command(cli_path('claude'), session_id), **process_options(cwd))
            self.process = proc
            threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True).start()
            if self.cancelled.is_set():
                stop_process(proc)
                return
            proc.stdin.write(text)
            proc.stdin.close()
            for line in proc.stdout:
                if self.cancelled.is_set():
                    break
                try: obj = json.loads(line)
                except ValueError: continue
                if obj.get('session_id'):
                    self.emit('session', {'id':obj['session_id']})
                kind = obj.get('type')
                if kind == 'stream_event':
                    event = obj.get('event') or {}
                    if event.get('type') == 'message_start':
                        self.emit('item', {'id':(event.get('message') or {}).get('id') or ''})
                    delta = event.get('delta') or {}
                    if delta.get('type') == 'text_delta':
                        streamed = True
                        self.emit('delta', {'text':delta.get('text') or ''})
                elif kind == 'assistant':
                    content = (obj.get('message') or {}).get('content') or []
                    if not streamed:
                        text_out = '\n'.join(c.get('text','') for c in content if c.get('type') == 'text')
                        if text_out:
                            self.emit('delta', {'text':text_out + '\n'})
                    for c in content:
                        if c.get('type') == 'tool_use':
                            self.emit('activity', {'text':'Claude · ' + str(c.get('name') or '工具')})
                elif kind == 'result':
                    final = True
                    result_obj = obj
            code = proc.wait(timeout=10)
            if self.cancelled.is_set():
                pass  # The finally block emits one terminal event after the process stops.
            elif final:
                if result_obj.get('is_error'):
                    msg = result_obj.get('result') or '\n'.join(result_obj.get('errors') or []) or 'Claude 本轮未完成'
                    self.emit('error', {'text':str(msg)})
                else:
                    self.emit('done', {'text':result_obj.get('result') or '', 'status':'completed'})
            elif not final:
                self.emit('error', {'text':''.join(errors)[-2000:] or f'Claude 已退出（{code}），没有收到完整结果。'})
        except Exception as err:
            if not self.cancelled.is_set():
                self.emit('error', {'text':str(err)})
        finally:
            if self.cancelled.is_set():
                self.emit('done', {'status':'interrupted'})

    def stop(self):
        self.cancelled.set()
        if self.process:
            threading.Thread(target=stop_process, args=(self.process,), daemon=True).start()

    def close(self):
        self.cancelled.set()
        stop_process(self.process)


class CodexTransport:
    def __init__(self, emit):
        self.emit = emit
        self.process = None
        self.thread_id = ''
        self.turn_id = ''
        self._counter = 0
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending = {}
        self._requests = {}
        self._items = {}
        self._stderr = deque(maxlen=25)
        self._cancel = threading.Event()
        self._closed = False
        self._in_turn = False
        self._finished = threading.Event()

    def _write(self, obj):
        with self._write_lock:
            if not self.process or self.process.poll() is not None:
                raise RuntimeError('Codex 本地连接已断开，请重试。')
            self.process.stdin.write(json.dumps(obj, ensure_ascii=False) + '\n')
            self.process.stdin.flush()

    def rpc(self, method, params, timeout=45):
        with self._lock:
            self._counter += 1
            rid = f'mini-{self._counter}'
            pending = {'event':threading.Event()}
            self._pending[rid] = pending
        try:
            self._write({'id':rid,'method':method,'params':params})
            if not pending['event'].wait(timeout):
                raise RuntimeError(f'Codex {method} 等待超时，请重试。')
            result = pending.get('reply') or {}
            if 'error' in result:
                raise RuntimeError(str((result['error'] or {}).get('message') or 'Codex 请求失败'))
            return result.get('result') or {}
        finally:
            with self._lock:
                self._pending.pop(rid, None)

    def _connect(self, cwd):
        if self.process and self.process.poll() is None:
            return
        self.process = subprocess.Popen([cli_path('codex'), 'app-server'], **process_options(cwd))
        proc = self.process
        threading.Thread(target=lambda:self._stderr.extend(proc.stderr), daemon=True).start()
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        self.rpc('initialize', {'clientInfo':{'name':'claude_pet_mini','title':'Claude + Codex Mini','version':__version__},
                                'capabilities':{'experimentalApi':True}})
        self._write({'method':'initialized','params':{}})
        self.thread_id = ''

    def send(self, text, cwd, session_id):
        self._cancel.clear()
        self._finished.clear()
        self._in_turn = True
        self.turn_id = ''
        self._sender = threading.Thread(target=self._send, args=(text,cwd,session_id), daemon=True)
        self._sender.start()

    def _send(self, text, cwd, session_id):
        try:
            self._connect(cwd)
            if self._cancel.is_set():
                self._in_turn = False
                self.emit('done', {'status':'interrupted'})
                return
            if not session_id or session_id != self.thread_id:
                params = {'cwd':cwd, 'approvalPolicy':'on-request', 'sandbox':'workspace-write',
                          'approvalsReviewer':'user'}
                if session_id:
                    params['threadId'] = session_id
                    try:
                        result = self.rpc('thread/resume', params)
                    except RuntimeError as err:
                        if 'active writer' not in str(err).lower():
                            raise
                        # Desktop can hold a writer on a persisted Mini thread.
                        # Keep its history intact and continue in a new owned fork.
                        result = self.rpc('thread/fork', params)
                        self.emit('notice', {'text':'原 Mini 会话正被其它窗口占用，已保留上下文并在新分支中继续。'})
                else:
                    result = self.rpc('thread/start', params)
                self.thread_id = result['thread']['id']
                self.emit('session', {'id':self.thread_id})
            self._items.clear()
            if self._cancel.is_set():
                self._in_turn = False
                self.emit('done', {'status':'interrupted'})
                return
            result = self.rpc('turn/start', {'threadId':self.thread_id,
                             'input':[{'type':'text','text':text}]})
            self.turn_id = (result.get('turn') or {}).get('id') or self.turn_id
            if self._cancel.is_set() and self.turn_id:
                self.rpc('turn/interrupt', {'threadId':self.thread_id,'turnId':self.turn_id})
        except Exception as err:
            self._in_turn = False
            self._finished.set()
            if self._cancel.is_set():
                self.emit('done', {'status':'interrupted'})
            else:
                self.emit('error', {'text':str(err)})

    def _read(self, proc):
        try:
            for line in proc.stdout:
                try: obj = json.loads(line)
                except ValueError: continue
                self.handle(obj)
        finally:
            with self._lock:
                for pending in self._pending.values():
                    pending['reply'] = {'error':{'message':'Codex 本地进程已退出。'}}
                    pending['event'].set()
            if not self._closed and self._in_turn:
                self._in_turn = False
                if self._cancel.is_set():
                    self.emit('done', {'status':'interrupted'})
                else:
                    self.emit('error', {'text':''.join(self._stderr)[-1500:] or 'Codex 本地连接断开，请重试。'})
            self._finished.set()

    def handle(self, obj):
        if 'id' in obj and 'method' not in obj:
            with self._lock:
                pending = self._pending.get(obj['id'])
                if pending:
                    pending['reply'] = obj
                    pending['event'].set()
            return
        method, p = obj.get('method') or '', obj.get('params') or {}
        if method.startswith(('turn/','item/')) and p.get('threadId') and self.thread_id and p['threadId'] != self.thread_id:
            return
        if 'id' in obj:
            if method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval',
                          'item/permissions/requestApproval','item/tool/requestUserInput','tool/requestUserInput'):
                rid = str(obj['id'])
                self._requests[rid] = obj
                self.emit('request', {'id':rid,'method':method,'params':p})
            else:
                self._write({'id':obj['id'],'error':{'code':-32601,'message':'Mini does not support this client request'}})
                self.emit('activity', {'text':'Codex 请求的界面功能暂不支持：' + method})
            return
        if method == 'turn/started':
            self.turn_id = (p.get('turn') or {}).get('id') or self.turn_id
        elif method == 'item/agentMessage/delta':
            if self._cancel.is_set():
                return
            item = p.get('itemId') or 'message'
            if item not in self._items:
                self._items[item] = ''
                self.emit('item', {'id':item})
            self._items[item] += p.get('delta') or ''
            self.emit('delta', {'text':p.get('delta') or ''})
        elif method in ('item/started', 'item/completed'):
            item = p.get('item') or {}
            kind = item.get('type')
            if kind == 'agentMessage' and method == 'item/completed':
                iid = item.get('id') or 'message'
                # Completed text is authoritative, but do not append streamed text twice.
                if iid not in self._items:
                    self.emit('item', {'id':iid})
                    self.emit('delta', {'text':item.get('text') or ''})
                self._items[iid] = item.get('text') or self._items.get(iid, '')
            elif kind in ('commandExecution', 'fileChange', 'mcpToolCall','webSearch'):
                name = {'commandExecution':'在跑命令','fileChange':'在改文件',
                        'mcpToolCall':'在用工具','webSearch':'在查资料'}[kind]
                self.emit('activity', {'text':'Codex ' + name})
        elif method == 'turn/plan/updated':
            self.emit('plan', {'plan':p.get('plan') or []})
        elif method == 'serverRequest/resolved':
            rid = str(p.get('requestId'))
            self._requests.pop(rid, None)
            self.emit('resolved', {'id':rid})
        elif method == 'turn/completed':
            self._in_turn = False
            self._finished.set()
            turn = p.get('turn') or {}
            self._requests.clear()
            if turn.get('status') == 'failed':
                self.emit('error', {'text':(turn.get('error') or {}).get('message') or 'Codex 本轮失败'})
            else:
                self.emit('done', {'status':turn.get('status') or 'completed'})
        elif method == 'error' and not p.get('willRetry'):
            self.emit('activity', {'text':(p.get('error') or {}).get('message') or 'Codex 正在恢复连接…'})

    def answer(self, request_id, result):
        request = self._requests.pop(str(request_id), None)
        if request:
            self._write({'id':request['id'],'result':result})

    def stop(self):
        self._cancel.set()
        if self.thread_id and self.turn_id:
            threading.Thread(target=self._interrupt, daemon=True).start()

    def _interrupt(self):
        try:
            self.rpc('turn/interrupt', {'threadId':self.thread_id,'turnId':self.turn_id}, timeout=5)
        except Exception:
            pass
        if not self._finished.wait(3):
            # A stalled server must not leave Stop as a cosmetic status change.
            # Terminate only Mini's server, then reconnect on the next Send.
            stop_process(self.process)
            sender = getattr(self,'_sender',None)
            if sender and sender is not threading.current_thread():
                sender.join(timeout=1)
            self._in_turn = False
            self._finished.set()
            self.emit('done', {'status':'interrupted'})

    def close(self):
        self._closed = True
        self._cancel.set()
        stop_process(self.process)


class ChatHub(QObject):
    changed = Signal()
    incoming = Signal(str, dict)

    def __init__(self, path=None):
        super().__init__()
        self.path = str(path) if path else data_path('mini_chat.json')
        self.records = load_json(self.path, {}) or {}
        if not isinstance(self.records, dict):
            self.records = {}
        self.current = {}
        self.transports = {p:cls(lambda kind,data,provider=p:self.incoming.emit(provider,dict(data,kind=kind)))
                           for p,cls in (('claude',ClaudeTransport),('codex',CodexTransport))}
        self.incoming.connect(self._on_event)
        self.busy = {'claude':False,'codex':False}
        self.activity = {'claude':'准备好了','codex':'准备好了'}
        self.requests = {}
        self.plans = {}

    def select(self, provider, cwd):
        key = hashlib.sha256((provider+'\0'+os.path.normcase(os.path.abspath(cwd))).encode('utf-8')).hexdigest()[:24]
        if self.busy[provider] and self.current.get(provider) != key:
            raise RuntimeError('这个助手还在处理上一条消息，请先停止或等它完成。')
        self.current[provider] = key
        if key not in self.records:
            self.records[key] = {'provider':provider,'cwd':cwd,'session_id':'','messages':[], 'at':time.time()}
        return self.records[key]

    def record(self, provider):
        return self.records.get(self.current.get(provider)) or {}

    def send(self, provider, text, cwd):
        text = text.strip()
        if not text:
            return
        if self.busy[provider]:
            raise RuntimeError('这一轮还没结束，可以先点停止。')
        if not os.path.isdir(cwd):
            raise RuntimeError('工作目录不存在，请重新选择。')
        rec = self.select(provider,cwd)
        rec['messages'].append({'role':'user','text':text})
        rec['messages'].append({'role':'assistant','text':''})
        rec['last_status']='running'
        self.busy[provider] = True
        self.activity[provider] = '正在连接…'
        self._save()
        self.changed.emit()
        self.transports[provider].send(text,cwd,rec.get('session_id') or '')

    def _on_event(self, provider, e):
        rec = self.record(provider)
        if not rec:
            return
        kind = e['kind']
        if kind == 'session':
            rec['session_id'] = e['id']
            self.activity[provider] = '正在思考…'
        elif kind == 'item':
            messages = rec['messages']
            if messages and messages[-1]['role'] == 'assistant' and messages[-1]['text']:
                messages.append({'role':'assistant','text':''})
        elif kind == 'delta':
            messages = rec['messages']
            if not messages or messages[-1]['role'] != 'assistant':
                messages.append({'role':'assistant','text':''})
            messages[-1]['text'] += e.get('text') or ''
            self.activity[provider] = '正在回复…'
        elif kind == 'activity':
            self.activity[provider] = e['text']
        elif kind == 'notice':
            rec['messages'].append({'role':'system','text':e.get('text') or ''})
        elif kind == 'request':
            e['provider'] = provider
            self.requests[(provider,e['id'])] = e
            self.activity[provider] = '等你确认'
        elif kind == 'resolved':
            self.requests.pop((provider,e['id']),None)
        elif kind == 'plan':
            self.plans[provider] = e['plan']
        elif kind in ('done','error'):
            self.busy[provider] = False
            rec['finished_at']=time.time()
            rec['last_status']='error' if kind=='error' else e.get('status') or 'completed'
            if kind == 'error':
                self.activity[provider] = '出错了'
                raw = str(e.get('text') or '连接失败')
                text = friendly_error(provider,raw)
                last = rec['messages'][-1] if rec['messages'] else None
                if last and last['role']=='assistant' and raw.strip() == last['text'].strip():
                    last.update(role='system',text=text)
                else:
                    rec['messages'].append({'role':'system','text':text})
            else:
                self.activity[provider] = '已停止' if e.get('status') == 'interrupted' else '完成了'
                if e.get('text') and rec['messages'] and not rec['messages'][-1]['text']:
                    rec['messages'][-1]['text'] = e['text']
            for key in list(self.requests):
                if key[0] == provider:
                    del self.requests[key]
            self._save()
        self.changed.emit()

    def answer(self, provider, rid, response):
        self.transports[provider].answer(rid,response)
        self.requests.pop((provider,rid),None)
        self.activity[provider] = '正在继续…'
        self.changed.emit()

    def stop(self, provider):
        if self.busy[provider]:
            self.activity[provider] = '正在停止…'
            self.transports[provider].stop()
            self.changed.emit()

    def new_chat(self, provider, cwd):
        if self.busy[provider]:
            raise RuntimeError('请先停止当前任务，再开新对话。')
        rec = self.select(provider,cwd)
        rec.update(session_id='',messages=[],draft='',at=time.time())
        self._save()
        self.changed.emit()

    def _save(self):
        for provider in ('claude','codex'):
            rec = self.record(provider)
            if rec:
                rec['at'] = time.time()
        for rec in self.records.values():
            rec['messages'] = rec.get('messages',[])[-160:]
        save_json(self.path,dict(sorted(self.records.items(),key=lambda kv:kv[1].get('at',0),reverse=True)[:30]))

    def close(self):
        self._save()
        for transport in self.transports.values():
            transport.close()
