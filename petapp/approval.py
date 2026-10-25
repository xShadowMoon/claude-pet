# -*- coding: utf-8 -*-
"""展示 Claude 权限确认、提问和计划审批卡片。

Claude Code 要你确认的时候（跑命令、改文件、问你问题、批准计划），桌宠头顶弹一张卡片，
直接在这里点，不用切回窗口。终端或桌面版里的确认框照样会出现，哪边先点算哪边。

几个保护：
- 卡片出来后按钮先锁 0.6 秒，防止手快误点；
- "总是允许"只用 Claude Code 自己建议的规则，而且只加"允许"类的；
- 等太久没人点，就什么都不回，交回窗口里让你点。
"""

import json
import os
import time
from urllib.parse import urlparse

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSizePolicy, QTextBrowser, QVBoxLayout, QWidget)

LOCK_MS = 600
CARD_W = 372

QSS = """
QWidget { font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"; font-size: 13px; color: rgb(72, 56, 66); }
#card { background: rgba(255, 253, 254, 252); border: 1.5px solid rgb(236, 170, 196); border-radius: 14px; }
QLabel { background: transparent; }
QLabel[role="chip"] { color: rgb(214, 92, 140); background: rgb(252, 228, 238); border-radius: 8px; padding: 1px 8px; font-size: 11px; font-weight: 700; }
QLabel[role="title"] { font-size: 14px; font-weight: 700; }
QLabel[role="sub"] { color: rgb(150, 128, 140); font-size: 12px; }
QLabel[role="code"] { font-family: Consolas, "Cascadia Mono", "Microsoft YaHei UI", monospace; font-size: 12px; background: rgb(250, 242, 246); border: 1px solid rgb(244, 220, 231); border-radius: 8px; padding: 6px 8px; }
QPushButton { border-radius: 9px; padding: 6px 12px; font-weight: 700; border: 1px solid rgb(236, 190, 210); background: white; }
QPushButton:hover { background: rgb(253, 240, 245); }
QPushButton:disabled { color: rgb(205, 190, 198); border-color: rgb(242, 228, 234); }
QPushButton[role="allow"] { background: rgb(82, 190, 130); color: white; border: none; }
QPushButton[role="allow"]:hover { background: rgb(70, 175, 117); }
QPushButton[role="allow"]:disabled { background: rgb(190, 228, 206); color: white; }
QPushButton[role="deny"] { color: rgb(220, 80, 90); }
QPushButton[role="link"] { border: none; background: transparent; color: rgb(150, 128, 140); font-weight: 400; padding: 2px 2px; }
QPushButton[role="link"]:hover { color: rgb(214, 92, 140); }
QPushButton[role="option"] { text-align: left; font-weight: 400; padding: 7px 10px; }
QPushButton[role="option"]:checked { background: rgb(252, 228, 238); border: 1.5px solid rgb(214, 92, 140); }
QLineEdit { border: 1px solid rgb(236, 190, 210); border-radius: 8px; padding: 5px 8px; background: white; }
QTextBrowser { border: 1px solid rgb(244, 220, 231); border-radius: 8px; background: rgb(255, 250, 252); padding: 2px; }
"""

DEST_TEXT = {"session": "这次会话里", "localSettings": "这个项目（只在本机）", "projectSettings": "这个项目",
             "userSettings": "所有项目"}


def clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[: n - 1] + "…"


def label(text="", role=None, wrap=True):
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    lab.setWordWrap(wrap)
    return lab


def button(text, role=None, slot=None):
    btn = QPushButton(text)
    if role:
        btn.setProperty("role", role)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setFocusPolicy(Qt.NoFocus)
    if slot is not None:
        btn.clicked.connect(lambda _=False: slot())
    return btn


def clear_layout(lay):
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


