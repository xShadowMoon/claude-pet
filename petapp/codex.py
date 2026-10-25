# -*- coding: utf-8 -*-
"""Read-only Codex Desktop/CLI adapter. No API key, notify replacement, or hooks.

Codex owns the transcripts. Read only complete JSONL lines and keep byte offsets;
read the thread database in mode=ro for titles/paths, with a file-only fallback.
Never execute the tool-call text stored in a transcript.
"""

import datetime
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

from PySide6.QtCore import QObject, Signal

from .common import fmt_ago, fmt_clock, fmt_tokens, load_json, parse_iso, save_json, data_path
from .sessions import clip, describe_tool
from .codex_stats import CodexStats

POLL_SECONDS = 2
DISCOVERY_SECONDS = 20
TAIL_BYTES = 768 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024
ACTIVE_AGE = 24 * 3600


def codex_home():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')


def _json(value):
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _path_key(value):
    text = str(value)
    if text.startswith('\\\\?\\'):
        text = text[4:]
    return os.path.normcase(os.path.abspath(text))


def normalize_limits(raw, at):
    if not isinstance(raw, dict):
        return None
    # Some versions return a map of limit ids rather than one limit object.
    if 'primary' not in raw and 'secondary' not in raw:
        raw = raw.get('codex') or raw.get('default') or {}
    windows = []
    for field, default_key in (('primary', 'five_hour'), ('secondary', 'seven_day')):
        val = raw.get(field)
        if not isinstance(val, dict) or val.get('used_percent') is None:
            continue
        try:
            used = float(val['used_percent'])
            minutes = int(val.get('window_minutes') or (300 if field == 'primary' else 10080))
            reset = float(val['resets_at']) if val.get('resets_at') is not None else None
        except (ValueError, TypeError):
            continue
        windows.append({'key': default_key, 'label': '5 小时' if minutes == 300 else
                        ('本周' if minutes == 10080 else f'{minutes} 分钟'),
                        'used': max(0., min(100., used)), 'resets_at': reset,
                        'window_minutes': minutes})
    if not windows:
        return None
    return {'fetched_at': at, 'source': 'codex_local', 'plan': raw.get('plan_type') or '',
            'windows': windows, 'credits': raw.get('credits') or {}}


def tool_description(name, raw):
    args = _json(raw)
    leaf = name.split('.')[-1]
    if leaf in ('exec', 'js'):
        # Code-mode is a container for actual tools. Classify names only; don't
        # eval the code, or infer permissions from quoted example code.
        text = raw if isinstance(raw, str) else ''
        calls = re.findall(r'\btools\.([A-Za-z0-9_]+)\s*\(', text)
        for candidate in calls:
            if candidate in ('exec_command', 'apply_patch', 'web__run'):
                return {'exec_command': ('running', 'Codex 在跑命令', ''),
                        'apply_patch': ('working', 'Codex 在改文件', ''),
                        'web__run': ('web', 'Codex 在查资料', '')}[candidate]
        return 'working', 'Codex 在调用工具', ''
    if leaf in ('exec_command', 'shell', 'shell_command', 'run_shell_command'):
        return describe_tool('Bash', {'command': args.get('cmd') or args.get('command') or '',
                                      'description': args.get('description') or ''})
    if leaf in ('apply_patch', 'write_file', 'edit_file'):
        return 'working', 'Codex 在改文件', ''
    if leaf in ('web__run', 'web_run', 'web_search', 'search_query'):
        return 'web', 'Codex 在查资料', ''
    if leaf in ('request_user_input', 'ask_user'):
        return 'waiting', 'Codex 有问题等你回答', ''
    if leaf == 'request_user_input_async':
        return 'thinking', 'Codex 发了一个可稍后回答的问题', ''
    if leaf in ('spawn_agent', 'multi_agent_v1__spawn_agent'):
        return 'agent', 'Codex 叫了个帮手', ''
    if leaf in ('update_plan',):
        return 'working', 'Codex 在更新任务清单', ''
    if leaf in ('wait', 'wait_agent', 'write_stdin'):
        return 'running', 'Codex 等待工具执行结果', ''
    return describe_tool(name, args)


