# -*- coding: utf-8 -*-
"""命令行遥控器。

不管你在用 chat、Cowork、还是在剪视频，敲一句就能让桌宠换状态。
任何能跑命令的东西都能调它，脚本、快捷方式、别的程序都行。

用法：
    pet.bat focus               开始专注，计时器一起启动
    pet.bat rest                休息
    pet.bat working             在干活
    pet.bat thinking            在想事情
    pet.bat done                完成，庆祝一下
    pet.bat done "编译过了"      完成，并且让她说这句话
    pet.bat failed              出错了
    pet.bat waiting             等你确认
    pet.bat wave                挥个手，用来确认她还活着
    pet.bat idle                回到待机
    pet.bat say "你好"           让她说一句话
    pet.bat run <命令>           帮你跑一条命令：跑完庆祝、失败了晕一下，还告诉你花了多久
    pet.bat usage               看额度还剩多少、今天用了多少
    pet.bat sessions            看每个 Claude / Codex 会话在干什么
    pet.bat panel [页]           打开面板：额度 / 统计 / 会话 / 电脑 / codex
    pet.bat mini [tasks]        打开迷你聊天窗，或迷你任务页
    pet.bat widget <名字> <值> [秒]   在面板里放个小组件，比如 pet.bat widget 构建 通过 600
    pet.bat status              打印她现在的状态
    pet.bat                     列出所有能用的词
"""

import json
import subprocess
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_PORT = 47828

# 顺手的说法 -> 桌宠认识的事件名
ALIASES = {
    "focus": "focus", "专注": "focus", "start": "focus",
    "rest": "rest", "休息": "rest", "break": "rest",
    "working": "tool_edit", "干活": "tool_edit", "work": "tool_edit",
    "running": "tool_bash", "跑命令": "tool_bash",
    "thinking": "prompt", "想": "prompt", "think": "prompt",
    "reading": "tool_read", "看": "tool_read",
    "web": "tool_web", "查": "tool_web",
    "done": "done", "完成": "done", "ok": "done",
    "failed": "tool_failed", "报错": "tool_failed", "error": "tool_failed",
    "waiting": "waiting", "等": "waiting",
    "wave": "wave", "挥手": "wave", "hi": "wave",
    "idle": "wake", "待机": "wake", "wake": "wake",
    "away": "away", "back": "back",
}
SPECIAL = ["say", "run", "usage", "sessions", "panel", "widget", "status", "mini"]

# 这几个是"桌宠自己的"事件，不该被算进 Claude Code 的统计
LOCAL_DONE = {"done": "cmd_done", "tool_failed": "cmd_failed", "tool_bash": "cmd_start"}


def read_port():
    try:
        from PySide6.QtCore import QSettings
        return int(QSettings("ClaudePet", "ClaudePet").value("port", DEFAULT_PORT))
    except Exception:
        return DEFAULT_PORT


def call(path, port, body=None, timeout=3):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def tell(port, event, text):
    """通知桌宠，连不上也不要紧。"""
    try:
        call(f"/e/{event}", port, {"text": text})
        return True
    except Exception:
        return False


def clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def spent(seconds):
    s = int(seconds)
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分 {s % 60} 秒"
    return f"{s // 3600} 小时 {(s % 3600) // 60} 分"


def usage():
    print(__doc__.strip())
    print()
    print("能用的词：")
    print("  " + "  ".join(sorted(ALIASES) + SPECIAL))


def not_running(port, err):
    print(f"连不上桌宠（端口 {port}）：{err}")
    print("先双击 run.bat 把她打开。")
    return 1


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        usage()
        return 0

    word = args[0].strip().lower()
    rest = args[1:]
    port = read_port()

    if word == "status":
        try:
            st = call("/status", port)
        except Exception as err:
            return not_running(port, err)
        print(f"她现在：{st.get('state')}（常驻 {st.get('base')}）")
        return 0

    if word == 'mini':
        try:
            call('/e/mini',port,{'text':' '.join(rest)})
            return 0
        except Exception as err:
            return not_running(port,err)

    if word in ("usage", "额度", "用量"):
        try:
            u = call("/usage", port)
        except Exception as err:
            return not_running(port, err)
        for line in u.get("lines") or ["还没拿到额度数据，等几秒再试"]:
            print(line)
        codex = u.get('codex') or {}
        if codex.get('enabled'):
            print('Codex：' + (codex.get('brief') or '等待用量记录'))
        return 0

    if word in ("sessions", "会话"):
        try:
            data = call("/sessions", port)
        except Exception as err:
            return not_running(port, err)
        sessions = data.get("sessions") or []
        if not sessions:
            print("现在没有 Claude / Codex 会话。")
        for s in sessions:
            provider = 'Codex' if s.get('provider') == 'codex' else 'Claude'
            line = f"● [{provider}] {s.get('name')}：{s.get('status')}"
            if s.get("activity"):
                line += f" · {s['activity']}"
            print(line)
            if s.get("facts"):
                print(f"    {s['facts']}")
        return 0

    if word in ("say", "说"):
        if not rest:
            print("用法：pet.bat say \"想让她说的话\"")
            return 2
        try:
            call("/e/say", port, {"text": " ".join(rest)})
        except Exception as err:
            return not_running(port, err)
        return 0

    if word in ("panel", "面板"):
        try:
            call("/e/panel", port, {"text": rest[0] if rest else ""})
        except Exception as err:
            return not_running(port, err)
        return 0

    if word in ("widget", "小组件"):
        if len(rest) < 2:
            print("用法：pet.bat widget <名字> <值> [多少秒后自动消失]")
            return 2
        body = {"id": rest[0], "title": rest[0], "value": rest[1]}
        if len(rest) > 2:
            try:
                body["ttl"] = float(rest[2])
            except ValueError:
                pass
        try:
            call("/widget", port, body)
        except Exception as err:
            return not_running(port, err)
        print(f"放好了：{rest[0]} = {rest[1]}")
        return 0

    if word in ("run", "跑"):
        if not rest:
            print("用法：pet.bat run <命令>，比如 pet.bat run npm test")
            return 2
        line = " ".join(rest)
        tell(port, "cmd_start", f"在跑：{clip(line, 36)}")
        start = time.time()
        try:
            code = subprocess.call(line, shell=True)
        except KeyboardInterrupt:
            code = 130
        took = spent(time.time() - start)
        if code == 0:
            tell(port, "cmd_done", f"跑完了，用了 {took}：{clip(line, 26)}")
        else:
            tell(port, "cmd_failed", f"失败了（退出码 {code}，{took}）：{clip(line, 22)}")
        return code

    event = ALIASES.get(word)
    if event is None:
        print(f"不认识这个词：{word}")
        print()
        usage()
        return 2

    # 说一句自定义的话：pet.bat done "编译过了"
    body = {}
    if rest:
        body["text"] = " ".join(rest)
    event = LOCAL_DONE.get(event, event) if word in ("done", "完成", "ok", "failed", "报错", "error") else event

    try:
        call(f"/e/{event}", port, body)
    except Exception as err:
        return not_running(port, err)

    print(f"已经让她切到「{word}」了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
