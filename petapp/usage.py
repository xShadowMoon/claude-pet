# -*- coding: utf-8 -*-
"""获取 Claude 额度并汇总本机 token 与估算费用。

两路数据，都在后台线程里跑，算好了用 Qt 信号交给主线程：

1. 额度：问 Anthropic 的用量接口（就是 Claude Code 里 /usage 用的那个），拿到
   5 小时窗口和本周的已用百分比、重置时间、附加额度、额外用量、本周用在哪。
   登录令牌从 ~/.claude/.credentials.json 里读，只读不写，也不替你续期，
   只发给 api.anthropic.com。令牌过期了就等 Claude Code 自己续上。
2. 用量：扫本机 ~/.claude/projects 下的对话记录，统计每天用了多少 token、
   按 API 标价相当于多少钱。只读本地文件，不联网。
"""

import datetime
import glob
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque

from PySide6.QtCore import QObject, Signal

from . import __version__
from .common import (claude_home, data_path, day_key, fmt_clock, fmt_money,
                     fmt_span, fmt_tokens, load_json, parse_iso, save_json)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
QUOTA_INTERVAL_S = 300      # 平时五分钟问一次
QUOTA_MIN_GAP_S = 45        # 再急也不比这更勤，免得被限流
SCAN_INTERVAL_S = 30        # 本地记录半分钟扫一次，只读新增的部分
TURN_DELAY_S = 1.5          # 一轮结束后等一下再算账，对话记录是异步写的
KEEP_DAYS = 62
READ_CHUNK = 4 << 20
CACHE_VERSION = 1

# ---------------------------------------------------------------------------
# 价格：美元 / 百万 token，(输入, 输出, 缓存读取)
# 缓存写入按输入价 ×1.25（5 分钟）和 ×2（1 小时）算，fast 模式整体 ×2。
# 要改就在 data/prices.json 里写同样格式的覆盖，比如 {"claude-opus-5-5": [4, 20, 0.2]}
# ---------------------------------------------------------------------------

PRICES = {
    "claude-fable-5-1": (10, 50, 0.25),
    "claude-mythos-5-1": (10, 50, 0.25),
    "claude-fable-5": (10, 50, 1.0),
    "claude-mythos-5": (10, 50, 1.0),
    "claude-opus-5-5": (4, 20, 0.20),
    "claude-opus-5": (5, 25, 0.50),
    "claude-opus-4-8": (5, 25, 0.50),
    "claude-opus-4-7": (5, 25, 0.50),
    "claude-opus-4-6": (5, 25, 0.50),
    "claude-opus-4-5": (5, 25, 0.50),
    "claude-opus-4-1": (15, 75, 1.50),
    "claude-opus-4": (15, 75, 1.50),
    "claude-sonnet-5-5": (2, 10, 0.20),
    "claude-sonnet-5": (2, 10, 0.20),
    "claude-sonnet-4-6": (3, 15, 0.30),
    "claude-sonnet-4-5": (3, 15, 0.30),
    "claude-sonnet-4": (3, 15, 0.30),
    "claude-3-7-sonnet": (3, 15, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1, 5, 0.10),
    "claude-3-5-haiku": (0.8, 4, 0.08),
}
FAMILY_FALLBACK = (
    ("fable", (10, 50, 0.25)), ("mythos", (10, 50, 0.25)), ("opus", (5, 25, 0.50)),
    ("sonnet", (3, 15, 0.30)), ("haiku", (1, 5, 0.10)),
)

_DATE_SUFFIX = re.compile(r"-20\d{6}$")
_PRETTY = re.compile(r"claude-(opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d+))?")
_PRETTY_OLD = re.compile(r"claude-(\d+)(?:-(\d+))?-(opus|sonnet|haiku)")


def normalize_model(model):
    m = str(model or "").lower().strip()
    m = m.split("[")[0].split("@")[0]
    i = m.find("claude-")
    if i > 0:
        m = m[i:]                      # 去掉 bedrock 之类的前缀
    return _DATE_SUFFIX.sub("", m)