class CodexReader:
    """Pure reader, independently testable without starting Qt threads."""
    def __init__(self, root=None):
        self.root = Path(root) if root else codex_home()
        self.files = {}
        self.quota = None
        self.last_discovery = 0.
        self.rows = {}
        self.last_error = ''

    def discover(self, now):
        rows = {}
        excluded = set()
        databases = sorted(self.root.glob('state_*.sqlite'), key=lambda p: p.stat().st_mtime, reverse=True)
        if databases:
            try:
                with closing(sqlite3.connect(databases[0].as_uri() + '?mode=ro', uri=True, timeout=.3)) as db:
                    db.row_factory = sqlite3.Row
                    for r in db.execute('SELECT id, rollout_path, title, cwd, source, model, archived, updated_at '
                                        'FROM threads WHERE updated_at>? '
                                        'ORDER BY updated_at DESC LIMIT 50', (now - ACTIVE_AGE,)):
                        src = _json(r['source'])
                        key = _path_key(r['rollout_path'])
                        if r['archived'] or 'subagent' in src or 'subagent' in str(r['source']).lower():
                            excluded.add(key)
                            continue
                        rows[key] = dict(r)
            except (OSError, sqlite3.Error, ValueError):
                pass  # Existing Desktop versions and locked DBs use file fallback.
        # Include today's and yesterday's files even if sqlite lags behind.
        base = self.root / 'sessions'
        for date in {datetime.date.today(), datetime.datetime.now(datetime.timezone.utc).date()}:
            for delta in range(2):
                d = date - datetime.timedelta(days=delta)
                folder = base / f'{d.year:04d}' / f'{d.month:02d}' / f'{d.day:02d}'
                try:
                    candidates = folder.glob('*.jsonl')
                    for path in candidates:
                        if path.stat().st_mtime >= now - ACTIVE_AGE:
                            key = _path_key(path)
                            if key not in excluded:
                                rows.setdefault(key, {})
                except OSError:
                    continue
        self.rows = rows
        self.last_discovery = now

    def poll(self, now=None, force=False):
        now = time.time() if now is None else now
        if force or now - self.last_discovery >= DISCOVERY_SECONDS:
            self.discover(now)
        updates = []
        for filename, meta in list(self.rows.items()):
            try:
                update = self.read(Path(filename), meta, now)
                if update:
                    updates.append(update)
            except (OSError, ValueError, TypeError):
                continue  # Partial writes/replaced transcripts must not kill monitoring.
        # Bounded memory: old sessions can be rediscovered when they run again.
        for filename in list(self.files):
            if filename not in self.rows:
                old=self.files.pop(filename)
                if old.get('id'):
                    updates.append({'id':old['id'],'removed':True})
        return updates

    def read(self, path, meta, now):
        stat = path.stat()
        key = str(path)
        state = self.files.get(key)
        bootstrap = state is None or stat.st_size < state['offset']
        title = meta.get('title') or ''
        if bootstrap:
            with path.open('rb') as fh:
                first = fh.readline(MAX_LINE_BYTES)
            try:
                header = json.loads(first)
            except ValueError:
                return None
            h = header.get('payload') or {}
            source = h.get('source') or ''
            if isinstance(source, dict) and 'subagent' in source:
                self.files[key] = {'offset': stat.st_size, 'ignored': True}
                return None
            sid = h.get('id') or meta.get('id')
            if not sid:
                return None
            state = {'offset': max(0, stat.st_size - TAIL_BYTES), 'id': f'codex:{sid}',
                     'thread_id': sid, 'provider': 'codex', 'title': title, 'cwd': h.get('cwd') or meta.get('cwd') or '',
                     'source': 'Codex 桌面版' if ('desktop' in str(h.get('originator')).lower()
                               or source == 'vscode') else 'Codex 终端',
                     'state': 'idle', 'kind': 'idle', 'activity': '', 'last_message': '',
                     'last_event_at': 0., 'turn_started_at': None, 'turn_id': '', 'turns': 0,
                     'tokens': {}, 'turn_tokens': 0, 'baseline': 0, 'model': meta.get('model') or '',
                     'context_pct': None, 'changed': [], 'todo_done': 0, 'todo_total': 0,
                     'unseen': False, 'ignored': False, 'pending_calls': {}}
            self.files[key] = state
        if state.get('ignored'):
            state['offset'] = stat.st_size
            return None
        title_changed = bool(title and state['title'] != title)
        if title:
            state['title'] = title
        if not bootstrap and stat.st_size == state['offset'] and not title_changed:
            return None
        recent_event = None
        with path.open('rb') as fh:
            fh.seek(state['offset'])
            if bootstrap and state['offset']:
                fh.readline()  # discard first incomplete line of a tail read
                state['offset'] = fh.tell()
            while True:
                line = fh.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    # Skip one giant line as a unit; never parse its tail as JSON.
                    while line and not line.endswith(b'\n'):
                        line = fh.readline(MAX_LINE_BYTES + 1)
                    if line.endswith(b'\n'):
                        state['offset'] = fh.tell()
                        continue
                    break
                if not line.endswith(b'\n'):
                    break
                state['offset'] = fh.tell()
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                event = self.apply(state, obj)
                if event:
                    recent_event = event
        result = {k:v for k,v in state.items() if k not in ('offset', 'pending_calls', 'baseline')}
        # Never replay old completion sounds/bubbles at startup.
        result['event'] = None if bootstrap else recent_event
        result['bootstrap'] = bootstrap
        result['transcript'] = str(path)
        # A historical start without an end must not create a forever-busy pet.
        if now - state['last_event_at'] > 1200 and result['state'] in ('working', 'thinking', 'compacting', 'waiting'):
            result.update(state='idle', kind='idle', activity='好一会儿没动静了', turn_started_at=None)
        return result

    def apply(self, s, obj):
        p = obj.get('payload')
        if not isinstance(p, dict):
            return None
        ts = parse_iso(obj.get('timestamp')) or s['last_event_at']
        typ, sub = obj.get('type'), p.get('type')
        event = None
        if typ == 'token_usage_record':
            # New Desktop transcripts contain exact per-turn totals. These are
            # the same request as token_count, not another charge to add up.
            total = p.get('thread_token_usage') or {}
            turn = p.get('turn_token_usage') or {}
            if total.get('total_tokens') is not None and turn.get('total_tokens') is not None:
                s['tokens'] = total
                s['turn_tokens'] = int(turn['total_tokens'])
                s['baseline'] = max(0, int(total['total_tokens']) - s['turn_tokens'])
        elif typ == 'turn_context':
            s['model'] = p.get('model') or s['model']
            s['cwd'] = p.get('cwd') or s['cwd']
        elif typ == 'event_msg':
            if sub in ('task_started', 'turn_started'):
                turn = p.get('turn_id') or str(ts)
                if turn != s['turn_id']:
                    s.update(turn_id=turn, state='thinking', kind='thinking', activity='Codex 在想…',
                             turn_started_at=p.get('started_at') or ts, unseen=False, turn_tokens=0)
                    s['baseline'] = int(s['tokens'].get('total_tokens') or 0)
                    s['pending_calls'].clear()
                    event = 'prompt'
            elif sub in ('task_complete', 'turn_complete'):
                s.update(state='idle', kind='idle', activity='做完了', turn_started_at=None, unseen=True)
                s['pending_calls'].clear()
                s['turns'] += 1
                if p.get('last_agent_message'):
                    s['last_message'] = clip(p['last_agent_message'], 160)
                event = 'done'
            elif sub in ('turn_aborted', 'task_aborted', 'interrupted'):
                s.update(state='idle', kind='idle', activity='这一轮已停止', turn_started_at=None, unseen=False)
                s['pending_calls'].clear()
                event = 'interrupt'
            elif sub in ('error', 'task_failed'):
                s.update(state='error', kind='idle', activity=clip(p.get('message') or 'Codex 出错了', 80),
                         turn_started_at=None)
                event = 'api_error'
            elif sub in ('approval_request', 'request_user_input'):
                s.update(state='waiting', kind='waiting', activity='Codex 在等你确认')
                event = 'waiting'
            elif sub == 'token_count':
                info = p.get('info') or {}
                total = info.get('total_token_usage')
                if isinstance(total, dict):
                    s['tokens'] = total
                    s['turn_tokens'] = max(0, int(total.get('total_tokens') or 0) - s['baseline'])
                last, size = info.get('last_token_usage') or {}, info.get('model_context_window')
                if size:
                    s['context_pct'] = min(100., 100. * int(last.get('input_tokens') or 0) / size)
                q = normalize_limits(p.get('rate_limits'), ts)
                if q and (not self.quota or ts >= self.quota['fetched_at']):
                    self.quota = q
            elif sub == 'item_completed':
                item = p.get('item') or {}
                itype = str(item.get('type') or '').lower()
                if itype in ('commandexecution', 'command_execution') and s['state'] != 'idle':
                    s.update(kind='thinking', state='thinking', activity='Codex 在想下一步…')
                elif itype in ('filechange', 'file_change'):
                    changes = item.get('changes') or []
                    paths = list(changes) if isinstance(changes, dict) else \
                            [c.get('path') for c in changes if isinstance(c, dict)]
                    for path in paths:
                        if path and path not in s['changed']:
                            s['changed'].append(path)
        elif typ == 'response_item':
            if sub in ('function_call', 'custom_tool_call'):
                name = p.get('name') or ''
                raw = p.get('arguments') if sub == 'function_call' else p.get('input')
                kind, activity, _ = tool_description(name, raw)
                s['pending_calls'][p.get('call_id') or name] = kind
                s.update(state='waiting' if kind == 'waiting' else 'working', kind=kind, activity=activity)
                if s['turn_started_at'] is None:
                    s['turn_started_at'] = ts
                args = _json(raw)
                if name.split('.')[-1] == 'update_plan':
                    plan = [t for t in args.get('plan', []) if isinstance(t, dict)]
                    s['todo_total'] = len(plan)
                    s['todo_done'] = sum(t.get('status') == 'completed' for t in plan)
                event = 'waiting' if kind == 'waiting' else 'activity'
            elif sub in ('function_call_output', 'custom_tool_call_output'):
                s['pending_calls'].pop(p.get('call_id'), None)
                if s['state'] != 'idle':
                    waiting = 'waiting' in s['pending_calls'].values()
                    s.update(state='waiting' if waiting else 'thinking', kind='waiting' if waiting else 'thinking',
                             activity='Codex 在等你回答' if waiting else 'Codex 在想下一步…')
            elif sub == 'message' and p.get('role') == 'assistant':
                parts = [v.get('text', '') for v in p.get('content', []) if isinstance(v, dict)]
                text = ' '.join(parts).strip()
                if p.get('phase') == 'final_answer' and text:
                    s['last_message'] = clip(text, 160)
            elif sub == 'reasoning' and s['state'] not in ('idle', 'waiting'):
                s.update(kind='thinking', state='thinking', activity='Codex 在想…')
        if typ in ('event_msg', 'response_item', 'turn_context'):
            s['last_event_at'] = max(s['last_event_at'], ts)
        return event


