#!/usr/bin/env bash
# socat 接線 spike 腳本（§15.4.1、Task 9）
# 驗證 socat PTY＋EXEC 拓樸之 raw 轉接、binary 保真、no-echo 及子程序 exit 時之 EOF 語意。
# 拓樸可行性檢查，非正式差分證據，亦非 gate（socat 缺席時輸出 NOT_RUN 並 exit 0）。

set -euo pipefail

OUT_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "錯誤：--out 需要路徑參數" >&2
        exit 1
      fi
      OUT_DIR="$2"
      shift 2
      ;;
    *)
      echo "未知參數: $1" >&2
      exit 1
      ;;
  esac
done

if [[ -z "$OUT_DIR" ]]; then
  OUT_DIR="$(mktemp -d -t socat-spike-out.XXXXXX)"
  echo "未指定 --out，輸出目錄設為: $OUT_DIR" >&2
fi
mkdir -p "$OUT_DIR"

# 清理舊報告與日誌，避免重跑或失敗時殘留舊 PASS 結果
rm -f "$OUT_DIR/spike-report.json" "$OUT_DIR/socat.stderr.log"

# 檢查 socat 是否存在，缺少時印出 JSON 並 exit 0（非 gate 失敗）
if ! command -v socat >/dev/null 2>&1; then
  echo '{"result":"NOT_RUN","reason":"socat_missing"}'
  exit 0
fi

SOCAT_VERSION="$(socat -V 2>&1 | grep -E '^socat version' | head -n 1 | awk '{print $3}' || echo "unknown")"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"

T="$(mktemp -d -t socat-spike-pty.XXXXXX)"
PTY_PATH="$T/pty"
REPORT_PATH="$OUT_DIR/spike-report.json"
SOCAT_PID=""

cleanup() {
  if [[ -n "${SOCAT_PID:-}" ]] && kill -0 "$SOCAT_PID" 2>/dev/null; then
    kill "$SOCAT_PID" 2>/dev/null || true
    wait "$SOCAT_PID" 2>/dev/null || true
  fi
  if [[ -n "${T:-}" && -d "$T" ]]; then
    rm -rf "$T"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 步驟 1：啟動 socat 背景行程，stderr 存至 $OUT/socat.stderr.log
socat -d -d PTY,link="$PTY_PATH",raw,echo=0,wait-slave EXEC:"$PYTHON $HERE/echo_child.py",pipes 2>"$OUT_DIR/socat.stderr.log" &
SOCAT_PID=$!

for ((i = 0; i < 100; i++)); do
  if [[ -e "$PTY_PATH" ]]; then
    break
  fi
  if ! kill -0 "$SOCAT_PID" 2>/dev/null; then
    echo "錯誤：socat 程序異常終止，請參閱 $OUT_DIR/socat.stderr.log" >&2
    exit 1
  fi
  sleep 0.05
done

if [[ ! -e "$PTY_PATH" ]]; then
  echo "錯誤：PTY 連結逾時未建立：$PTY_PATH" >&2
  exit 1
fi

# 步驟 2～4：以 inline Python 執行 raw roundtrip、no-echo、EOF 延遲量測並產出報告
"$PYTHON" - "$PTY_PATH" "$REPORT_PATH" "$SOCAT_VERSION" <<'EOF'
from __future__ import annotations

import errno
import json
import os
import select
import sys
import termios
import time
import tty

pty_path = sys.argv[1]
report_path = sys.argv[2]
socat_version = sys.argv[3]

# 開啟 PTY
fd = os.open(pty_path, os.O_RDWR | os.O_NOCTTY)

# 驗證 socat 之 raw,echo=0 設定（在 harness 自身 setraw 之前先檢查 PTY 原始終端機設定）
attrs = termios.tcgetattr(fd)
iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
cfg_ok = not (lflag & (termios.ECHO | termios.ICANON | termios.ISIG)) and not (oflag & termios.OPOST)

# 將測試端 fd 設定為 raw 模式以進行二進位傳輸
tty.setraw(fd)

# 步驟 2：二進位保真測試 (bytes(range(256)) * 4 = 1024 bytes)
payload = bytes(range(256)) * 4
os.write(fd, payload)

received = bytearray()
deadline = time.monotonic() + 10.0
while len(received) < len(payload) and time.monotonic() < deadline:
    r, _, _ = select.select([fd], [], [], 0.5)
    if r:
        chunk = os.read(fd, len(payload) - len(received))
        if not chunk:
            break
        received.extend(chunk)

raw_roundtrip = (received == payload)

# 驗證無本地回顯 (no_echo)：socat raw,echo=0 生效且 50ms 內 PTY 無殘留多餘字元
r, _, _ = select.select([fd], [], [], 0.05)
no_echo = raw_roundtrip and cfg_ok and (not r)

# 步驟 3：寫入 __EXIT__\n 令 echo_child.py 結束，量測 PTY 端收到 EOF / EIO 之延遲
t0 = time.monotonic()
os.write(fd, b"__EXIT__\n")

eof_on_child_exit = False
eof_latency_ms = -1
eof_deadline = time.monotonic() + 10.0
while time.monotonic() < eof_deadline:
    r, _, _ = select.select([fd], [], [], 0.1)
    if not r:
        continue
    try:
        chunk = os.read(fd, 4096)
        if not chunk:
            t1 = time.monotonic()
            eof_on_child_exit = True
            eof_latency_ms = int((t1 - t0) * 1000)
            break
    except OSError as e:
        if e.errno == errno.EIO:
            t1 = time.monotonic()
            eof_on_child_exit = True
            eof_latency_ms = int((t1 - t0) * 1000)
            break
        raise

os.close(fd)

# 步驟 4：輸出 spike-report.json（嚴格禁止包含 verdict 欄位）
report = {
    "checks": {
        "eof_latency_ms": int(eof_latency_ms),
        "eof_on_child_exit": bool(eof_on_child_exit),
        "no_echo": bool(no_echo),
        "raw_roundtrip": bool(raw_roundtrip),
    },
    "note": "拓樸可行性檢查，非差分證據格式（§15.4.1）",
    "schema_version": "socat-spike.v1",
    "socat_version": socat_version,
}

tmp_report_path = report_path + ".tmp"
with open(tmp_report_path, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2, sort_keys=True)
    f.write("\n")
os.replace(tmp_report_path, report_path)
EOF

# 輸出結果至 stdout
cat "$REPORT_PATH"