def describe_request(tool, inp):
    """(标题, 内容, 内容是不是代码)"""
    inp = inp if isinstance(inp, dict) else {}
    if tool in ("Bash", "PowerShell"):
        desc = inp.get("description") or ""
        return (f"想跑一条命令：{clip(desc, 22)}" if desc else "想跑一条命令",
                clip(inp.get("command") or "", 700), True)
    if tool in ("Edit", "MultiEdit"):
        path = inp.get("file_path") or ""
        old = str(inp.get("old_string") or "").strip().splitlines()
        new = str(inp.get("new_string") or "").strip().splitlines()
        body = path
        if old or new:
            body += f"\n- {clip(old[0] if old else '', 70)}\n+ {clip(new[0] if new else '', 70)}"
            more = max(len(old), len(new)) - 1
            if more > 0:
                body += f"\n（还有 {more} 行改动）"
        return "想改一个文件", body, True
    if tool == "Write":
        lines = len(str(inp.get("content") or "").splitlines())
        return "想写一个文件", f"{inp.get('file_path') or ''}\n（{lines} 行）", True
    if tool == "NotebookEdit":
        return "想改笔记本", str(inp.get("notebook_path") or ""), True
    if tool == "Read":
        return "想看一个文件", str(inp.get("file_path") or ""), True
    if tool == "WebFetch":
        url = str(inp.get("url") or "")
        return f"想打开网页：{clip(urlparse(url).netloc, 24)}", url, True
    if tool == "WebSearch":
        return "想上网搜一下", str(inp.get("query") or ""), False
    if tool in ("Agent", "Task"):
        return "想派个帮手", f"{inp.get('subagent_type') or ''} {inp.get('description') or ''}".strip(), False
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        server = parts[1] if len(parts) > 2 else ""
        return f"想用 {server} 的 {parts[-1]}", clip(json.dumps(inp, ensure_ascii=False), 400), True
    return f"想用 {tool}", clip(json.dumps(inp, ensure_ascii=False), 300), True


def allow_suggestions(suggestions):
    """只挑"允许"类的建议：加允许规则、加目录、切到自动接受编辑。"""
    out = []
    for x in suggestions or []:
        if not isinstance(x, dict):
            continue
        kind = x.get("type")
        if kind == "addRules" and x.get("behavior") == "allow":
            out.append(x)
        elif kind == "addDirectories":
            out.append(x)
        elif kind == "setMode" and x.get("mode") == "acceptEdits":
            out.append(x)
    return out


def describe_suggestions(items):
    names, tips = [], []
    for x in items:
        kind = x.get("type")
        if kind == "addRules":
            rules = []
            for r in x.get("rules") or []:
                rc = r.get("ruleContent")
                rules.append(f"{r.get('toolName')}({rc})" if rc else str(r.get("toolName")))
            names.append("总是允许")
            tips.append(f"以后 {DEST_TEXT.get(x.get('destination'), '')}直接放行：{'、'.join(rules)}")
        elif kind == "addDirectories":
            names.append("允许这个目录")
            tips.append("把这些目录加进可访问范围：" + "、".join(x.get("directories") or []))
        elif kind == "setMode":
            names.append("这次都允许改文件")
            tips.append("这次会话切到自动接受编辑")
    name = names[0] if len(set(names)) == 1 else "总是允许"
    return name, "\n".join(tips)


class FocusLineEdit(QLineEdit):
    """卡片平时不抢焦点；点到输入框的时候才把键盘焦点要过来。"""

    def mousePressEvent(self, event):
        self.window().activateWindow()
        self.setFocus()
        super().mousePressEvent(event)


class Countdown(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(4)
        self.frac = 1.0

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0, 0, self.width(), self.height())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(246, 228, 236))
        p.drawRoundedRect(r, 2, 2)
        p.setBrush(QColor(236, 170, 196) if self.frac > 0.25 else QColor(238, 168, 58))
        p.drawRoundedRect(QRectF(0, 0, r.width() * max(0.0, min(1.0, self.frac)), r.height()), 2, 2)


