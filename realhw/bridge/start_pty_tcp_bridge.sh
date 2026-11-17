#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "用法：$0 <isolated-pty-link> [tcp-port]" >&2
  exit 2
fi

pty_link=$1
tcp_port=${2:-48701}
host=127.0.0.1
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ "$pty_link" == *,* ]]; then
  echo "PTY symlink 路徑不得含逗號：$pty_link" >&2
  exit 2
fi
if [[ ! "$tcp_port" =~ ^[0-9]+$ ]] || (( tcp_port < 1 || tcp_port > 65535 )); then
  echo "TCP port 必須介於 1 到 65535：$tcp_port" >&2
  exit 2
fi
if [[ -e "$pty_link" || -L "$pty_link" ]]; then
  echo "PTY symlink 已存在，拒絕覆寫：$pty_link" >&2
  exit 2
fi
mkdir -p -- "$(dirname -- "$pty_link")"

if command -v socat >/dev/null 2>&1; then
  echo "已找到 socat ($(command -v socat))；其 TCP client 在已建立連線被中斷後會退出並移除 PTY，改用可重連且保留同一 PTY 的 Python bridge。" >&2
else
  echo "找不到 socat，使用 Python PTY/TCP bridge。" >&2
fi
exec python3 "$script_dir/pty_tcp_bridge.py" \
  --pty-link "$pty_link" --host "$host" --port "$tcp_port"
