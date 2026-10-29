#!/usr/bin/env bash
# P7（Slice 2.3）黑箱 A/B（08 Task 18 Step 3；規格 §15.9.1）。
#
# 用法：scripts/rust/run_p7_ab.sh [--run-id ID] [--out DIR] [--rust-cli PATH] [--rust-daemon PATH]
#   先跑 Python 基線（A）並確認其 daemon 全數結束，再以新的 FakeTarget 實例跑 Rust（B）；兩輪 junit 交給
#   scripts/rust/p7_ab_report.py 逐案判定並寫出 docs/rust-refactor/evidence/p7-ab-<YYMMDD>.md（--out 可改）。
#   結束碼＝報告結束碼（PASS 0、FAIL 1、INCOMPLETE 3、參數錯誤 2）。
# 未承接（下一輪）：先前切片必要回歸（slice21／slice22 黑箱、l15 slice21）與 func-test 兩案的併列。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="$(date +%y%m%d-%H%M%S)"
OUT=""
RUST_CLI="$ROOT/target/debug/serialwrap"
RUST_D="$ROOT/target/debug/serialwrapd"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id) RUN_ID="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --rust-cli) RUST_CLI="$2"; shift 2 ;;
    --rust-daemon) RUST_D="$2"; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "run_p7_ab.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done
[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "run_p7_ab.sh: --run-id 只允許英數與 ._-" >&2; exit 2; }
RUN_DIR="$ROOT/target/p7-ab/$RUN_ID"
mkdir -p "$RUN_DIR"
[[ -n "$OUT" ]] || OUT="$ROOT/docs/rust-refactor/evidence/p7-ab-$(date +%y%m%d).md"
mkdir -p "$(dirname "$OUT")"
cd "$ROOT"
TESTS=(tests/contract_blackbox/test_p7_*.py)

# 必要節點清單（含參數化）：同一 commit 的 collect-only。
python3 -m pytest -q -p no:cacheprovider --collect-only "${TESTS[@]}" >"$RUN_DIR/collected.txt" 2>&1 || true
# A：Python 基線
set +e
env -u SERIALWRAP_DAEMON_EXE -u SERIALWRAP_CLI_EXE -u SERIALWRAP_AB_LABEL SW_AB_PIDLOG="$RUN_DIR/a.pids" \
  python3 -m pytest -q -p no:cacheprovider "${TESTS[@]}" --junitxml="$RUN_DIR/a.xml" >"$RUN_DIR/a.log" 2>&1
RC_A=$?
set -e
# A 的 daemon 必須全數結束（PTY 釋放）後才開始 B。
if [[ -f "$RUN_DIR/a.pids" ]]; then
  python3 - "$RUN_DIR/a.pids" <<'PY'
import json, os, sys, time
pids = [json.loads(l)["pid"] for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
deadline = time.monotonic() + 30
alive = pids
while alive and time.monotonic() < deadline:
    alive = [p for p in pids if os.path.exists(f"/proc/{p}") and open(f"/proc/{p}/stat").read().split(")")[-1].split()[0] != "Z"]
    time.sleep(0.2)
if alive:
    sys.exit(f"A 輪 daemon 未結束：{alive}")
PY
fi
# B：Rust CLI＋daemon
set +e
SERIALWRAP_DAEMON_EXE="$RUST_D" SERIALWRAP_CLI_EXE="$RUST_CLI" SERIALWRAP_AB_LABEL=rust-p7 \
  python3 -m pytest -q -p no:cacheprovider "${TESTS[@]}" --junitxml="$RUN_DIR/b.xml" >"$RUN_DIR/b.log" 2>&1
RC_B=$?
set -e
exec python3 scripts/rust/p7_ab_report.py --run-id "$RUN_ID" --junit-a "$RUN_DIR/a.xml" --junit-b "$RUN_DIR/b.xml" \
  --collected "$RUN_DIR/collected.txt" --rc-a "$RC_A" --rc-b "$RC_B" \
  --rust-cli "$RUST_CLI" --rust-daemon "$RUST_D" --out "$OUT"
