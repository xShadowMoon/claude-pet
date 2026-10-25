# -*- coding: utf-8 -*-
"""绘制 Claude 和 Codex 双行额度血条。

粗的那根是 5 小时窗口还剩多少，越少越红；细的那根是本周还剩多少。
点一下打开用量面板。
"""

import time

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .common import INK, MUTED, PINK, WEEK, fmt_hm, remain_rgb

W = 222
H = 62


class QuotaHud(QWidget):
    clicked = Signal()

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setCursor(Qt.PointingHandCursor)
        self.resize(W, H)
        self.five = None
        self.five_reset = None
        self.week = None
        self.stale = False
        self.note = "额度 ?"
        self.codex_five = None
        self.codex_reset = None
        self.codex_week = None
        self.codex_stale = False
        self.codex_enabled = True
        self._claude_tip = ""
        self._codex_tip = ""
        self._font = QFont()
        self._font.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI"])
        self._font.setPixelSize(11)
        self._font.setBold(True)
        self._small = QFont(self._font)
        self._small.setBold(False)
        self._small.setPixelSize(10)

    def set_quota(self, five=None, five_reset=None, week=None, stale=False, note="", tip=""):
        self.five = five
        self.five_reset = five_reset
        self.week = week
        self.stale = stale
        self.note = note or "额度 ?"
        self._claude_tip = tip
        self._update_tip()
        self.update()

    def set_codex(self, five=None, five_reset=None, week=None, stale=False, tip="", enabled=True):
        self.codex_five, self.codex_reset, self.codex_week = five, five_reset, week
        self.codex_stale, self.codex_enabled = stale, enabled
        self._codex_tip = tip
        self.resize(W, H if enabled else 32)
        self._update_tip()
        self.update()

    def _update_tip(self):
        self.setToolTip("Claude\n" + self._claude_tip + ("\n\n" + self._codex_tip if self.codex_enabled else ""))

    def set_claude_tip(self, tip):
        self._claude_tip = tip
        self._update_tip()

    def anchor_to(self, cx, bottom_y):
        self.move(int(cx - self.width() / 2), int(bottom_y - self.height()))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        body = QRectF(0.5, 0.5, w - 1, h - 1)
        p.setPen(QPen(QColor(*PINK), 1.2))
        p.setBrush(QColor(255, 253, 254, 232))
        p.drawRoundedRect(body, 9, 9)

        self._draw_row(p, 1, "Claude", self.five, self.five_reset, self.week, self.stale, self.note)
        if self.codex_enabled:
            self._draw_row(p, 31, "Codex", self.codex_five, self.codex_reset,
                           self.codex_week, self.codex_stale, "等待用量记录")

    def _draw_row(self, p, y, label, used, reset, week, stale, note):
        w = self.width()
        p.setFont(self._font)
        p.setPen(QColor(*(MUTED if stale else INK)))
        if used is None:
            p.drawText(QRectF(8, y + 2, w - 16, 20), Qt.AlignLeft | Qt.AlignVCenter, f"{label} · {note}")
            return
        remain = max(0.0, 100.0 - used)
        p.drawText(QRectF(8, y + 2, w - 16, 14), Qt.AlignLeft | Qt.AlignVCenter, f"{label} 5h 剩{remain:.0f}%")
        left = (reset or 0) - time.time()
        p.setFont(self._small)
        p.setPen(QColor(*MUTED))
        suffix = f"{fmt_hm(left)} 重置" if left > 0 else "待更新"
        p.drawText(QRectF(8, y + 2, w - 16, 14), Qt.AlignRight | Qt.AlignVCenter, suffix)
        self._bar(p, QRectF(8, y + 17, w - 16, 5), remain / 100.0, QColor(*remain_rgb(remain)))
        if week is not None:
            self._bar(p, QRectF(8, y + 24, w - 16, 3), max(0.0, 100.0 - week) / 100.0, QColor(*WEEK))

    @staticmethod
    def _bar(p, rect, frac, color):
        rad = rect.height() / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(243, 228, 235))
        p.drawRoundedRect(rect, rad, rad)
        frac = max(0.0, min(1.0, frac))
        if frac > 0:
            p.setBrush(color)
            p.drawRoundedRect(QRectF(rect.x(), rect.y(), max(rect.height(), rect.width() * frac), rect.height()),
                              rad, rad)
