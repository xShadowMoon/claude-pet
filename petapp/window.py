# -*- coding: utf-8 -*-
"""桌宠主窗口：渲染、物理、交互、菜单，以及和 Claude Code 的联动。

和 Claude Code 有关的几块分在别的文件里：
    sessions.py   每个会话在干什么
    usage.py      额度和用量（头顶血条、用量面板的数据）
    approval.py   在桌宠上点"允许"
    panel.py      用量面板
"""

import math
import os
import random
import sys
import time
from collections import OrderedDict

from PySide6.QtCore import QPoint, QRectF, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCursor,
    QGuiApplication,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QMenu, QPushButton, QSystemTrayIcon, QWidget

from . import winutil
from .ambient import Ambient
from .animator import Animator
from .approval import ApprovalCard
from .bubble import Bubble
from .common import APP_DIR, ASSET_DIR, data_path, fmt_clock, fmt_money, fmt_span, fmt_tokens
from .codex import CodexMonitor
from .chat import ChatHub
from .hotkey import MiniHotkey
from .hud import QuotaHud
from .server import BRIDGE, PENDING, STATUS, EventServer
from .sessions import STATE_RGB, Session, SessionTracker, clip, project_of
from .states import (
    AMBIENT_MAP,
    DEFAULT_STATE,
    EVENT_MAP,
    LOCAL_EVENTS,
    MENU_STATES,
    STATES,
    WALK_STATES,
)
from .usage import UsageHub

TICK_MS = 16                    # 约 60 帧
GRAVITY = 1500.0                # 像素/秒²
BOUNCE = 0.52                   # 撞地反弹保留多少速度
WALL_BOUNCE = 0.6
AIR_DRAG = 0.86                 # 每秒剩多少水平速度
SETTLE_SPEED = 60.0             # 慢到这个程度就算停下来了
WALK_SPEED = 78.0               # 平时溜达的速度（像素/秒）
FOLLOW_SPEED = 190.0
FOLLOW_GAP = 130.0              # 跟随时和鼠标保持的距离
DRAG_THRESHOLD = 5
MOVE_ART_SPEED = 22.0           # 水平速度超过这个就播走路动作
SLEEP_AFTER_S = 300             # 五分钟没人理就打盹
IDLE_RESET_S = 150              # 这么久没收到事件就回待机

SCALE_CHOICES = [0.45, 0.6, 0.75, 0.9, 1.1, 1.35]

FOCUS_CHOICES = [15, 25, 45, 60]   # 专注模式可选的时长（分钟）
FOCUS_REST_MIN = 5                 # 每段专注之后休息多久
CLAUDE_HOLD_S = 90                 # Claude Code 刚有动静的这段时间里，本地感知先让路

IDLE_CLEANUP_CHOICES = [(15, "15 分钟"), (30, "30 分钟"), (60, "1 小时"), (120, "2 小时"), (0, "一直留着")]
APPROVAL_WAIT_CHOICES = [(60, "1 分钟"), (120, "2 分钟"), (300, "5 分钟")]
TRAY_RGB = {"waiting": STATE_RGB["waiting"], "error": STATE_RGB["error"],
            "working": STATE_RGB["working"], "done": STATE_RGB["done"]}
PANEL_TABS = {"额度": 0, "quota": 0, "统计": 1, "用量": 1, "stats": 1, "会话": 2, "sessions": 2,
              "电脑": 3, "system": 3, "codex": 4, "Codex": 4,
              "codex-sessions":5,"codex-stats":6}

STAT_LABELS = [
    ("sessions", "开过的会话"),
    ("prompts", "你发的消息"),
    ("turns", "完成的回合"),
    ("tools", "工具调用"),
    ("edits", "改文件"),
    ("waits", "等你确认"),
    ("approvals", "在桌宠上审批"),
    ("errors", "报错"),
]
STAT_KEYS = [k for k, _ in STAT_LABELS] + ["work_s"]

API_ERRORS = {
    "overloaded": "服务器太忙了",
    "authentication_failed": "登录失效了",
    "oauth_org_not_allowed": "这个账号没权限",
    "billing_error": "账单出问题了",
    "server_error": "服务器出错了",
    "max_output_tokens": "回答太长被截断了",
    "invalid_request": "请求有问题",
    "model_not_found": "找不到这个模型",
}


def clamp(value, low, high):
    return max(low, min(high, value))


