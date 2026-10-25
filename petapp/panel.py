# -*- coding: utf-8 -*-
"""按助手分组展示额度、会话与历史统计。

两个助手大标签下的页面组成：
- 额度：5 小时和本周还剩多少、几点重置、照现在的速度多久用完、附加额度、额外用量、本周用在哪。
- 用量统计：今天 / 近 7 天 / 近 30 天用了多少 token、按 API 价相当于多少钱、柱状图、
  按模型分、最费的会话、桌宠今天记下的事。
- 会话：每个 Claude Code 会话现在在干什么，点一下把它的窗口叫出来；下面是自定义小组件。
- 电脑：CPU、内存、显卡、系统盘、电池、网速。
"""

import time

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import (QButtonGroup, QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QToolTip,
                               QVBoxLayout, QWidget)

from .common import (BAD, GOOD, MUTED, ROSE, WARN, WEEK, fmt_ago, fmt_clock, fmt_money, fmt_span,
                     fmt_tokens, remain_rgb)
from .sessions import STATE_RGB, clip
from .sysinfo import SysInfo, fmt_bytes

FONT_FAMILIES = ["Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI"]
MODEL_RGB = [ROSE, WEEK, GOOD, WARN, (160, 120, 230), (90, 180, 210)]
MIX_RGB = {"Claude Code": ROSE, "聊天": WEEK, "Cowork": GOOD, "其它": (200, 180, 190)}
TABS = ("额度", "会话", "统计")

QSS = """
QWidget { font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"; font-size: 13px; color: rgb(72, 56, 66); }
#card { background: rgba(255, 253, 254, 252); border: 1px solid rgb(236, 170, 196); border-radius: 16px; }
QLabel { background: transparent; }
QLabel[role="title"] { font-size: 15px; font-weight: 700; }
QLabel[role="chip"] { color: rgb(214, 92, 140); background: rgb(252, 228, 238); border-radius: 8px; padding: 1px 8px; font-size: 11px; font-weight: 700; }
QLabel[role="h"] { font-size: 13px; font-weight: 700; }
QLabel[role="big"] { font-size: 28px; font-weight: 800; }
QLabel[role="mid"] { font-size: 20px; font-weight: 700; }
QLabel[role="sub"] { color: rgb(150, 128, 140); font-size: 12px; }
QLabel[role="tiny"] { color: rgb(170, 150, 160); font-size: 11px; }
QLabel[role="warn"] { color: rgb(214, 84, 70); font-size: 12px; }
QLabel[role="good"] { color: rgb(52, 150, 98); font-size: 12px; }
QFrame#box { background: rgb(255, 247, 250); border: 1px solid rgb(246, 216, 229); border-radius: 12px; }
QFrame#row { background: rgb(255, 247, 250); border: 1px solid rgb(246, 216, 229); border-radius: 12px; }
QFrame#row:hover { border: 1px solid rgb(236, 170, 196); background: rgb(255, 242, 247); }
QPushButton#tab { border: none; padding: 5px 12px; border-radius: 12px; color: rgb(150, 128, 140); background: transparent; font-size: 13px; }
QPushButton#tab:checked { background: rgb(252, 228, 238); color: rgb(214, 92, 140); font-weight: 700; }
QPushButton#tab:hover:!checked { background: rgb(253, 240, 245); }
QPushButton#providerTab { border: 1px solid rgb(242, 213, 227); border-radius: 12px; padding: 9px 20px; font-size: 16px; font-weight: 700; background: rgb(255, 249, 252); color: rgb(150, 128, 140); }
QPushButton#providerTab:checked { background: rgb(252, 228, 238); border-color: rgb(236, 170, 196); color: rgb(214, 92, 140); }
QPushButton#providerTab[provider="codex"]:checked { background: rgb(239, 235, 255); border-color: rgb(189, 180, 234); color: rgb(126, 108, 192); }
QPushButton#icon { border: none; border-radius: 10px; padding: 2px 8px; color: rgb(150, 128, 140); background: transparent; font-size: 15px; }
QPushButton#icon:hover { background: rgb(252, 228, 238); color: rgb(214, 92, 140); }
QScrollArea { background: transparent; border: none; }
#holder { background: transparent; }
QStackedWidget { background: transparent; }
QScrollBar:vertical { width: 6px; background: transparent; margin: 4px 0; }
QScrollBar::handle:vertical { background: rgb(240, 200, 216); border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QToolTip { color: rgb(72, 56, 66); background: rgb(255, 253, 254); border: 1px solid rgb(236, 170, 196); padding: 4px; }
"""


# ---------------------------------------------------------------------------
# 小部件
# ---------------------------------------------------------------------------

def make_label(text="", role=None, wrap=False):
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    lab.setWordWrap(wrap)
    return lab


def set_role(widget, role):
    if widget.property("role") == role:
        return
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def make_box(name="box"):
    frame = QFrame()
    frame.setObjectName(name)
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(12, 10, 12, 10)
    lay.setSpacing(5)
    return frame, lay


def clear_layout(lay):
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())


def load_rgb(pct):
    """占用越高越红。"""
    if pct is None:
        return (190, 180, 186)
    if pct < 60:
        return GOOD
    if pct < 85:
        return WARN
    return BAD


class Bar(QWidget):
    def __init__(self, height=8):
        super().__init__()
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.frac = 0.0
        self.color = QColor(*GOOD)
        self.segments = None

    def set(self, frac, rgb):
        frac = max(0.0, min(1.0, float(frac or 0)))
        color = QColor(*rgb)
        if self.segments is None and abs(frac - self.frac) < 1e-4 and color == self.color:
            return
        self.frac, self.color, self.segments = frac, color, None
        self.update()

    def set_segments(self, parts):
        self.segments = [(max(0.0, float(f)), QColor(*c)) for f, c in parts]
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(0, 0, self.width(), self.height())
        rad = r.height() / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(243, 228, 235))
        p.drawRoundedRect(r, rad, rad)
        if self.segments:
            path = QPainterPath()
            path.addRoundedRect(r, rad, rad)
            p.setClipPath(path)
            x = 0.0
            for frac, color in self.segments:
                w = r.width() * frac
                if w <= 0:
                    continue
                p.setBrush(color)
                p.drawRect(QRectF(x, 0, w, r.height()))
                x += w
            return
        if self.frac > 0:
            p.setBrush(self.color)
            p.drawRoundedRect(QRectF(0, 0, max(r.height(), r.width() * self.frac), r.height()), rad, rad)


