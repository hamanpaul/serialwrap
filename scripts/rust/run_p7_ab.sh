#!/usr/bin/env bash
# P7（Slice 2.3）黑箱 A/B 與先前切片必要回歸（08 Task 18 Step 3；規格 §15.9.1、§11.5／§11.6）。
#
# 用法：scripts/rust/run_p7_ab.sh [--run-id ID] [--out DIR] [--run-dir DIR] [--rust-cli PATH] [--rust-daemon PATH]
#        [--od59-contract]
#   1. p7-a／p7-b：先跑 Python 基線（A）並確認其 daemon 全數結束，再以新的 FakeTarget 實例跑 Rust（B）。
#      其中一般 `test_control_latency_under_flood_with_events` 為快速檢查；只有 `--od59-contract` 才另跑契約樣本量 A／B。
#   2. 先前切片必要回歸（Rust artifact）：slice21-b（L1 鎖定節點）、slice22-b（P6 的
#      tests/contract_blackbox/slice22/；目錄不存在時記為 BLOCKED-UPSTREAM）、l15-slice21-b
#      （test_slice21_l15.py 的 rust 參數＋凍結清單）、l15-slice23
#      （test_slice23_l15.py；s23 情境含 Python 基線參數）；func-test 外部 bridge 模式 brcm-01-login-flow、ev-01-spawn-handler。
#   每個 pytest 階段寫 <run>/<slug>/{junit.xml,l1-results.json（SW_RESOURCE_REPORT）,log,rc}；func-test 階段寫
#   <run>/<slug>/{report/,log,rc}。全部結束後交 scripts/rust/p7_ab_report.py 逐案／逐階段判定並寫出
#   docs/rust-refactor/evidence/p7-ab-<YYMMDD>.md（--out 可改）。
#   L1.5 階段需 bridge 自測報告：未設 SERIALWRAP_L15_SELFTEST_REPORT 時先以 `uart_stdio_bridge selftest --endpoint posix-pty`
#   產生（自測不合格時 l15 階段照實記錄，報告判 FAIL／INCOMPLETE，不得 PASS）。
#   結束碼＝報告結束碼（PASS 0、FAIL 1、INCOMPLETE 3、參數錯誤 2）。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="$(date +%y%m%d-%H%M%S)"
OUT=""
RUN_DIR=""
RUST_CLI="$ROOT/target/debug/serialwrap"
RUST_D="$ROOT/target/debug/serialwrapd"
OD59_CONTRACT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id) RUN_ID="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --run-dir) RUN_DIR="$2"; shift 2 ;;
    --rust-cli) RUST_CLI="$2"; shift 2 ;;
    --rust-daemon) RUST_D="$2"; shift 2 ;;
    --od59-contract) OD59_CONTRACT=1; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "run_p7_ab.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done
[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "run_p7_ab.sh: --run-id 只允許英數與 ._-" >&2; exit 2; }
[[ -n "$RUN_DIR" ]] || RUN_DIR="$ROOT/target/p7-ab/$RUN_ID"
mkdir -p "$RUN_DIR"
[[ -n "$OUT" ]] || OUT="$ROOT/docs/rust-refactor/evidence/p7-ab-$(date +%y%m%d).md"
mkdir -p "$(dirname "$OUT")"
cd "$ROOT"
TESTS=(tests/contract_blackbox/test_p7_*.py)
PY_D="$ROOT/tests/contract_blackbox/bin/py-serialwrapd"
RUST_ENV=(SERIALWRAP_DAEMON_EXE="$RUST_D" SERIALWRAP_CLI_EXE="$RUST_CLI")

# slice21 的目錄含依 OD-72 排除的 Windows 與 DF-20 節點；執行時只傳凍結清單中的精確 nodeid。
S21_NODEIDS_FILE="$RUN_DIR/slice21-nodeids.txt"
python3 - "$ROOT/tests/acceptance/l1_required.lock.json" "$S21_NODEIDS_FILE" <<'PY'
import json
import pathlib
import sys

