# -*- coding: utf-8 -*-
"""提供共享路径、时间金额格式和主题配色。"""

import datetime
import json
import os
import sys
import time

if getattr(sys, "frozen", False):
    APP_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    DATA_DIR = os.path.join(os.path.dirname(sys.executable), "data")
else:
    APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(APP_DIR, "data")
ASSET_DIR = os.path.join(APP_DIR, "assets")


def claude_home():
    """Claude Code 的配置目录。默认 ~/.claude，设了 CLAUDE_CONFIG_DIR 就用那个。"""
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def data_path(name):
    os.makedirs(DATA_DIR, exist_ok=True)
    return os.path.join(DATA_DIR, name)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, data):
    """先写临时文件再换过去，写到一半断电也不会把旧文件弄坏。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        pass


def parse_iso(text):
    """ISO 时间字符串 -> Unix 秒。解析不了返回 None。"""
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def day_key(ts=None):
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if ts is None else ts))


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------

def fmt_tokens(n):
    n = float(n or 0)
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= size:
            v = n / size
            return f"{v:.0f}{unit}" if v >= 100 else f"{v:.1f}{unit}"
    return str(int(n))


def fmt_money(x):
    if x is None:
        return "—"
    if x >= 100:
        return f"${x:,.0f}"
    if x >= 10:
        return f"${x:.1f}"
    return f"${x:.2f}"


def fmt_span(seconds):
    """2 小时 11 分 / 47 分钟 / 3 天 5 小时。"""
    s = max(0, int(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} 天 {h} 小时" if h else f"{d} 天"
    if h:
        return f"{h} 小时 {m} 分" if m else f"{h} 小时"
    if m:
        return f"{m} 分钟"
    return "不到 1 分钟"


def fmt_hm(seconds):
    """血条上用的短格式：2:11 或 47分。"""
    s = max(0, int(seconds))
    if s >= 86400:
        return f"{s // 86400}天{(s % 86400) // 3600}时"
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}"
    return f"{max(1, s // 60)}分"


WEEKDAYS = "一二三四五六日"


def fmt_clock(ts):
    """今天的只写几点，别的日子带上日期和星期。"""
    lt = time.localtime(ts)
    hm = f"{lt.tm_hour}:{lt.tm_min:02d}"
    days = (datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday) - datetime.date.today()).days
    if days == 0:
        return hm
    if days == 1:
        return f"明天 {hm}"
    if days == -1:
        return f"昨天 {hm}"
    return f"{lt.tm_mon}月{lt.tm_mday}日 周{WEEKDAYS[lt.tm_wday]} {hm}"


def fmt_ago(ts):
    if not ts:
        return "还没"
    s = time.time() - ts
    if s < 60:
        return "刚刚"
    return fmt_span(s) + "前"


# ---------------------------------------------------------------------------
# 配色，和气泡一个色系
# ---------------------------------------------------------------------------

INK = (72, 56, 66)
MUTED = (150, 128, 140)
PINK = (236, 170, 196)
ROSE = (214, 92, 140)
GOOD = (82, 190, 130)
WARN = (238, 168, 58)
BAD = (230, 88, 92)
WEEK = (128, 148, 238)


def remain_rgb(remain):
    """剩得多是绿的，快没了是红的。"""
    if remain is None:
        return (190, 180, 186)
    if remain > 50:
        return GOOD
    if remain > 20:
        return WARN
    return BAD
