#!/usr/bin/env bash
# 桌宠通知器（macOS / Linux）。Claude Code 的 hook 会调它，把事件原样转给桌宠。
#   用法: claude-pet-notify.sh <事件名> [端口] [wake]
# 第三个参数是 wake 的时候，桌宠没开就顺手把她叫起来（data/no-autowake 存在就不叫）。
# 不管成功失败都返回 0。
EVT="$1"
PORT="${2:-47828}"
DIR="$(cd "$(dirname "$0")" && pwd)"
if ! curl -s -m 2 --noproxy 127.0.0.1 -X POST -H "Content-Type: application/json" --data-binary @- \
  -o /dev/null "http://127.0.0.1:${PORT}/e/${EVT}?src=claude-pet" >/dev/null 2>&1; then
  if [ "$3" = "wake" ] && [ ! -e "$DIR/data/no-autowake" ]; then
    (cd "$DIR" && nohup python3 main.py >/dev/null 2>&1 &)
  fi
fi
exit 0
