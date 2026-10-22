# -*- coding: utf-8 -*-
"""命令行版 Claude Code 的状态栏。

Claude Code 每次刷新状态栏，都会把这个会话的数据（模型、上下文用了多少、官方的额度百分比……）
从 stdin 交给这个脚本。这里做两件事：
1. 顺手把数据转给桌宠，她的额度血条就能用上最新的官方数字；
2. 打印一行简短的状态，显示在终端底部。桌宠没开也照常打印。

用法（install_hooks.py 会帮你写进设置）：
    python claude-pet-statusline.py [端口]
"""

import json
import sys
import time
import urllib.request


def main():
    port = 47828
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    raw = sys.stdin.buffer.read()
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        data = {}

    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/statusline", data=raw,
                                     headers={"Content-Type": "application/json"})
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=0.4).read()
    except Exception:
        pass

    parts = []
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(str(model))
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"上下文 {float(ctx):.0f}%")
    limits = data.get("rate_limits") or {}
    five = limits.get("five_hour") or {}
    if five.get("used_percentage") is not None:
        text = f"5h {float(five['used_percentage']):.0f}%"
        left = float(five.get("resets_at") or 0) - time.time()
        if left > 0:
            text += f"（{int(left // 3600)}:{int(left % 3600 // 60):02d} 后重置）"
        parts.append(text)
    week = limits.get("seven_day") or {}
    if week.get("used_percentage") is not None:
        parts.append(f"本周 {float(week['used_percentage']):.0f}%")
    cost = (data.get("cost") or {}).get("total_cost_usd")
    if cost:
        parts.append(f"本会话 ≈${float(cost):.2f}")
    sys.stdout.buffer.write(" · ".join(parts).encode("utf-8"))


if __name__ == "__main__":
    main()
