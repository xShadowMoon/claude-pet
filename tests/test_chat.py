"""验证 Mini 会话隔离、消息流、续聊与权限决定。"""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication
from petapp.chat import ChatHub, ClaudeTransport, CodexTransport, friendly_error


class KeepBuffer(io.StringIO):
    def close(self):
        self.saved = self.getvalue()


class ChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hub = ChatHub(Path(self.tmp.name)/'chat.json')
        self.backends = {p:Mock() for p in ('claude','codex')}
        self.hub.transports = self.backends

    def tearDown(self):
        self.tmp.cleanup()

    def test_sending_to_one_does_not_start_other(self):
        self.hub.send('claude','你好',self.tmp.name)
        self.backends['claude'].send.assert_called_once_with('你好',self.tmp.name,'')
        self.backends['codex'].send.assert_not_called()
        self.assertTrue(self.hub.busy['claude'])
        self.assertFalse(self.hub.busy['codex'])

    def test_each_provider_keeps_its_own_session(self):
        for provider,sid in (('claude','claude-owned'),('codex','codex-owned')):
            self.hub.send(provider,'hello',self.tmp.name)
            self.hub._on_event(provider,{'kind':'session','id':sid})
            self.hub._on_event(provider,{'kind':'delta','text':'你好'})
            self.hub._on_event(provider,{'kind':'done'})
        self.hub.send('claude','继续',self.tmp.name)
        self.backends['claude'].send.assert_called_with('继续',self.tmp.name,'claude-owned')
        self.assertEqual(self.hub.record('codex')['session_id'],'codex-owned')

    def test_busy_provider_rejects_double_send_or_directory_switch(self):
        self.hub.send('claude','one',self.tmp.name)
        with self.assertRaises(RuntimeError): self.hub.send('claude','two',self.tmp.name)
        with self.assertRaises(RuntimeError): self.hub.select('claude',str(Path(self.tmp.name)/'other'))
        self.assertEqual(len(self.hub.record('claude')['messages']),2)

    def test_utf8_history_survives_restart(self):
        self.hub.send('codex','项目名字含中文',self.tmp.name)
        self.hub._on_event('codex',{'kind':'session','id':'our-thread'})
        self.hub._on_event('codex',{'kind':'done','text':'收到中文'})
        other = ChatHub(self.hub.path)
        rec = other.select('codex',self.tmp.name)
        self.assertEqual(rec['session_id'],'our-thread')
        self.assertEqual(rec['messages'][-1]['text'],'收到中文')

    def test_unknown_directory_does_not_start_cli(self):
        with self.assertRaises(RuntimeError): self.hub.send('claude','hello',str(Path(self.tmp.name)/'missing'))
        self.backends['claude'].send.assert_not_called()

    def test_request_is_only_answered_by_user_action(self):
        self.hub.send('codex','hello',self.tmp.name)
        self.hub._on_event('codex',{'kind':'request','id':'q','method':'item/fileChange/requestApproval','params':{}})
        self.backends['codex'].answer.assert_not_called()
        self.hub.answer('codex','q',{'decision':'decline'})
        self.backends['codex'].answer.assert_called_once_with('q',{'decision':'decline'})
        self.assertEqual(self.hub.requests,{})

    def test_completed_result_does_not_duplicate_streamed_reply(self):
        self.hub.send('claude','hi',self.tmp.name)
        self.hub._on_event('claude',{'kind':'delta','text':'回答'})
        self.hub._on_event('claude',{'kind':'done','text':'回答'})
        self.assertEqual(self.hub.record('claude')['messages'][-1]['text'],'回答')

    def test_error_clears_busy_and_pending(self):
        self.hub.send('codex','hi',self.tmp.name)
        self.hub._on_event('codex',{'kind':'request','id':'q','method':'tool/requestUserInput','params':{}})
        self.hub._on_event('codex',{'kind':'error','text':'网络不可用'})
        self.assertFalse(self.hub.busy['codex'])
        self.assertFalse(self.hub.requests)
        self.assertEqual(self.hub.record('codex')['messages'][-1]['text'],'网络不可用')

    def test_stop_only_targets_selected_provider(self):
        self.hub.send('claude','hello',self.tmp.name)
        self.hub.stop('claude')
        self.backends['claude'].stop.assert_called_once()
        self.backends['codex'].stop.assert_not_called()

    def test_quota_error_is_chinese_and_not_duplicated(self):
        self.hub.send('claude','hi',self.tmp.name)
        error="You've hit your weekly limit · resets Oct 27, 9pm (America/Los_Angeles)"
        self.hub._on_event('claude',{'kind':'delta','text':error})
        self.hub._on_event('claude',{'kind':'error','text':error})
        messages=self.hub.record('claude')['messages']
        self.assertEqual(len(messages),2)
        self.assertIn('本周额度已用完',messages[-1]['text'])
        self.assertIn('21:00',messages[-1]['text'])


