# -*- coding: utf-8 -*-
"""接收本地 hooks、审批请求和遥控事件。

Claude Code 的 hook 用 curl 打本地地址，这里收下来，转成 Qt 信号丢给主线程。
只绑 127.0.0.1，外面的机器连不上。

    POST /e/hook          hook 原样转过来的事件（新版 hook 都走这里）
    POST /e/<事件名>       老版本 hook 和命令行遥控用
    POST /permission      要你点"允许"的请求：会一直挂着，等你在桌宠上点了再回
    POST /statusline      命令行版 Claude Code 的状态栏数据（额度、上下文）
    POST /widget          自定义小组件
    GET  /ping            探活
    GET  /status          当前状态
    GET  /usage           额度和用量
    GET  /sessions        各个会话在干什么
"""

import json
import os
import select
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

from PySide6.QtCore import QObject, Signal

from . import winutil

DEFAULT_PORT = 47828
PORT_TRIES = 6

# 主线程写、服务线程读的共享状态
STATUS = {"state": "idle", "base": "idle", "approvals_on": True, "approval_timeout": 120}

# 这些事件顺手查一下是哪个窗口发来的，用来"点桌宠跳到会话窗口"
_CHAIN_EVENTS = {"SessionStart", "UserPromptSubmit"}
_KNOWN_SESSIONS = set()

# 只有带着 CLAUDE_PET_DEBUG=1 启动时才开调试接口，给自动测试用（点卡片按钮、截界面）
DEBUG = os.environ.get("CLAUDE_PET_DEBUG") == "1"


class EventBridge(QObject):
    """跨线程把事件送进 GUI 线程。"""
    received = Signal(str, dict)          # 老接口：事件名, 数据
    hook = Signal(dict)                   # hook 原样数据
    permission = Signal(str, dict)        # 请求 id, hook 数据
    permission_gone = Signal(str, str)    # 请求 id, 原因（gone / timeout）
    statusline = Signal(dict)
    widget = Signal(dict)
    debug = Signal(dict)


BRIDGE = EventBridge()


class Pending:
    """挂着的审批请求。主线程点了按钮就 resolve，服务线程那边就能回给 Claude Code。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}

    def open(self):
        rid = uuid.uuid4().hex[:12]
        item = {"event": threading.Event(), "result": None}
        with self.lock:
            self.items[rid] = item
        return rid, item

    def resolve(self, rid, result):
        with self.lock:
            item = self.items.get(rid)
        if item is not None:
            item["result"] = result
            item["event"].set()

    def close(self, rid):
        with self.lock:
            self.items.pop(rid, None)


PENDING = Pending()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")      # 默认 ensure_ascii，管道里不怕乱码
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _reply_empty(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8", "ignore"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _chain(self):
        return winutil.client_chain(self.client_address[1], self.server.server_address[1])

    def _client_gone(self):
        """curl 那头断了没有（比如你在终端里先点了，Claude Code 把 hook 收掉了）。"""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _handle(self):
        path = urlparse(self.path).path

        if path == "/ping":
            self._reply({"ok": True, "app": "claude-pet"})
            return
        if path == "/status":
            self._reply({"ok": True, "state": STATUS.get("state"), "base": STATUS.get("base"),
                         "geom": STATUS.get("geom")})
            return
        if path == '/mini/status':
            self._reply({'ok':True, **(STATUS.get('mini') or {})})
            return
        if path == "/usage":
            self._reply({"ok": True, **(STATUS.get("usage") or {}),
                         "codex": STATUS.get("codex_usage") or {}})
            return
        if path == "/sessions":
            self._reply({"ok": True, "sessions": STATUS.get("sessions") or []})
            return

        if path == "/permission":
            self._permission()
            return

        if path == "/debug" and DEBUG:
            BRIDGE.debug.emit(dict(parse_qsl(urlparse(self.path).query)))
            self._reply({"ok": True})
            return

        if path == "/statusline":
            data = self._read_body()
            self._reply({"ok": True})
            if data:
                BRIDGE.statusline.emit(data)
            return

        if path == "/widget":
            data = self._read_body()
            if not data:
                self._reply({"ok": False, "error": "empty widget"}, 400)
                return
            BRIDGE.widget.emit(data)
            self._reply({"ok": True})
            return

        if path.startswith("/e/"):
            name = path[3:].strip("/")
            data = self._read_body()
            if not name:
                self._reply({"ok": False, "error": "empty event"}, 400)
                return
            sid = data.get("session_id")
            if data.get("hook_event_name") and sid:
                if data["hook_event_name"] in _CHAIN_EVENTS or sid not in _KNOWN_SESSIONS:
                    data["_pids"] = self._chain()
                    _KNOWN_SESSIONS.add(sid)
            if name == "hook":
                BRIDGE.hook.emit(data)
            else:
                BRIDGE.received.emit(name, data)
            self._reply({"ok": True, "event": name})
            return

        self._reply({"ok": False, "error": "not found"}, 404)

    def _permission(self):
        data = self._read_body()
        timeout = float(STATUS.get("approval_timeout") or 0)
        if not data or not STATUS.get("approvals_on") or timeout <= 0:
            self._reply_empty()             # 不在桌宠上审批：什么都不回，照常在窗口里问你
            return
        data["_pids"] = self._chain()
        rid, item = PENDING.open()
        BRIDGE.permission.emit(rid, data)
        deadline = time.time() + timeout
        gone = False
        while time.time() < deadline:
            if item["event"].wait(0.25):
                break
            if self._client_gone():
                gone = True
                break
        PENDING.close(rid)
        result = item["result"]
        if not result:
            BRIDGE.permission_gone.emit(rid, "gone" if gone else "timeout")
            if not gone:
                self._reply_empty()
            return
        self._reply(result)

    do_GET = _handle
    do_POST = _handle

    def log_message(self, *args, **kwargs):
        # 不要往控制台刷日志
        pass


class EventServer:
    def __init__(self, port=DEFAULT_PORT):
        self.wanted_port = port
        self.port = None
        self.httpd = None
        self.thread = None

    def start(self):
        last_err = None
        for offset in range(PORT_TRIES):
            port = self.wanted_port + offset
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
            except OSError as err:
                last_err = err
                continue
            self.httpd.daemon_threads = True
            self.port = port
            self.thread = threading.Thread(
                target=self.httpd.serve_forever,
                kwargs={"poll_interval": 0.3},
                daemon=True,
            )
            self.thread.start()
            return port
        print("端口都被占了，事件服务没起来：", last_err)
        return None

    def stop(self):
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
