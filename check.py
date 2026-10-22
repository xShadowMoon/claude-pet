# -*- coding: utf-8 -*-
"""桌宠自检。

一路查下来，哪一步断了会直接告诉你怎么修。
用法：双击 check.bat，或者 python check.py
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MARKER = "claude-pet-notify"

# Windows 控制台有时不是 UTF-8，先掰正，免得中文报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


OK = "[ OK ]"
BAD = "[ 问题 ]"
WARN = "[ 注意 ]"

problems = []


def report(good, title, detail="", fix=""):
    print(f"{OK if good else BAD} {title}")
    if detail:
        print(f"        {detail}")
    if not good:
        if fix:
            print(f"        怎么修：{fix}")
        problems.append(title)
    return good


def note(title, detail=""):
    print(f"{WARN} {title}")
    if detail:
        print(f"        {detail}")


def section(name):
    print()
    print(f"--- {name} ---")


# ---------------------------------------------------------------------------
def check_python():
    section("1. 运行环境")
    v = sys.version_info
    report(v >= (3, 9), f"Python：{v.major}.{v.minor}.{v.micro}",
           fix="装 Python 3.9 或更新的版本，安装时勾上 Add Python to PATH")
    try:
        import PySide6
        report(True, f"PySide6：{PySide6.__version__}")
    except ImportError:
        report(False, "PySide6",
               fix="双击 install.bat，或者跑 pip install PySide6")
    try:
        import psutil
        report(True, f"psutil：{psutil.__version__}")
    except ImportError:
        note("没装 psutil", "用量面板的“电脑”那页会少一些数据。双击 install.bat 就会装上")
    report(shutil.which("curl") is not None, "curl",
           detail=shutil.which("curl") or "",
           fix="Windows 10 1803 以后自带 curl，太老的系统去 curl.se 下一个")


def check_assets():
    section("2. 素材")
    folder = os.path.join(APP_DIR, "assets")
    gifs = sorted(f for f in os.listdir(folder)) if os.path.isdir(folder) else []
    report(len(gifs) >= 9, f"素材：{len(gifs)} 个动作文件",
           detail="、".join(gifs),
           fix="assets 文件夹不完整，重新解压一次压缩包")


def read_port():
    """从上次保存的设置里读端口，读不到就用默认的。"""
    try:
        from PySide6.QtCore import QSettings
        return int(QSettings("ClaudePet", "ClaudePet").value("port", 47828))
    except Exception:
        return 47828


def check_hooks(port):
    section("3. Claude Code 的 hook")
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    if not report(os.path.exists(path), "settings.json", detail=path,
                  fix="先双击 install-hooks.bat"):
        return

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as err:
        report(False, "settings.json 格式", detail=str(err),
               fix="这个文件的 JSON 格式坏了，用记事本打开检查一下")
        return

    text = json.dumps(data, ensure_ascii=False)
    count = text.count(MARKER) + text.count("claude-pet-ask")
    report(count > 0, f"桌宠 hook：装了 {count} 条",
           fix="双击 install-hooks.bat，看它有没有打印“装了 16 条 hook”")
    if count and '"hook",' not in text:
        note("hook 还是旧版的", "重新跑一次 install-hooks.bat，才能用上多会话、额度、在桌宠上审批这些新功能")
    elif count and "claude-pet-ask" not in text:
        note("没装审批 hook", "想在桌宠上点“允许”的话，重新跑一次 install-hooks.bat")

    # 路径对不对
    script = os.path.join(
        APP_DIR,
        "claude-pet-notify.cmd" if sys.platform.startswith("win") else "claude-pet-notify.sh",
    )
    report(os.path.exists(script), "通知脚本", detail=script,
           fix="别删这个文件")
    if count and script.replace("\\", "\\\\") not in text and script not in text:
        note("hook 里写的路径和现在这个文件夹对不上",
             "你大概是装完 hook 之后挪过文件夹。重新跑一次 install-hooks.bat 就好。")

    # 端口对不对
    if count and f"{port}" not in text:
        note(f"hook 里的端口和桌宠现在用的端口（{port}）对不上",
             f"重新跑 python install_hooks.py --port {port}")


def http_get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def check_running(port):
    section("4. 桌宠在不在听")
    url = f"http://127.0.0.1:{port}/ping"
    try:
        data = http_get(url)
        alive = bool(data.get("ok"))
    except Exception as err:
        alive = False
        detail = str(err)
    else:
        detail = json.dumps(data, ensure_ascii=False)

    if not report(alive, f"桌宠端口 {port}", detail=detail,
                  fix="先双击 run.bat 把桌宠打开，再跑一次这个检查"):
        return False

    try:
        st = http_get(f"http://127.0.0.1:{port}/status")
        print(f"        她现在的状态：{st.get('state')}（常驻 {st.get('base')}）")
    except Exception:
        pass
    return True


def check_curl_path(port):
    """走一遍 Claude Code 真正会走的那条路。"""
    section("5. 通知脚本能不能打通")
    script = os.path.join(
        APP_DIR,
        "claude-pet-notify.cmd" if sys.platform.startswith("win") else "claude-pet-notify.sh",
    )
    if not os.path.exists(script):
        report(False, "通知脚本", fix="重新解压压缩包")
        return

    if sys.platform.startswith("win"):
        cmd = ["cmd", "/c", "call", script, "wave", str(port)]
    else:
        cmd = ["bash", script, "wave", str(port)]

    try:
        proc = subprocess.run(cmd, input=b"{}", capture_output=True, timeout=10)
        code = proc.returncode
    except Exception as err:
        report(False, "通知脚本调用", detail=str(err))
        return

    report(code == 0, f"通知脚本调用：退出码 {code}",
           fix="退出码不是 0 的话，Claude Code 会报错。把上面的信息发给我")
    print()
    print("        >>> 现在看一眼桌宠，她应该挥了下手并且冒了个气泡。")
    print("        >>> 挥了，就说明整条链路都通了。")


def check_usage(port):
    section("6. 额度和会话")
    try:
        u = http_get(f"http://127.0.0.1:{port}/usage")
    except Exception as err:
        report(False, "额度数据", detail=str(err))
        return
    lines = u.get("lines") or []
    report(bool(lines), "额度数据", detail=" / ".join(lines[:3]) or "还没拿到",
           fix="等几秒再跑一次；还不行就打开 data/pet.log 看看报了什么错")
    try:
        s = http_get(f"http://127.0.0.1:{port}/sessions")
        print(f"        她现在看到 {len(s.get('sessions') or [])} 个 Claude Code 会话")
    except Exception:
        pass


def main():
    print("=" * 56)
    print("  Claude 桌宠自检")
    print("=" * 56)

    check_python()
    check_assets()
    port = read_port()
    check_hooks(port)
    alive = check_running(port)
    if alive:
        check_curl_path(port)
        check_usage(port)

    section("结论")
    if problems:
        print(f"有 {len(problems)} 项没过：")
        for item in problems:
            print(f"  - {item}")
        print()
        print("按上面每项写的“怎么修”处理一下，然后再跑一次。")
    else:
        print("全部通过。")
        print()
        print("最后一步只能你自己看：新开一个 Claude Code 会话，")
        print("让它跑条命令，看她会不会跟着换表情。")
        print("改过 hook 之后 Claude Code 一般会自己读到；万一没反应，重开一个会话就好。")
    print()


if __name__ == "__main__":
    main()
