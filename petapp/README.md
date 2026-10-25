# 核心模块导航

本目录负责同一只桌宠与 Claude、Codex 的联动。

| 模块 | 用途 |
| --- | --- |
| [__init__.py](__init__.py) | 维护双助手桌宠的包标识和版本号 |
| [ambient.py](ambient.py) | 感知电脑空闲、CPU 忙碌并触发健康提醒 |
| [animator.py](animator.py) | 解码、缩放并复用角色 GIF 动画 |
| [approval.py](approval.py) | 展示 Claude 权限确认、提问和计划审批卡片 |
| [bubble.py](bubble.py) | 绘制角色气泡及每轮用量提示 |
| [chat.py](chat.py) | 连接双助手 Mini 聊天并处理流式回复与审批 |
| [codex.py](codex.py) | 读取 Codex 会话状态、额度和任务变化 |
| [codex_stats.py](codex_stats.py) | 增量汇总 Codex 历史用量并防止重复计数 |
| [common.py](common.py) | 提供共享路径、时间金额格式和主题配色 |
| [hotkey.py](hotkey.py) | 注册 Mini 全局快捷键并处理组合键冲突 |
| [hud.py](hud.py) | 绘制 Claude 和 Codex 双行额度血条 |
| [mini.py](mini.py) | 提供迷你聊天、任务进度、草稿和权限操作 |
| [panel.py](panel.py) | 按助手分组展示额度、会话与历史统计 |
| [server.py](server.py) | 接收本地 hooks、审批请求和遥控事件 |
| [sessions.py](sessions.py) | 追踪各会话状态并优先展示待确认任务 |
| [states.py](states.py) | 定义角色动作和事件到表情的映射 |
| [sysinfo.py](sysinfo.py) | 采集 CPU、内存、显卡、磁盘和网速 |
| [usage.py](usage.py) | 获取 Claude 额度并汇总本机 token 与估算费用 |
| [window.py](window.py) | 协调桌宠渲染、物理交互、悬停血条与双助手 |
| [winutil.py](winutil.py) | 处理 Windows 窗口定位、进程关系和单实例锁 |

窗口入口是 `window.py`；完整面板在 `panel.py`，Mini 小窗在 `mini.py`。角色动作配置见 `states.py`，素材使用边界见上级 [CREDITS.md](../CREDITS.md)。
