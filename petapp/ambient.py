# -*- coding: utf-8 -*-
"""感知电脑空闲、CPU 忙碌并触发健康提醒。

不依赖任何 AI，只看两件事：你有多久没碰电脑了，以及 CPU 忙不忙。
桌宠靠这两个信号自己活着，chat、Cowork、打游戏、写文档都一样有反应。

全部用 ctypes 直接问系统，不需要额外装库。拿不到就返回 None，
上层看到 None 就当这个信号不存在，不会报错。
"""

import ctypes
import os
import sys
import time

IS_WIN = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")


# ---------------------------------------------------------------------------
# 你多久没动鼠标键盘了
# ---------------------------------------------------------------------------

if IS_WIN:
    class _LastInput(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds():
    """返回距离上次敲键盘或动鼠标过了多少秒。拿不到就返回 None。"""
    if not IS_WIN:
        return None
    try:
        info = _LastInput()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = ctypes.windll.kernel32.GetTickCount()
        delta = (now - info.dwTime) & 0xFFFFFFFF     # 计数器会回绕，掩一下
        return delta / 1000.0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# CPU 忙不忙
# ---------------------------------------------------------------------------

class CpuMeter:
    """两次采样之间的 CPU 占用百分比。"""

    def __init__(self):
        self._prev = None

    def _sample(self):
        if IS_WIN:
            try:
                idle = ctypes.c_ulonglong()
                kernel = ctypes.c_ulonglong()
                user = ctypes.c_ulonglong()
                ok = ctypes.windll.kernel32.GetSystemTimes(
                    ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
                )
                if not ok:
                    return None
                # kernel 里已经包含了 idle
                return idle.value, kernel.value + user.value
            except Exception:
                return None

        if IS_LINUX:
            try:
                with open("/proc/stat", "r") as fh:
                    parts = fh.readline().split()
                nums = [int(x) for x in parts[1:]]
                total = sum(nums)
                idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
                return idle, total
            except Exception:
                return None

        return None

    def percent(self):
        cur = self._sample()
        if cur is None:
            return None
        if self._prev is None:
            self._prev = cur
            return None
        d_idle = cur[0] - self._prev[0]
        d_total = cur[1] - self._prev[1]
        self._prev = cur
        if d_total <= 0:
            return None
        busy = 1.0 - (d_idle / d_total)
        return max(0.0, min(100.0, busy * 100.0))


# ---------------------------------------------------------------------------
# 把两个信号整理成事件
# ---------------------------------------------------------------------------

BUSY_ON = 72.0          # CPU 高过这个并且持续一会儿，就算电脑在忙
BUSY_OFF = 45.0         # 掉到这个以下才算闲下来
BUSY_HOLD_S = 12.0      # 要连续这么久才认

AWAY_S = 240.0          # 四分钟没碰电脑就算人走开了
LONG_SIT_S = 50 * 60    # 连续用这么久提醒一下
SIT_BREAK_S = 5 * 60    # 中间离开超过这么久就重新计时
SIT_COOLDOWN_S = 60 * 60
LATE_HOUR = 23          # 几点之后算深夜


class Ambient:
    """每秒问一次系统，攒够条件就吐一个事件出来。"""

    def __init__(self):
        self.cpu = CpuMeter()
        self.cpu_percent = None
        self.busy = False
        self._busy_since = None
        self._calm_since = None

        self.away = False
        self._sit_start = None
        self._away_start = None
        self._last_sit_warn = 0.0
        self._last_late_note = 0.0

        self._last_poll = 0.0

    def poll(self, now=None):
        """返回这一轮发生的事件列表。没事就返回空列表。"""
        now = now or time.time()
        if now - self._last_poll < 1.0:
            return []
        self._last_poll = now

        events = []
        events += self._poll_cpu(now)
        events += self._poll_presence(now)
        return events

    # ---------------- CPU ----------------

    def _poll_cpu(self, now):
        pct = self.cpu.percent()
        if pct is None:
            return []
        self.cpu_percent = pct

        if pct >= BUSY_ON:
            self._calm_since = None
            if self._busy_since is None:
                self._busy_since = now
            elif not self.busy and now - self._busy_since >= BUSY_HOLD_S:
                self.busy = True
                return ["cpu_busy"]
        elif pct <= BUSY_OFF:
            self._busy_since = None
            if self._calm_since is None:
                self._calm_since = now
            elif self.busy and now - self._calm_since >= BUSY_HOLD_S:
                self.busy = False
                return ["cpu_calm"]
        return []

    # ---------------- 人在不在 ----------------

    def _poll_presence(self, now):
        idle = idle_seconds()
        if idle is None:
            return []

        events = []
        away_now = idle >= AWAY_S

        if away_now and not self.away:
            self.away = True
            self._away_start = now - idle
            events.append("user_away")
        elif not away_now and self.away:
            self.away = False
            gone = now - (self._away_start or now)
            if gone >= SIT_BREAK_S:
                self._sit_start = now          # 休息够了，重新计时
            events.append("user_back")

        if not away_now:
            if self._sit_start is None:
                self._sit_start = now
            sat = now - self._sit_start
            if (sat >= LONG_SIT_S
                    and now - self._last_sit_warn >= SIT_COOLDOWN_S):
                self._last_sit_warn = now
                events.append("long_sit")

            hour = time.localtime(now).tm_hour
            if hour >= LATE_HOUR or hour < 4:
                if now - self._last_late_note >= 3 * 3600:
                    self._last_late_note = now
                    events.append("late_night")

        return events

    def sitting_minutes(self):
        if self._sit_start is None or self.away:
            return 0
        return int((time.time() - self._sit_start) / 60)