lock = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
nodes = lock.get("sets", {}).get("p7_gate:contract-blackbox-slice21-b", {}).get("linux", {}).get("nodeids")
if not isinstance(nodes, list) or not nodes or any(not isinstance(n, str) or "::" not in n for n in nodes):
    raise SystemExit("p7_gate:contract-blackbox-slice21-b.linux 凍結 nodeid 清單無效")
pathlib.Path(sys.argv[2]).write_text("".join(n + "\n" for n in nodes), encoding="utf-8")
PY
mapfile -t S21_NODES <"$S21_NODEIDS_FILE"
[[ ${#S21_NODES[@]} -gt 0 ]] || { echo "run_p7_ab.sh: slice21 凍結 nodeid 清單為空" >&2; exit 2; }

# pytest 階段：run_phase <slug> <env 指派…> -- <pytest 參數…>；env 指派之後以 `--` 分隔。
run_phase() {
  local slug="$1"; shift
  local envs=()
  while [[ $# -gt 0 && "$1" != "--" ]]; do envs+=("$1"); shift; done
  shift
  local d="$RUN_DIR/$slug"
  mkdir -p "$d"
  set +e
  env "${envs[@]}" SW_RESOURCE_REPORT="$d/l1-results.json" \
    python3 -m pytest -q -p no:cacheprovider "$@" --junitxml="$d/junit.xml" >"$d/log" 2>&1
  echo "$?" >"$d/rc"
  set -e
}

wait_pidlog_drained() {
  local pidlog="$1"
  [[ -f "$pidlog" ]] || return 0
  python3 - "$pidlog" <<'PY'
import json, os, sys, time
pids = [json.loads(line)["pid"] for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
deadline = time.monotonic() + 30
alive = pids
while alive and time.monotonic() < deadline:
    alive = [p for p in pids if os.path.exists(f"/proc/{p}") and open(f"/proc/{p}/stat").read().split(")")[-1].split()[0] != "Z"]
    time.sleep(0.2)
if alive:
    sys.exit(f"A 輪 daemon 未結束：{alive}")
PY
}

# 必要節點清單（含參數化）：同一 commit 的 collect-only。
python3 -m pytest -q -p no:cacheprovider --collect-only "${TESTS[@]}" >"$RUN_DIR/collected.txt" 2>&1 || true
# A：Python 基線
run_phase p7-a -u SERIALWRAP_DAEMON_EXE -u SERIALWRAP_CLI_EXE -u SERIALWRAP_AB_LABEL SW_AB_PIDLOG="$RUN_DIR/a.pids" \
  -- "${TESTS[@]}"
# A 的 daemon 必須全數結束（PTY 釋放）後才開始 B。
if [[ -f "$RUN_DIR/a.pids" ]]; then
  wait_pidlog_drained "$RUN_DIR/a.pids"
fi
# B：Rust CLI＋daemon
run_phase p7-b "${RUST_ENV[@]}" SERIALWRAP_AB_LABEL=rust-p7 -- "${TESTS[@]}"

# OD-71：一般 p7-a／p7-b 仍用小樣本快檢，不作契約判定；出口 gate 另以 event_latency 的正式樣本數與輪數跑 A／B。
if [[ "$OD59_CONTRACT" == "1" ]]; then
  read -r OD59_SAMPLES OD59_REPEATS < <(python3 - <<'PY'
import pathlib, yaml
latency = yaml.safe_load(pathlib.Path("contracts/resource_limits.yaml").read_text(encoding="utf-8"))["event_latency"]
print(latency["samples_per_run"], latency["repeats"])
PY
  )
  [[ "$OD59_SAMPLES" =~ ^[1-9][0-9]*$ && "$OD59_REPEATS" =~ ^[1-9][0-9]*$ ]] || {
    echo "run_p7_ab.sh: event_latency 契約樣本設定無效" >&2; exit 2;
  }
  OD59_TEST=tests/contract_blackbox/test_p7_latency_noise.py::test_control_latency_under_flood_with_events
  OD59_ENV=(P7_OD59_CANCEL_SAMPLES="$OD59_SAMPLES" P7_OD59_STOP_SAMPLES="$OD59_SAMPLES"
            P7_OD59_REPEATS="$OD59_REPEATS" P7_OD59_REQUIRE_CONTRACT=1)
  run_phase od59-contract-a -u SERIALWRAP_DAEMON_EXE -u SERIALWRAP_CLI_EXE -u SERIALWRAP_AB_LABEL \
    SW_AB_PIDLOG="$RUN_DIR/od59-contract-a.pids" "${OD59_ENV[@]}" -- -s "$OD59_TEST"
  wait_pidlog_drained "$RUN_DIR/od59-contract-a.pids"
  run_phase od59-contract-b "${RUST_ENV[@]}" SERIALWRAP_AB_LABEL=rust-p7 \
    SW_AB_PIDLOG="$RUN_DIR/od59-contract-b.pids" "${OD59_ENV[@]}" -- -s "$OD59_TEST"
  wait_pidlog_drained "$RUN_DIR/od59-contract-b.pids"
fi

# 先前切片必要回歸（Rust artifact）。
run_phase slice21-b "${RUST_ENV[@]}" SERIALWRAP_AB_LABEL=rust-s21 -- "${S21_NODES[@]}"
S22_DIR="$ROOT/tests/contract_blackbox/slice22"
if [[ -d "$S22_DIR" ]]; then
  run_phase slice22-b "${RUST_ENV[@]}" SERIALWRAP_AB_LABEL=rust-s22 -- "$S22_DIR/"
else
  mkdir -p "$RUN_DIR/slice22-b"
  echo "BLOCKED-UPSTREAM：P6 Slice 2.2 黑箱目錄不存在：$S22_DIR" >"$RUN_DIR/slice22-b/blocked"
fi
if [[ -z "${SERIALWRAP_L15_SELFTEST_REPORT:-}" ]]; then
  mkdir -p "$RUN_DIR/bridge-selftest"
  set +e
  PYTHONPATH="$ROOT/tools/uart_stdio_bridge/src" python3 -m uart_stdio_bridge selftest --endpoint posix-pty \
    --out "$RUN_DIR/bridge-selftest" >"$RUN_DIR/bridge-selftest/log" 2>&1
  set -e
  SERIALWRAP_L15_SELFTEST_REPORT="$(ls "$RUN_DIR"/bridge-selftest/*.json 2>/dev/null | head -n1 || true)"
fi
L15_ENV=(SERIALWRAP_GATE=1 SERIALWRAP_L15_SELFTEST_REPORT="${SERIALWRAP_L15_SELFTEST_REPORT:-}"
         SERIALWRAP_L15_PY_DAEMON="$PY_D" SERIALWRAP_L15_RUST_DAEMON="$RUST_D" SERIALWRAP_CLI_EXE="$RUST_CLI")
run_phase l15-slice21-b "${L15_ENV[@]}" -- tests/acceptance/l15/test_slice21_l15.py -k "rust or test_case_list_matches_freeze"
run_phase l15-slice23 "${L15_ENV[@]}" -- tests/acceptance/l15/test_slice23_l15.py

# func-test 外部 bridge 模式兩案（06 cross_slice_cases 交給 2.3 者），Rust daemon。
for case in brcm-01-login-flow ev-01-spawn-handler; do
  d="$RUN_DIR/func-$case"
  mkdir -p "$d"
  set +e
  env "${RUST_ENV[@]}" SERIALWRAP_AB_LABEL=rust-s23 python3 func-test/runner.py --mode bridge --case "$case" \
    --selftest-report "${SERIALWRAP_L15_SELFTEST_REPORT:-/nonexistent}" --report-dir "$d/report" >"$d/log" 2>&1
  echo "$?" >"$d/rc"
  set -e
done

exec python3 scripts/rust/p7_ab_report.py --run-id "$RUN_ID" --junit-a "$RUN_DIR/p7-a/junit.xml" \
  --junit-b "$RUN_DIR/p7-b/junit.xml" --collected "$RUN_DIR/collected.txt" \
  --rc-a "$(cat "$RUN_DIR/p7-a/rc")" --rc-b "$(cat "$RUN_DIR/p7-b/rc")" \
  --regression-dir "$RUN_DIR" --rust-cli "$RUST_CLI" --rust-daemon "$RUST_D" --out "$OUT"
