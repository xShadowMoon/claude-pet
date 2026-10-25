# -*- coding: utf-8 -*-
"""追踪各会话状态并优先展示待确认任务。

每个 Claude Code 会话单独记一份：在干什么、这一轮干了多久、改了哪些文件、
任务清单做到第几项、最后说了什么、是不是在等你。
桌宠的表情跟着最需要你的那个会话走：等你确认 > 在干活 > 闲着。
"""

import os
import time
from urllib.parse import urlparse

READ_TOOLS = {"Read", "Grep", "Glob", "LS", "NotebookRead"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell", "BashOutput", "KillShell", "KillBash"}
AGENT_TOOLS = {"Agent", "Task", "Workflow"}
PLAN_TOOLS = {"TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet"}

PRIORITY = {"waiting": 5, "error": 4, "compacting": 3, "working": 3, "thinking": 3, "idle": 1}
STATE_RGB = {
    "waiting": (238, 168, 58),
    "error": (230, 88, 92),
    "thinking": (160, 120, 230),
    "compacting": (160, 120, 230),
    "working": (90, 150, 240),
    "done": (82, 190, 130),
    "idle": (190, 180, 186),
}
STATE_TEXT = {"waiting": "等你", "error": "出错了", "thinking": "在想", "working": "干活中",
              "compacting": "整理记忆", "idle": "闲着"}
PET_KINDS = ("thinking", "running", "working", "reading", "web", "agent")

# 多久没动静就当它已经停了（秒）。跑命令和派帮手可能本来就很久。
STALE_S = {"running": 1200, "agent": 1200}
STALE_DEFAULT = 600

# 隐私模式下用的笼统说法
GENERIC = {"running": "在跑命令", "working": "在干活", "reading": "在翻代码", "web": "在查资料",
           "agent": "叫了个帮手", "thinking": "在想…", "waiting": "等你确认"}


def clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def short_path(path):
    path = str(path or "")
    return os.path.basename(path.rstrip("\\/")) or path


def project_of(cwd):
    cwd = str(cwd or "").rstrip("\\/")
    if not cwd:
        return ""
    norm = cwd.replace("\\", "/")
    marker = "/.claude/worktrees/"
    if marker in norm:
        repo, _, rest = norm.partition(marker)
        return f"{os.path.basename(repo)}·{rest.split('/')[0]}"
    return os.path.basename(cwd) or cwd


def source_of(chain):
    """从进程链判断会话开在哪：桌面版 / VS Code / 终端。"""
    names = [str(exe).lower() for _, exe in chain]
    try:
        engine = names.index("claude.exe")
    except ValueError:
        return ""
    above = names[engine + 1:]
    if "claude.exe" in above:
        return "桌面版"
    for exe, label in (("code.exe", "VS Code"), ("cursor.exe", "Cursor"), ("windowsterminal.exe", "终端")):
        if exe in above:
            return label
    return "终端"


def fmt_elapsed(seconds):
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def describe_tool(name, inp):
    """把一次工具调用说成一句人话。返回 (动作, 一句话, 细节)。"""
    inp = inp if isinstance(inp, dict) else {}
    if name in SHELL_TOOLS:
        cmd = inp.get("command") or ""
        desc = inp.get("description") or ""
        return "running", f"跑命令：{clip(desc or cmd, 26)}", str(cmd)
    if name in EDIT_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        verb = "写" if name == "Write" else "改"
        return "working", f"{verb} {clip(short_path(path), 28)}", str(path)
    if name in READ_TOOLS:
        if name == "Read":
            path = inp.get("file_path") or ""
            return "reading", f"看 {clip(short_path(path), 28)}", str(path)
        target = inp.get("pattern") or inp.get("path") or ""
        return "reading", f"搜 {clip(target, 26)}", str(target)
    if name == "WebSearch":
        return "web", f"搜网页：{clip(inp.get('query'), 22)}", str(inp.get("query") or "")
    if name == "WebFetch":
        url = str(inp.get("url") or "")
        return "web", f"看网页：{clip(urlparse(url).netloc or url, 24)}", url
    if name in AGENT_TOOLS:
        what = inp.get("description") or inp.get("subagent_type") or ""
        return "agent", f"派帮手：{clip(what, 22)}", str(inp.get("prompt") or "")
    if name in PLAN_TOOLS:
        return "working", "排一下任务清单", ""
    if name == "AskUserQuestion":
        qs = inp.get("questions") or []
        q = qs[0].get("question") if qs and isinstance(qs[0], dict) else ""
        return "waiting", f"有事问你：{clip(q, 22)}", str(q or "")
    if name == "ExitPlanMode":
        return "waiting", "计划写好了，等你批准", str(inp.get("plan") or "")
    if name == "Skill":
        return "working", f"用技能 {clip(inp.get('skill') or inp.get('name'), 20)}", ""
    if name.startswith("mcp__"):
        parts = name.split("__")
        server = parts[1] if len(parts) > 2 else ""
        return "working", f"用 {clip(server, 14)} 的 {clip(parts[-1], 16)}", ""
    return "working", f"用 {clip(name, 22)}", ""


class Session:
    def __init__(self, sid):
        now = time.time()
        self.id = sid
        self.provider = "claude"
        self.tokens = {}
        self.turn_tokens = 0
        self.cwd = ""
        self.project = ""
        self.title = ""
        self.source = ""
        self.state = "idle"
        self.kind = "idle"
        self.activity = ""
        self.detail = ""
        self.tool = ""
        self.started_at = now
        self.last_event_at = now
        self.turn_started_at = None
        self.last_turn_seconds = None
        self.turns = 0
        self.tools = 0
        self.work_seconds = 0.0
        self.last_message = ""
        self.last_prompt = ""
        self.changed = set()
        self.todo_done = 0
        self.todo_total = 0
        self.tasks_done = 0
        self.tasks_total = 0
        self.subagents = 0
        self.error = ""
        self.waiting_for = ""
        self.unseen = False
        self.pids = []
        self.window_title = ""
        self.model = ""
        self.context_pct = None
        self.cost = None
        self.turn_cost = None
        self.transcript = ""
        self.mode = ""

    def name(self):
        return self.title or self.project or self.id[:8]

    def elapsed(self):
        return time.time() - self.turn_started_at if self.turn_started_at else 0.0

    def todo_text(self):
        if self.tasks_total:
            return f"任务 {min(self.tasks_done, self.tasks_total)}/{self.tasks_total}"
        if self.todo_total:
            return f"任务 {self.todo_done}/{self.todo_total}"
        return ""

    def status_text(self):
        if self.state in ("thinking", "working", "compacting"):
            return f"{STATE_TEXT[self.state]} {fmt_elapsed(self.elapsed())}" if self.turn_started_at else STATE_TEXT[self.state]
        if self.state == "idle" and self.unseen:
            return "做完了"
        return STATE_TEXT.get(self.state, self.state)

    def color_key(self):
        if self.state == "idle" and self.unseen:
            return "done"
        return self.state

    def activity_text(self, privacy=False):
        if self.state == "waiting":
            return GENERIC["waiting"] if privacy else (self.waiting_for or "等你确认")
        if self.state == "error":
            return "出错了" if privacy else (self.error or "出错了")
        if privacy:
            return GENERIC.get(self.kind, "") if self.state != "idle" else ""
        return self.activity

    def facts(self):
        parts = []
        if self.model:
            parts.append(self.model)
        if self.provider == "codex" and self.tokens.get("total_tokens"):
            from .common import fmt_tokens
            parts.append(f"累计 {fmt_tokens(self.tokens['total_tokens'])} tokens")
        if self.changed:
            parts.append(f"改了 {len(self.changed)} 个文件")
        todo = self.todo_text()
        if todo:
            parts.append(todo)
        if self.subagents:
            parts.append(f"{self.subagents} 个帮手在干活")
        if self.context_pct is not None:
            parts.append(f"上下文 {self.context_pct:.0f}%")
        if self.turn_cost is not None:
            parts.append(f"上一轮 ≈${self.turn_cost:.2f}")
        if self.turns:
            parts.append(f"{self.turns} 轮")
        if self.source:
            parts.append(self.source)
        return " · ".join(parts)

    def to_dict(self, privacy=False):
        return {
            "id": self.id, "name": self.name(), "provider": self.provider,
            "state": self.state, "status": self.status_text(),
            "activity": self.activity_text(privacy), "facts": self.facts(), "source": self.source,
            "unseen": self.unseen, "last_message": "" if privacy else self.last_message,
            "window": self.window_title, "turns": self.turns, "turn_cost": self.turn_cost,
            "tokens": self.tokens, "turn_tokens": self.turn_tokens,
        }


class SessionTracker:
    def __init__(self):
        self.sessions = {}

    # ---------------- 吃事件 ----------------

    def apply(self, p, now=None):
        """吃一条 hook 事件，返回给桌宠的反应：
        {"event": 事件名, "session": 会话, "bubble": 详细说法, "generic": 笼统说法, "quiet": 不用换表情}
        不需要任何反应就返回 None。
        """
        now = now or time.time()
        ev = p.get("hook_event_name") or ""
        sid = p.get("session_id") or ""
        if not ev or not sid:
            return None
        s = self.sessions.get(sid)
        if ev == "SessionEnd":
            if s is None:
                return None
            self.sessions.pop(sid, None)
            if p.get("reason") == "clear":
                return {"event": "session_end", "session": s, "quiet": True}
            return {"event": "session_end", "session": s}
        if s is None:
            s = self.sessions[sid] = Session(sid)
        s.last_event_at = now
        self._absorb(s, p)
        handler = getattr(self, "_on_" + ev, None)
        return handler(s, p, now) if handler else None

    def _absorb(self, s, p):
        cwd = p.get("cwd")
        if cwd and cwd != s.cwd:
            s.cwd = cwd
            s.project = project_of(cwd)
        if p.get("transcript_path"):
            s.transcript = p["transcript_path"]
        if p.get("permission_mode"):
            s.mode = p["permission_mode"]
        if p.get("session_title"):
            s.title = clip(p["session_title"], 40)
        chain = p.get("_pids")
        if chain:
            s.pids = chain
            s.source = source_of(chain) or s.source

    def _on_SessionStart(self, s, p, now):
        if p.get("model"):
            s.model = str(p["model"])
        if (p.get("source") or "startup") in ("startup", "resume", "fork"):
            return {"event": "session_start", "session": s}
        return None

    def _on_UserPromptSubmit(self, s, p, now):
        s.state = "thinking"
        s.kind = "thinking"
        s.turn_started_at = now
        s.last_prompt = clip(p.get("prompt"), 80)
        s.activity = "在想…"
        s.detail = ""
        s.error = ""
        s.waiting_for = ""
        s.unseen = False
        s.turn_cost = None
        return {"event": "prompt", "session": s}

    def _on_PreToolUse(self, s, p, now):
        name = p.get("tool_name") or ""
        inp = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        kind, text, detail = describe_tool(name, inp)
        s.tools += 1
        s.tool = name
        if s.turn_started_at is None:
            s.turn_started_at = now
        if name in EDIT_TOOLS:
            path = inp.get("file_path") or inp.get("notebook_path")
            if path:
                s.changed.add(str(path))
        if name == "TodoWrite":
            todos = [t for t in (inp.get("todos") or []) if isinstance(t, dict)]
            s.todo_total = len(todos)
            s.todo_done = sum(1 for t in todos if t.get("status") == "completed")
        s.activity = text
        s.detail = detail
        if kind == "waiting":
            s.state = "waiting"
            s.kind = "waiting"
            s.waiting_for = text
            event = "question" if name == "AskUserQuestion" else "plan_review"
            return {"event": event, "session": s, "bubble": text, "generic": GENERIC["waiting"]}
        s.state = "working"
        s.kind = kind
        s.waiting_for = ""
        if kind == "running":
            event = "tool_bash"
        elif kind == "reading":
            event = "tool_read"
        elif kind == "web":
            event = "tool_web"
        elif kind == "agent":
            event = "tool_agent"
        elif name in EDIT_TOOLS:
            event = "tool_edit"
        elif name in PLAN_TOOLS:
            event = "tool_plan"
        else:
            event = "tool_other"
        return {"event": event, "session": s, "bubble": text, "generic": GENERIC.get(kind)}

    def _on_PostToolUse(self, s, p, now):
        if s.state == "waiting":
            s.state = "working"
            s.kind = "thinking"
            s.waiting_for = ""
        return {"event": "tool_done", "session": s, "quiet": True}

    def _on_PostToolUseFailure(self, s, p, now):
        if s.state == "waiting":
            s.state = "working"
            s.kind = "thinking"
            s.waiting_for = ""
        if p.get("is_interrupt"):
            return {"event": "tool_done", "session": s, "quiet": True}
        err = str(p.get("error") or "").strip().split("\n")[0]
        s.error = clip(err, 80)
        name = p.get("tool_name") or ""
        if name in SHELL_TOOLS:
            text = f"命令没跑通：{clip(err, 22)}" if err else "命令没跑通"
        else:
            text = f"{clip(name, 20)} 出错了"
        return {"event": "tool_failed", "session": s, "bubble": text, "generic": "呃…报错了"}

    def _on_PermissionRequest(self, s, p, now):
        name = p.get("tool_name") or ""
        inp = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        _, text, _ = describe_tool(name, inp)
        s.state = "waiting"
        s.kind = "waiting"
        s.waiting_for = text
        if s.turn_started_at is None:
            s.turn_started_at = now
        return {"event": "waiting", "session": s, "bubble": f"等你点允许：{text}", "generic": "等你点允许！"}

    def _on_Notification(self, s, p, now):
        nt = p.get("notification_type") or ""
        if nt == "permission_prompt":
            s.state = "waiting"
            s.kind = "waiting"
            return {"event": "waiting_nudge", "session": s}
        if nt in ("elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"):
            s.state = "waiting"
            s.kind = "waiting"
            s.waiting_for = {"elicitation_dialog": "MCP 要你填个表",
                             "elicitation_url_dialog": "MCP 要你打开个网页",
                             "agent_needs_input": "后台会话在等你回话"}[nt]
            return {"event": "waiting_nudge", "session": s, "bubble": s.waiting_for, "generic": "等你一下"}
        if nt == "idle_prompt":
            return {"event": "idle_prompt", "session": s}
        if nt == "quota_auto_resume_fired":
            return {"event": "quota_resume", "session": s}
        return None

    def _on_SubagentStart(self, s, p, now):
        agent = p.get("agent_type") or ""
        if not agent:
            return None         # Claude Code 自己的内部小助手，不算
        s.subagents += 1
        if s.state != "waiting":
            s.state = "working"
            s.kind = "agent"
        s.activity = f"帮手 {clip(agent, 16)} 出发了"
        return {"event": "subagent_start", "session": s, "bubble": f"叫了个帮手：{clip(agent, 16)}",
                "generic": "叫了个帮手"}

    def _on_SubagentStop(self, s, p, now):
        if not (p.get("agent_type") or ""):
            return None
        s.subagents = max(0, s.subagents - 1)
        if s.state == "working" and s.kind == "agent" and not s.subagents:
            s.kind = "thinking"
        return {"event": "subagent_stop", "session": s}

    def _on_PreCompact(self, s, p, now):
        s.state = "compacting"
        s.kind = "thinking"
        s.activity = "整理上下文…"
        return {"event": "compact", "session": s}

    def _on_PostCompact(self, s, p, now):
        if s.state == "compacting":
            s.state = "thinking" if s.turn_started_at else "idle"
        return {"event": "tool_done", "session": s, "quiet": True}

    def _on_Stop(self, s, p, now):
        secs = (now - s.turn_started_at) if s.turn_started_at else None
        if secs:
            s.work_seconds += secs
            s.last_turn_seconds = secs
        s.turns += 1
        s.turn_started_at = None
        s.state = "idle"
        s.kind = "idle"
        s.waiting_for = ""
        s.unseen = True
        msg = str(p.get("last_assistant_message") or "").strip()
        if msg:
            s.last_message = clip(msg.replace("\r", "").split("\n")[0], 160)
        bg = p.get("background_tasks") or []
        s.activity = f"做完了，后台还有 {len(bg)} 个任务" if bg else "做完了"
        return {"event": "done", "session": s, "message": msg, "seconds": secs}

    def _on_StopFailure(self, s, p, now):
        err = p.get("error") or "unknown"
        s.turn_started_at = None
        s.state = "error"
        s.kind = "idle"
        s.error = clip(p.get("last_assistant_message") or err, 80)
        s.activity = "这轮出错停下了"
        return {"event": "api_error", "session": s, "error": err}

    def _on_TaskCreated(self, s, p, now):
        s.tasks_total += 1
        return {"event": "tool_done", "session": s, "quiet": True}

    def _on_TaskCompleted(self, s, p, now):
        s.tasks_done += 1
        subject = clip(p.get("task_subject"), 22)
        return {"event": "task_done", "session": s, "bubble": f"✓ {subject}" if subject else "完成一项",
                "generic": "完成一项"}

    # ---------------- 汇总 ----------------

    def waiting(self):
        return [s for s in self.sessions.values() if s.state == "waiting"]

    def busy(self):
        return [s for s in self.sessions.values() if s.state in ("thinking", "working", "compacting")]

    def display(self):
        """桌宠该摆哪个常驻表情。没有会话在忙就返回 None。"""
        if self.waiting():
            return "waiting"
        busy = self.busy()
        if not busy:
            return None
        s = max(busy, key=lambda x: x.last_event_at)
        if s.state == "compacting":
            return "thinking"
        return s.kind if s.kind in PET_KINDS else "working"

    def attention(self):
        """点桌宠的时候该把哪个会话叫出来：先找等你的，再找做完了还没看的。"""
        waiting = sorted(self.waiting(), key=lambda s: -s.last_event_at)
        if waiting:
            return waiting[0]
        done = sorted((s for s in self.sessions.values() if s.unseen), key=lambda s: -s.last_event_at)
        return done[0] if done else None

    def tray_key(self):
        keys = {s.color_key() for s in self.sessions.values()}
        for key in ("waiting", "error", "working", "thinking", "compacting", "done"):
            if key in keys:
                return "working" if key in ("thinking", "compacting") else key
        return None

    def ordered(self):
        return sorted(self.sessions.values(), key=lambda s: (-PRIORITY.get(s.state, 0), -s.last_event_at))

    def cleanup(self, idle_limit_s, now=None):
        """长时间没动静的会话：在干活的当作已经停了；闲着太久的就不显示了。"""
        now = now or time.time()
        changed = False
        for sid, s in list(self.sessions.items()):
            quiet = now - s.last_event_at
            if s.state in ("thinking", "working", "compacting") and quiet > STALE_S.get(s.kind, STALE_DEFAULT):
                s.state = "idle"
                s.kind = "idle"
                s.turn_started_at = None
                s.activity = "好一会儿没动静了"
                changed = True
            elif s.state == "waiting" and quiet > 1800:
                s.state = "idle"
                s.kind = "idle"
                changed = True
            elif s.state == "error" and quiet > 90:
                s.state = "idle"
                changed = True
            if s.state == "idle" and idle_limit_s and quiet > idle_limit_s:
                del self.sessions[sid]
                changed = True
        return changed

    def snapshot(self, privacy=False):
        return [s.to_dict(privacy) for s in self.ordered()]
