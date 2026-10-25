# -*- coding: utf-8 -*-
"""采集 CPU、内存、显卡、磁盘和网速。

只有用量面板翻到"电脑"那页的时候，才在后台每两秒采一次，平时不占资源。
有 psutil 就用 psutil；没有就退回只看内存和 CPU。显卡数据来自 nvidia-smi，没有就不显示。
"""

import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time

try:
    import psutil
except Exception:          # 没装也能跑，只是看到的少一点
    psutil = None

IS_WIN = sys.platform.startswith("win")
CREATE_NO_WINDOW = 0x08000000


class _MemStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _memory_fallback():
    if not IS_WIN:
        return None
    try:
        st = _MemStatus()
        st.dwLength = ctypes.sizeof(st)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        used = st.ullTotalPhys - st.ullAvailPhys
        return used, st.ullTotalPhys, float(st.dwMemoryLoad)
    except Exception:
        return None


class SysInfo:
    def __init__(self, cpu_fallback=None):
        self.data = {}
        self.active = False
        self._cpu_fallback = cpu_fallback
        self._net = None
        self._thread = None
        self._gpu_tool = shutil.which("nvidia-smi")
        if psutil is not None:
            try:
                psutil.cpu_percent(interval=None)        # 第一次调用只是打个底
            except Exception:
                pass

    def set_active(self, on):
        self.active = bool(on)
        if self.active and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _loop(self):
        while self.active:
            try:
                self.data = self.sample()
            except Exception:
                pass
            time.sleep(2.0)

    def sample(self):
        d = {"at": time.time()}
        if psutil is not None:
            d["cpu"] = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            d["mem"] = (vm.used, vm.total, vm.percent)
            drive = (os.environ.get("SystemDrive") or "C:") + "\\" if IS_WIN else "/"
            du = psutil.disk_usage(drive)
            d["disk"] = (du.used, du.total, du.percent, drive.rstrip("\\"))
            try:
                bat = psutil.sensors_battery()
            except Exception:
                bat = None
            if bat is not None:
                d["battery"] = (bat.percent, bat.power_plugged, bat.secsleft)
            io = psutil.net_io_counters()
            now = time.time()
            if self._net is not None and now > self._net[0]:
                dt = now - self._net[0]
                d["net"] = ((io.bytes_sent - self._net[1]) / dt, (io.bytes_recv - self._net[2]) / dt)
            self._net = (now, io.bytes_sent, io.bytes_recv)
            d["boot"] = psutil.boot_time()
        else:
            mem = _memory_fallback()
            if mem:
                d["mem"] = mem
            if self._cpu_fallback is not None:
                d["cpu"] = self._cpu_fallback()
        if self._gpu_tool:
            gpu = self._gpu()
            if gpu:
                d["gpu"] = gpu
        return d

    def _gpu(self):
        try:
            out = subprocess.run(
                [self._gpu_tool, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
                creationflags=CREATE_NO_WINDOW if IS_WIN else 0,
            )
            first = (out.stdout or "").strip().splitlines()[0]
            name, util, used, total, temp = [x.strip() for x in first.split(",")[:5]]
            return {"name": name, "util": float(util), "mem_used": float(used), "mem_total": float(total),
                    "temp": float(temp)}
        except Exception:
            return None


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
