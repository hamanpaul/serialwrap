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
  echo "PTY bridge backend=socat link=$pty_link tcp=$host:$tcp_port" >&2
  exec socat -d -d \
    "PTY,link=$pty_link,rawer,echo=0,wait-slave" \
    "TCP4:$host:$tcp_port,retry=1,interval=1,forever"
fi

echo "PTY bridge backend=python link=$pty_link tcp=$host:$tcp_port" >&2
exec python3 "$script_dir/pty_tcp_bridge.py" \
  --pty-link "$pty_link" --host "$host" --port "$tcp_port"
