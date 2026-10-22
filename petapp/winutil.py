# -*- coding: utf-8 -*-
"""Windows 上的几个系统小工具，全部用 ctypes 直接问系统：

- 从本地端口反查是哪个进程连过来的（hook 里的 curl）
- 顺着父进程一路往上找，找到那个会话所在的终端 / VS Code / 桌面版窗口
- 把窗口叫到前台
- 单实例锁，免得开出两只

别的平台上这些函数什么都不做，返回空值。
"""

import ctypes
import sys
from ctypes import wintypes

IS_WIN = sys.platform.startswith("win")
_KEEP = []

if IS_WIN:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    iphlpapi = ctypes.WinDLL("iphlpapi")

    INVALID_HANDLE = ctypes.c_void_p(-1).value
    TH32CS_SNAPPROCESS = 0x2
    AF_INET = 2
    TCP_TABLE_OWNER_PID_ALL = 5
    ERROR_INSUFFICIENT_BUFFER = 122
    ERROR_ALREADY_EXISTS = 183

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = [
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD
    iphlpapi.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                                             wintypes.ULONG, ctypes.c_int, wintypes.ULONG]

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindow.argtypes = [wintypes.HWND, ctypes.c_uint]
    user32.GetWindow.restype = wintypes.HWND
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ctypes.c_size_t]


def _swap16(port):
    return ((port & 0xFF) << 8) | ((port >> 8) & 0xFF)


def process_table():
    """{pid: (父进程 pid, exe 名)}"""
    if not IS_WIN:
        return {}
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return {}
    out = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def pid_for_local_port(port, server_port=None):
    """哪个进程用本地端口 port 连着我们。找不到返回 None。"""
    if not IS_WIN or not port:
        return None
    try:
        size = wintypes.DWORD(0)
        iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
        for _ in range(4):
            buf = ctypes.create_string_buffer(size.value + 8192)
            size = wintypes.DWORD(len(buf))
            ret = iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, AF_INET,
                                               TCP_TABLE_OWNER_PID_ALL, 0)
            if ret == 0:
                break
            if ret != ERROR_INSUFFICIENT_BUFFER:
                return None
        else:
            return None
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
        rows = ctypes.cast(ctypes.addressof(buf) + 4, ctypes.POINTER(MIB_TCPROW_OWNER_PID))
        want = _swap16(port)
        remote = _swap16(server_port) if server_port else None
        for i in range(count):
            row = rows[i]
            if (row.dwLocalPort & 0xFFFF) != want:
                continue
            if remote is not None and (row.dwRemotePort & 0xFFFF) != remote:
                continue
            return int(row.dwOwningPid)
    except Exception:
        return None
    return None


def ancestry(pid, table=None, limit=14):
    """[(pid, exe), ...]：从这个进程开始，一路往上到最老的祖先。"""
    if not pid:
        return []
    table = table if table is not None else process_table()
    chain, seen = [], set()
    while pid and pid in table and pid not in seen and len(chain) < limit:
        seen.add(pid)
        parent, exe = table[pid]
        chain.append((int(pid), exe))
        pid = parent
    return chain


def client_chain(client_port, server_port):
    """hook 请求是谁发来的：curl -> cmd -> claude -> 终端 / 桌面版 ……"""
    try:
        return [list(x) for x in ancestry(pid_for_local_port(client_port, server_port))]
    except Exception:
        return []


def top_windows():
    """[(hwnd, pid, 标题)]：所有看得见、有标题的顶层窗口。"""
    if not IS_WIN:
        return []
    out = []

    def cb(hwnd, _lparam):
        try:
            if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):   # 4 = GW_OWNER
                n = user32.GetWindowTextLengthW(hwnd)
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    out.append((hwnd, int(pid.value), buf.value))
        except Exception:
            pass
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return out


def find_window(pids):
    """按顺序（离会话近的优先）找第一个有顶层窗口的进程。返回 (hwnd, 标题)。"""
    if not IS_WIN or not pids:
        return None, ""
    wins = top_windows()
    for pid in pids:
        for hwnd, wpid, title in wins:
            if wpid == pid:
                return hwnd, title
    return None, ""


def session_window(chain):
    """给一条进程链，找出会话所在的窗口。跳过前两个（curl 和 cmd，早就退出了）。"""
    pids = [int(pid) for pid, _ in (chain or [])]
    return find_window(pids[2:] or pids)


def focus_window(hwnd):
    if not IS_WIN or not hwnd:
        return False
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        if user32.SetForegroundWindow(hwnd):
            return True
        # 被"前台锁"拦了：按一下 Alt 再试，这是 Windows 允许切前台的常用办法
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.BringWindowToTop(hwnd)
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False


def single_instance(name="Local\\ClaudePet.SingleInstance"):
    """拿到锁返回 True；已经有一只在跑就返回 False。锁跟着进程走，退出自动释放。"""
    if not IS_WIN:
        return True
    try:
        handle = kernel32.CreateMutexW(None, False, name)
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            return False
        _KEEP.append(handle)
    except Exception:
        pass
    return True
