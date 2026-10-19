#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
host="${HERDR_PORTAL_HOST:-127.0.0.1}"
port="${HERDR_PORTAL_PORT:-8787}"
url="http://${host}:${port}/"
runtime="${HERDR_PORTAL_RUNTIME:-$HOME/.local/state/herdr-portal}"
if ! python3 "$root/board/daemon.py" start; then
  echo "看板服务启动失败。"
  echo "日志: $runtime/server.log"
  tail -n 20 "$runtime/server.log" 2>/dev/null || true
  exit 1
fi

# /api/* 需要 token：读出来带进 URL（前端读一次就存 sessionStorage、把地址栏里的抹掉）
token=""
if [ -r "$runtime/token" ]; then
  IFS= read -r token < "$runtime/token" || token=""
fi
open_url="$url"
if [ -n "$token" ]; then
  open_url="${url}?token=${token}"
fi

if [ -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]; then
  # 优先复用已打开的门户标签页，避免每次重复开新标签（减少内存占用）
  if osascript -e "
tell application \"Google Chrome\"
  set found to false
  repeat with w in windows
    repeat with t in tabs of w
      if URL of t starts with \"${url}\" then
        set active tab index of w to index of t
        set index of w to 1
        set found to true
        exit repeat
      end if
    end repeat
    if found then exit repeat
  end repeat
  if not found then
    open location \"${open_url}\"
  end if
  activate
end tell" >/dev/null 2>&1; then
    :
  else
    open "$url"
  fi
elif command -v open >/dev/null 2>&1; then
  open "$url"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$url"
fi

cat <<EOF
HERDR 全局看板已打开

  $url

服务在后台运行，刷新间隔 1.5s。
EOF
if [ -t 0 ]; then
  echo "按任意键关闭这个提示窗（看板网页会继续开着）。"
  read -rsn1 || true
fi
