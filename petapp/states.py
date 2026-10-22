# -*- coding: utf-8 -*-
"""动作表 + 事件到动作的映射。

想改哪个事件对应哪个表情，直接在这个文件里改，不用动别的代码。
"""

# ---------------------------------------------------------------------------
# 动作表
#   gif      : assets 文件夹里的文件名
#   label    : 右键菜单和托盘上显示的名字
#   bubble   : 气泡里说的话，None 表示不说话
#   hold_ms  : 只有一次性动作才需要，播完这么久就回到常驻状态
# ---------------------------------------------------------------------------

STATES = {
    # --- 常驻状态 ---
    "idle": {
        "gif": "06_idle.gif",
        "label": "待机",
        "bubble": None,
    },
    "sleeping": {
        "gif": "04_smile.gif",
        "label": "打盹",
        "bubble": None,
    },
    "thinking": {
        "gif": "07_shy.gif",
        "label": "思考中",
        "bubble": "在想…",
    },
    "working": {
        "gif": "03_happy.gif",
        "label": "干活中",
        "bubble": "写代码中",
    },
    "running": {
        "gif": "03_happy.gif",
        "label": "跑命令",
        "bubble": "在跑命令",
    },
    "reading": {
        "gif": "07_shy.gif",
        "label": "看代码",
        "bubble": "翻代码中",
    },
    "web": {
        "gif": "04_smile.gif",
        "label": "查资料",
        "bubble": "上网查一下",
    },
    "agent": {
        "gif": "07_shy.gif",
        "label": "带小弟",
        "bubble": "叫了个帮手",
    },
    "waiting": {
        "gif": "05_surprised.gif",
        "label": "等确认",
        "bubble": "等你点允许！",
    },
    "resting": {
        "gif": "04_smile.gif",
        "label": "休息中",
        "bubble": None,
    },

    # --- 移动动作。这两张素材本身就分左右，所以播的时候不做镜像翻转 ---
    "walk_right": {
        "gif": "08_walk_right.gif",
        "label": "向右移动",
        "bubble": None,
        "directional": True,
    },
    "walk_left": {
        "gif": "09_walk_left.gif",
        "label": "向左移动",
        "bubble": None,
        "directional": True,
    },

    # --- 一次性动作，播完自动回去 ---
    "done": {
        "gif": "02_smug.gif",
        "label": "完成",
        "bubble": "搞定啦",
        "hold_ms": 4200,
    },
    "failed": {
        "gif": "01_dizzy.gif",
        "label": "出错了",
        "bubble": "呃…报错了",
        "hold_ms": 3600,
    },
    "greeting": {
        "gif": "03_happy.gif",
        "label": "打招呼",
        "bubble": "开工咯",
        "hold_ms": 3000,
    },
    "bye": {
        "gif": "04_smile.gif",
        "label": "告别",
        "bubble": "下次见",
        "hold_ms": 3000,
    },
    "patted": {
        "gif": "02_smug.gif",
        "label": "被摸头",
        "bubble": "嘿嘿",
        "hold_ms": 2600,
    },
    "wave": {
        "gif": "03_happy.gif",
        "label": "挥手",
        "bubble": "在的~",
        "hold_ms": 2600,
    },
    "held": {
        "gif": "01_dizzy.gif",
        "label": "被拎着",
        "bubble": "晕啦晕啦",
    },
    "landed": {
        "gif": "01_dizzy.gif",
        "label": "摔了",
        "bubble": "好痛…",
        "hold_ms": 1800,
    },
    "compacting": {
        "gif": "01_dizzy.gif",
        "label": "整理记忆",
        "bubble": "脑子在整理…",
        "hold_ms": 5000,
    },
    "alarmed": {
        "gif": "05_surprised.gif",
        "label": "吓一跳",
        "bubble": None,
        "hold_ms": 5000,
    },
    "cheer": {
        "gif": "03_happy.gif",
        "label": "欢呼",
        "bubble": None,
        "hold_ms": 4500,
    },
    "exhausted": {
        "gif": "01_dizzy.gif",
        "label": "额度见底",
        "bubble": None,
        "hold_ms": 6000,
    },
}

DEFAULT_STATE = "idle"

# 移动的时候用哪个动作：1 = 往右，-1 = 往左
WALK_STATES = {1: "walk_right", -1: "walk_left"}

# 右键菜单里能手动选的动作
MENU_STATES = [
    "idle", "sleeping", "thinking", "working", "reading",
    "web", "waiting", "walk_left", "walk_right", "done", "failed", "patted",
]


