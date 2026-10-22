#!/usr/bin/env bash
# 桌宠审批桥（macOS / Linux）。Claude Code 要你点"允许"的时候会调它：
# 把请求交给桌宠，等你在桌宠上点了按钮，再把结果原样交回给 Claude Code。
# 桌宠没开、等太久、或者你选了"去窗口里处理"，它什么都不输出，Claude Code 就照常问你。
#   用法: claude-pet-ask.sh [端口] [最多等几秒]
PORT="${1:-47828}"
WAIT="${2:-600}"
curl -s -m "$WAIT" --noproxy 127.0.0.1 -X POST -H "Content-Type: application/json" --data-binary @- \
  "http://127.0.0.1:${PORT}/permission" 2>/dev/null
exit 0