class RollLabel(QLabel):
    """数字变的时候滚过去，像余额跳动那样。"""

    def __init__(self, fmt, role="big"):
        super().__init__("—")
        self.setProperty("role", role)
        self._fmt = fmt
        self._val = None
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(700)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(lambda v: self.setText(self._fmt(float(v))))

    def set_value(self, value):
        value = float(value or 0)
        if self._val is None:
            self._val = value
            self.setText(self._fmt(value))
            return
        if abs(value - self._val) < 1e-6:
            return
        start = self._val
        if self._anim.state() == QAbstractAnimation.Running:
            start = float(self._anim.currentValue())
            self._anim.stop()
        self._anim.setStartValue(start)
        self._anim.setEndValue(value)
        self._val = value
        self._anim.start()


class BarChart(QWidget):
    def __init__(self,formatter=fmt_money):
        super().__init__()
        self.formatter=formatter
        self.setMinimumHeight(120)
        self.setMouseTracking(True)
        self.values, self.labels, self.tips, self.ticks, self.hl = [], [], [], set(), -1
        self._sig = None
        self._font = QFont()
        self._font.setFamilies(FONT_FAMILIES)
        self._font.setPixelSize(10)

    def set_data(self, values, labels, tips, ticks, highlight):
        sig = (tuple(round(float(v), 5) for v in values), tuple(labels), highlight)
        if sig == self._sig:
            return
        self._sig = sig
        self.values = [float(v) for v in values]
        self.labels = list(labels)
        self.tips = list(tips)
        self.ticks = set(ticks)
        self.hl = highlight
        self.update()

    def _plot(self):
        return QRectF(4, 16, max(10, self.width() - 8), max(10, self.height() - 34))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setFont(self._font)
        n = len(self.values)
        if not n or max(self.values) <= 0:
            p.setPen(QColor(*MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "这段时间还没有记录")
            return
        plot = self._plot()
        vmax = max(self.values)
        slot = plot.width() / n
        bw = max(2.0, slot * 0.64)
        p.setPen(QColor(170, 150, 160))
        p.drawText(QRectF(plot.x(), 0, plot.width(), 14), Qt.AlignLeft | Qt.AlignVCenter, f"最高 {self.formatter(vmax)}")
        p.setPen(QColor(240, 222, 230))
        p.drawLine(plot.bottomLeft(), plot.bottomRight())
        p.setPen(Qt.NoPen)
        for i, v in enumerate(self.values):
            h = plot.height() * (v / vmax)
            if v > 0:
                h = max(h, 2.0)
            x = plot.x() + i * slot + (slot - bw) / 2
            p.setBrush(QColor(*ROSE) if i == self.hl else QColor(240, 176, 202))
            rad = min(3.0, bw / 2, h / 2)
            p.drawRoundedRect(QRectF(x, plot.bottom() - h, bw, h), rad, rad)
        p.setPen(QColor(*MUTED))
        for i in sorted(self.ticks):
            if 0 <= i < n:
                cx = plot.x() + i * slot + slot / 2
                p.drawText(QRectF(cx - 24, plot.bottom() + 3, 48, 14), Qt.AlignCenter, self.labels[i])

    def mouseMoveEvent(self, e):
        n = len(self.values)
        if not n:
            return
        plot = self._plot()
        i = int((e.position().x() - plot.x()) // (plot.width() / n))
        if 0 <= i < min(n, len(self.tips)):
            QToolTip.showText(e.globalPosition().toPoint(), self.tips[i], self)


# ---------------------------------------------------------------------------
# 额度
# ---------------------------------------------------------------------------

class QuotaPage(QWidget):
    def __init__(self, hub):
        super().__init__()
        self.hub = hub
        self._other_keys = None
        self._other_rows = {}
        self._tints = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        self.empty = make_label("", "sub", wrap=True)
        lay.addWidget(self.empty)

        self.five_box, b = make_box()
        head = QHBoxLayout()
        head.addWidget(make_label("5 小时窗口", "h"))
        head.addStretch(1)
        self.five_used = make_label("", "sub")
        head.addWidget(self.five_used)
        b.addLayout(head)
        row = QHBoxLayout()
        self.five_big = RollLabel(lambda v: f"剩 {v:.0f}%", "big")
        row.addWidget(self.five_big)
        row.addStretch(1)
        col = QVBoxLayout()
        col.setSpacing(1)
        self.five_reset = make_label("", "sub")
        self.five_reset.setAlignment(Qt.AlignRight)
        self.five_left = make_label("", "sub")
        self.five_left.setAlignment(Qt.AlignRight)
        col.addWidget(self.five_reset)
        col.addWidget(self.five_left)
        row.addLayout(col)
        b.addLayout(row)
        self.five_bar = Bar(10)
        b.addWidget(self.five_bar)
        self.five_pred = make_label("", "sub", wrap=True)
        b.addWidget(self.five_pred)
        self.five_local = make_label("", "tiny", wrap=True)
        b.addWidget(self.five_local)
        lay.addWidget(self.five_box)

        self.week_box, b = make_box()
        head = QHBoxLayout()
        head.addWidget(make_label("本周 · 全部模型", "h"))
        head.addStretch(1)
        self.week_used = make_label("", "sub")
        head.addWidget(self.week_used)
        b.addLayout(head)
        self.week_big = RollLabel(lambda v: f"剩 {v:.0f}%", "mid")
        b.addWidget(self.week_big)
        self.week_bar = Bar(8)
        b.addWidget(self.week_bar)
        self.week_reset = make_label("", "sub", wrap=True)
        b.addWidget(self.week_reset)
        self.week_local = make_label("", "tiny", wrap=True)
        b.addWidget(self.week_local)
        lay.addWidget(self.week_box)

        self.other_box, self.other_lay = make_box()
        self.other_box.hide()
        lay.addWidget(self.other_box)

        self.money_box, b = make_box()
        b.addWidget(make_label("钱包", "h"))
        self.money_text = make_label("", "sub", wrap=True)
        b.addWidget(self.money_text)
        lay.addWidget(self.money_box)

        self.mix_box, b = make_box()
        b.addWidget(make_label("本周额度花在哪", "h"))
        self.mix_bar = Bar(10)
        b.addWidget(self.mix_bar)
        self.mix_text = make_label("", "sub", wrap=True)
        b.addWidget(self.mix_text)
        lay.addWidget(self.mix_box)
        lay.addStretch(1)

    def _tint(self, key, widget, rgb):
        if self._tints.get(key) != rgb:
            self._tints[key] = rgb
            widget.setStyleSheet("color: rgb(%d, %d, %d);" % tuple(rgb))

    def refresh(self):
        hub = self.hub
        now = time.time()
        q = hub.quota or {}
        five = hub.window("five_hour")
        week = hub.window("seven_day")
        has = bool(q.get("windows"))
        self.empty.setVisible(not has)
        for w in (self.money_box, self.mix_box):
            w.setVisible(has)
        if not has:
            self.five_box.hide()
            self.week_box.hide()
            self.other_box.hide()
            self.empty.setText(hub.quota_error or "正在向 Anthropic 要额度数据…")
            return

        self.five_box.setVisible(five is not None)
        if five is not None:
            used = five["used"]
            remain = max(0.0, 100.0 - used)
            rgb = remain_rgb(remain)
            self.five_big.set_value(remain)
            self._tint("five", self.five_big, rgb)
            self.five_used.setText(f"已用 {used:.0f}%")
            self.five_bar.set(remain / 100.0, rgb)
            reset = five.get("resets_at")
            if five.get("after_reset"):
                self.five_reset.setText("已经重置了")
                self.five_left.setText("等下一次刷新")
            elif reset:
                self.five_reset.setText(f"{fmt_clock(reset)} 重置")
                self.five_left.setText(f"还有 {fmt_span(reset - now)}")
            else:
                self.five_reset.setText("")
                self.five_left.setText("")
            eta = hub.predict("five_hour")
            if used >= 100:
                pred, role = "已经用完了，只能等重置", "warn"
            elif eta and reset and eta < reset - now:
                pred, role = f"照现在的速度，大约 {fmt_span(eta)} 后用完，比重置早 {fmt_span(reset - now - eta)}", "warn"
            elif eta:
                pred, role = "照现在的速度，撑得到重置", "good"
            else:
                pred, role = "", "sub"
            self.five_pred.setText(pred)
            self.five_pred.setVisible(bool(pred))
            set_role(self.five_pred, role)
            win = hub.local_window("five_hour")
            per = hub.cost_per_percent()
            if win and win[0]:
                text = f"这个窗口里本机记录到 {fmt_tokens(win[0])} tokens，按 API 价约 {fmt_money(win[1])}"
                if per:
                    text += f"（1% ≈ {fmt_money(per)}）"
                self.five_local.setText(text)
                self.five_local.show()
            else:
                self.five_local.hide()

        self.week_box.setVisible(week is not None)
        if week is not None:
            used = week["used"]
            remain = max(0.0, 100.0 - used)
            rgb = WEEK if remain > 20 else remain_rgb(remain)
            self.week_big.set_value(remain)
            self._tint("week", self.week_big, rgb)
            self.week_used.setText(f"已用 {used:.0f}%")
            self.week_bar.set(remain / 100.0, rgb)
            reset = week.get("resets_at")
            if reset:
                self.week_reset.setText(f"{fmt_clock(reset)} 重置 · 还有 {fmt_span(reset - now)}")
            else:
                self.week_reset.setText("已经重置了，等下一次刷新" if week.get("after_reset") else "")
            win = hub.local_window("seven_day")
            self.week_local.setVisible(bool(win and win[0]))
            if win and win[0]:
                self.week_local.setText(f"这一周本机记录到 {fmt_tokens(win[0])} tokens，按 API 价约 {fmt_money(win[1])}")

        self._fill_others([w for w in hub.windows() if w["key"] not in ("five_hour", "seven_day")])

        lines = []
        for m in q.get("money") or []:
            s = f"{m['label']}：还剩 {fmt_money(m['remaining'])} / {fmt_money(m['limit'])}"
            if m.get("resets_at"):
                s += f"，{fmt_clock(m['resets_at'])} 到期"
            lines.append(s)
        ex = q.get("extra") or {}
        if ex.get("enabled"):
            s = "额外用量：开着"
            if ex.get("used") is not None:
                s += f"，这个月花了 {fmt_money(ex['used'])}"
            if ex.get("limit"):
                s += f" / 上限 {fmt_money(ex['limit'])}"
            if ex.get("reached"):
                s += "（到上限了）"
        else:
            s = "额外用量：关着，额度用完就只能等重置"
        lines.append(s)
        self.money_text.setText("\n".join(lines))

        mix = q.get("breakdown") or []
        self.mix_box.setVisible(bool(mix))
        if mix:
            self.mix_bar.set_segments([(pct / 100.0, MIX_RGB.get(name, (200, 180, 190))) for name, pct in mix])
            self.mix_text.setText("   ".join(f"{name} {pct:.0f}%" for name, pct in mix))

    def _fill_others(self, wins):
        keys = tuple(w["key"] for w in wins)
        if keys != self._other_keys:
            self._other_keys = keys
            clear_layout(self.other_lay)
            self._other_rows = {}
            if wins:
                self.other_lay.addWidget(make_label("分模型 / 分产品的限额", "h"))
            for w in wins:
                top = QHBoxLayout()
                top.addWidget(make_label(w["label"], "sub"))
                top.addStretch(1)
                val = make_label("", "sub")
                top.addWidget(val)
                self.other_lay.addLayout(top)
                bar = Bar(6)
                self.other_lay.addWidget(bar)
                self._other_rows[w["key"]] = (val, bar)
        self.other_box.setVisible(bool(wins))
        for w in wins:
            val, bar = self._other_rows[w["key"]]
            remain = max(0.0, 100.0 - w["used"])
            text = f"已用 {w['used']:.0f}%"
            if w.get("resets_at"):
                text += f" · {fmt_clock(w['resets_at'])} 重置"
            val.setText(text)
            bar.set(remain / 100.0, remain_rgb(remain))


# ---------------------------------------------------------------------------
# 用量统计
# ---------------------------------------------------------------------------

class StatsPage(QWidget):
    RANGES = (("r1", "今天"), ("r7", "近 7 天"), ("r30", "近 30 天"))
    LOCAL = (("prompts", "你发的消息"), ("turns", "完成的回合"), ("tools", "工具调用"), ("edits", "改文件"),
             ("waits", "等你确认"), ("approvals", "在桌宠上审批"), ("errors", "报错"))

    def __init__(self, hub, pet):
        super().__init__()
        self.hub = hub
        self.pet = pet
        self.range = "r1"
        self._key = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.group = QButtonGroup(self)
        for i, (_, text) in enumerate(self.RANGES):
            btn = QPushButton(text)
            btn.setObjectName("tab")
            btn.setCheckable(True)
            btn.setChecked(i == 0)
            btn.setCursor(Qt.PointingHandCursor)
            self.group.addButton(btn, i)
            row.addWidget(btn)
        row.addStretch(1)
        self.group.idClicked.connect(self._pick)
        lay.addLayout(row)

        self.loading = make_label("正在读本机的对话记录……第一次要几秒钟", "sub", wrap=True)
        lay.addWidget(self.loading)

        self.head, b = make_box()
        b.addWidget(make_label("按 API 标价，相当于", "sub"))
        self.money = RollLabel(fmt_money, "big")
        b.addWidget(self.money)
        self.totals = make_label("", "sub", wrap=True)
        b.addWidget(self.totals)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(0)
        self.tok = {}
        for col, (key, text) in enumerate((("in", "输入"), ("out", "输出"), ("cw", "缓存写入"), ("cr", "缓存读取"))):
            v = make_label("0", "h")
            v.setAlignment(Qt.AlignCenter)
            t = make_label(text, "tiny")
            t.setAlignment(Qt.AlignCenter)
            grid.addWidget(v, 0, col)
            grid.addWidget(t, 1, col)
            self.tok[key] = v
        b.addLayout(grid)
        lay.addWidget(self.head)

        self.chart_box, b = make_box()
        self.chart_title = make_label("", "h")
        b.addWidget(self.chart_title)
        self.chart = BarChart()
        b.addWidget(self.chart)
        lay.addWidget(self.chart_box)

        self.model_box, self.model_lay = make_box()
        lay.addWidget(self.model_box)
        self.sess_box, self.sess_lay = make_box()
        lay.addWidget(self.sess_box)

        self.local_box, b = make_box()
        b.addWidget(make_label("今天桌宠记下的", "h"))
        self.local_text = make_label("", "sub", wrap=True)
        b.addWidget(self.local_text)
        lay.addWidget(self.local_box)

        lay.addWidget(make_label("Pro 订阅不按这个扣钱。这里是按 API 标价估出来的，让你知道这些对话值多少；"
                                 "价格不对可以在 data/prices.json 里改。", "tiny", wrap=True))
        lay.addStretch(1)

    def _pick(self, idx):
        self.range = self.RANGES[idx][0]
        self._key = None
        self.refresh()

    def refresh(self):
        snap = self.hub.local or {}
        ready = bool(snap.get("ready"))
        self.loading.setVisible(not ready)
        for w in (self.head, self.chart_box, self.model_box, self.sess_box):
            w.setVisible(ready)
        stats = getattr(self.pet, "stats", {}) or {}
        # These old hook counters combine both providers. Do not label them Claude statistics.
        self.local_box.hide()
        parts = [f"{label} {stats.get(k, 0)}" for k, label in self.LOCAL]
        work = int(stats.get("work_s", 0) or 0)
        if work:
            parts.append(f"Claude 干活 {fmt_span(work)}")
        self.local_text.setText("，".join(parts))
        if not ready:
            return
        key = (snap.get("at"), self.range, self.pet.privacy)
        if key == self._key:
            return
        self._key = key
        s = snap.get(self.range) or {}
        self.money.set_value(s.get("cost", 0.0))
        self.totals.setText(f"{fmt_tokens(s.get('tokens', 0))} tokens · {s.get('msgs', 0)} 条回复 · "
                            f"{s.get('nsessions', 0)} 个会话")
        for k, lab in self.tok.items():
            lab.setText(fmt_tokens(s.get(k, 0)))
        if self.range == "r1":
            hours = s.get("hours") or [0.0] * 24
            htok = s.get("htok") or [0] * 24
            self.chart_title.setText("今天每小时（按 API 价）")
            self.chart.set_data(hours, [str(h) for h in range(24)],
                                [f"{h}:00–{h}:59   {fmt_money(hours[h])} · {fmt_tokens(htok[h])} tokens" for h in range(24)],
                                {0, 6, 12, 18, 23}, time.localtime().tm_hour)
        else:
            series = s.get("series") or []
            n = len(series)
            ticks = set(range(n)) if n <= 7 else {i for i in range(n) if (n - 1 - i) % 5 == 0}
            self.chart_title.setText("每天（按 API 价）")
            self.chart.set_data([c for _, c, _ in series], [d[5:].replace("-", "/") for d, _, _ in series],
                                [f"{d}   {fmt_money(c)} · {fmt_tokens(t)} tokens" for d, c, t in series], ticks, n - 1)
        self._fill_models(s.get("models") or [], s.get("cost", 0.0))
        self._fill_sessions(s.get("sessions") or [])

    def _fill_models(self, models, total):
        clear_layout(self.model_lay)
        self.model_lay.addWidget(make_label("按模型", "h"))
        if not models:
            self.model_lay.addWidget(make_label("这段时间没有记录", "sub"))
            return
        for i, (name, cost, tokens, msgs) in enumerate(models[:6]):
            share = cost / total if total > 0 else 0.0
            top = QHBoxLayout()
            top.addWidget(make_label(name, "sub"))
            top.addStretch(1)
            top.addWidget(make_label(f"{fmt_money(cost)} · {share * 100:.0f}% · {msgs} 条", "sub"))
            self.model_lay.addLayout(top)
            bar = Bar(6)
            bar.set(share, MODEL_RGB[i % len(MODEL_RGB)])
            self.model_lay.addWidget(bar)

    def _fill_sessions(self, sessions):
        clear_layout(self.sess_lay)
        self.sess_lay.addWidget(make_label("最费的会话", "h"))
        if not sessions:
            self.sess_lay.addWidget(make_label("这段时间没有记录", "sub"))
            return
        for _sid, title, cost, tokens in sessions[:5]:
            row = QHBoxLayout()
            private = getattr(self.pet,'privacy',False)
            name = make_label('Claude 会话' if private else clip(title, 22), "sub")
            name.setToolTip('' if private else title)
            row.addWidget(name)
            row.addStretch(1)
            row.addWidget(make_label(f"{fmt_money(cost)} · {fmt_tokens(tokens)}", "sub"))
            self.sess_lay.addLayout(row)


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

class SessionRow(QFrame):
    def __init__(self, s, pet):
        super().__init__()
        self.setObjectName("row")
        self.sid = s.id
        self.pet = pet
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("点一下把这个会话的窗口叫到前面")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 9)
        lay.setSpacing(3)
        top = QHBoxLayout()
        dot = QLabel("●")
        dot.setStyleSheet("color: rgb(%d, %d, %d); font-size: 12px;" % STATE_RGB.get(s.color_key(), (190, 180, 186)))
        top.addWidget(dot)
        provider = "Codex" if s.provider == "codex" else "Claude"
        top.addWidget(make_label(f"{provider} · {clip(s.name(), 18)}", "h"))
        top.addStretch(1)
        top.addWidget(make_label(s.status_text(), "sub"))
        lay.addLayout(top)
        act = s.activity_text(pet.privacy)
        if act:
            lay.addWidget(make_label(act, "sub", wrap=True))
        facts = s.facts()
        if facts:
            lay.addWidget(make_label(facts, "tiny", wrap=True))
        if s.unseen and s.last_message and not pet.privacy:
            lay.addWidget(make_label("“" + clip(s.last_message, 70) + "”", "tiny", wrap=True))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.pet.focus_session(self.sid)
            event.accept()


class SessionsPage(QWidget):
    def __init__(self, pet, provider='claude'):
        super().__init__()
        self.pet = pet
        self.provider = provider
        self._sig = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.list_lay = QVBoxLayout()
        self.list_lay.setSpacing(8)
        lay.addLayout(self.list_lay)
        self.widget_box, self.widget_lay = make_box()
        lay.addWidget(self.widget_box)
        note='只列出 Claude 会话。点一个会话打开原窗口；需要确认时仍可用桌宠的确认卡片。'
        if provider=='codex':
            note='只列出 Codex 会话。外部会话的确认请回原窗口处理；Mini 发起的请求可在 Mini 内处理。'
        lay.addWidget(make_label(note, "tiny", wrap=True))
        lay.addStretch(1)

    def refresh(self):
        pet = self.pet
        sessions = [s for s in pet.tracker.ordered() if s.provider == self.provider]
        sig = tuple((s.id, s.state, s.activity, s.status_text(), s.facts(), s.unseen, s.last_message[:24])
                    for s in sessions) + (pet.privacy,)
        wsig = tuple((w["id"], w["title"], w["value"]) for w in pet.widgets.values())
        if (sig, wsig) == self._sig:
            return
        self._sig = (sig, wsig)
        clear_layout(self.list_lay)
        if not sessions:
            name='Codex' if self.provider=='codex' else 'Claude'
            self.list_lay.addWidget(make_label(f"现在没有 {name} 会话在跑。新开一个会话，或者在已有会话里发条消息，"
                                               "这里就会出现。", "sub", wrap=True))
        for s in sessions:
            self.list_lay.addWidget(SessionRow(s, pet))
        clear_layout(self.widget_lay)
        self.widget_box.setVisible(bool(pet.widgets) and self.provider=='claude')
        if pet.widgets and self.provider=='claude':
            self.widget_lay.addWidget(make_label("小组件", "h"))
            for w in pet.widgets.values():
                row = QHBoxLayout()
                row.addWidget(make_label(clip(w["title"], 16), "sub"))
                row.addStretch(1)
                val = make_label(clip(w["value"], 30), "h")
                val.setToolTip(f"{fmt_ago(w['at'])}更新")
                row.addWidget(val)
                self.widget_lay.addLayout(row)


# ---------------------------------------------------------------------------
# 电脑
# ---------------------------------------------------------------------------

class CodexPage(QWidget):
    def __init__(self, pet):
        super().__init__()
        self.pet = pet
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.info = make_label("", "sub", wrap=True)
        lay.addWidget(self.info)
        self.rows = {}
        for key, label in (("five_hour", "5 小时窗口"), ("seven_day", "每周额度")):
            box, col = make_box()
            col.addWidget(make_label(label, "h"))
            value = make_label("等待 Codex 用量记录", "mid")
            col.addWidget(value)
            bar = Bar(9)
            col.addWidget(bar)
            note = make_label("", "tiny", wrap=True)
            col.addWidget(note)
            lay.addWidget(box)
            self.rows[key] = value, bar, note
        box, col = make_box()
        col.addWidget(make_label("当前 Codex 会话用量", "h"))
        self.tokens = make_label("", "sub", wrap=True)
        col.addWidget(self.tokens)
        lay.addWidget(box)
        lay.addWidget(make_label("额度来自 Codex 自己保存的本机记录，工作时自动更新。"
                                "暂停使用后数字可能较旧，下一次 Codex 请求会刷新。\n"
                                "这里显示实际 token 数，不把订阅额度换算成美元。", "tiny", wrap=True))
        lay.addStretch(1)

    def refresh(self):
        hub = self.pet.codex
        plan = (hub.quota or {}).get('plan')
        self.info.setText(('Codex · ' + plan.capitalize() if plan else 'Codex') + '\n' +
                          (hub.error or ('本机记录 · ' + fmt_ago((hub.quota or {}).get('fetched_at')) + '更新')))
        for key, (value, bar, note) in self.rows.items():
            w = hub.window(key)
            if not w:
                value.setText('等待 Codex 用量记录')
                bar.set(0, MUTED)
                note.setText('在 Codex 里发送一条消息后，会自动读到用量。')
                continue
            remain = max(0., 100. - w['used'])
            value.setText(f"剩 {remain:.0f}% · 已用 {w['used']:.0f}%")
            bar.set(remain / 100., remain_rgb(remain))
            reset = w.get('resets_at')
            text = f'{fmt_clock(reset)} 重置' if reset else '没有重置时间'
            if w.get('expired'):
                text += ' · 已过重置时间，等待 Codex 新数据'
            elif reset:
                text += f' · 还有 {fmt_span(reset - time.time())}'
            note.setText(text)
        lines = []
        for s in self.pet.tracker.ordered():
            if s.provider != 'codex':
                continue
            title = '会话' if self.pet.privacy else clip(s.name(), 22)
            total = s.tokens.get('total_tokens') or 0
            cached = s.tokens.get('cached_input_tokens') or 0
            lines.append(f'{title}\n累计 {fmt_tokens(total)} tokens · 缓存命中 {fmt_tokens(cached)}'
                         f' · 本轮 {fmt_tokens(s.turn_tokens)}')
        self.tokens.setText('\n\n'.join(lines) or '当前没有活跃的 Codex 会话。')


class CodexStatsPage(QWidget):
    RANGES = (("r1", "今天"), ("r7", "近 7 天"), ("r30", "近 30 天"))

    def __init__(self, hub, pet):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Minimum)
        self.hub, self.pet = hub, pet
        self.range='r1'
        self._key=None
        lay=QVBoxLayout(self)
        lay.setContentsMargins(0,0,0,0)
        lay.setSpacing(10)
        row=QHBoxLayout()
        self.group=QButtonGroup(self)
        for i,(_,text) in enumerate(self.RANGES):
            b=QPushButton(text)
            b.setObjectName('tab')
            b.setCheckable(True)
            b.setChecked(i==0)
            self.group.addButton(b,i)
            row.addWidget(b)
        row.addStretch(1)
        self.group.idClicked.connect(self._pick)
        lay.addLayout(row)
        self.loading=make_label('正在整理 Codex 本机用量记录…','sub',wrap=True)
        lay.addWidget(self.loading)
        self.head,col=make_box()
        col.addWidget(make_label('Codex 实际 token 用量','sub'))
        self.total=RollLabel(fmt_tokens,'big')
        col.addWidget(self.total)
        self.details=make_label('','sub',wrap=True)
        self.details.setMinimumHeight(48)
        col.addWidget(self.details)
        lay.addWidget(self.head)
        self.chart_box,col=make_box()
        self.chart_title=make_label('','h')
        col.addWidget(self.chart_title)
        self.chart=BarChart(lambda n:fmt_tokens(n)+' tokens')
        col.addWidget(self.chart)
        self.chart_box.setMinimumHeight(165)
        lay.addWidget(self.chart_box)
        self.models,self.models_lay=make_box()
        lay.addWidget(self.models)
        self.sessions,self.sessions_lay=make_box()
        lay.addWidget(self.sessions)
        lay.addWidget(make_label('只统计 Codex 本机记录（含子任务），不混入 Claude。输入已包含缓存读取，输出已包含思考 token；不把这些子项重复加到总数。','tiny',wrap=True))
        lay.addStretch(1)

    def _pick(self,idx):
        self.range=self.RANGES[idx][0]
        self._key=None
        self.refresh()

    def refresh(self):
        snap=self.hub.local or {}
        ready=bool(snap.get('ready'))
        self.loading.setVisible(not ready)
        for w in (self.head,self.chart_box,self.models,self.sessions):
            w.setVisible(ready)
        if not ready:
            return
        key=(snap.get('at'),self.range,self.pet.privacy)
        if key==self._key:
            return
        self._key=key
        s=snap.get(self.range) or {}
        self.total.set_value(s.get('tokens',0))
        self.details.setText(f"{s.get('msgs',0)} 次响应 · {s.get('nsessions',0)} 个用量会话\n"
                             f"输入 {fmt_tokens(s.get('in',0))} · 输出 {fmt_tokens(s.get('out',0))}\n"
                             f"其中缓存 {fmt_tokens(s.get('cached',0))} · 思考 {fmt_tokens(s.get('reasoning',0))}")
        if self.range=='r1':
            values=s.get('hours') or [0]*24
            self.chart_title.setText('今天每小时（tokens）')
            self.chart.set_data(values,[str(h) for h in range(24)],
                [f'{h}:00–{h}:59 · {fmt_tokens(v)} tokens' for h,v in enumerate(values)],
                {0,6,12,18,23},time.localtime().tm_hour)
        else:
            series=s.get('series') or []
            n=len(series)
            ticks=set(range(n)) if n<=7 else {i for i in range(n) if (n-1-i)%5==0}
            self.chart_title.setText('每天（tokens）')
            self.chart.set_data([v for _,v in series],[d[5:].replace('-','/') for d,_ in series],
                                 [f'{d} · {fmt_tokens(v)} tokens' for d,v in series],ticks,n-1)
        clear_layout(self.models_lay)
        self.models_lay.addWidget(make_label('按模型','h'))
        for i,(model,tokens,count) in enumerate(s.get('models') or []):
            self.models_lay.addWidget(make_label(f'{model} · {fmt_tokens(tokens)} tokens · {count} 次','sub',wrap=True))
            bar=Bar(5)
            bar.set(tokens/max(s.get('tokens',0),1),MODEL_RGB[i%len(MODEL_RGB)])
            self.models_lay.addWidget(bar)
        clear_layout(self.sessions_lay)
        self.sessions_lay.addWidget(make_label('用量最多的会话','h'))
        for _,title,tokens,count in s.get('sessions') or []:
            title='Codex 会话' if self.pet.privacy else clip(title,25)
            self.sessions_lay.addWidget(make_label(f'{title}\n{fmt_tokens(tokens)} tokens · {count} 次响应','sub',wrap=True))


