import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest

from petapp.codex import CodexReader, normalize_limits, tool_description
from petapp.sessions import SessionTracker


class CodexReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = time.time()
        d = datetime.date.today()
        folder = self.root / 'sessions' / f'{d.year:04}' / f'{d.month:02}' / f'{d.day:02}'
        folder.mkdir(parents=True)
        self.path = folder / 'rollout-test.jsonl'
        self.reader = CodexReader(self.root)
        self.write('session_meta', {'id':'abc', 'source':'vscode', 'cwd':'D:/test', 'originator':'Codex Desktop'})

    def tearDown(self):
        self.temp.cleanup()

    def write(self, typ, payload):
        ts = datetime.datetime.fromtimestamp(self.now, datetime.timezone.utc).isoformat()
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps({'timestamp':ts, 'type':typ, 'payload':payload}) + '\n')

    def poll(self):
        return self.reader.poll(self.now, force=True)

    def test_bootstrap_does_not_replay_completion(self):
        self.write('event_msg', {'type':'task_started', 'turn_id':'1'})
        self.write('event_msg', {'type':'task_complete', 'turn_id':'1', 'last_agent_message':'完成'})
        s = self.poll()[0]
        self.assertEqual(s['id'], 'codex:abc')
        self.assertEqual(s['state'], 'idle')
        self.assertIsNone(s['event'])
        self.assertEqual(self.poll(), [])

    def test_incremental_turn_and_tokens(self):
        self.poll()
        self.write('event_msg', {'type':'task_started', 'turn_id':'1'})
        self.assertEqual(self.poll()[0]['event'], 'prompt')
        self.write('response_item', {'type':'function_call', 'name':'exec_command', 'call_id':'a',
                                     'arguments':json.dumps({'cmd':'pytest'})})
        self.assertEqual(self.poll()[0]['kind'], 'running')
        self.write('event_msg', {'type':'token_count', 'info':{
            'total_token_usage':{'total_tokens':1234, 'input_tokens':1000, 'output_tokens':234},
            'last_token_usage':{'input_tokens':1000}, 'model_context_window':10000},
            'rate_limits':{'primary':{'used_percent':70,'window_minutes':300,'resets_at':self.now+1000},
                           'secondary':{'used_percent':12,'window_minutes':10080,'resets_at':self.now+2000}}})
        s = self.poll()[0]
        self.assertEqual(s['turn_tokens'], 1234)
        self.assertEqual(s['context_pct'], 10)
        self.assertEqual(self.reader.quota['windows'][0]['used'], 70)
        self.write('event_msg', {'type':'task_complete', 'turn_id':'1', 'last_agent_message':'ok'})
        s = self.poll()[0]
        self.assertEqual(s['event'], 'done')
        self.assertIsNone(s['turn_started_at'])
        self.write('event_msg', {'type':'task_started', 'turn_id':'2'})
        self.assertEqual(self.poll()[0]['turn_tokens'], 0)

    def test_partial_line_waits_for_newline(self):
        self.poll()
        line = json.dumps({'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                           'type':'event_msg','payload':{'type':'task_started','turn_id':'p'}})
        cut = len(line)//2
        with self.path.open('a') as f: f.write(line[:cut])
        self.assertEqual(self.poll()[0]['state'], 'idle')
        with self.path.open('a') as f: f.write(line[cut:] + '\n')
        self.assertEqual(self.poll()[0]['state'], 'thinking')

    def test_guardian_subagents_never_become_user_sessions(self):
        self.path.write_text('')
        self.write('session_meta', {'id':'guardian', 'source':{'subagent':{'other':'guardian'}}})
        self.write('event_msg', {'type':'task_started', 'turn_id':'g'})
        self.assertEqual(self.poll(), [])
        self.write('event_msg', {'type':'task_complete', 'turn_id':'g'})
        self.assertEqual(self.poll(), [])

    def test_truncate_recovers_without_replaying_old_event(self):
        self.poll()
        self.write('event_msg', {'type':'task_started','turn_id':'1'})
        self.poll()
        self.path.write_text('')
        self.write('session_meta', {'id':'abc','source':'cli'})
        s = self.poll()[0]
        self.assertEqual(s['state'], 'idle')
        self.assertIsNone(s['event'])

    def test_question_clears_when_its_answer_arrives(self):
        self.poll()
        self.write('response_item', {'type':'function_call','name':'request_user_input','call_id':'q','arguments':'{}'})
        self.assertEqual(self.poll()[0]['state'], 'waiting')
        self.write('response_item', {'type':'function_call_output','call_id':'q','output':'answer'})
        self.assertEqual(self.poll()[0]['state'], 'thinking')

    def test_interrupt_clears_working(self):
        self.poll()
        self.write('event_msg', {'type':'task_started','turn_id':'1'})
        self.poll()
        self.write('event_msg', {'type':'turn_aborted','turn_id':'1'})
        self.assertEqual(self.poll()[0]['state'], 'idle')

    def test_malformed_line_does_not_stop_reader(self):
        with self.path.open('a') as f: f.write('invalid json\n')
        self.write('event_msg', {'type':'task_started','turn_id':'1'})
        self.assertEqual(self.poll()[0]['state'], 'thinking')

    def test_code_mode_classifies_nested_tools_without_executing(self):
        self.assertEqual(tool_description('exec','text(await tools.exec_command({cmd:"test"}))')[0], 'running')
        self.assertEqual(tool_description('exec','text(await tools.apply_patch("patch"))')[0], 'working')
        self.assertNotEqual(tool_description('request_user_input_async','{}')[0], 'waiting')

    def test_unknown_quota_does_not_mean_zero(self):
        self.assertIsNone(normalize_limits({}, self.now))
        self.assertIsNone(normalize_limits(None, self.now))

    def test_desktop_file_changes_map(self):
        self.poll()
        self.write('event_msg', {'type':'item_completed', 'item':{'type':'FileChange',
                   'changes':{'D:/file.py':{'type':'update','unified_diff':'example'}}}})
        self.assertEqual(self.poll()[0]['changed'], ['D:/file.py'])

    def test_token_usage_record_does_not_double_count_and_restores_turn(self):
        self.write('token_usage_record', {'thread_token_usage':{'total_tokens':5000},
                                          'turn_token_usage':{'total_tokens':1200}})
        self.write('event_msg', {'type':'token_count', 'info':{'total_token_usage':{'total_tokens':5000}}})
        state = self.poll()[0]
        self.assertEqual(state['turn_tokens'], 1200)
        self.assertEqual(state['tokens']['total_tokens'], 5000)

    def test_claude_lifecycle_still_works(self):
        t = SessionTracker()
        def apply(ev, **kw):
            return t.apply(dict(session_id='abc', hook_event_name=ev, **kw), self.now)
        apply('SessionStart', cwd='D:/test')
        apply('UserPromptSubmit', prompt='test')
        self.assertEqual(t.sessions['abc'].state, 'thinking')
        apply('PreToolUse', tool_name='Bash', tool_input={'command':'pytest'})
        self.assertEqual(t.display(), 'running')
        apply('PermissionRequest', tool_name='Bash', tool_input={})
        self.assertEqual(t.display(), 'waiting')
        apply('PostToolUse', tool_name='Bash')
        apply('Stop', last_assistant_message='ok')
        self.assertTrue(t.sessions['abc'].unseen)
        self.assertEqual(t.snapshot()[0]['provider'], 'claude')

    def test_removed_transcript_removes_session_from_live_view(self):
        self.poll()
        self.path.unlink()
        self.assertEqual(self.poll(),[{'id':'codex:abc','removed':True}])


if __name__ == '__main__':
    unittest.main()