class CodexMonitor(QObject):
    updated = Signal(dict)
    quota_changed = Signal()
    status_changed = Signal()
    stats_changed = Signal()

    def __init__(self, root=None):
        super().__init__()
        self.reader = CodexReader(root)
        self.quota = load_json(data_path('codex_quota.json'))
        self.quota_error = ''
        self.local = {}
        self.stats = CodexStats(self.reader.root, data_path('codex_stats.json'))
        self.enabled = True
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._reset = False
        self.error = ''

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name='codex-pet-reader')
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def refresh_quota(self, force=False):
        self._wake.set()

    def set_enabled(self, on):
        self.enabled = bool(on)
        self._reset = bool(on)
        self._wake.set()

    def _loop(self):
        first = True
        next_stats = 0.
        while not self._stop.is_set():
            self._wake.clear()
            if self.enabled:
                try:
                    if self._reset:
                        self.reader.files.clear()
                        self._reset = False
                    updates = self.reader.poll(force=first)
                    first = False
                    q = self.reader.quota
                    if q and q != self.quota:
                        self.quota = q
                        save_json(data_path('codex_quota.json'), q)
                        self.quota_changed.emit()
                    for state in updates:
                        self.updated.emit(state)
                    if time.time() >= next_stats:
                        titles={r['id']:r['title'] for r in self.reader.rows.values() if r.get('id') and r.get('title')}
                        self.stats.scan(titles)
                        self.local = self.stats.snapshot()
                        self.stats.save()
                        self.stats_changed.emit()
                        next_stats=time.time()+30
                    if self.error:
                        self.error = ''
                        self.status_changed.emit()
                except Exception as err:
                    self.error = f'Codex 本机记录暂时读不到：{err}'
                    self.status_changed.emit()
            self._wake.wait(POLL_SECONDS)

    def window(self, key):
        for raw in (self.quota or {}).get('windows', []):
            if raw.get('key') == key:
                w = dict(raw)
                if w.get('resets_at') and w['resets_at'] <= time.time():
                    # A reset date passing doesn't prove a fresh full allowance.
                    # Keep the observed value, mark it stale, wait for Codex data.
                    w['expired'] = True
                return w
        return None

    def quota_age(self):
        return time.time() - (self.quota or {}).get('fetched_at', 0)

    def brief(self):
        return ' · '.join(f"{w['label']} 剩{100-w['used']:.0f}%" + ('（待更新）' if w.get('expired') else '')
                          for w in (self.window('five_hour'), self.window('seven_day')) if w) or '等待 Codex 用量记录'

    def tooltip(self):
        lines = ['Codex · ' + self.brief()]
        for w in (self.window('five_hour'), self.window('seven_day')):
            if w and w.get('resets_at'):
                lines.append(f"{w['label']}：{fmt_clock(w['resets_at'])} 重置")
        if self.quota:
            lines.append(f"来自本机 Codex 记录，{fmt_ago(self.quota['fetched_at'])}更新")
        lines.append('Codex 工作时自动更新；点击打开面板')
        return '\n'.join(lines)