class Pet(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("ClaudePet", "ClaudePet")

        self.setWindowTitle("Claude + Codex 桌宠")
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)

        # ---- 设置 ----
        st = self.settings
        self.scale = float(st.value("scale", 0.7))
        self.on_top = st.value("on_top", True, type=bool)
        self.wander_on = st.value("wander", True, type=bool)
        self.physics_on = st.value("physics", True, type=bool)
        self.follow_on = st.value("follow", False, type=bool)
        self.bubble_on = st.value("bubble", True, type=bool)
        self.use_walk_art = st.value("walk_art", True, type=bool)
        self.ambient_on = st.value("ambient", True, type=bool)
        self.sit_reminder_on = st.value("sit_reminder", True, type=bool)
        self.focus_minutes = int(st.value("focus_minutes", 25))
        self.hud_on = st.value("hud", True, type=bool)
        self.quota_alerts_on = st.value("quota_alerts", True, type=bool)
        self.fetch_quota_on = st.value("fetch_quota", True, type=bool)
        self.approvals_on = st.value("approvals", True, type=bool)
        self.approval_wait = int(st.value("approval_wait", 120))
        self.dnd = st.value("dnd", False, type=bool)
        self.privacy = st.value("privacy", False, type=bool)
        self.sound_on = st.value("sound", False, type=bool)
        self.idle_cleanup_min = int(st.value("idle_cleanup", 30))
        self.codex_on = st.value("codex", True, type=bool)

        # ---- 动画 ----
        self.anim = Animator(ASSET_DIR)
        self.anim.set_scale(self.scale)
        self.anim.preload_all(sorted({s["gif"] for s in STATES.values()}))

        self.base_state = DEFAULT_STATE
        self.state = DEFAULT_STATE
        self._overlay_until = 0.0
        self._last_event_at = time.time()
        self._last_touch_at = time.time()
        self._claude_at = 0.0        # 最后一次真的收到 Claude Code 事件

        # 本地感知 + 专注模式，这两条不依赖任何 AI
        self.ambient = Ambient()
        self.focus_on = False
        self.focus_phase = "work"
        self.focus_end = 0.0

        # ---- 物理 ----
        self.ax = 0.0           # 脚下那个点，屏幕坐标
        self.ay = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.flying = False     # True = 被甩出去了，走重力
        self.facing = 1         # 1 朝右，-1 朝左
        self.lean = 0.0
        self.squash = 1.0
        self.bob_phase = random.random() * 6.28
        self.walk_target = None
        self.rest_until = time.time() + random.uniform(2.0, 5.0)
        self.jump_cooldown = 0.0
        self.hop_y = 0.0        # 蹦跶用的额外垂直偏移
        self.hop_v = 0.0
        self._speed_x = 0.0     # 平滑过的水平速度
        self._flip = True       # 当前素材要不要左右镜像

        # 每帧只取一次画面，省点开销
        self._pix = QPixmap()
        self._pw = 1
        self._ph = 1
        self.tray = None

        # ---- 鼠标 ----
        self._press_screen = None
        self._press_anchor = None
        self._dragging = False
        self._drag_samples = []

        # ---- 统计 ----
        self._stats_day = self._today()
        self.stats = self._load_stats()

        self.bubble = Bubble()

        # ---- 额度、会话、审批 ----
        self.hud = QuotaHud()
        self.hud.clicked.connect(self.open_panel)
        self.panel = None
        self.usage = UsageHub(fetch_quota=self.fetch_quota_on)
        self.usage.quota_changed.connect(self.on_quota)
        self.usage.local_changed.connect(self.on_local_usage)
        self.usage.turn_cost.connect(self.on_turn_cost)
        self.tracker = SessionTracker()
        self.codex = CodexMonitor()
        self.codex.enabled = self.codex_on
        self.codex.updated.connect(self.on_codex)
        self.codex.quota_changed.connect(self.on_codex_quota)
        self._codex_marks = {}
        self.chat = ChatHub()
        self.mini = None
        self._mini_request_keys = set()
        self.chat.changed.connect(self._mini_changed)
        self.card = ApprovalCard()
        self.card.decided.connect(self.on_card_decided)
        self.card.focus_requested.connect(self.focus_session)
        self.pending = OrderedDict()     # 等你点的审批请求，先来先显示
        self.widgets = OrderedDict()     # 自定义小组件
        self._quota_marks = {}           # 每个额度窗口已经提醒到第几档了
        self._last_quota = {}            # 窗口 -> (已用, 重置时间)，用来认出"刷新了"
        self._done_at = 0.0
        self._started_at = time.time()
        self._greeted_quota = False
        self._hud_raise_at = 0.0
        self._hud_hover_until = 0.0
        self._last_sweep = 0.0
        self._last_rescue = time.time()
        self._card_anchor = None
        self._tray_key = "unset"
        self._tray_tip = ""
        self._tray_base = None

        self._apply_state(DEFAULT_STATE, force=True)
        self._restore_position()
        self.apply_on_top()

        # ---- 事件服务 ----
        self._sync_status()
        self.server = EventServer(int(self.settings.value("port", 47828)))
        self.port = self.server.start()
        BRIDGE.received.connect(self.on_claude_event)
        BRIDGE.hook.connect(self.on_hook)
        BRIDGE.permission.connect(self.on_permission)
        BRIDGE.permission_gone.connect(self.on_permission_gone)
        BRIDGE.statusline.connect(self.on_statusline)
        BRIDGE.widget.connect(self.on_widget)
        BRIDGE.debug.connect(self._debug)

        # ---- 循环 ----
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(TICK_MS)
        self._last_tick = time.time()

        self._build_tray()
        self.mini_hotkey = MiniHotkey(self.open_mini)
        self._mini_changed()
        self.usage.start()
        self.codex.start()
        QTimer.singleShot(350, self.on_codex_quota)
        QTimer.singleShot(300, self.on_quota)        # 先用上次存下的额度把血条画出来

        QTimer.singleShot(600, lambda: self.trigger("session_start", {}, from_hook=False))

    # ==================================================================
    # 状态
    # ==================================================================

    def _apply_state(self, name, force=False):
        if name not in STATES:
            return
        if name == self.state and not force:
            return
        self.state = name
        self.anim.play(STATES[name]["gif"])
        self._refresh_frame()
        self._resize_to_frame()
        STATUS["state"] = name
        STATUS["base"] = self.base_state
        if self.tray is not None:
            self._update_tray(force_tip=True)

    def set_base_state(self, name, bubble=None, bubble_ms=4200):
        if name not in STATES:
            return
        self.base_state = name
        if time.time() >= self._overlay_until:
            self._apply_state(name)
        text = bubble if bubble is not None else STATES[name].get("bubble")
        self.say(text, bubble_ms)

    def play_once(self, name, bubble=None, hold_ms=None, sub=None):
        if name not in STATES:
            return
        hold = hold_ms or STATES[name].get("hold_ms") or 2500
        self._overlay_until = time.time() + hold / 1000.0
        self._apply_state(name)
        text = bubble if bubble is not None else STATES[name].get("bubble")
        self.say(text, min(hold, 6000), sub=sub)

    def say(self, text, duration_ms=4200, sub=None):
        if not self.bubble_on or not text or self.card.isVisible():
            self.bubble.dismiss()
            return
        self.bubble.say(text, duration_ms, sub=sub)
        self._place_bubble()

    def _derived_base(self):
        """按会话和专注模式，现在该摆什么常驻表情。都没有就返回 None。"""
        if self.chat.requests:
            return 'waiting'
        shown = self.tracker.display()
        if shown:
            return shown
        if any(self.chat.busy.values()):
            return 'thinking'
        if self.focus_on:
            return "working" if self.focus_phase == "work" else "resting"
        return None

    # ==================================================================
    # 事件：命令行、本地感知、老版本 hook
    # ==================================================================

    def on_claude_event(self, name, data):
        data = data or {}
        if data.get("hook_event_name") and data.get("session_id"):
            self.on_hook(data)       # 老版本的 hook 也带着完整数据，一样按会话来
            return
        self.trigger(name, data)

    def trigger(self, name, data, from_hook=True):
        data = data or {}
        if name == 'mini':
            self.open_mini(force_show=True, tab=1 if data.get('text') in ('tasks','任务') else 0)
            return
        if name == "say":
            text = (data.get("text") or "").strip()
            if text:
                self.say(clip(text, 120), 5000)
            return
        if name == "panel":
            tab = PANEL_TABS.get((data.get("text") or "").strip())
            self.open_panel(force_show=True, tab=tab)
            return
        rule = EVENT_MAP.get(name)
        if rule is None:
            return

        self._last_event_at = time.time()
        if name not in LOCAL_EVENTS:
            self._claude_at = self._last_event_at

        # 命令行敲 focus / rest 的时候，顺手把计时器也开起来
        if name == "focus" and not self.focus_on:
            self.start_focus(self.focus_minutes)
            return
        if name == "rest" and self.focus_on:
            self.stop_focus()

        key = rule.get("count_as")
        if key and (from_hook or name != "session_start"):
            self.stats[key] = self.stats.get(key, 0) + 1
            self._save_stats()

        bubble = rule.get("bubble")

        # 命令行可以直接指定要说的话：pet.bat done "编译过了"
        custom = (data.get("text") or "").strip()
        if custom:
            bubble = clip(custom, 60)

        if name == "session_start" and not from_hook:
            bubble = "桌宠上线啦"

        if rule.get("kind") == "once":
            reset = rule.get("reset_base")
            if reset:
                self.base_state = self._derived_base() or reset
            self.play_once(rule["state"], bubble, hold_ms=rule.get("hold_ms"))
        else:
            self._overlay_until = 0.0
            self.set_base_state(rule["state"], bubble)

        # 等确认的时候原地蹦两下
        if rule["state"] == "waiting":
            self.jump_cooldown = 0.0

    # ==================================================================
    # 事件：Claude Code（按会话）
    # ==================================================================

    def on_hook(self, p):
        now = time.time()
        upd = self.tracker.apply(p, now)
        self._clear_stale_cards(p)
        self._enrich(p.get("session_id"))
        # 后台 hook 可能比审批请求晚到一步；只要这个会话还有卡片没点，就一直算"等你"
        s0 = self.tracker.sessions.get(p.get("session_id"))
        if s0 is not None and any(r["sid"] == s0.id for r in self.pending.values()):
            s0.state = "waiting"
            s0.kind = "waiting"
            if upd and not upd.get("quiet") and EVENT_MAP.get(upd["event"], {}).get("state") != "waiting":
                upd = dict(upd, quiet=True)
        if not upd:
            self._refresh_views()
            return
        self._claude_at = now
        self._last_event_at = now
        event = upd["event"]
        s = upd.get("session")
        sid = p.get("session_id") or ""
        path = p.get("transcript_path") or ""

        # 记账：一轮开始的时候记个底，结束的时候算这一轮花了多少
        if event == "prompt" and path:
            self.usage.mark_turn(sid, path)
        elif event == "done":
            if path:
                self.usage.finish_turn(sid, path, upd.get("seconds"))
            self._done_at = now
            self.stats["work_s"] = int(self.stats.get("work_s", 0) + (upd.get("seconds") or 0))
            self._save_stats()
            self.usage.refresh_quota()
        elif event == "api_error" and upd.get("error") == "rate_limit":
            self.usage.refresh_quota(force=True)
        elif event == "session_end":
            self._drop_cards(sid)

        if not upd.get("quiet"):
            self._react(event, upd, s)
        self._refresh_views()

    def on_codex(self, data):
        """Codex snapshots use a separate id namespace; never enter Claude accounting."""
        if not self.codex_on:
            return
        sid = data['id']
        if data.get('removed'):
            self.tracker.sessions.pop(sid,None)
            self._refresh_views()
            return
        for record in self.chat.records.values():
            if record.get('provider')!='codex' or 'codex:'+record.get('session_id','')!=sid:
                continue
            if record.get('last_status') in ('completed','interrupted','error') and \
                    data.get('last_event_at',0)<=record.get('finished_at',0):
                status=record['last_status']
                data=dict(data,state='error' if status=='error' else 'idle',kind='idle',
                          turn_started_at=None,activity='Mini 已停止' if status=='interrupted' else 'Mini 已结束')
                if status!='completed':
                    data['event']=None
            break
        now = time.time()
        if data.get('bootstrap') and data['state'] == 'idle' and self.idle_cleanup_min and \
                now - data['last_event_at'] > self.idle_cleanup_min * 60:
            return
        s = self.tracker.sessions.get(sid)
        if s is None:
            s = self.tracker.sessions[sid] = Session(sid)
        s.provider = 'codex'
        for key in ('cwd', 'title', 'source', 'state', 'kind', 'activity', 'last_event_at',
                    'turn_started_at', 'turns', 'last_message', 'unseen', 'model',
                    'context_pct', 'tokens', 'turn_tokens', 'todo_done', 'todo_total', 'transcript'):
            if key in data:
                setattr(s, key, data[key])
        s.project = project_of(s.cwd)
        s.changed = set(data.get('changed') or [])
        s.waiting_for = s.activity if s.state == 'waiting' else ''
        ev = data.get('event')
        if ev:
            self._claude_at = self._last_event_at = now
            if ev == 'activity':
                ev = {'running':'tool_bash', 'reading':'tool_read', 'web':'tool_web',
                      'agent':'tool_agent'}.get(s.kind, 'tool_other')
            if ev != 'interrupt':
                upd = {'event': ev, 'session': s, 'bubble': s.activity, 'generic': 'Codex 在干活',
                       'message': s.last_message, 'error': s.activity}
                self._react(ev, upd, s)
                if ev == 'done' and self.bubble.isVisible() and s.turn_tokens:
                    if not any(w is not s for w in self.tracker.waiting()):
                        self.bubble.set_sub(f"Codex 本轮 {fmt_tokens(s.turn_tokens)} tokens（本机记录）")
        self._refresh_views()

    def on_codex_quota(self):
        five, week = self.codex.window('five_hour'), self.codex.window('seven_day')
        self.hud.set_codex(five=five['used'] if five else None,
                           five_reset=five.get('resets_at') if five else None,
                           week=week['used'] if week else None,
                           stale=self.codex.quota_age() > 1200 or bool(five and five.get('expired')),
                           tip=self.codex.tooltip(), enabled=self.codex_on)
        STATUS['codex_usage'] = {'brief': self.codex.brief(), 'quota': self.codex.quota,
                                 'enabled': self.codex_on, 'error': self.codex.error}
        if self.quota_alerts_on and not self.dnd and self.codex.quota_age() < 1200:
            for w in (five, week):
                if not w or w.get('expired'):
                    continue
                level = next((n for n in (100, 95, 80) if w['used'] >= n), 0)
                key = (w['key'], w.get('resets_at'))
                previous = self._codex_marks.get(key)
                self._codex_marks[key] = level
                if previous is not None and level > previous and level:
                    self.say(f"Codex {w['label']}只剩 {max(0, 100-w['used']):.0f}% 了", 5500)
        self._update_tray(force_tip=True)

    def _enrich(self, sid):
        """给会话补上好认的名字（对话标题）和它所在的窗口。"""
        s = self.tracker.sessions.get(sid) if sid else None
        if s is None:
            return
        if not s.title:
            title = self.usage.scanner.titles.get(sid)
            if title:
                s.title = clip(title, 40)
        if s.pids and not s.window_title and time.time() - getattr(s, "window_checked", 0.0) > 30:
            s.window_checked = time.time()
            _, s.window_title = winutil.session_window(s.pids)

    def _react(self, event, upd, s):
        rule = EVENT_MAP.get(event)
        if rule is None:
            return
        if event == "session_end" and time.time() - self._done_at < 8:
            return          # 刚报完这一轮的结果（比如 claude -p 跑完就退），别用"下次见"盖掉
        key = rule.get("count_as")
        if key:
            self.stats[key] = self.stats.get(key, 0) + 1
            self._save_stats()

        text = self._event_text(event, rule, upd)
        if text and s is not None:
            provider = "Codex" if s.provider == "codex" else "Claude"
            text = f"[{provider} · {clip(s.name(), 12)}] {text}" if len(self.tracker.sessions) > 1 else f"[{provider}] {text}"
        if self.dnd or self.card.isVisible():
            text = ""

        others_waiting = any(w is not s for w in self.tracker.waiting())
        if others_waiting and rule['state'] != 'waiting':
            return  # Neither provider may override another session's request for attention.
        if rule["kind"] == "once":
            reset = rule.get("reset_base")
            if reset:
                self.base_state = self._derived_base() or reset
            self.play_once(rule["state"], text, hold_ms=rule.get("hold_ms"))
        elif others_waiting and rule["state"] != "waiting":
            pass            # 别的会话在等你，先别把"等你"的样子盖掉
        else:
            self._overlay_until = 0.0
            self.set_base_state(self._derived_base() or rule["state"], text)

        if rule["state"] == "waiting":
            self.jump_cooldown = 0.0
            self._beep("waiting")
        elif event == "done":
            self._beep("done")

    def _event_text(self, event, rule, upd):
        if event == "done":
            msg = "" if self.privacy else (upd.get("message") or "")
            first = msg.replace("\r", "").strip().split("\n")[0].strip() if msg else ""
            return clip(first, 60) if first else "搞定啦"
        if event == "api_error":
            return self._api_error_text(upd.get("error"))
        if event == "session_start":
            return "开工咯"
        text = upd.get("bubble")
        if text and self.privacy:
            text = upd.get("generic") or rule.get("bubble")
        return text or rule.get("bubble")

    def _api_error_text(self, err):
        if err == "rate_limit":
            five = self.usage.window("five_hour")
            if five and five.get("resets_at"):
                return f"额度用完了…{fmt_span(five['resets_at'] - time.time())}后恢复"
            return "额度用完了，等它重置吧"
        return API_ERRORS.get(err, "API 出错了") + "，这一轮停下了"

    def on_statusline(self, data):
        """命令行版 Claude Code 的状态栏数据：上下文用了多少、官方额度百分比。"""
        sid = data.get("session_id")
        s = self.tracker.sessions.get(sid) if sid else None
        if s is not None:
            ctx = (data.get("context_window") or {}).get("used_percentage")
            if ctx is not None:
                s.context_pct = float(ctx)
            cost = (data.get("cost") or {}).get("total_cost_usd")
            if cost is not None:
                s.cost = float(cost)
            if data.get("session_name"):
                s.title = clip(data["session_name"], 40)
        rl = data.get("rate_limits")
        if isinstance(rl, dict) and rl:
            self.usage.update_from_statusline(rl)

    def on_widget(self, w):
        wid = str(w.get("id") or w.get("title") or "widget")[:40]
        if w.get("remove"):
            self.widgets.pop(wid, None)
            return
        title = str(w.get("title") or wid)[:30]
        value = str(w.get("value") if w.get("value") is not None else "")[:80]
        try:
            ttl = float(w.get("ttl") or 0)
        except (TypeError, ValueError):
            ttl = 0.0
        self.widgets[wid] = {"id": wid, "title": title, "value": value, "at": time.time(), "ttl": ttl}
        if not self.dnd:
            if w.get("alert"):
                self.play_once("alarmed", f"{title}：{value}")
            elif w.get("say"):
                self.say(f"{title}：{value}", 4000)

    # ==================================================================
    # 在桌宠上审批
    # ==================================================================

    def on_permission(self, rid, p):
        now = time.time()
        upd = self.tracker.apply(dict(p, hook_event_name="PermissionRequest"), now)
        s = upd.get("session") if upd else None
        self._enrich(p.get("session_id"))
        self._claude_at = self._last_event_at = now
        if self.dnd or not self.approvals_on:
            PENDING.resolve(rid, None)
            self._refresh_views()
            return
        self.pending[rid] = {"rid": rid, "payload": p, "sid": p.get("session_id") or "",
                             "deadline": now + self.approval_wait, "session": s}
        self.stats["waits"] = self.stats.get("waits", 0) + 1
        self._save_stats()
        self._show_next_card()
        self._overlay_until = 0.0
        self.base_state = "waiting"
        self._apply_state("waiting")
        self.jump_cooldown = 0.0
        self._beep("waiting")
        self._refresh_views()

    def _show_next_card(self):
        if self.card.isVisible() and self.card.rid in self.pending:
            self.card.set_queue(len(self.pending) - 1)
            return
        if not self.pending:
            self.card.clear()
            return
        req = next(iter(self.pending.values()))
        s = req.get("session")
        self.bubble.dismiss()
        self.card.present(req, len(self.pending) - 1, privacy=self.privacy, name=s.name() if s else "")
        self._card_anchor = None
        self._place_card()

    def on_card_decided(self, rid, decision):
        req = self.pending.pop(rid, None)
        if decision is None:
            PENDING.resolve(rid, None)
        else:
            PENDING.resolve(rid, {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                                         "decision": decision}})
            self.stats["approvals"] = self.stats.get("approvals", 0) + 1
            self._save_stats()
            s = self.tracker.sessions.get(req["sid"]) if req else None
            allowed = decision.get("behavior") == "allow"
            if s is not None and s.state == "waiting":
                s.state = "working" if allowed else "thinking"
                s.kind = "thinking"
                s.waiting_for = ""
        self._show_next_card()
        if decision is not None and not self.card.isVisible():
            allowed = decision.get("behavior") == "allow"
            self.base_state = self._derived_base() or "idle"
            self.play_once("patted" if allowed else "wave", "好嘞，放行" if allowed else "好，不让它做", hold_ms=1800)
        self._refresh_views()

    def on_permission_gone(self, rid, why):
        if self.pending.pop(rid, None) is None:
            return
        # 你在终端或桌面版里先点了，或者等太久交回去了
        if self.card.rid == rid:
            self.card.clear()
        self._show_next_card()
        s_waiting = self.tracker.waiting()
        if why == "timeout" and not self.dnd and not self.card.isVisible():
            self.say("等太久了，交回窗口里让你点", 3500)
        elif not s_waiting:
            self.base_state = self._derived_base() or "idle"
        self._refresh_views()

    def _clear_stale_cards(self, p):
        """会话已经往下走了（工具跑完了、这一轮结束了），说明你在别处点过了，卡片就收起来。"""
        if not self.pending:
            return
        ev = p.get("hook_event_name")
        sid = p.get("session_id")
        changed = False
        for rid, req in list(self.pending.items()):
            if req["sid"] != sid:
                continue
            rp = req["payload"]
            same_tool = rp.get("tool_name") == p.get("tool_name") and rp.get("tool_input") == p.get("tool_input")
            if ev in ("Stop", "StopFailure", "UserPromptSubmit", "SessionEnd") or \
                    (ev in ("PostToolUse", "PostToolUseFailure") and same_tool):
                PENDING.resolve(rid, None)
                self.pending.pop(rid, None)
                if self.card.rid == rid:
                    self.card.clear()
                changed = True
        if changed:
            self._show_next_card()

    def _drop_cards(self, sid=None):
        for rid, req in list(self.pending.items()):
            if sid is None or req["sid"] == sid:
                PENDING.resolve(rid, None)
                self.pending.pop(rid, None)
                if self.card.rid == rid:
                    self.card.clear()
        self._show_next_card()

    def _place_card(self):
        if not self.card.isVisible():
            return
        rect = self.screen_rect()
        cw, ch = self.card.width(), self.card.height()
        head = self.ay - self.sprite_h() - 4 - (34 if self.hud.isVisible() else 0)
        x = self.ax - cw / 2
        y = head - ch
        if y < rect.top() + 4:                  # 头顶放不下就放旁边
            y = self.ay - self.sprite_h() / 2 - ch / 2
            x = self.ax + self.sprite_w() / 2 + 6
            if x + cw > rect.right():
                x = self.ax - self.sprite_w() / 2 - 6 - cw
        x = clamp(x, rect.left() + 4, rect.right() - cw - 4)
        y = clamp(y, rect.top() + 4, rect.bottom() - ch - 4)
        self.card.move(int(x), int(y))
        self._card_anchor = (self.ax, self.ay, ch)

    def focus_session(self, sid):
        s = self.tracker.sessions.get(sid)
        if s is None:
            return False
        hwnd, title = winutil.session_window(s.pids)
        if not hwnd and s.provider == 'codex':
            hwnd, title = winutil.provider_window('codex')
        if hwnd and winutil.focus_window(hwnd):
            s.window_title = title
            s.unseen = False
            self._refresh_views()
            return True
        self.say("没找到它的窗口…", 2500)
        return False

    # ==================================================================
    # 额度和用量
    # ==================================================================

    def open_panel(self, force_show=False, tab=None):
        from .panel import UsagePanel
        if self.panel is None:
            self.panel = UsagePanel(self.usage, self)
        if self.panel.isVisible() and not force_show and tab is None:
            self.panel.hide()
            return
        if tab is not None:
            self.panel.show_tab(tab)
        self.panel.show_near(self.frameGeometry())
        self.usage.refresh_quota()

    def open_mini(self, force_show=False, tab=None):
        from .mini import MiniWindow
        if self.mini is None:
            self.mini = MiniWindow(self)
        if self.mini.isVisible() and not force_show and tab is None:
            self.mini.hide()
            return
        if tab is not None:
            self.mini.show_tab(tab)
        self.mini.show_near(self.frameGeometry())
        STATUS.setdefault('mini',{})['visible']=True

    def _mini_changed(self):
        for provider in ('claude','codex'):
            record=self.chat.record(provider)
            if not self.chat.busy[provider] and record.get('last_status') in ('completed','interrupted','error'):
                sid=('codex:' if provider=='codex' else '')+record.get('session_id','')
                s=self.tracker.sessions.get(sid)
                if s is not None and s.last_event_at<=record.get('finished_at',0):
                    s.state='error' if record['last_status']=='error' else 'idle'
                    s.kind='idle'
                    s.turn_started_at=None
                    s.waiting_for=''
                    s.activity=self.chat.activity[provider]
        STATUS['mini'] = {'busy':dict(self.chat.busy),'activity':dict(self.chat.activity),
                          'waiting':len(self.chat.requests),
                          'visible':bool(self.mini is not None and self.mini.isVisible()),
                          'hotkey':getattr(getattr(self,'mini_hotkey',None),'label','点桌宠打开')}
        keys = set(self.chat.requests)
        new_requests = keys - self._mini_request_keys
        self._mini_request_keys = keys
        if new_requests and not self.dnd:
            self.open_mini(force_show=True,tab=0)
            request = next(iter(self.chat.requests.values()))
            self.mini.select_provider(request['provider'])
            self.base_state = 'waiting'
            self._overlay_until = 0.
            self._apply_state('waiting')

    def on_quota(self):
        five = self.usage.window("five_hour")
        week = self.usage.window("seven_day")
        stale = bool(self.usage.quota_error) or self.usage.quota_age() > 20 * 60
        self.hud.set_quota(
            five=five["used"] if five else None,
            five_reset=five.get("resets_at") if five else None,
            week=week["used"] if week else None,
            stale=stale,
            note="额度拿不到" if self.usage.quota_error else "额度 ?",
            tip=self.usage.tooltip(),
        )
        self._check_quota_alerts(five, week)
        self._update_tray(force_tip=True)
        STATUS["usage"] = self.usage.cli_summary()

    def on_local_usage(self):
        STATUS["usage"] = self.usage.cli_summary()
        self.hud.set_claude_tip(self.usage.tooltip())

    def on_turn_cost(self, sid, info):
        s = self.tracker.sessions.get(sid)
        cost = float(info.get("cost") or 0.0)
        tokens = int(info.get("tokens") or 0)
        if s is not None:
            s.turn_cost = cost
        if tokens <= 0 or self.dnd or not self.bubble_on:
            return
        parts = [f"本轮 {fmt_tokens(tokens)} tokens", f"≈{fmt_money(cost)}"]
        per = self.usage.cost_per_percent()
        if per:
            parts.append(f"约占 5h 额度 {cost / per:.1f}%")
        sub = " · ".join(parts)
        now = time.time()
        if self.bubble.isVisible() and now - self._done_at < 9:
            self.bubble.set_sub(sub, extend_ms=2500)
            self._place_bubble()
        elif now - self._done_at < 20 and not self.card.isVisible():
            name = f"[{clip(s.name(), 12)}] " if (s is not None and len(self.tracker.sessions) > 1) else ""
            self.say(name + "这一轮算完账了", 4500, sub=sub)

    def _check_quota_alerts(self, five, week):
        quiet = time.time() - self._started_at < 20      # 刚启动先记个底，别一上来就念叨
        for key, win, steps in (("five_hour", five, (50, 80, 95, 100)), ("seven_day", week, (75, 90, 100))):
            if win is None:
                continue
            used = win["used"]
            reset = win.get("resets_at") or 0
            prev = self._last_quota.get(key)
            self._last_quota[key] = (used, reset)
            level = sum(1 for t in steps if used >= t)
            if prev is not None:
                pu, pr = prev
                if (reset and pr and reset - pr > 1800) or pu - used >= 20:
                    self._quota_marks[key] = level          # 窗口刷新了
                    if key == "five_hour" and pu >= 30 and not quiet and self.quota_alerts_on and not self.dnd:
                        self.play_once("cheer", "5 小时额度刷新啦，满血复活！")
                    continue
            if key not in self._quota_marks or quiet:
                self._quota_marks[key] = level
                continue
            if level > self._quota_marks[key]:
                self._quota_marks[key] = level
                if self.quota_alerts_on and not self.dnd:
                    self._announce_quota(key, win)
        # 刚启动的时候报一次还剩多少
        if (not self._greeted_quota and five is not None and not self.usage.quota_error
                and self.usage.quota_age() < 120):
            self._greeted_quota = True
            if self.quota_alerts_on and not self.dnd and time.time() - self._started_at < 60:
                msg = f"5 小时额度还剩 {max(0, 100 - five['used']):.0f}%"
                if week is not None:
                    msg += f"，本周还剩 {max(0, 100 - week['used']):.0f}%"
                QTimer.singleShot(2600, lambda m=msg: self.say(m, 4500))

    def _announce_quota(self, key, win):
        used = win["used"]
        remain = max(0.0, 100.0 - used)
        reset = win.get("resets_at")
        now = time.time()
        when = fmt_clock(reset) if reset else "下次"
        if key == "five_hour":
            if used >= 100:
                text, state = f"5 小时额度用完了，{fmt_span(reset - now) if reset else '等'}后恢复", "exhausted"
            elif used >= 95:
                text, state = f"5 小时额度快见底了，只剩 {remain:.0f}%！", "alarmed"
            elif used >= 80:
                text, state = f"5 小时额度只剩 {remain:.0f}% 了，{when} 重置", "alarmed"
                eta = self.usage.predict("five_hour")
                if eta and reset and eta < reset - now:
                    text += f"，照这速度 {fmt_span(eta)} 就用完"
            else:
                text, state = f"5 小时额度用掉一半了，{when} 重置", "wave"
        else:
            if used >= 100:
                text, state = f"本周额度用完了，{when} 恢复", "exhausted"
            elif used >= 90:
                text, state = f"本周额度只剩 {remain:.0f}% 了，省着点用", "alarmed"
            else:
                text, state = f"本周额度已经用了 {used:.0f}%，{when} 重置", "wave"
        self.play_once(state, text, hold_ms=6000)

    # ==================================================================
    # 每帧
    # ==================================================================

    def _refresh_frame(self):
        pix = self.anim.frame()
        if not pix.isNull():
            self._pix = pix
            self._pw = pix.width()
            self._ph = pix.height()
        else:
            size = self.anim.frame_size()
            self._pw = max(1, size.width())
            self._ph = max(1, size.height())

    def _update_visual(self):
        """决定这一帧到底播哪个 gif。

        走起来就换成左右移动的素材，那两张本身分左右，所以不做镜像。
        一次性动作（庆祝、报错、被摸头这些）优先，不会被走路盖掉。
        """
        state_gif = STATES[self.state]["gif"]
        gif = state_gif
        self._flip = not STATES[self.state].get("directional", False)

        moving = abs(self._speed_x) > MOVE_ART_SPEED
        if (self.use_walk_art and moving and not self._dragging
                and not self._overlay_until):
            walk = WALK_STATES[1 if self._speed_x > 0 else -1]
            gif = STATES[walk]["gif"]
            self._flip = False

        if gif != self.anim.current_file:
            self.anim.play(gif)
            self._refresh_frame()
            self._resize_to_frame()

    def tick(self):
        now = time.time()
        dt = clamp(now - self._last_tick, 0.001, 0.05)
        self._last_tick = now
        prev_ax = self.ax
        self._refresh_frame()

        # 一次性动作播完了就回常驻
        if self._overlay_until and now >= self._overlay_until:
            self._overlay_until = 0.0
            self._apply_state(self.base_state)

        if now - self._last_sweep >= 1.0:
            self._last_sweep = now
            self._sweep(now)

        self._poll_focus(now)
        if self.ambient_on:
            self._poll_ambient(now)

        if self._dragging:
            self._update_drag()
        elif self.flying:
            self._step_physics(dt)
        else:
            self._step_walk(dt, now)

        # 真实的水平速度，用来决定放不放走路动作
        raw = (self.ax - prev_ax) / dt
        self._speed_x += (raw - self._speed_x) * min(1.0, dt * 12.0)

        self._settle_visuals(dt)
        self._update_visual()
        self._sync_geometry()
        self.update()
        self._place_hud(now)
        if self.bubble.isVisible():
            self._place_bubble()
        if self.card.isVisible() and self._card_anchor is not None:
            ax, ay, ch = self._card_anchor
            if abs(self.ax - ax) > 30 or abs(self.ay - ay) > 30 or ch != self.card.height():
                self._place_card()

    def _sweep(self, now):
        """一秒一次：整理会话、决定常驻表情、打盹、跨天清零、救回跑丢的桌宠。"""
        if self._today() != self._stats_day:
            self._stats_day = self._today()
            self.stats = {}
            self._save_stats()
        self.tracker.cleanup(self.idle_cleanup_min * 60, now)
        if self.chat.requests:
            # A live Mini approval is more authoritative than delayed JSONL events.
            for (provider,_),req in self.chat.requests.items():
                record = self.chat.record(provider)
                sid = ('codex:' if provider == 'codex' else '') + record.get('session_id','')
                s = self.tracker.sessions.get(sid)
                if s is not None:
                    s.state, s.kind, s.waiting_for = 'waiting','waiting','Mini 在等你处理'
        for wid, w in list(self.widgets.items()):
            if w["ttl"] and now - w["at"] > w["ttl"]:
                del self.widgets[wid]

        derived = self._derived_base()
        if derived:
            if derived != self.base_state and not self._dragging:
                self.base_state = derived
                if not self._overlay_until:
                    self._apply_state(derived)
        else:
            # 长时间没事件，回到待机；再久一点就打盹
            quiet = now - self._last_event_at
            if self.base_state not in ("idle", "sleeping"):
                if quiet > IDLE_RESET_S or (self.base_state in ("waiting",) and not self.pending):
                    self.base_state = "idle"
                    if not self._overlay_until:
                        self._apply_state("idle")
            elif quiet > SLEEP_AFTER_S and now - self._last_touch_at > SLEEP_AFTER_S:
                if self.base_state != "sleeping":
                    self.base_state = "sleeping"
                    if not self._overlay_until:
                        self._apply_state("sleeping")

        if now - self._last_rescue > 2.0:
            self._last_rescue = now
            self._rescue_if_offscreen()
        STATUS["geom"] = [self.x(), self.y(), self.width(), self.height()]
        self._update_tray()
        STATUS["sessions"] = self.tracker.snapshot(self.privacy)

    def _refresh_views(self):
        self._update_tray(force_tip=True)
        STATUS["sessions"] = self.tracker.snapshot(self.privacy)

    def _rescue_if_offscreen(self):
        if self._dragging or self.flying:
            return
        pt = QPoint(int(self.ax), int(self.ay - self.sprite_h() / 2))
        if QGuiApplication.screenAt(pt) is None:
            self.reset_position()
            self.say("刚才跑出屏幕了，我回来啦", 3000)

    def _beep(self, kind):
        if not self.sound_on or self.dnd or not sys.platform.startswith("win"):
            return
        try:
            import winsound
            winsound.MessageBeep(0x40 if kind == "waiting" else 0x0)
        except Exception:
            pass

    # ==================================================================
    # 本地感知：不靠 Claude Code，只看你在不在、电脑忙不忙
    # ==================================================================

    def _claude_active(self, now):
        """Claude Code 刚才有动静吗？有的话本地感知先让路。"""
        return (now - self._claude_at) < CLAUDE_HOLD_S or bool(self.tracker.busy())

    def _poll_ambient(self, now):
        for signal in self.ambient.poll(now):
            event = AMBIENT_MAP.get(signal)
            if event is None:
                continue

            # 久坐和深夜是提醒，什么时候都可以说
            if event in ("stretch", "late"):
                if event == "stretch" and not self.sit_reminder_on:
                    continue
                if self._dragging or self.dnd:
                    continue
                self.trigger(event, {}, from_hook=False)
                continue

            # 剩下的是状态类，专注模式和 Claude Code 优先
            if self.focus_on or self._claude_active(now) or self.pending:
                continue
            self.trigger(event, {}, from_hook=False)

    # ==================================================================
    # 专注模式：一个不需要任何 AI 的番茄钟
    # ==================================================================

    def start_focus(self, minutes):
        self.focus_minutes = int(minutes)
        self.settings.setValue("focus_minutes", self.focus_minutes)
        self.focus_on = True
        self.focus_phase = "work"
        self.focus_end = time.time() + self.focus_minutes * 60
        self._overlay_until = 0.0
        self.set_base_state("working", f"专注 {self.focus_minutes} 分钟，开始")

    def stop_focus(self):
        if not self.focus_on:
            return
        self.focus_on = False
        self.focus_end = 0.0
        self._overlay_until = 0.0
        self.set_base_state("idle", "专注结束")

    def _poll_focus(self, now):
        if not self.focus_on or now < self.focus_end:
            return
        if self.focus_phase == "work":
            self.focus_phase = "rest"
            self.focus_end = now + FOCUS_REST_MIN * 60
            self.base_state = "resting"
            self.play_once("done", f"{self.focus_minutes} 分钟到了，歇 {FOCUS_REST_MIN} 分钟")
        else:
            self.focus_phase = "work"
            self.focus_end = now + self.focus_minutes * 60
            self._overlay_until = 0.0
            self.set_base_state("working", "继续干活")

    def focus_left_text(self):
        if not self.focus_on:
            return ""
        left = max(0, int(self.focus_end - time.time()))
        word = "专注" if self.focus_phase == "work" else "休息"
        return f"（{word}还剩 {left // 60} 分 {left % 60} 秒）"

    # ---------------- 物理 ----------------

    def _step_physics(self, dt):
        rect = self.screen_rect()
        self.vy += GRAVITY * dt
        self.vx *= AIR_DRAG ** dt

        self.ax += self.vx * dt
        self.ay += self.vy * dt

        half = self.sprite_w() / 2
        left = rect.left() + half
        right = rect.right() - half
        floor = rect.bottom() - 2
        ceil = rect.top() + self.sprite_h()

        if self.ax < left:
            self.ax = left
            self.vx = abs(self.vx) * WALL_BOUNCE
        elif self.ax > right:
            self.ax = right
            self.vx = -abs(self.vx) * WALL_BOUNCE

        if self.ay < ceil:
            self.ay = ceil
            self.vy = abs(self.vy) * WALL_BOUNCE

        if self.ay >= floor:
            self.ay = floor
            if self.vy > 240:
                self.squash = 0.8           # 落地压扁一下
            self.vy = -abs(self.vy) * BOUNCE
            self.vx *= 0.72
            if abs(self.vy) < SETTLE_SPEED and abs(self.vx) < SETTLE_SPEED:
                self._land()

        if abs(self.vx) > 12:
            self.facing = 1 if self.vx > 0 else -1

    def _land(self):
        self.flying = False
        self.vx = 0.0
        self.vy = 0.0
        self.walk_target = None
        self.rest_until = time.time() + random.uniform(1.2, 2.4)
        self._save_position()
        if not self._overlay_until:
            self.play_once("landed")

    # ---------------- 走动 ----------------

    def _step_walk(self, dt, now):
        rect = self.screen_rect()
        half = self.sprite_w() / 2
        holding = self.card.isVisible()          # 有卡片等你点的时候站着别动

        if self.follow_on and not holding:
            cursor = QCursor.pos()
            tx = cursor.x()
            ty = cursor.y() + self.sprite_h() * 0.55
            dx = tx - self.ax
            dy = ty - self.ay
            dist = math.hypot(dx, dy)
            if dist > FOLLOW_GAP:
                step = min(FOLLOW_SPEED * dt, dist - FOLLOW_GAP)
                self.ax += dx / dist * step
                self.ay += dy / dist * step
                if abs(dx) > 8:
                    self.facing = 1 if dx > 0 else -1
        elif self.wander_on and self.base_state != "sleeping" and not holding:
            if self.walk_target is None:
                if now >= self.rest_until:
                    self._pick_target()
            else:
                dx = self.walk_target.x() - self.ax
                dy = self.walk_target.y() - self.ay
                dist = math.hypot(dx, dy)
                if dist < 4:
                    self.walk_target = None
                    self.rest_until = now + random.uniform(3.0, 9.0)
                else:
                    step = min(WALK_SPEED * dt, dist)
                    self.ax += dx / dist * step
                    self.ay += dy / dist * step
                    if abs(dx) > 6:
                        self.facing = 1 if dx > 0 else -1
        elif holding:
            self.walk_target = None

        # 等确认的时候蹦两下叫你
        if self.base_state == "waiting":
            self.jump_cooldown -= dt
            if self.jump_cooldown <= 0:
                self.jump_cooldown = 1.1
                self._hop()

        self.ax = clamp(self.ax, rect.left() + half, rect.right() - half)
        self.ay = clamp(self.ay, rect.top() + self.sprite_h(), rect.bottom() - 2)

    def _hop(self):
        if self.hop_y == 0.0:
            self.hop_v = -300.0
            self.squash = 1.14

    def _pick_target(self):
        rect = self.screen_rect()
        half = self.sprite_w() / 2
        span_x = rect.width() * 0.4
        span_y = rect.height() * 0.22
        tx = clamp(self.ax + random.uniform(-span_x, span_x),
                   rect.left() + half, rect.right() - half)
        ty = clamp(self.ay + random.uniform(-span_y, span_y),
                   rect.top() + self.sprite_h(), rect.bottom() - 2)
        self.walk_target = QPoint(int(tx), int(ty))

    # ---------------- 姿态 ----------------

    def _settle_visuals(self, dt):
        moving = abs(self.vx) if self.flying else 0.0
        if not self.flying and self.walk_target is not None:
            moving = WALK_SPEED * (1 if self.facing > 0 else -1)
        elif self.flying:
            moving = self.vx

        target_lean = clamp(moving * 0.03, -10.0, 10.0)
        self.lean += (target_lean - self.lean) * min(1.0, dt * 7.0)

        self.squash += (1.0 - self.squash) * min(1.0, dt * 8.0)
        self.bob_phase += dt * (5.2 if self.walk_target is not None else 2.4)

        # 蹦一下
        if self.hop_v != 0.0 or self.hop_y < 0.0:
            self.hop_v += GRAVITY * dt
            self.hop_y += self.hop_v * dt
            if self.hop_y >= 0.0:
                self.hop_y = 0.0
                self.hop_v = 0.0
                self.squash = 0.88

    def bob_offset(self):
        if self.flying or self._dragging:
            return self.hop_y
        amp = 5.0 if self.walk_target is not None or self.follow_on else 3.0
        return math.sin(self.bob_phase) * amp + self.hop_y

    # ==================================================================
    # 几何
    # ==================================================================

    def sprite_w(self):
        return self._pw

    def sprite_h(self):
        return self._ph

    def _resize_to_frame(self):
        w = int(self._pw * 1.45)
        h = int(self._ph * 1.4)
        if self.size() != QSize(w, h):
            self.resize(w, h)

    def anchor_in_window(self):
        """脚下那个点在窗口里的位置。"""
        return self.width() / 2, self.height() - self.sprite_h() * 0.18

    def _sync_geometry(self):
        self._resize_to_frame()
        cx, by = self.anchor_in_window()
        self.move(int(self.ax - cx), int(self.ay + self.bob_offset() - by))

    def screen_rect(self):
        pt = QPoint(int(self.ax), int(self.ay))
        screen = QGuiApplication.screenAt(pt) or QGuiApplication.primaryScreen()
        return screen.availableGeometry()

    def _head_top(self):
        return self.y() + (self.height() - self.sprite_h() * 1.18)

    def _place_bubble(self):
        top = self._head_top()
        if self.hud.isVisible():
            top = self.hud.y() - 2
        self.bubble.anchor_to(self.x() + self.width() / 2, top)

    def _place_hud(self, now):
        self._sync_hud_hover(now)
        if not self.hud.isVisible():
            return
        self.hud.anchor_to(self.x() + self.width() / 2, self._head_top() + 6)
        # 血条要压在她上面，不然点不到；倒计时一秒刷一下就够了
        if now - self._hud_raise_at > 1.0:
            self._hud_raise_at = now
            self.hud.raise_()
            self.hud.update()

    def _sync_hud_hover(self,now,position=None):
        if not self.hud_on or not self.isVisible():
            self.hud.hide()
            self._hud_hover_until=0.
            return
        self.hud.anchor_to(self.x()+self.width()/2,self._head_top()+6)
        position=position if position is not None else QCursor.pos()
        on_pet=self.frameGeometry().contains(position)
        on_hud=False
        if self.hud.isVisible():
            # Keep the short gap between the character and clickable HUD usable.
            area=self.frameGeometry().united(self.hud.frameGeometry()).adjusted(-4,-4,4,4)
            on_hud=area.contains(position)
        if on_pet or on_hud:
            self._hud_hover_until=now+.32
            if not self.hud.isVisible():
                self.hud.show()
        elif now>=self._hud_hover_until:
            self.hud.hide()

    def _restore_position(self):
        saved = self.settings.value("anchor")
        rect = QGuiApplication.primaryScreen().availableGeometry()
        if saved is not None:
            try:
                self.ax = float(saved.x())
                self.ay = float(saved.y())
            except Exception:
                saved = None
        if saved is None:
            self.ax = rect.right() - 180
            self.ay = rect.bottom() - 40
        self._sync_geometry()

    def _save_position(self):
        self.settings.setValue("anchor", QPoint(int(self.ax), int(self.ay)))

    # ==================================================================
    # 画
    # ==================================================================

    def paintEvent(self, _event):
        pix = self._pix
        if pix.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.setRenderHint(QPainter.Antialiasing, True)

        cx, by = self.anchor_in_window()
        p.translate(cx, by)
        p.rotate(self.lean)
        # 压扁的时候横着胖一点，但别夸张
        sy = self.squash
        mirror = self.facing if self._flip else 1
        sx = mirror * (1.0 + (1.0 - sy) * 0.7)
        p.scale(sx, sy)
        p.drawPixmap(int(-pix.width() / 2), int(-pix.height()), pix)

    # ==================================================================
    # 鼠标
    # ==================================================================

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self.open_panel()
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            self._press_screen = event.globalPosition().toPoint()
            self._press_anchor = (self.ax, self.ay)
            self._dragging = False
            self._drag_samples = [(time.time(), self.ax, self.ay)]
            self.walk_target = None
            self.flying = False
            self._last_touch_at = time.time()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._press_screen is None:
            return
        now_pt = event.globalPosition().toPoint()
        if not self._dragging:
            if (now_pt - self._press_screen).manhattanLength() < DRAG_THRESHOLD:
                return
            self._dragging = True
            self._overlay_until = time.time() + 999
            self._apply_state("held")
            self.say(STATES["held"]["bubble"], 1800)
        delta = now_pt - self._press_screen
        self.ax = self._press_anchor[0] + delta.x()
        self.ay = self._press_anchor[1] + delta.y()
        event.accept()

    def _update_drag(self):
        now = time.time()
        self._drag_samples.append((now, self.ax, self.ay))
        self._drag_samples = [s for s in self._drag_samples if now - s[0] < 0.12]

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        was_drag = self._dragging
        self._press_screen = None
        self._dragging = False
        self._overlay_until = 0.0
        self._last_touch_at = time.time()

        if not was_drag:
            self.play_once("patted")
            self.open_mini()
        else:
            self._throw()
            self._save_position()
        event.accept()

    def _throw(self):
        samples = self._drag_samples
        if len(samples) >= 2 and self.physics_on:
            t0, x0, y0 = samples[0]
            t1, x1, y1 = samples[-1]
            dt = max(0.016, t1 - t0)
            self.vx = clamp((x1 - x0) / dt, -2600, 2600)
            self.vy = clamp((y1 - y0) / dt, -2600, 2600)
        else:
            self.vx = self.vy = 0.0

        if self.physics_on and (abs(self.vx) > 120 or abs(self.vy) > 120):
            self.flying = True
        else:
            self.flying = False
            self.vx = self.vy = 0.0
            self._apply_state(self.base_state)
            self.rest_until = time.time() + random.uniform(1.5, 3.0)
        self._drag_samples = []

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._last_touch_at = time.time()
            self.play_once("wave")
            self._hop()
            event.accept()

    def enterEvent(self, event):
        self._last_touch_at = time.time()
        if self.base_state == "sleeping":
            self.base_state = "idle"
            self._apply_state("idle")
        super().enterEvent(event)

    def contextMenuEvent(self, event):
        self.build_menu().exec(event.globalPos())

    def wheelEvent(self, event):
        """滚轮直接调大小。"""
        step = 1 if event.angleDelta().y() > 0 else -1
        idx = min(range(len(SCALE_CHOICES)),
                  key=lambda i: abs(SCALE_CHOICES[i] - self.scale))
        idx = clamp(idx + step, 0, len(SCALE_CHOICES) - 1)
        self.set_scale(SCALE_CHOICES[idx])
        event.accept()

    # ==================================================================
    # 菜单
    # ==================================================================

    def _info(self, menu, text):
        act = QAction(text, menu)
        act.setEnabled(False)
        menu.addAction(act)

    def _check(self, menu, label, attr, key):
        act = QAction(label, menu)
        act.setCheckable(True)
        act.setChecked(bool(getattr(self, attr)))
        act.toggled.connect(lambda checked, a=attr, k=key: self._toggle(a, k, checked))
        menu.addAction(act)
        return act

    def _radio(self, menu, choices, current, slot):
        group = QActionGroup(menu)
        group.setExclusive(True)
        for value, label in choices:
            act = QAction(label, menu)
            act.setCheckable(True)
            act.setChecked(value == current)
            act.triggered.connect(lambda _=False, v=value: slot(v))
            group.addAction(act)
            menu.addAction(act)

    def _sessions_line(self):
        n = len(self.tracker.sessions)
        if not n:
            return "现在没有 Claude / Codex 会话"
        busy = len(self.tracker.busy())
        waiting = len(self.tracker.waiting())
        parts = [f"{n} 个会话"]
        if busy:
            parts.append(f"{busy} 个在忙")
        if waiting:
            parts.append(f"{waiting} 个等你")
        return "，".join(parts)

    def build_menu(self):
        menu = QMenu(self)
        mini = QAction('迷你聊天 / 任务窗口',menu)
        mini.triggered.connect(lambda:self.open_mini(force_show=True))
        menu.addAction(mini)
        hotkey = getattr(self,'mini_hotkey',None)
        if hotkey:
            self._info(menu, 'Mini 快捷键：' + hotkey.label)

        self._info(menu, f"当前：{STATES[self.state]['label']}{self.focus_left_text()}")
        self._info(menu, self.usage.brief())
        self._info(menu, self._sessions_line())
        self._info(menu, f"事件端口 {self.port}" if self.port else "事件端口没起来")
        menu.addSeparator()

        act = QAction("打开用量面板", menu)
        act.triggered.connect(lambda: self.open_panel(force_show=True))
        menu.addAction(act)
        act = QAction("看看今天干了啥", menu)
        act.triggered.connect(self.show_stats)
        menu.addAction(act)
        menu.addSeparator()

        act_menu = menu.addMenu("手动换动作")
        group = QActionGroup(act_menu)
        group.setExclusive(True)
        for key in MENU_STATES:
            act = QAction(STATES[key]["label"], act_menu)
            act.setCheckable(True)
            act.setChecked(key == self.state)
            act.triggered.connect(lambda _=False, k=key: self._manual_state(k))
            group.addAction(act)
            act_menu.addAction(act)

        size_menu = menu.addMenu("大小")
        self._radio(size_menu, [(s, f"{int(s * 100)}%") for s in SCALE_CHOICES],
                    min(SCALE_CHOICES, key=lambda s: abs(s - self.scale)), self.set_scale)

        show_menu = menu.addMenu("显示")
        self._check(show_menu, "总在最前", "on_top", "on_top")
        self._check(show_menu, "显示气泡", "bubble_on", "bubble")
        self._check(show_menu, "鼠标悬停时显示额度血条", "hud_on", "hud")
        self._check(show_menu, "移动时用跑步动作", "use_walk_art", "walk_art")
        self._check(show_menu, "隐私模式（分享屏幕时用，不显示命令和文件名）", "privacy", "privacy")

        move_menu = menu.addMenu("行为")
        self._check(move_menu, "自己溜达", "wander_on", "wander")
        self._check(move_menu, "跟随鼠标", "follow_on", "follow")
        self._check(move_menu, "能被甩出去", "physics_on", "physics")

        tip_menu = menu.addMenu("提醒")
        self._check(tip_menu, "额度快用完时提醒我", "quota_alerts_on", "quota_alerts")
        self._check(tip_menu, "本地感知（打字和 CPU）", "ambient_on", "ambient")
        self._check(tip_menu, "久坐提醒", "sit_reminder_on", "sit_reminder")
        self._check(tip_menu, "提示音（要你确认、做完了）", "sound_on", "sound")
        tip_menu.addSeparator()
        self._check(tip_menu, "勿扰模式（不说话、不弹卡片）", "dnd", "dnd")

        cc_menu = menu.addMenu("Claude Code")
        self._check(cc_menu, "在桌宠上点“允许”", "approvals_on", "approvals")
        wait_menu = cc_menu.addMenu("审批卡片最多等")
        self._radio(wait_menu, APPROVAL_WAIT_CHOICES, self.approval_wait, self.set_approval_wait)
        auto = QAction("开新会话时自动把她叫醒", cc_menu)
        auto.setCheckable(True)
        auto.setChecked(self.autowake_enabled())
        auto.toggled.connect(self.set_autowake)
        cc_menu.addAction(auto)
        clean_menu = cc_menu.addMenu("闲着的会话多久后收起来")
        self._radio(clean_menu, IDLE_CLEANUP_CHOICES, self.idle_cleanup_min, self.set_idle_cleanup)
        cc_menu.addSeparator()
        self._check(cc_menu, "从 Anthropic 拉额度数据", "fetch_quota_on", "fetch_quota")
        act = QAction("马上刷新额度", cc_menu)
        act.triggered.connect(lambda: self.usage.refresh_quota(force=True))
        cc_menu.addAction(act)

        cx_menu = menu.addMenu("Codex")
        self._check(cx_menu, "同时跟随 Codex（读取本机记录）", "codex_on", "codex")
        act = QAction("Codex 额度和用量", cx_menu)
        act.triggered.connect(lambda: self.open_panel(force_show=True, tab=4))
        cx_menu.addAction(act)
        act = QAction("马上扫描 Codex 状态", cx_menu)
        act.triggered.connect(self.codex.refresh_quota)
        cx_menu.addAction(act)
        self._info(cx_menu, "Codex 提问和审批：点会话回原窗口处理")

        focus_menu = menu.addMenu("专注模式")
        fgroup = QActionGroup(focus_menu)
        fgroup.setExclusive(True)
        off = QAction("关闭", focus_menu)
        off.setCheckable(True)
        off.setChecked(not self.focus_on)
        off.triggered.connect(lambda _=False: self.stop_focus())
        fgroup.addAction(off)
        focus_menu.addAction(off)
        for m in FOCUS_CHOICES:
            act = QAction(f"{m} 分钟", focus_menu)
            act.setCheckable(True)
            act.setChecked(self.focus_on and self.focus_minutes == m)
            act.triggered.connect(lambda _=False, v=m: self.start_focus(v))
            fgroup.addAction(act)
            focus_menu.addAction(act)
        if self.focus_on:
            focus_menu.addSeparator()
            self._info(focus_menu, self.focus_left_text().strip("（）"))

        boot = QAction("开机自启", menu)
        boot.setCheckable(True)
        boot.setChecked(self.autostart_enabled())
        boot.toggled.connect(self.set_autostart)
        menu.addAction(boot)

        menu.addSeparator()
        act = QAction("回到右下角", menu)
        act.triggered.connect(self.reset_position)
        menu.addAction(act)
        act = QAction("退出", menu)
        act.triggered.connect(self.quit_app)
        menu.addAction(act)
        return menu

    def _manual_state(self, key):
        self._overlay_until = 0.0
        self.base_state = key
        self._apply_state(key)
        self.say(STATES[key].get("bubble"), 3000)

    def _toggle(self, attr, key, checked):
        setattr(self, attr, checked)
        self.settings.setValue(key, checked)
        if key == "codex":
            self.codex.set_enabled(checked)
            if not checked:
                self.tracker.sessions = {sid:s for sid,s in self.tracker.sessions.items() if s.provider != 'codex'}
            self.on_codex_quota()
            self._refresh_views()
        if key == "on_top":
            self.apply_on_top()
        if key == "bubble" and not checked:
            self.bubble.dismiss()
        if key == "follow" and checked:
            self.walk_target = None
        if key == "hud":
            self._sync_hud_hover(time.time())
        if key == "fetch_quota":
            self.usage.set_fetch_enabled(checked)
        if key in ("approvals", "dnd"):
            self._sync_status()
            if not self.approvals_on or self.dnd:
                self._drop_cards()
        if key == "dnd" and checked:
            self.bubble.dismiss()
        if key == "privacy":
            self._refresh_views()

    def _sync_status(self):
        STATUS["approvals_on"] = bool(self.approvals_on and not self.dnd)
        STATUS["approval_timeout"] = int(self.approval_wait)

    def set_approval_wait(self, seconds):
        self.approval_wait = int(seconds)
        self.settings.setValue("approval_wait", self.approval_wait)
        self._sync_status()

    def set_idle_cleanup(self, minutes):
        self.idle_cleanup_min = int(minutes)
        self.settings.setValue("idle_cleanup", self.idle_cleanup_min)

    def autowake_enabled(self):
        return not os.path.exists(data_path("no-autowake"))

    def set_autowake(self, enabled):
        flag = data_path("no-autowake")
        try:
            if enabled and os.path.exists(flag):
                os.remove(flag)
            elif not enabled:
                with open(flag, "w", encoding="utf-8") as fh:
                    fh.write("有这个文件，新开 Claude Code 会话时就不会自动把桌宠叫起来。\n")
        except Exception as err:
            self.say(f"设置失败：{err}", 4000)

    def set_scale(self, value):
        self.scale = float(value)
        self.settings.setValue("scale", self.scale)
        self.anim.set_scale(self.scale)
        self._resize_to_frame()

    def apply_on_top(self):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.on_top)
        self.show()
        self.hud.setWindowFlag(Qt.WindowStaysOnTopHint, self.on_top)
        self._sync_hud_hover(time.time())

    def reset_position(self):
        rect = QGuiApplication.primaryScreen().availableGeometry()
        self.flying = False
        self.walk_target = None
        self.vx = self.vy = 0.0
        self.ax = rect.right() - 180
        self.ay = rect.bottom() - 40
        self._save_position()

    def show_stats(self):
        lines = []
        for key, label in STAT_LABELS:
            lines.append(f"{label} {self.stats.get(key, 0)}")
        text = "今天：" + "，".join(lines)

        extra = []
        work = int(self.stats.get("work_s", 0) or 0)
        if work:
            extra.append(f"Claude 干了 {fmt_span(work)}")
        sat = self.ambient.sitting_minutes()
        if sat >= 5:
            extra.append(f"你已经连着坐了 {sat} 分钟")
        if self.ambient.cpu_percent is not None:
            extra.append(f"CPU {self.ambient.cpu_percent:.0f}%")
        if self.focus_on:
            extra.append(self.focus_left_text().strip("（）"))
        if extra:
            text += "。" + "，".join(extra)
        today = (self.usage.local or {}).get("r1") or {}
        sub = []
        if today.get("tokens"):
            sub.append(f"今天 {fmt_tokens(today['tokens'])} tokens，按 API 价约 {fmt_money(today.get('cost', 0))}")
        sub.append(self.usage.brief())
        self.say(text, 10000, sub="；".join(sub))

    # ==================================================================
    # 统计
    # ==================================================================

    def _today(self):
        return time.strftime("%Y-%m-%d")

    def _load_stats(self):
        day = self.settings.value("stats_day", "")
        if day != self._today():
            return {}
        data = {}
        for key in STAT_KEYS:
            data[key] = int(self.settings.value(f"stats/{key}", 0))
        return data

    def _save_stats(self):
        self.settings.setValue("stats_day", self._today())
        for key in STAT_KEYS:
            self.settings.setValue(f"stats/{key}", int(self.stats.get(key, 0)))

    # ==================================================================
    # 开机自启
    # ==================================================================

    def _startup_path(self):
        if sys.platform != "win32":
            return None
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        folder = os.path.join(
            appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
        )
        return os.path.join(folder, "claude-pet.vbs")

    def autostart_enabled(self):
        path = self._startup_path()
        return bool(path) and os.path.exists(path)

    def set_autostart(self, enabled):
        path = self._startup_path()
        if not path:
            self.say("这个功能只在 Windows 上有", 3000)
            return
        try:
            if enabled:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                pyw = sys.executable.replace("python.exe", "pythonw.exe")
                entry = os.path.join(APP_DIR, "main.py")
                script = (
                    'Set sh = CreateObject("WScript.Shell")\r\n'
                    f'sh.CurrentDirectory = "{APP_DIR}"\r\n'
                    f'sh.Run """{pyw}"" ""{entry}""", 0, False\r\n'
                )
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(script)
                self.say("好，开机就来陪你", 3000)
            else:
                if os.path.exists(path):
                    os.remove(path)
                self.say("以后开机不自动来了", 3000)
        except Exception as err:
            self.say(f"设置失败：{err}", 5000)

    # ==================================================================
    # 托盘
    # ==================================================================

    def _build_tray(self):
        base = QPixmap(64, 64)
        base.fill(Qt.transparent)
        if not self._pix.isNull():
            pic = self._pix.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p = QPainter(base)
            p.drawPixmap((64 - pic.width()) // 2, (64 - pic.height()) // 2, pic)
            p.end()
        self._tray_base = base
        self.tray = QSystemTrayIcon(QIcon(base), self)
        self.tray.setToolTip("Claude + Codex 桌宠")
        self._tray_menu = QMenu()
        self._tray_menu.aboutToShow.connect(self._fill_tray_menu)
        self.tray.setContextMenu(self._tray_menu)
        self.tray.activated.connect(self._on_tray)
        self.tray.show()
        self._update_tray(force_tip=True)

    def _fill_tray_menu(self):
        # 每次打开都重新生成，勾选状态和额度数字才是最新的
        self._tray_menu.clear()
        fresh = self.build_menu()
        for act in fresh.actions():
            self._tray_menu.addAction(act)
        self._tray_keep = fresh

    def _update_tray(self, force_tip=False):
        if self.tray is None:
            return
        key = self.tracker.tray_key()
        if key != self._tray_key and self._tray_base is not None:
            self._tray_key = key
            pm = QPixmap(self._tray_base)
            if key in TRAY_RGB:
                p = QPainter(pm)
                p.setRenderHint(QPainter.Antialiasing, True)
                p.setPen(QPen(QColor(255, 255, 255), 4))
                p.setBrush(QColor(*TRAY_RGB[key]))
                p.drawEllipse(QRectF(38, 38, 24, 24))
                p.end()
            self.tray.setIcon(QIcon(pm))
        lines = [f"Claude + Codex · {STATES[self.state]['label']}", "Claude：" + self.usage.brief()]
        if self.codex_on:
            lines.append("Codex：" + self.codex.brief())
        if self.tracker.sessions:
            lines.append(self._sessions_line())
        tip = "\n".join(lines)
        if tip != self._tray_tip or force_tip:
            self._tray_tip = tip
            self.tray.setToolTip(tip)

    def _on_tray(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.open_mini()
        elif reason == QSystemTrayIcon.DoubleClick:
            self.show()
            self.raise_()
            self.reset_position()

    # ==================================================================
    # 调试（只有带着 CLAUDE_PET_DEBUG=1 启动才会收到）
    # ==================================================================

    def _debug(self, q):
        what = q.get("do")
        if what == "click":
            text = q.get("text") or ""
            for btn in self.card.frame.findChildren(QPushButton):
                if btn.text().split("\n")[0] == text and btn.isEnabled() and btn.isVisible():
                    btn.click()
                    return
        elif what == "snap":
            folder = data_path("debug")
            os.makedirs(folder, exist_ok=True)
            for name, w in (("pet", self), ("hud", self.hud), ("bubble", self.bubble),
                            ("card", self.card), ("panel", self.panel), ("mini",self.mini)):
                path = os.path.join(folder, name + ".png")
                if w is not None and w.isVisible():
                    w.grab().save(path)
                elif os.path.exists(path):
                    os.remove(path)
        elif what == "panel":
            self.open_panel(force_show=True, tab=int(q.get("tab") or 0))
        elif what == "hide_panel" and self.panel is not None:
            self.panel.hide()
        elif what == 'mini':
            self.open_mini(force_show=True,tab=int(q.get('tab') or 0))

    # ==================================================================
    def quit_app(self):
        self._save_position()
        self._save_stats()
        self.settings.sync()
        self._drop_cards()
        self.bubble.dismiss()
        self.hud.hide()
        self.card.hide()
        if self.panel is not None:
            self.panel.hide()
        if self.mini is not None:
            self.mini.hide()
        self.mini_hotkey.close()
        self.chat.close()
        self.usage.stop()
        self.codex.stop()
        self.server.stop()
        QApplication.quit()
