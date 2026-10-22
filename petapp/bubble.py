# -*- coding: utf-8 -*-
"""头顶的气泡。

单独一个透明窗口，鼠标点不到它，只负责显示文字。
主文字下面还能挂一行灰色小字，比如这一轮花了多少。
"""

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

PAD_X = 14
PAD_Y = 9
TAIL_H = 9
RADIUS = 12
MAX_W = 300
MIN_W = 46
SUB_GAP = 6


class Bubble(QWidget):
    def __init__(self, parent=None):
        super().__init__(None)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self.text = ""
        self.lines = []
        self.sub = ""
        self.sub_lines = []
        self._font = QFont()
        self._font.setFamilies(["Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Segoe UI"])
        self._font.setPixelSize(14)
        self._sub_font = QFont(self._font)
        self._sub_font.setPixelSize(12)
        self._line_h = 18
        self._sub_h = 16

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    # ------------------------------------------------------------------
    def say(self, text, duration_ms=4000, sub=None):
        text = (text or "").strip()
        if not text:
            self.dismiss()
            return
        if len(text) > 160:
            text = text[:157] + "…"
        self.text = text
        self.sub = (sub or "").strip()
        self._layout_text()
        self.update()
        self.show()
        self.raise_()
        if duration_ms > 0:
            self._hide_timer.start(duration_ms)
        else:
            self._hide_timer.stop()

    def set_sub(self, sub, extend_ms=0):
        """给正在显示的气泡补一行小字。气泡已经收起来了就返回 False。"""
        if not self.isVisible():
            return False
        self.sub = (sub or "").strip()
        self._layout_text()
        self.update()
        if extend_ms > 0 and self._hide_timer.isActive():
            self._hide_timer.start(self._hide_timer.remainingTime() + extend_ms)
        return True

    def dismiss(self):
        self._hide_timer.stop()
        self.hide()

    # ------------------------------------------------------------------
    @staticmethod
    def _wrap(fm, text, max_w):
        lines = []
        for para in text.split("\n"):
            if not para:
                lines.append("")
                continue
            cur = ""
            for ch in para:
                if fm.horizontalAdvance(cur + ch) > max_w and cur:
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
            lines.append(cur)
        return lines

    def _layout_text(self):
        fm = QFontMetrics(self._font)
        sfm = QFontMetrics(self._sub_font)
        max_text_w = MAX_W - PAD_X * 2

        lines = self._wrap(fm, self.text, max_text_w)
        self.lines = lines[:6]
        if len(lines) > 6:
            self.lines[-1] = self.lines[-1][:-1] + "…"
        sub = self._wrap(sfm, self.sub, max_text_w) if self.sub else []
        self.sub_lines = sub[:2]
        if len(sub) > 2:
            self.sub_lines[-1] = self.sub_lines[-1][:-1] + "…"

        widths = [fm.horizontalAdvance(s) for s in self.lines]
        widths += [sfm.horizontalAdvance(s) for s in self.sub_lines]
        text_w = max(widths or [0])
        self._line_h = fm.height() + 2
        self._sub_h = sfm.height() + 1
        w = max(MIN_W, min(MAX_W, text_w + PAD_X * 2))
        h = self._line_h * len(self.lines) + PAD_Y * 2 + TAIL_H
        if self.sub_lines:
            h += SUB_GAP + self._sub_h * len(self.sub_lines)
        self.resize(int(w), int(h))

    def anchor_to(self, cx, bottom_y):
        """把气泡的尖尖对准 (cx, bottom_y)。"""
        x = int(cx - self.width() / 2)
        y = int(bottom_y - self.height())
        self.move(x, y)

    # ------------------------------------------------------------------
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        w = self.width()
        h = self.height()
        body = QRectF(0.5, 0.5, w - 1, h - TAIL_H - 1)

        path = QPainterPath()
        path.addRoundedRect(body, RADIUS, RADIUS)

        tail = QPainterPath()
        cx = w / 2
        tail.moveTo(cx - 7, body.bottom() - 1)
        tail.lineTo(cx, h - 1)
        tail.lineTo(cx + 7, body.bottom() - 1)
        tail.closeSubpath()
        path = path.united(tail)

        p.setPen(QPen(QColor(236, 170, 196), 1.4))
        p.setBrush(QColor(255, 253, 254, 244))
        p.drawPath(path)

        p.setFont(self._font)
        p.setPen(QColor(72, 56, 66))
        y = PAD_Y
        for line in self.lines:
            p.drawText(QRectF(PAD_X, y, w - PAD_X * 2, self._line_h),
                       Qt.AlignLeft | Qt.AlignVCenter, line)
            y += self._line_h

        if self.sub_lines:
            y += SUB_GAP / 2
            p.setPen(QPen(QColor(244, 214, 226), 1))
            p.drawLine(QPointF(PAD_X, y), QPointF(w - PAD_X, y))
            y += SUB_GAP / 2
            p.setFont(self._sub_font)
            p.setPen(QColor(150, 128, 140))
            for line in self.sub_lines:
                p.drawText(QRectF(PAD_X, y, w - PAD_X * 2, self._sub_h),
                           Qt.AlignLeft | Qt.AlignVCenter, line)
                y += self._sub_h
