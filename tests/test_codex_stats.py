import datetime
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from petapp.codex_stats import CodexStats


class CodexStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.path=self.root/'sessions'/'a.jsonl'
        self.path.parent.mkdir()
        self.cache=self.root/'cache.json'
        self.stats=CodexStats(self.root,self.cache)
        self.write('session_meta',{'id':'a','cwd':'D:/project'})
        self.write('turn_context',{'model':'test-model'})

    def tearDown(self):
        self.temp.cleanup()

    def write(self,typ,payload,days=0):
        stamp=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=days)).isoformat()
        with self.path.open('a',encoding='utf-8') as f:
            f.write(json.dumps({'timestamp':stamp,'type':typ,'payload':payload})+'\n')

    def response(self,rid='r1',inp=100,out=20,cached=60,reasoning=5,days=0):
        u={'input_tokens':inp,'output_tokens':out,'cached_input_tokens':cached,
           'reasoning_output_tokens':reasoning,'total_tokens':inp+out}
        self.write('token_usage_record',{'response_id':rid,'usage':u,'thread_token_usage':u},days)
        return u

    def summary(self):
        self.stats.scan()
        return self.stats.summary(1)

    def test_response_and_snapshot_count_once(self):
        usage=self.response()
        self.write('event_msg',{'type':'token_count','info':{'total_token_usage':usage}})
        s=self.summary()
        self.assertEqual((s['tokens'],s['msgs'],s['cached']),(120,1,60))

    def test_legacy_cumulative_deltas(self):
        for inp,out in ((100,10),(180,20),(180,20)):
            self.write('event_msg',{'type':'token_count','info':{'total_token_usage':{'input_tokens':inp,'output_tokens':out,'total_tokens':inp+out}}})
        s=self.summary()
        self.assertEqual(s['tokens'],200)
        self.assertEqual(s['msgs'],2)

    def test_cache_restart_is_incremental(self):
        self.response()
        self.summary()
        self.stats.save()
        restored=CodexStats(self.root,self.cache)
        restored.scan()
        self.assertEqual(restored.summary(1)['tokens'],120)

    def test_forked_response_does_not_double_count(self):
        self.response()
        copy=self.path.with_name('b.jsonl')
        shutil.copy2(self.path,copy)
        s=self.summary()
        self.assertEqual((s['tokens'],s['msgs']),(120,1))

    def test_archive_move_does_not_double_count(self):
        self.response()
        self.summary()
        archive=self.root/'archived_sessions'
        archive.mkdir()
        self.path.rename(archive/'a.jsonl')
        s=self.summary()
        self.assertEqual(s['tokens'],120)

    def test_cached_and_reasoning_are_subsets(self):
        self.response()
        s=self.summary()
        self.assertEqual(s['tokens'],s['in']+s['out'])
        self.assertEqual(s['reasoning'],5)
        self.assertEqual(s['models'][0][0],'test-model')

    def test_partial_line_is_deferred(self):
        self.summary()
        line=json.dumps({'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),
                         'type':'token_usage_record','payload':{'response_id':'partial','usage':{'input_tokens':50,'output_tokens':10}}})
        with self.path.open('a') as f: f.write(line[:40])
        self.assertEqual(self.summary()['tokens'],0)
        with self.path.open('a') as f: f.write(line[40:]+'\n')
        self.assertEqual(self.summary()['tokens'],60)

    def test_date_ranges_are_not_live_session_totals(self):
        self.response('today')
        self.response('last-week',days=4)
        self.response('last-month',days=20)
        self.stats.scan()
        self.assertEqual(self.stats.summary(1)['tokens'],120)
        self.assertEqual(self.stats.summary(7)['tokens'],240)
        self.assertEqual(self.stats.summary(30)['tokens'],360)

    def test_refined_response_only_adds_difference(self):
        self.response(out=10)
        self.response(out=20)
        s=self.summary()
        self.assertEqual((s['tokens'],s['msgs']),(120,1))

    def test_legacy_then_response_does_not_recount_aggregate(self):
        self.write('event_msg',{'type':'token_count','info':{'total_token_usage':{'input_tokens':20,'output_tokens':10,'total_tokens':30}}})
        u={'input_tokens':40,'output_tokens':10,'total_tokens':50}
        total={'input_tokens':60,'output_tokens':20,'total_tokens':80}
        self.write('token_usage_record',{'response_id':'new','usage':u,'thread_token_usage':total})
        self.write('event_msg',{'type':'token_count','info':{'total_token_usage':total}})
        self.assertEqual(self.summary()['tokens'],80)

    def test_inconsistent_legacy_total_does_not_inflate_tokens(self):
        self.write('event_msg',{'type':'token_count','info':{'total_token_usage':
            {'input_tokens':100,'output_tokens':20,'total_tokens':500}}})
        s=self.summary()
        self.assertEqual(s['tokens'],120)


if __name__=='__main__': unittest.main()
