"""增量汇总 Codex 历史用量并防止重复计数。

Prefer response-level usage records. Legacy token_count snapshots provide a
fallback, never an additional charge for a response already recorded.
"""
import datetime
import hashlib
import json
from pathlib import Path
import time

from .common import day_key, load_json, parse_iso, save_json

FIELDS = ('in', 'out', 'cached', 'reasoning', 'tokens')


def token_values(usage):
    usage = usage if isinstance(usage, dict) else {}
    inp = max(0, int(usage.get('input_tokens') or 0))
    out = max(0, int(usage.get('output_tokens') or 0))
    return [inp, out, min(inp, max(0, int(usage.get('cached_input_tokens') or 0))),
            min(out,max(0, int(usage.get('reasoning_output_tokens') or 0))), inp + out]


class CodexStats:
    def __init__(self, root, cache_path=None):
        self.root = Path(root)
        self.cache_path = str(cache_path) if cache_path else None
        self.files, self.days, self.seen, self.titles = {}, {}, {}, {}
        self.ready = False
        cache = load_json(self.cache_path, {}) if self.cache_path else {}
        if isinstance(cache, dict) and cache.get('version') == 2:
            for field in ('files', 'days', 'seen', 'titles'):
                setattr(self, field, cache.get(field) or {})

    def scan(self, titles=None):
        if titles:
            self.titles.update(titles)
        changed = False
        for folder in ('sessions', 'archived_sessions'):
            for path in sorted((self.root / folder).rglob('*.jsonl')):
                try:
                    changed = self.read(path) or changed
                except (OSError, ValueError, TypeError):
                    continue
        self.ready = True
        return changed

    def read(self, path):
        key = str(path.resolve())
        size = path.stat().st_size
        info = self.files.setdefault(key, {'pos':0, 'id':'', 'model':'未知模型', 'prev':[0]*5})
        if size < info['pos']:
            info.update(pos=0, prev=[0]*5)
        if size == info['pos']:
            return False
        changed = False
        with path.open('rb') as fh:
            fh.seek(info['pos'])
            while True:
                line = fh.readline(8 * 1024 * 1024 + 1)
                if len(line)>8*1024*1024:
                    while line and not line.endswith(b'\n'):
                        line=fh.readline(8*1024*1024+1)
                    if line.endswith(b'\n'):
                        info['pos']=fh.tell()
                        continue
                    break
                if not line or not line.endswith(b'\n'):
                    break
                info['pos'] = fh.tell()
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                p = o.get('payload')
                if not isinstance(p, dict):
                    continue
                typ = o.get('type')
                if typ == 'session_meta':
                    info['id'] = p.get('id') or info['id']
                    cwd = str(p.get('cwd') or '').replace('\\', '/').rstrip('/')
                    self.titles.setdefault(info['id'], cwd.rsplit('/', 1)[-1] or info['id'][:8])
                elif typ == 'turn_context':
                    info['model'] = p.get('model') or info['model']
                elif typ == 'token_usage_record' and isinstance(p.get('usage'), dict):
                    total = p.get('thread_token_usage')
                    if isinstance(total, dict):
                        info['prev'] = token_values(total)
                    response = p.get('response_id')
                    if not response:
                        response = hashlib.sha256(line).hexdigest()
                    values = token_values(p['usage'])
                    stamp = parse_iso(o.get('timestamp')) or path.stat().st_mtime
                    changed = self.add(str(response), stamp, info, values) or changed
                elif typ == 'event_msg' and p.get('type') == 'token_count':
                    data = p.get('info') or {}
                    current = data.get('total_token_usage')
                    if not isinstance(current, dict):
                        continue
                    values = token_values(current)
                    prev = info['prev']
                    # A resumed runtime can reset cumulative counters.
                    delta = values if values[-1] < prev[-1] else [max(0, a-b) for a,b in zip(values,prev)]
                    delta[-1]=delta[0]+delta[1]
                    info['prev'] = values
                    if not delta[-1]:
                        continue
                    stamp = parse_iso(o.get('timestamp')) or path.stat().st_mtime
                    marker = 'legacy:' + hashlib.sha256(json.dumps([o.get('timestamp'), values], separators=(',', ':')).encode()).hexdigest()
                    changed = self.add(marker, stamp, info, delta) or changed
        return changed

    def add(self, response, stamp, info, values):
        old = self.seen.get(response)
        if old:
            delta = [max(0, a-b) for a,b in zip(values,old['values'])]
            delta[-1]=delta[0]+delta[1]
            if not any(delta):
                return False
            stamp, sid, model = old['at'], old['sid'], old['model']
            count = 0
            old['values'] = [max(a,b) for a,b in zip(values,old['values'])]
        else:
            sid, model = info['id'], info['model']
            delta, count = values, 1
            self.seen[response] = {'values':values, 'at':stamp, 'sid':sid, 'model':model}
        day = day_key(stamp)
        d = self.days.setdefault(day, {**{k:0 for k in FIELDS}, 'msgs':0, 'models':{}, 'sessions':{}, 'hours':[0]*24})
        for name,value in zip(FIELDS,delta):
            d[name] += value
        d['msgs'] += count
        d['hours'][time.localtime(stamp).tm_hour] += delta[-1]
        for table,key in ((d['models'],model),(d['sessions'],sid)):
            row = table.setdefault(key, [0,0])
            row[0] += delta[-1]
            row[1] += count
        return True

    def summary(self, ndays):
        today = datetime.date.today()
        keys = [(today-datetime.timedelta(days=n)).isoformat() for n in range(ndays)][::-1]
        result = {**{k:0 for k in FIELDS}, 'msgs':0, 'models':{}, 'sessions':{}, 'series':[]}
        for key in keys:
            d = self.days.get(key) or {}
            for field in (*FIELDS,'msgs'):
                result[field] += d.get(field,0)
            result['series'].append([key,d.get('tokens',0)])
            for field in ('models','sessions'):
                for name,values in d.get(field,{}).items():
                    row = result[field].setdefault(name,[0,0])
                    row[0] += values[0]
                    row[1] += values[1]
        result['nsessions'] = len(result['sessions'])
        result['models'] = sorted([[name,*v] for name,v in result['models'].items()],key=lambda x:-x[1])
        result['sessions'] = sorted([[sid,self.titles.get(sid) or sid[:8],*v] for sid,v in result['sessions'].items()],key=lambda x:-x[2])[:10]
        if ndays == 1:
            result['hours'] = (self.days.get(keys[0]) or {}).get('hours') or [0]*24
        return result

    def snapshot(self):
        return {'ready':self.ready, 'at':time.time(), **{f'r{n}':self.summary(n) for n in (1,7,30)}}

    def save(self):
        if not self.cache_path:
            return
        edge = time.time()-62*86400
        self.days = {k:v for k,v in self.days.items() if k >= day_key(edge)}
        self.seen = {k:v for k,v in self.seen.items() if v['at'] >= edge}
        save_json(self.cache_path, {'version':2, 'files':self.files, 'days':self.days,
                                   'seen':self.seen, 'titles':self.titles})