# ---------------------------------------------------------------------------
# 事件映射
#   state    : 切到哪个动作
#   kind     : base = 常驻（一直保持到下个事件）；once = 播一次就回去
#   bubble   : 覆盖动作自带的气泡文字，None 表示用动作自己的
#   count_as : 统计里算进哪一项
# Claude Code 的事件先经过 sessions.py 翻译成这里的事件名，所以名字和 hook 不完全一样。
# ---------------------------------------------------------------------------

EVENT_MAP = {
    "session_start": {"state": "greeting", "kind": "once", "count_as": "sessions"},
    "session_end":   {"state": "bye", "kind": "once"},
    "prompt":        {"state": "thinking", "kind": "base", "bubble": "收到，我看看", "count_as": "prompts"},

    "tool_bash":     {"state": "running", "kind": "base", "count_as": "tools"},
    "tool_edit":     {"state": "working", "kind": "base", "bubble": "改文件中", "count_as": "edits"},
    "tool_read":     {"state": "reading", "kind": "base", "count_as": "tools"},
    "tool_web":      {"state": "web", "kind": "base", "count_as": "tools"},
    "tool_agent":    {"state": "agent", "kind": "base", "count_as": "tools"},
    "tool_plan":     {"state": "working", "kind": "base", "bubble": "排一下任务", "count_as": "tools"},
    "tool_other":    {"state": "working", "kind": "base", "count_as": "tools"},
    "tool_failed":   {"state": "failed", "kind": "once", "count_as": "errors"},

    "waiting":       {"state": "waiting", "kind": "base", "count_as": "waits"},
    "waiting_nudge": {"state": "waiting", "kind": "base", "bubble": "还在等你点允许！"},
    "question":      {"state": "waiting", "kind": "base", "bubble": "Claude 有事问你", "count_as": "waits"},
    "plan_review":   {"state": "waiting", "kind": "base", "bubble": "计划写好了，看一眼？", "count_as": "waits"},
    "idle_prompt":   {"state": "wave", "kind": "once", "bubble": "还在吗？等你回话呢", "hold_ms": 3000},

    "subagent_start": {"state": "agent", "kind": "base"},
    "subagent_stop":  {"state": "thinking", "kind": "base", "bubble": "帮手回来了"},

    "compact":       {"state": "compacting", "kind": "once"},
    "done":          {"state": "done", "kind": "once", "reset_base": "idle", "count_as": "turns"},
    "api_error":     {"state": "exhausted", "kind": "once", "reset_base": "idle", "count_as": "errors"},
    "quota_resume":  {"state": "cheer", "kind": "once", "bubble": "额度回来了，接着干活"},
    "task_done":     {"state": "patted", "kind": "once", "hold_ms": 1800},

    # 自检用的：戳一下让她挥个手，证明整条链路是通的
    "wave":          {"state": "wave", "kind": "once", "bubble": "收到啦"},

    # ---- 下面这些不靠 Claude Code，命令行、托盘菜单、本地感知都能触发 ----
    "focus":         {"state": "working", "kind": "base", "bubble": "开始专注，加油"},
    "rest":          {"state": "resting", "kind": "base", "bubble": "休息一下"},
    "wake":          {"state": "idle", "kind": "base", "bubble": None},
    "busy":          {"state": "working", "kind": "base", "bubble": "电脑在忙"},
    "calm":          {"state": "idle", "kind": "base", "bubble": None},
    "away":          {"state": "sleeping", "kind": "base", "bubble": None},
    "back":          {"state": "idle", "kind": "base", "bubble": "欢迎回来"},
    "stretch":       {"state": "wave", "kind": "once",
                      "bubble": "坐了快一个钟头了，起来走两步吧", "hold_ms": 6000},
    "late":          {"state": "sleeping", "kind": "once",
                      "bubble": "已经很晚了，早点睡", "hold_ms": 6000},
    "cmd_start":     {"state": "running", "kind": "base", "bubble": "在跑命令"},
    "cmd_done":      {"state": "done", "kind": "once", "reset_base": "idle", "bubble": "跑完了"},
    "cmd_failed":    {"state": "failed", "kind": "once", "reset_base": "idle", "bubble": "命令失败了"},
}

# 这些事件是你自己或者本地感知触发的，不算 Claude Code 的动静
LOCAL_EVENTS = {"focus", "rest", "wake", "busy", "calm", "away", "back",
                "stretch", "late", "wave", "cmd_start", "cmd_done", "cmd_failed"}

# 本地感知的信号 -> 事件名
AMBIENT_MAP = {
    "cpu_busy": "busy",
    "cpu_calm": "calm",
    "user_away": "away",
    "user_back": "back",
    "long_sit": "stretch",
    "late_night": "late",
}