class ApprovalCard(QWidget):
    decided = Signal(str, object)        # 请求 id, 决定（None = 不做决定，交回窗口）
    focus_requested = Signal(str)        # 会话 id

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setStyleSheet(QSS)
        self.setFixedWidth(CARD_W)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 12)
        self.frame = QFrame()
        self.frame.setObjectName("card")
        outer.addWidget(self.frame)
        shadow = QGraphicsDropShadowEffect(self.frame)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(214, 92, 140, 80))
        self.frame.setGraphicsEffect(shadow)
        self.box = QVBoxLayout(self.frame)
        self.box.setContentsMargins(14, 12, 14, 10)
        self.box.setSpacing(8)

        self.rid = None
        self.req = None
        self.sid = ""
        self.deadline = 0.0
        self.started = 0.0
        self.queue_label = None
        self.countdown = None
        self._locked_until = 0.0
        self._questions = []
        self._answers = {}
        self._qi = 0
        self._picked = []
        self._input = {}
        self._qbox = None
        self._other = None
        self.feedback = None

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start(150)

    # ---------------- 对外 ----------------

    def present(self, req, queue_n=0, privacy=False, name=""):
        self.rid = req["rid"]
        self.req = req
        self.sid = req.get("sid") or ""
        self.deadline = req.get("deadline") or (time.time() + 120)
        self.started = time.time()
        p = req.get("payload") or {}
        tool = p.get("tool_name") or ""
        inp = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        clear_layout(self.box)

        head = QHBoxLayout()
        head.addWidget(label(clip(name or os.path.basename(str(p.get("cwd") or "")) or "Claude", 24), "chip", wrap=False))
        head.addStretch(1)
        self.queue_label = label("", "sub", wrap=False)
        head.addWidget(self.queue_label)
        self.box.addLayout(head)

        if tool == "AskUserQuestion" and inp.get("questions"):
            self._build_questions(inp)
        elif tool == "ExitPlanMode":
            self._build_plan(inp, privacy)
        else:
            self._build_tool(tool, inp, p.get("permission_suggestions"), privacy)

        self.countdown = Countdown()
        self.box.addWidget(self.countdown)
        self.set_queue(queue_n)
        self._relayout()
        self._lock(LOCK_MS)
        self.show()
        self.raise_()

    def set_queue(self, n):
        if self.queue_label is not None:
            self.queue_label.setText(f"后面还有 {n} 个" if n else "")

    def clear(self):
        self.hide()
        self.rid = None
        self.req = None

    # ---------------- 各种卡片 ----------------

    def _build_tool(self, tool, inp, suggestions, privacy):
        title, body, mono = describe_request(tool, inp)
        self.box.addWidget(label(title, "title"))
        if body and not privacy:
            self.box.addWidget(label(body, "code" if mono else "sub"))
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(button("允许", "allow", lambda: self._decide({"behavior": "allow"})))
        always = allow_suggestions(suggestions)
        if always:
            text, tip = describe_suggestions(always)
            btn = button(text, None, lambda: self._decide({"behavior": "allow", "updatedPermissions": always}))
            btn.setToolTip(tip)
            row.addWidget(btn)
        row.addWidget(button("拒绝", "deny", lambda: self._decide(
            {"behavior": "deny", "message": "用户在桌宠上拒绝了这一步。"})))
        row.addStretch(1)
        self.box.addLayout(row)
        self.box.addLayout(self._link_row("去窗口里处理"))

    def _build_plan(self, inp, privacy):
        self.box.addWidget(label("计划写好了，要按这个来吗？", "title"))
        if not privacy:
            view = QTextBrowser()
            view.setOpenLinks(False)
            view.setMarkdown(str(inp.get("plan") or "（计划内容没有传过来，去窗口里看吧）"))
            view.setFixedHeight(200)
            self.box.addWidget(view)
        self.feedback = FocusLineEdit()
        self.feedback.setPlaceholderText("想让它改哪里写这儿，再点“接着改”（可以不填）")
        self.box.addWidget(self.feedback)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(button("批准", "allow", lambda: self._decide({"behavior": "allow"})))
        row.addWidget(button("批准，改文件不用再问", None, lambda: self._decide({
            "behavior": "allow",
            "updatedPermissions": [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]})))
        row.addWidget(button("接着改", "deny", self._keep_planning))
        row.addStretch(1)
        self.box.addLayout(row)
        self.box.addLayout(self._link_row("去窗口里看"))

    def _keep_planning(self):
        note = self.feedback.text().strip() if self.feedback is not None else ""
        msg = "用户想先继续完善计划，暂时不要开始动手。"
        if note:
            msg += f"用户的意见：{note}"
        self._decide({"behavior": "deny", "message": msg})

    def _build_questions(self, inp):
        self._input = dict(inp)
        self._questions = [q for q in inp.get("questions") or [] if isinstance(q, dict)]
        self._answers = {}
        self._qi = 0
        self._qbox = QVBoxLayout()
        self._qbox.setSpacing(6)
        self.box.addLayout(self._qbox)
        self._render_question()
        self.box.addLayout(self._link_row("去窗口里回答"))

    def _render_question(self):
        clear_layout(self._qbox)
        q = self._questions[self._qi]
        top = QHBoxLayout()
        if q.get("header"):
            top.addWidget(label(clip(q["header"], 16), "chip", wrap=False))
        if len(self._questions) > 1:
            top.addWidget(label(f"第 {self._qi + 1}/{len(self._questions)} 题", "sub", wrap=False))
        top.addStretch(1)
        self._qbox.addLayout(top)
        self._qbox.addWidget(label(q.get("question") or "", "title"))
        multi = bool(q.get("multiSelect"))
        self._picked = []
        for opt in q.get("options") or []:
            text = opt.get("label") if isinstance(opt, dict) else str(opt)
            desc = opt.get("description") if isinstance(opt, dict) else ""
            btn = button(text + (f"\n{clip(desc, 60)}" if desc else ""), "option")
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            if desc:
                btn.setToolTip(desc)
            if multi:
                btn.setCheckable(True)
                btn.toggled.connect(lambda on, t=text: self._toggle_pick(t, on))
            else:
                btn.clicked.connect(lambda _=False, t=text: self._answer(t))
            self._qbox.addWidget(btn)
        row = QHBoxLayout()
        self._other = FocusLineEdit()
        self._other.setPlaceholderText("其它答案…")
        self._other.returnPressed.connect(self._submit_question)
        row.addWidget(self._other, 1)
        row.addWidget(button("确定" if multi else "发送", "allow", self._submit_question))
        self._qbox.addLayout(row)
        self._lock(400)

    def _toggle_pick(self, text, on):
        if on and text not in self._picked:
            self._picked.append(text)
        elif not on and text in self._picked:
            self._picked.remove(text)

    def _submit_question(self):
        if time.time() < self._locked_until:
            return
        extra = self._other.text().strip() if self._other is not None else ""
        picks = list(self._picked)
        if extra:
            picks.append(extra)
        if not picks:
            return
        self._answer(", ".join(picks))

    def _answer(self, value):
        if time.time() < self._locked_until:
            return
        q = self._questions[self._qi]
        self._answers[q.get("question") or f"问题{self._qi + 1}"] = value
        self._qi += 1
        if self._qi < len(self._questions):
            self._render_question()
            self._relayout()
            return
        updated = dict(self._input)
        updated["answers"] = self._answers
        self._decide({"behavior": "allow", "updatedInput": updated})

    def _link_row(self, text):
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button(text + " →", "link", self._hand_back))
        return row

    # ---------------- 内部 ----------------

    def _hand_back(self):
        rid, sid = self.rid, self.sid
        if rid is None:
            return
        self.clear()
        self.focus_requested.emit(sid)
        self.decided.emit(rid, None)

    def _decide(self, decision):
        if self.rid is None or time.time() < self._locked_until:
            return
        rid = self.rid
        self.clear()
        self.decided.emit(rid, decision)

    def _lock(self, ms):
        self._locked_until = time.time() + ms / 1000.0
        for btn in self.frame.findChildren(QPushButton):
            btn.setEnabled(False)
        QTimer.singleShot(ms + 20, self._unlock)

    def _unlock(self):
        if time.time() + 0.01 < self._locked_until:
            return
        for btn in self.frame.findChildren(QPushButton):
            btn.setEnabled(True)

    def _relayout(self):
        self.frame.layout().activate()
        self.layout().activate()
        self.adjustSize()

    def _on_tick(self):
        if not self.isVisible() or self.countdown is None:
            return
        total = max(1.0, self.deadline - self.started)
        self.countdown.frac = (self.deadline - time.time()) / total
        self.countdown.update()