class SystemPage(QWidget):
    ROWS = (("cpu", "CPU"), ("mem", "内存"), ("gpu", "显卡"), ("vram", "显存"), ("disk", "系统盘"), ("battery", "电池"))

    def __init__(self, info):
        super().__init__()
        self.info = info
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.wait = make_label("正在读取……", "sub")
        lay.addWidget(self.wait)
        self.rows = {}
        for key, title in self.ROWS:
            box, b = make_box()
            top = QHBoxLayout()
            top.addWidget(make_label(title, "h"))
            top.addStretch(1)
            val = make_label("", "sub")
            top.addWidget(val)
            b.addLayout(top)
            bar = Bar(8)
            b.addWidget(bar)
            note = make_label("", "tiny", wrap=True)
            note.hide()
            b.addWidget(note)
            box.hide()
            self.rows[key] = (box, val, bar, note)
            lay.addWidget(box)
        self.net_box, b = make_box()
        b.addWidget(make_label("网络", "h"))
        self.net = make_label("", "sub")
        b.addWidget(self.net)
        self.net_box.hide()
        lay.addWidget(self.net_box)
        self.misc = make_label("", "tiny", wrap=True)
        lay.addWidget(self.misc)
        lay.addStretch(1)

    def _set(self, key, pct, text, note="", rgb=None):
        box, val, bar, lab = self.rows[key]
        if pct is None:
            box.hide()
            return
        box.show()
        val.setText(text)
        bar.set(pct / 100.0, rgb or load_rgb(pct))
        lab.setText(note)
        lab.setVisible(bool(note))

    def refresh(self):
        d = self.info.data or {}
        self.wait.setVisible(not d)
        if not d:
            return
        cpu = d.get("cpu")
        self._set("cpu", cpu, f"{cpu:.0f}%" if cpu is not None else "")
        mem = d.get("mem")
        self._set("mem", mem[2] if mem else None, f"{fmt_bytes(mem[0])} / {fmt_bytes(mem[1])}" if mem else "")
        gpu = d.get("gpu")
        if gpu:
            self._set("gpu", gpu["util"], f"{gpu['util']:.0f}% · {gpu['temp']:.0f}°C", gpu["name"])
            self._set("vram", 100.0 * gpu["mem_used"] / max(1.0, gpu["mem_total"]),
                      f"{gpu['mem_used'] / 1024:.1f} / {gpu['mem_total'] / 1024:.1f} GB")
        else:
            self._set("gpu", None, "")
            self._set("vram", None, "")
        disk = d.get("disk")
        self._set("disk", disk[2] if disk else None,
                  f"{disk[3]} 用了 {fmt_bytes(disk[0])} / {fmt_bytes(disk[1])}" if disk else "")
        bat = d.get("battery")
        if bat:
            pct, plugged, secs = bat
            note = f"大约还能用 {fmt_span(secs)}" if (not plugged and secs and 0 < secs < 10 ** 7) else ""
            self._set("battery", pct, f"{pct:.0f}%" + ("，在充电" if plugged else ""), note, remain_rgb(pct))
        else:
            self._set("battery", None, "")
        net = d.get("net")
        self.net_box.setVisible(net is not None)
        if net:
            self.net.setText(f"↑ {fmt_bytes(net[0])}/s      ↓ {fmt_bytes(net[1])}/s")
        boot = d.get("boot")
        self.misc.setText(f"这次开机 {fmt_span(time.time() - boot)} 了" if boot else "")