class TransportTests(unittest.TestCase):
    def test_claude_prompt_is_stdin_never_shell_or_argv(self):
        prompt = '中文 "quoted" & $(do not execute)'
        events=[]
        transport=ClaudeTransport(lambda kind,data:events.append((kind,data)))
        messages=[{'type':'system','session_id':'owned'},
                  {'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':'好'}}},
                  {'type':'result','result':'好','is_error':False,'session_id':'owned'}]
        proc=Mock()
        proc.stdin=KeepBuffer()
        proc.stdout=io.StringIO('\n'.join(json.dumps(m) for m in messages)+'\n')
        proc.stderr=io.StringIO('')
        proc.wait.return_value=0
        with patch('petapp.chat.cli_path',return_value='claude.exe'), patch('petapp.chat.subprocess.Popen',return_value=proc) as start:
            transport._run(prompt,'.','')
        args,kwargs=start.call_args
        self.assertNotIn(prompt,args[0])
        self.assertFalse(kwargs.get('shell',False))
        self.assertEqual(proc.stdin.saved,prompt)
        self.assertEqual([k for k,_ in events].count('done'),1)
        self.assertEqual(''.join(v['text'] for k,v in events if k=='delta'),'好')

    def test_codex_stream_and_completed_item_are_not_duplicated(self):
        events=[]
        transport=CodexTransport(lambda k,d:events.append((k,d)))
        transport.handle({'method':'item/agentMessage/delta','params':{'itemId':'m','delta':'收到'}})
        transport.handle({'method':'item/completed','params':{'item':{'id':'m','type':'agentMessage','text':'收到'}}})
        transport.handle({'method':'turn/completed','params':{'turn':{'status':'completed'}}})
        self.assertEqual(''.join(d['text'] for k,d in events if k=='delta'),'收到')
        self.assertEqual(events[-1][0],'done')

    def test_codex_preserves_native_request_id_when_answering(self):
        transport=CodexTransport(lambda *_:None)
        transport._write=Mock()
        transport.handle({'id':45,'method':'item/commandExecution/requestApproval','params':{'command':'pytest'}})
        transport._write.assert_not_called()
        transport.answer('45',{'decision':'accept'})
        transport._write.assert_called_once_with({'id':45,'result':{'decision':'accept'}})

    def test_unknown_server_request_is_not_auto_approved(self):
        transport=CodexTransport(lambda *_:None)
        transport._write=Mock()
        transport.handle({'id':9,'method':'unknown/dangerous','params':{}})
        self.assertIn('error',transport._write.call_args.args[0])

    def test_busy_writer_forks_owned_history_before_sending(self):
        events=[]
        transport=CodexTransport(lambda k,d:events.append((k,d)))
        transport._connect=Mock()
        calls=[]
        def rpc(method,params):
            calls.append((method,dict(params)))
            if method=='thread/resume': raise RuntimeError('thread already has an active writer')
            if method=='thread/fork': return {'thread':{'id':'new-owned-fork'}}
            if method=='turn/start': return {'turn':{'id':'new-turn'}}
        transport.rpc=rpc
        transport._send('continue','.','old-owned-mini')
        self.assertEqual([m for m,_ in calls],['thread/resume','thread/fork','turn/start'])
        self.assertEqual(calls[-1][1]['threadId'],'new-owned-fork')
        self.assertTrue(any(k=='notice' for k,_ in events))


if __name__=='__main__': unittest.main()