def pretty_model(model):
    key = normalize_model(model)
    hit = _PRETTY.match(key)
    if hit:
        fam, a, b = hit.groups()
        return f"{fam.capitalize()} {a}.{b}" if b else f"{fam.capitalize()} {a}"
    hit = _PRETTY_OLD.match(key)
    if hit:
        a, b, fam = hit.groups()
        return f"{fam.capitalize()} {a}.{b}" if b else f"{fam.capitalize()} {a}"
    return key or "未知模型"


def load_price_overrides():
    out = {}
    raw = load_json(data_path("prices.json"), {}) or {}
    if isinstance(raw, dict):
        for key, val in raw.items():
            try:
                out[normalize_model(key)] = tuple(float(x) for x in list(val)[:3])
            except Exception:
                continue
    return out


def price_for(model, overrides):
    key = normalize_model(model)
    if key in overrides:
        return overrides[key]
    if key in PRICES:
        return PRICES[key]
    for k in sorted(PRICES, key=len, reverse=True):
        if key.startswith(k):
            return PRICES[k]
    for fam, price in FAMILY_FALLBACK:
        if fam in key:
            return price
    return None


def usage_cost(model, u, overrides):
    """一条回复的 token 数 [输入, 输出, 缓存读, 缓存写] 和按 API 价算的美元。"""
    inp = int(u.get("input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    cw = int(u.get("cache_creation_input_tokens") or 0)
    cc = u.get("cache_creation") or {}
    w1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    w5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
    if w1 + w5 < cw:
        w5 += cw - (w1 + w5)
    tokens = [inp, out, cr, w1 + w5]
    price = price_for(model, overrides)
    if price is None:
        return tokens, 0.0
    pin, pout, pcr = price
    if normalize_model(model).startswith("claude-haiku-5-5") and inp + cr + cw > 100_000:
        pin, pout, pcr = pin * 5, pout * 5, pcr * 5        # 长提示词的阶梯价
    cost = (inp * pin + out * pout + cr * pcr + w5 * pin * 1.25 + w1 * pin * 2.0) / 1e6
    if u.get("speed") == "fast":
        cost *= 2
    searches = (u.get("server_tool_use") or {}).get("web_search_requests") or 0
    cost += int(searches) * 0.01
    return tokens, cost


# ---------------------------------------------------------------------------
# 本机对话记录
# ---------------------------------------------------------------------------

class TranscriptScanner:
    """增量扫描 ~/.claude/projects 下的对话记录。

    每个文件记住读到了哪个字节，下次只读新增的部分。同一条回复在记录里会按
    内容块拆成好几行，用 message.id 去重；分叉、续聊时复制过来的旧记录也不会
    重复算。结果存在 data/usage_cache.json，重启不用从头扫。
    """

    def __init__(self):
        self.root = os.path.join(claude_home(), "projects")
        self.cache_path = data_path("usage_cache.json")
        self.overrides = load_price_overrides()
        self.files = {}       # 路径 -> {"pos": 读到哪, "last": 最后一条回复}
        self.days = {}        # 日期 -> 当天汇总
        self.seen = {}        # message.id -> 日期
        self.titles = {}      # 会话 -> 标题
        self.places = {}      # 会话 -> 工作目录名，没标题时拿来当名字
        self.sess = {}        # 会话 -> [tokens, 美元]，用来算每一轮花了多少
        self.recent = []      # [时间, tokens, 美元]，只留最近 8 天，算额度窗口用
        self.marks = {}
        self.window_starts = {}
        self.ready = False
        self._load()

    # ---------------- 缓存 ----------------

    def _load(self):
        data = load_json(self.cache_path)
        if not isinstance(data, dict) or data.get("v") != CACHE_VERSION:
            return
        self.files = data.get("files") or {}
        self.days = data.get("days") or {}
        self.seen = data.get("seen") or {}
        self.titles = data.get("titles") or {}
        self.places = data.get("places") or {}
        self.sess = data.get("sess") or {}
        self.recent = data.get("recent") or []

    def save(self):
        cutoff = day_key(time.time() - KEEP_DAYS * 86400)
        self.days = {k: v for k, v in self.days.items() if k >= cutoff}
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        edge = time.time() - 8 * 86400
        self.recent = [r for r in self.recent if r[0] >= edge]
        self.files = {k: v for k, v in self.files.items() if os.path.exists(k)}
        alive = set()
        for d in self.days.values():
            alive.update(d.get("sess", {}))
        self.sess = {k: v for k, v in self.sess.items() if k in alive or k in self.marks}
        self.titles = {k: v for k, v in self.titles.items() if k in alive}
        self.places = {k: v for k, v in self.places.items() if k in alive}
        save_json(self.cache_path, {
            "v": CACHE_VERSION, "files": self.files, "days": self.days, "seen": self.seen,
            "titles": self.titles, "places": self.places, "sess": self.sess, "recent": self.recent,
        })

    # ---------------- 扫描 ----------------

    def scan_all(self):
        changed = False
        old_edge = time.time() - KEEP_DAYS * 86400
        for path in glob.glob(os.path.join(self.root, "**", "*.jsonl"), recursive=True):
            key = os.path.normcase(os.path.abspath(path))
            info = self.files.get(key)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if info is not None and info.get("pos") == st.st_size:
                continue
            if info is None and st.st_mtime < old_edge:
                self.files[key] = {"pos": st.st_size, "last": None}
                continue
            if self.scan_file(path):
                changed = True
        self.ready = True
        return changed

    def scan_session(self, path):
        """只扫一个会话：它自己的记录加上它派出去的子 agent 的记录。"""
        changed = self.scan_file(path)
        sub = os.path.join(os.path.splitext(path)[0], "subagents")
        for f in glob.glob(os.path.join(sub, "*.jsonl")):
            changed = self.scan_file(f) or changed
        return changed

    def scan_file(self, path):
        key = os.path.normcase(os.path.abspath(path))
        info = self.files.get(key)
        if info is None:
            info = self.files[key] = {"pos": 0, "last": None}
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        pos = int(info.get("pos") or 0)
        if size < pos:                      # 文件被重写了，从头来；有去重，不会重复算
            pos = 0
            info["last"] = None
        if size == pos:
            return False
        changed = False
        try:
            with open(path, "rb") as fh:
                fh.seek(pos)
                carry = b""
                while True:
                    chunk = fh.read(READ_CHUNK)
                    if not chunk:
                        break
                    buf = carry + chunk
                    end = buf.rfind(b"\n")
                    if end < 0:
                        carry = buf
                        continue
                    for line in buf[:end].split(b"\n"):
                        if line and self._line(line, info):
                            changed = True
                    pos += end + 1           # 最后半行没写完的留到下次
                    carry = buf[end + 1:]
        except OSError:
            pass
        info["pos"] = pos
        return changed

    def _line(self, line, info):
        if b'"type":"assistant"' in line:
            if b'"usage"' not in line:
                return False
            try:
                o = json.loads(line)
            except ValueError:
                return False
            return self._assistant(o, info)
        if b'"type":"custom-title"' in line:
            try:
                o = json.loads(line)
            except ValueError:
                return False
            sid = o.get("sessionId")
            title = o.get("customTitle")
            if sid and title:
                self.titles[sid] = str(title).strip()[:80]
        return False

    def _assistant(self, o, info):
        if o.get("type") != "assistant":
            return False
        msg = o.get("message") or {}
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return False
        model = msg.get("model") or ""
        if model.startswith("<"):            # <synthetic> 这种是本地生成的，不花钱
            return False
        ts = parse_iso(o.get("timestamp")) or time.time()
        sid = o.get("sessionId") or ""
        key = msg.get("id") or o.get("requestId") or ""
        tok, cost = usage_cost(model, usage, self.overrides)
        cwd = o.get("cwd")
        if sid and cwd and sid not in self.places:
            self.places[sid] = os.path.basename(str(cwd).rstrip("\\/")) or str(cwd)

        last = info.get("last")
        if key and last and last[0] == key:
            # 同一条回复的后续几行：只补上变大的那部分
            new_tok = [max(a, b) for a, b in zip(tok, last[1])]
            new_cost = max(cost, last[2])
            delta = [a - b for a, b in zip(new_tok, last[1])]
            dcost = new_cost - last[2]
            info["last"] = [key, new_tok, new_cost, last[3]]
            if any(delta) or dcost > 1e-12:
                self._add(last[3], model, sid, delta, dcost, 0)
                return True
            return False
        if key:
            if key in self.seen:
                return False
            self.seen[key] = day_key(ts)
        self._add(ts, model, sid, tok, cost, 1)
        info["last"] = [key, list(tok), cost, ts]
        return True

    def _add(self, ts, model, sid, tok, cost, msgs):
        lt = time.localtime(ts)
        day = time.strftime("%Y-%m-%d", lt)
        d = self.days.get(day)
        if d is None:
            d = self.days[day] = {"cost": 0.0, "in": 0, "out": 0, "cr": 0, "cw": 0, "msgs": 0,
                                  "models": {}, "sess": {}, "hours": [0.0] * 24, "htok": [0] * 24}
        inp, out, cr, cw = tok
        total = inp + out + cr + cw
        d["cost"] += cost
        d["in"] += inp
        d["out"] += out
        d["cr"] += cr
        d["cw"] += cw
        d["msgs"] += msgs
        d["hours"][lt.tm_hour] += cost
        d["htok"][lt.tm_hour] += total
        m = d["models"].setdefault(pretty_model(model), [0.0, 0, 0])
        m[0] += cost
        m[1] += total
        m[2] += msgs
        if sid:
            s = d["sess"].setdefault(sid, [0.0, 0])
            s[0] += cost
            s[1] += total
            st = self.sess.setdefault(sid, [0, 0.0])
            st[0] += total
            st[1] += cost
        self.recent.append([ts, total, cost])

    # ---------------- 汇总 ----------------

    def title_of(self, sid):
        return self.titles.get(sid) or self.places.get(sid) or (sid[:8] if sid else "未知会话")

    def since(self, start):
        tok, cost = 0, 0.0
        for t, a, c in self.recent:
            if t >= start:
                tok += a
                cost += c
        return [tok, cost]

    def summary(self, ndays):
        today = datetime.date.today()
        keys = [(today - datetime.timedelta(days=i)).isoformat() for i in range(ndays)][::-1]
        tot = {"cost": 0.0, "in": 0, "out": 0, "cr": 0, "cw": 0, "msgs": 0}
        models, sess, series = {}, {}, []
        for day in keys:
            d = self.days.get(day)
            if not d:
                series.append([day, 0.0, 0])
                continue
            for f in tot:
                tot[f] += d[f]
            series.append([day, d["cost"], d["in"] + d["out"] + d["cr"] + d["cw"]])
            for name, vals in d["models"].items():
                m = models.setdefault(name, [0.0, 0, 0])
                m[0] += vals[0]
                m[1] += vals[1]
                m[2] += vals[2]
            for sid, vals in d["sess"].items():
                s = sess.setdefault(sid, [0.0, 0])
                s[0] += vals[0]
                s[1] += vals[1]
        tot["tokens"] = tot["in"] + tot["out"] + tot["cr"] + tot["cw"]
        tot["models"] = sorted(([name, v[0], v[1], v[2]] for name, v in models.items()), key=lambda r: -r[1])
        tot["sessions"] = sorted(([sid, self.title_of(sid), v[0], v[1]] for sid, v in sess.items()),
                                 key=lambda r: -r[2])[:8]
        tot["nsessions"] = len(sess)
        tot["series"] = series
        if ndays == 1:
            d = self.days.get(keys[-1]) or {}
            tot["hours"] = list(d.get("hours") or [0.0] * 24)
            tot["htok"] = list(d.get("htok") or [0] * 24)
        return tot

    def build_snapshot(self):
        snap = {"ready": self.ready, "at": time.time(),
                "r1": self.summary(1), "r7": self.summary(7), "r30": self.summary(30), "win": {}}
        for key, start in list(self.window_starts.items()):
            snap["win"][key] = self.since(start)
        return snap


# ---------------------------------------------------------------------------
# 额度接口
# ---------------------------------------------------------------------------

WINDOW_LABELS = {
    "five_hour": "5 小时",
    "seven_day": "本周 · 全部模型",
    "seven_day_opus": "本周 · Opus",
    "seven_day_sonnet": "本周 · Sonnet",
    "seven_day_cowork": "本周 · Cowork",
    "seven_day_oauth_apps": "本周 · 第三方应用",
}
LIMIT_KINDS = {"session": "five_hour", "weekly_all": "seven_day"}
MIX_NAMES = {"Claude Code": "Claude Code", "Chats": "聊天", "Cowork": "Cowork", "Other": "其它"}


class QuotaError(Exception):
    def __init__(self, kind, text):
        super().__init__(text)
        self.kind = kind
        self.text = text


def read_credentials():
    data = load_json(os.path.join(claude_home(), ".credentials.json"))
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        return None
    return {"token": token, "expires": (oauth.get("expiresAt") or 0) / 1000.0,
            "plan": oauth.get("subscriptionType") or ""}


def _money(obj):
    if obj is None:
        return None
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, dict) and "amount_minor" in obj:
        try:
            return obj["amount_minor"] / (10 ** int(obj.get("exponent", 2)))
        except Exception:
            return None
    return None


def parse_quota(data, plan=""):
    windows, money = [], []
    for key, val in data.items():
        if not isinstance(val, dict) or "utilization" not in val:
            continue
        if val.get("limit_dollars") is not None:
            limit = float(val["limit_dollars"])
            used = float(val.get("used_dollars") or 0)
            rem = val.get("remaining_dollars")
            money.append({"key": key, "label": "附加额度", "limit": limit, "used": used,
                          "remaining": float(rem) if rem is not None else max(0.0, limit - used),
                          "resets_at": parse_iso(val.get("resets_at"))})
            continue
        if val.get("utilization") is None:
            continue
        windows.append({"key": key, "label": WINDOW_LABELS.get(key, "其它限额"),
                        "used": float(val["utilization"]), "resets_at": parse_iso(val.get("resets_at"))})
    if not windows:
        for item in data.get("limits") or []:
            key = LIMIT_KINDS.get(item.get("kind"), item.get("kind") or "other")
            if item.get("percent") is None:
                continue
            windows.append({"key": key, "label": WINDOW_LABELS.get(key, "其它限额"),
                            "used": float(item["percent"]), "resets_at": parse_iso(item.get("resets_at"))})
    order = list(WINDOW_LABELS)
    windows.sort(key=lambda w: order.index(w["key"]) if w["key"] in order else 99)

    extra = data.get("extra_usage") or {}
    spend = data.get("spend") or {}
    extra_info = {
        "enabled": bool(extra.get("is_enabled") or spend.get("enabled")),
        "used": _money(spend.get("used")),
        "limit": _money(spend.get("limit")),
        "reached": bool(extra.get("spend_limit_reached")),
    }
    rows = (data.get("seven_day_breakdown") or {}).get("rows") or []
    mix = [[MIX_NAMES.get(r.get("display_name"), r.get("display_name") or r.get("key") or "其它"),
            float(r.get("percent") or 0)] for r in rows]
    return {"fetched_at": time.time(), "source": "api", "plan": plan, "windows": windows,
            "money": money, "extra": extra_info, "breakdown": mix}


def fetch_quota():
    cred = read_credentials()
    if cred is None:
        raise QuotaError("none", "没找到 Claude Code 的登录信息，在命令行里登录一次就有了")
    if cred["expires"] and cred["expires"] < time.time() + 30:
        raise QuotaError("auth", "登录令牌过期了，用一下 Claude Code 它会自己续上")
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + cred["token"],
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": f"claude-pet/{__version__}",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            raise QuotaError("auth", "登录令牌失效了，用一下 Claude Code 它会自己续上")
        if err.code == 429:
            raise QuotaError("rate", "问得太勤被限流了，过一会儿再试")
        raise QuotaError("net", f"额度接口返回了 {err.code}")
    except (urllib.error.URLError, OSError, ValueError):
        raise QuotaError("net", "连不上 Anthropic，看看网络")
    if not isinstance(data, dict):
        raise QuotaError("net", "额度接口返回的东西看不懂")
    return parse_quota(data, cred["plan"])


# ---------------------------------------------------------------------------
# 给界面用的总入口
# ---------------------------------------------------------------------------

class UsageHub(QObject):
    quota_changed = Signal()
    local_changed = Signal()
    turn_cost = Signal(str, dict)       # 会话 id, {"tokens", "cost", "seconds"}

    def __init__(self, fetch_quota=True):
        super().__init__()
        self.scanner = TranscriptScanner()
        self.fetch_enabled = bool(fetch_quota)
        self.quota = None
        self.quota_error = ""
        self.history = deque(maxlen=240)     # (时间, 5 小时已用, 重置时间)，用来估速度
        self.local = {}
        self._stop = False
        self._recompute = True
        self._qwake = threading.Event()
        self._swake = threading.Event()
        self._qdue = 0.0
        self._last_fetch = 0.0
        self._tasks = []
        self._tlock = threading.Lock()
        self._threads = []
        cached = load_json(data_path("quota.json"))
        if isinstance(cached, dict) and isinstance(cached.get("windows"), list):
            self.quota = cached
            for item in cached.get("history") or []:
                if isinstance(item, list) and len(item) == 3:
                    self.history.append(tuple(item))
            self._set_window_starts(cached)

    # ---------------- 生命周期 ----------------

    def start(self):
        for target in (self._scan_loop, self._quota_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop = True
        self._qwake.set()
        self._swake.set()
        if self._threads:
            self._threads[0].join(timeout=3)

    def refresh_quota(self, force=False):
        now = time.time()
        due = now if force else max(now, self._last_fetch + QUOTA_MIN_GAP_S)
        if force or due < self._qdue:
            self._qdue = due
        self._qwake.set()

    def set_fetch_enabled(self, on):
        self.fetch_enabled = bool(on)
        if on:
            self.refresh_quota(force=True)

    def mark_turn(self, sid, path):
        with self._tlock:
            self._tasks.append([time.time(), "mark", sid, path, None])
        self._swake.set()

    def finish_turn(self, sid, path, seconds):
        with self._tlock:
            self._tasks.append([time.time() + TURN_DELAY_S, "turn", sid, path, seconds])
        self._swake.set()

    def update_from_statusline(self, rl):
        """命令行版状态栏带来的官方额度数字，比定时问接口更新，直接拿来用。"""
        now = time.time()
        snap = dict(self.quota or {"money": [], "extra": {}, "breakdown": [], "plan": ""})
        wins = {w["key"]: dict(w) for w in snap.get("windows", [])}
        changed = False
        for key in ("five_hour", "seven_day"):
            src = rl.get(key) or {}
            used = src.get("used_percentage")
            if used is None:
                continue
            w = wins.setdefault(key, {"key": key, "label": WINDOW_LABELS[key]})
            w["used"] = float(used)
            if src.get("resets_at"):
                w["resets_at"] = float(src["resets_at"])
            changed = True
        if not changed:
            return
        order = list(WINDOW_LABELS)
        snap["windows"] = sorted(wins.values(), key=lambda w: order.index(w["key"]) if w["key"] in order else 99)
        snap["fetched_at"] = now
        snap["source"] = "statusline"
        five = wins.get("five_hour")
        if five:
            self.history.append((now, five["used"], five.get("resets_at") or 0))
        self.quota = snap
        self._set_window_starts(snap)
        self.quota_changed.emit()

    # ---------------- 查询 ----------------

    def window(self, key):
        for w in (self.quota or {}).get("windows", []):
            if w.get("key") == key:
                out = dict(w)
                reset = out.get("resets_at")
                if reset and reset < time.time():
                    out["used"] = 0.0             # 过了重置时间，旧数字就不算了
                    out["after_reset"] = True
                    out["resets_at"] = None
                return out
        return None

    def windows(self):
        out = []
        for w in (self.quota or {}).get("windows", []):
            x = self.window(w.get("key"))
            if x:
                out.append(x)
        return out

    def quota_age(self):
        if not self.quota:
            return float("inf")
        return time.time() - (self.quota.get("fetched_at") or 0)

    def local_window(self, key):
        return ((self.local or {}).get("win") or {}).get(key)

    def predict(self, key="five_hour"):
        """照最近一个多小时的速度，还有多少秒用完。算不出来返回 None。"""
        w = self.window(key)
        if key != "five_hour" or not w or w.get("after_reset") or w["used"] >= 100:
            return None
        now = time.time()
        reset = w.get("resets_at") or 0
        pts = [(t, u) for t, u, r in self.history if abs((r or 0) - reset) < 300 and now - t < 75 * 60]
        if len(pts) < 2:
            return None
        (t0, u0), (t1, u1) = pts[0], pts[-1]
        if t1 - t0 < 8 * 60 or u1 <= u0:
            return None
        return (100.0 - u1) / ((u1 - u0) / (t1 - t0))

    def cost_per_percent(self):
        """5 小时额度的 1% 大约值多少美元（按本机记录和已用百分比对出来的）。"""
        w = self.window("five_hour")
        win = self.local_window("five_hour")
        if not w or not win or w.get("after_reset") or w["used"] < 5 or win[1] < 0.5:
            return None
        return win[1] / w["used"]

    def brief(self):
        parts = []
        f = self.window("five_hour")
        w = self.window("seven_day")
        if f:
            parts.append(f"5h 已用 {f['used']:.0f}%")
        if w:
            parts.append(f"本周 {w['used']:.0f}%")
        return " · ".join(parts) or (self.quota_error or "额度还没拿到")

    def tooltip(self):
        lines = []
        now = time.time()
        for key in ("five_hour", "seven_day"):
            w = self.window(key)
            if not w:
                continue
            line = f"{w['label']}：已用 {w['used']:.0f}%"
            if w.get("resets_at"):
                line += f"，{fmt_clock(w['resets_at'])} 重置（还有 {fmt_span(w['resets_at'] - now)}）"
            lines.append(line)
        eta = self.predict()
        f = self.window("five_hour")
        if eta and f and f.get("resets_at") and eta < f["resets_at"] - now:
            lines.append(f"照现在的速度，大约 {fmt_span(eta)} 后用完")
        today = (self.local or {}).get("r1") or {}
        if today.get("tokens"):
            lines.append(f"今天 {fmt_tokens(today['tokens'])} tokens，按 API 价约 {fmt_money(today.get('cost', 0))}")
        if self.quota_error:
            lines.append(self.quota_error)
        lines.append("点一下打开用量面板")
        return "\n".join(lines)

    def cli_summary(self):
        lines = []
        for w in self.windows():
            s = f"{w['label']}：已用 {w['used']:.0f}%，还剩 {max(0, 100 - w['used']):.0f}%"
            if w.get("resets_at"):
                s += f"，{fmt_clock(w['resets_at'])} 重置"
            lines.append(s)
        for m in (self.quota or {}).get("money", []):
            lines.append(f"{m['label']}：剩 {fmt_money(m['remaining'])} / {fmt_money(m['limit'])}")
        today = (self.local or {}).get("r1") or {}
        if today:
            lines.append(f"今天：{fmt_tokens(today.get('tokens', 0))} tokens，按 API 价约 {fmt_money(today.get('cost', 0))}")
        if self.quota_error:
            lines.append(f"（{self.quota_error}）")
        return {"lines": lines, "brief": self.brief(), "fetched_at": (self.quota or {}).get("fetched_at")}

    # ---------------- 后台：额度 ----------------

    def _set_window_starts(self, snap):
        starts = {}
        for w in snap.get("windows", []):
            reset = w.get("resets_at")
            if not reset:
                continue
            if w["key"] == "five_hour":
                starts["five_hour"] = reset - 5 * 3600
            elif w["key"] == "seven_day":
                starts["seven_day"] = reset - 7 * 86400
        self.scanner.window_starts = starts
        self._recompute = True
        self._swake.set()

    def _next_due(self, snap):
        now = time.time()
        due = now + QUOTA_INTERVAL_S
        for w in snap.get("windows", []):
            reset = w.get("resets_at")
            if reset and now < reset + 20 < due:
                due = reset + 20                  # 到点重置了马上再问一次
        return due

    def _quota_loop(self):
        backoff = QUOTA_INTERVAL_S
        while not self._stop:
            self._qwake.clear()
            wait = self._qdue - time.time()
            if wait > 0:
                self._qwake.wait(min(wait, 60))
                continue
            if not self.fetch_enabled:
                self._qdue = time.time() + 30
                continue
            self._last_fetch = time.time()
            try:
                snap = fetch_quota()
            except QuotaError as err:
                self.quota_error = err.text
                if err.kind == "rate":
                    backoff = min(backoff * 2, 1800)
                    self._qdue = time.time() + backoff
                else:
                    self._qdue = time.time() + (90 if err.kind == "net" else 120)
            except Exception as err:
                self.quota_error = f"额度数据出错了：{err}"
                self._qdue = time.time() + 120
            else:
                backoff = QUOTA_INTERVAL_S
                self.quota_error = ""
                five = next((w for w in snap["windows"] if w["key"] == "five_hour"), None)
                if five:
                    self.history.append((snap["fetched_at"], five["used"], five.get("resets_at") or 0))
                snap["history"] = [list(h) for h in list(self.history)[-60:]]
                self.quota = snap
                save_json(data_path("quota.json"), snap)
                self._set_window_starts(snap)
                self._qdue = self._next_due(snap)
            self.quota_changed.emit()

    # ---------------- 后台：本机记录 ----------------

    def _next_task_at(self):
        with self._tlock:
            return min((t[0] for t in self._tasks), default=None)

    def _run_tasks(self):
        now = time.time()
        with self._tlock:
            due = [t for t in self._tasks if t[0] <= now]
            self._tasks = [t for t in self._tasks if t[0] > now]
        for _, kind, sid, path, seconds in due:
            try:
                self.scanner.scan_session(path)
            except Exception:
                pass
            cur = list(self.scanner.sess.get(sid) or [0, 0.0])
            if kind == "mark":
                self.scanner.marks[sid] = cur
                continue
            base = self.scanner.marks.get(sid)
            self.scanner.marks[sid] = cur
            if base is not None:
                self.turn_cost.emit(sid, {"tokens": cur[0] - base[0], "cost": cur[1] - base[1],
                                          "seconds": seconds})
        return bool(due)

    def _scan_loop(self):
        next_scan = 0.0
        last_save = time.time()
        dirty = False
        while not self._stop:
            self._swake.clear()
            ran = False
            changed = False
            try:
                ran = self._run_tasks()
            except Exception:
                pass
            if time.time() >= next_scan:
                try:
                    changed = self.scanner.scan_all()
                except Exception:
                    changed = False
                next_scan = time.time() + SCAN_INTERVAL_S
            if changed or ran or self._recompute or not self.local:
                self._recompute = False
                try:
                    self.local = self.scanner.build_snapshot()
                except Exception:
                    pass
                self.local_changed.emit()
            dirty = dirty or changed or ran
            if dirty and time.time() - last_save > 60:
                self.scanner.save()
                last_save = time.time()
                dirty = False
            wake = next_scan
            nt = self._next_task_at()
            if nt is not None:
                wake = min(wake, nt)
            self._swake.wait(max(0.05, min(SCAN_INTERVAL_S, wake - time.time())))
        try:
            self.scanner.save()
        except Exception:
            pass