# ---------------------------------------------------------------------------
# 面板本体
# ---------------------------------------------------------------------------

class UsagePanel(QWidget):
    def __init__(self, hub, pet):
        super().__init__(None)
        self.hub = hub
        self.pet = pet
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("Claude + Codex 用量")
        self.setStyleSheet(QSS)
        self.info = SysInfo(cpu_fallback=lambda: getattr(pet.ambient, "cpu_percent", None))
        self.provider=str(pet.settings.value('panel_provider','claude'))
        if self.provider not in ('claude','codex'):
            self.provider='claude'
        self.sections={p:str(pet.settings.value('panel_section_'+p,'quota')) for p in ('claude','codex')}
        self.section='quota'

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        card = QFrame()
        card.setObjectName("card")
        outer.addWidget(card)
        self.card = card          # 阴影在 paintEvent 里自己画：阴影特效套在滚动区外面会把背景画成黑的
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 12, 10, 12)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(make_label("Claude + Codex", "title"))
        self.plan = make_label("", "chip")
        self.plan.hide()
        top.addWidget(self.plan)
        top.addStretch(1)
        self.system_button=QPushButton('电脑')
        self.system_button.setObjectName('tab')
        self.system_button.setCheckable(True)
        self.system_button.clicked.connect(self.show_system)
        top.addWidget(self.system_button)
        top.addWidget(self._icon_button("↻", "马上刷新额度", self._refresh_now))
        top.addWidget(self._icon_button("×", "关掉（Esc）", self.hide))
        lay.addLayout(top)

        providers=QHBoxLayout()
        providers.setSpacing(8)
        self.providers=QButtonGroup(self)
        for i,(provider,text) in enumerate((('claude','Claude'),('codex','Codex'))):
            b=QPushButton(text)
            b.setObjectName('providerTab')
            b.setProperty('provider',provider)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            self.providers.addButton(b,i)
            providers.addWidget(b,1)
        self.providers.idClicked.connect(lambda i:self.select_provider(('claude','codex')[i]))
        lay.addLayout(providers)

        tabs = QHBoxLayout()
        tabs.setSpacing(2)
        self.tabs = QButtonGroup(self)
        for i, text in enumerate(TABS):
            btn = QPushButton(text)
            btn.setObjectName("tab")
            btn.setCheckable(True)
            btn.setChecked(i == 0)
            btn.setCursor(Qt.PointingHandCursor)
            self.tabs.addButton(btn, i)
            tabs.addWidget(btn)
        self.tabs.idClicked.connect(self._switch)
        lay.addLayout(tabs)

        self.stack = QStackedWidget()
        self.page_keys=[('claude','quota'),('claude','sessions'),('claude','stats'),
                        ('codex','quota'),('codex','sessions'),('codex','stats'),('system','system')]
        self.pages = [QuotaPage(hub),SessionsPage(pet,'claude'),StatsPage(hub,pet),
                      CodexPage(pet),SessionsPage(pet,'codex'),CodexStatsPage(pet.codex,pet),SystemPage(self.info)]
        for page in self.pages:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.viewport().setAutoFillBackground(False)
            page.setAutoFillBackground(False)
            holder = QWidget()
            holder.setObjectName("holder")
            hl = QVBoxLayout(holder)
            hl.setContentsMargins(0, 0, 6, 0)
            hl.addWidget(page)
            scroll.setWidget(holder)
            # setWidget 会强制给内容涂底色，透明窗口里就成了一块黑，这里关掉
            holder.setAutoFillBackground(False)
            scroll.viewport().setAutoFillBackground(False)
            scroll.viewport().setStyleSheet("background: transparent;")
            self.stack.addWidget(scroll)
        lay.addWidget(self.stack, 1)

        self.status = make_label("", "tiny", wrap=True)
        lay.addWidget(self.status)
        self.resize(404, 640)
        self._drag = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        hub.quota_changed.connect(self._on_data)
        hub.local_changed.connect(self._on_data)
        pet.codex.quota_changed.connect(self._on_data)
        pet.codex.stats_changed.connect(self._on_data)
        self.select_provider(self.provider)

    def _icon_button(self, text, tip, slot):
        btn = QPushButton(text)
        btn.setObjectName("icon")
        btn.setToolTip(tip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(lambda _=False: slot())
        return btn

    def _refresh_now(self):
        (self.pet.codex if self.provider=='codex' else self.hub).refresh_quota(force=True)
        self.status.setText("正在刷新……")

    def _switch(self, idx):
        self.navigate(self.provider,('quota','sessions','stats')[idx])

    def select_provider(self,provider):
        section=self.sections.get(provider,'quota')
        self.navigate(provider,section if section in ('quota','sessions','stats') else 'quota')

    def navigate(self,provider,section):
        if (provider,section) not in self.page_keys:
            return
        self.provider,self.section=provider,section
        self.sections[provider]=section
        self.pet.settings.setValue('panel_provider',provider)
        self.pet.settings.setValue('panel_section_'+provider,section)
        self.providers.button(0 if provider=='claude' else 1).setChecked(True)
        self.tabs.button(('quota','sessions','stats').index(section)).setChecked(True)
        self.system_button.setChecked(False)
        self.stack.setCurrentIndex(self.page_keys.index((provider,section)))
        self.info.set_active(False)
        self.refresh()

    def show_system(self):
        self.section='system'
        self.tabs.setExclusive(False)
        for b in self.tabs.buttons():
            b.setChecked(False)
        self.tabs.setExclusive(True)
        self.stack.setCurrentIndex(self.page_keys.index(('system','system')))
        self.system_button.setChecked(True)
        self.info.set_active(self.isVisible())
        self.refresh()

    def show_tab(self, idx):
        # Preserve the public CLI/mini shortcuts from earlier releases.
        routes={0:('claude','quota'),1:('claude','stats'),2:('claude','sessions'),
                4:('codex','quota'),5:('codex','sessions'),6:('codex','stats')}
        if idx==3:
            self.show_system()
        elif idx in routes:
            self.navigate(*routes[idx])

    def _on_data(self):
        if self.isVisible():
            self.refresh()

    def refresh(self):
        if not self.isVisible():
            return
        q = (self.pet.codex.quota if self.provider == 'codex' else self.hub.quota) or {}
        plan = str(q.get("plan") or "").strip()
        self.plan.setText(plan.capitalize())
        self.plan.setVisible(bool(plan) and self.section!='system')
        self.pages[self.stack.currentIndex()].refresh()
        self.status.setText(self._status_text())

    def _status_text(self):
        if self.section=='system':
            return '电脑状态 · Claude 和 Codex 共用'
        if self.provider == 'codex':
            local=self.pet.codex.local or {}
            return self.pet.codex.error or ('仅 Codex 数据 · '+fmt_ago(local.get('at'))+'整理')
        bits = []
        q = self.hub.quota
        if q:
            src = "命令行状态栏" if q.get("source") == "statusline" else "Anthropic"
            bits.append(f"额度来自 {src}，{fmt_ago(q.get('fetched_at'))}更新")
        if self.hub.quota_error:
            bits.append(self.hub.quota_error)
        loc = self.hub.local or {}
        if loc.get("ready"):
            bits.append(f"本机记录 {fmt_ago(loc.get('at'))}扫过")
        return " · ".join(bits)

    def show_near(self, rect):
        screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        w, h = self.width(), self.height()
        x = rect.left() - w + 30
        if x < area.left():
            x = rect.right() - 30
        x = max(area.left(), min(x, area.right() - w))
        y = max(area.top(), min(rect.bottom() - h + 20, area.bottom() - h))
        self.move(int(x), int(y))
        self.show()
        self.raise_()
        self.activateWindow()
        self.info.set_active(self.section == 'system')
        self.refresh()

    def hideEvent(self, event):
        self.info.set_active(False)
        super().hideEvent(event)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        r = QRectF(self.card.geometry())
        for i in range(12, 0, -1):
            p.setBrush(QColor(214, 92, 140, 4))
            p.drawRoundedRect(r.adjusted(-i, -i + 4, i, i + 4), 16 + i, 16 + i)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and event.position().y() < 64:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            self.move(event.globalPosition().toPoint() - self._drag)
            event.accept()

    def mouseReleaseEvent(self, _event):
        self._drag = None

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)
