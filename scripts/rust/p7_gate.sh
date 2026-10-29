#!/usr/bin/env bash
# Slice 2.3（P7）出口 gate 編排（08-slice23-login-events.md Task 19 Step 3）。
#
# 用法：scripts/rust/p7_gate.sh [--run-id ID] [--out-md PATH] [--skip-full-pytest]
#   建立 target/p7-gate/<run_id>/，依序執行並以 `p7_gate_report.py record` 記錄每個子程序的 argv、rc、時間、log
#   （落檔＋sha256）、測試計數與 evidence；測試列另附 required_set（`p7_gate:<列名 slug>`）與 l1_results。
#   本腳本只「執行並記錄」，列狀態一律由 scripts/rust/p7_gate_report.py 計算。全部子程序結束後才 `build` 寫出
#   綁定本次 run_id、HEAD、artifact sha256 的 md（預設 docs/rust-refactor/evidence/p7-gate-<YYMMDD>.md），最後 `verify`
#   必須回空清單。
#   - Windows 列：非 WSL interop 環境記 caps_missing `windows_native`；未設 SW_TEST_COM_PAIR 記 `windows_com`
#     （皆為 BLOCKED-OWNER B-06，不得標 PASS）。
#   - --skip-full-pytest：不跑 `python3 -m pytest -q tests/`（該列記為未執行 → INCOMPLETE；僅供除錯，不得作出口證據）。
# 結束碼：報告結論 PASS（含 BLOCKED-OWNER）且 verify 無不一致 → 0；否則 1；參數錯誤 → 2。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="$(date +%y%m%d-%H%M%S)"
OUT_MD=""
SKIP_FULL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --run-id) RUN_ID="$2"; shift 2 ;;
    --out-md) OUT_MD="$2"; shift 2 ;;
    --skip-full-pytest) SKIP_FULL=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "p7_gate.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done
[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "p7_gate.sh: --run-id 只允許英數與 ._-" >&2; exit 2; }
RUN="$ROOT/target/p7-gate/$RUN_ID"
[[ ! -e "$RUN/results.json" ]] || { echo "p7_gate.sh: $RUN 已存在，拒絕覆寫（請換 --run-id）" >&2; exit 2; }
mkdir -p "$RUN/logs"
[[ -n "$OUT_MD" ]] || OUT_MD="$ROOT/docs/rust-refactor/evidence/p7-gate-$(date +%y%m%d).md"
cd "$ROOT"
REPORT=(python3 scripts/rust/p7_gate_report.py)
RUST_CLI="$ROOT/target/debug/serialwrap"
RUST_D="$ROOT/target/debug/serialwrapd"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
argv_json() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@"; }

# run_row <列名> <slug> [--l1 <path>] [--evidence <p>…] -- <命令…>：執行並記錄（不中斷整體 gate）。
run_row() {
  local name="$1" slug="$2"; shift 2
  local l1=() ev=()
  while [[ $# -gt 0 && "$1" != "--" ]]; do
    case "$1" in
      --l1) l1=(--l1-results "$2"); shift 2 ;;
      --evidence) ev+=("$2"); shift 2 ;;
      *) echo "p7_gate.sh: run_row 參數錯誤 $1" >&2; exit 2 ;;
    esac
  done
  shift
  local log="$RUN/logs/$slug.log" started rc
  started="$(now)"
  set +e
  "$@" >"$log" 2>&1
  rc=$?
  set -e
  "${REPORT[@]}" record --results-dir "$RUN" --name "$name" --rc "$rc" --log "$log" --argv-json "$(argv_json "$@")" \
    --started-at "$started" --ended-at "$(now)" "${l1[@]}" $([[ ${#ev[@]} -gt 0 ]] && echo --evidence) "${ev[@]}"
}

# exec_logged <slug> <命令…>：執行並寫 log；rc 存入 LAST_RC（供需要事後整理證據的列）。
LAST_RC=0
exec_logged() {
  local log="$RUN/logs/$1.log"; shift
  set +e
  "$@" >"$log" 2>&1
  LAST_RC=$?
  set -e
}

# record_existing <列名> <rc|-> <log> <argv-json> [--l1 <path>] [--evidence <p>…]：記錄已由其他子程序產出的結果。
record_existing() {
  local name="$1" rc="$2" log="$3" argv="$4"; shift 4
  local extra=() ev=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --platform-windows) extra+=(--platform windows); shift ;;
      --l1) [[ -f "$2" ]] && extra+=(--l1-results "$2"); shift 2 ;;
      --evidence) [[ -f "$2" ]] && ev+=("$2"); shift 2 ;;
      *) echo "p7_gate.sh: record_existing 參數錯誤 $1" >&2; exit 2 ;;
    esac
  done
  [[ -f "$log" ]] || : >"$log"
  local rcarg=()
  [[ "$rc" == "-" ]] || rcarg=(--rc "$rc")
  "${REPORT[@]}" record --results-dir "$RUN" --name "$name" "${rcarg[@]}" --log "$log" --argv-json "$argv" \
    --started-at "$(now)" --ended-at "$(now)" "${extra[@]}" $([[ ${#ev[@]} -gt 0 ]] && echo --evidence) "${ev[@]}"
}

# 0. 受測 artifact：Rust CLI＋daemon（非 test-fakes）。
cargo build --workspace --bins >"$RUN/logs/build.log" 2>&1 || { echo "p7_gate.sh: cargo build 失敗（$RUN/logs/build.log）" >&2; exit 1; }
CAPS=()
if ! [[ -n "${WSL_DISTRO_NAME:-}" || -e /proc/sys/fs/binfmt_misc/WSLInterop ]]; then CAPS+=(windows_native windows_com); fi
[[ -n "${SW_TEST_COM_PAIR:-}" ]] || CAPS+=(windows_com)
"${REPORT[@]}" init --results-dir "$RUN" --run-id "$RUN_ID" --caps-missing "${CAPS[@]}" --artifact "$RUST_CLI" "$RUST_D"

# 1. cfg 邊界
run_row "check_cfg_boundary" check-cfg-boundary --evidence "$RUN/logs/check-cfg-boundary.log" -- \
  python3 scripts/rust/check_cfg_boundary.py
# 2. Linux L1（p7_gate:cargo-test-workspace）
mkdir -p "$RUN/cargo-test-workspace"
run_row "cargo test --workspace" cargo-test-workspace --l1 "$RUN/cargo-test-workspace/l1-results.json" -- \
  python3 scripts/rust/cargo_l1_results.py --platform linux --out "$RUN/cargo-test-workspace/l1-results.json" \
  -- --workspace --features test-fakes
# 3／4. Windows 原生（non-COM 與 COM pair）；能力缺失時只記錄，由報告判 BLOCKED-OWNER。
win_l1() {  # win_l1 <dir>：取 win-test 最新報告的 l1_results
  local rep l1
  rep="$(ls -t "$ROOT"/target/win-test-report/*.json 2>/dev/null | grep -v -e '/l1-results-' -e '/summary-' | head -n1 || true)"
  [[ -n "$rep" ]] || return 0
  l1="$(python3 -c 'import json,sys,os; d=json.load(open(sys.argv[1])); print(os.path.join(os.path.dirname(sys.argv[1]), d.get("l1_results") or ""))' "$rep")"
  [[ -f "$l1" ]] && cp "$l1" "$1/l1-results.json"
  return 0
}
mkdir -p "$RUN/win-non-com" "$RUN/win-com"
# 兩列的 win-test.sh 參數（tests/test_p7_gate_report.py 以 `--dry-run` 核對可被 win-test.sh 解析）。
WIN_NONCOM_ARGS=(--package sw-pal --package sw-core --package sw-daemon -- --features test-fakes -- --skip cap_)
WIN_COM_ARGS=(--caps windows_com --package sw-daemon -- --test cap_windows_com_p7 --features test-fakes)
if [[ " ${CAPS[*]} " == *" windows_native "* ]]; then
  echo "caps_missing: windows_native（非 WSL interop 環境；LOCAL-WINDOWS-TODO）" >"$RUN/logs/win-non-com.log"
  record_existing "win-test.sh (non-COM)" - "$RUN/logs/win-non-com.log" '["scripts/rust/win-test.sh"]'
else
  exec_logged win-non-com bash scripts/rust/win-test.sh "${WIN_NONCOM_ARGS[@]}"
  win_l1 "$RUN/win-non-com"
  record_existing "win-test.sh (non-COM)" "$LAST_RC" "$RUN/logs/win-non-com.log" \
    "$(argv_json scripts/rust/win-test.sh "${WIN_NONCOM_ARGS[@]}")" --l1 "$RUN/win-non-com/l1-results.json" --platform-windows
fi
if [[ " ${CAPS[*]} " == *" windows_com "* ]]; then
  echo "caps_missing: windows_com（未設 SW_TEST_COM_PAIR 或無 Windows；B-06 擁有者 host COM20:COM21）" >"$RUN/logs/win-com.log"
  record_existing "win-test.sh --caps windows_com" - "$RUN/logs/win-com.log" '["scripts/rust/win-test.sh","--caps","windows_com"]'
else
  # win-test.sh 只在 `--` 之後接受 cargo 參數（審查 P7-cd7acf0-5）。
  exec_logged win-com bash scripts/rust/win-test.sh "${WIN_COM_ARGS[@]}"
  win_l1 "$RUN/win-com"
  record_existing "win-test.sh --caps windows_com" "$LAST_RC" "$RUN/logs/win-com.log" \
    "$(argv_json scripts/rust/win-test.sh "${WIN_COM_ARGS[@]}")" --l1 "$RUN/win-com/l1-results.json" --platform-windows
fi

# 5～12. 黑箱 A/B、先前切片必要回歸、func-test 兩案（run_p7_ab.sh 一次產出，逐階段成列）。
AB="$RUN/ab"
AB_MD="$RUN/p7-ab.md"
set +e
bash scripts/rust/run_p7_ab.sh --run-id "$RUN_ID" --run-dir "$AB" --out "$AB_MD" --rust-cli "$RUST_CLI" \
  --rust-daemon "$RUST_D" >"$RUN/logs/run-p7-ab.log" 2>&1
AB_RC=$?
set -e
echo "run_p7_ab.sh rc=$AB_RC" >>"$RUN/logs/run-p7-ab.log"
phase_row() {  # phase_row <列名> <階段目錄名>
  local d="$AB/$2" rc="-"
  [[ -f "$d/rc" ]] && rc="$(cat "$d/rc")"
  local log="$d/log"
  [[ -f "$d/blocked" ]] && log="$d/blocked"
  record_existing "$1" "$rc" "$log" "$(argv_json run_p7_ab.sh "$2")" --l1 "$d/l1-results.json" --evidence "$d/junit.xml"
}
phase_row "contract_blackbox p7 A" p7-a
phase_row "contract_blackbox p7 B" p7-b
phase_row "contract_blackbox slice21 B" slice21-b
phase_row "contract_blackbox slice22 B" slice22-b
phase_row "l15 slice21 B" l15-slice21-b
phase_row "l15 slice23 B" l15-slice23
for c in brcm-01-login-flow ev-01-spawn-handler; do
  d="$AB/func-$c"
  # runner 報告為 report/<case>/<mode>/<run>/comparison.json，深度不固定，以 find 取（與 p7_ab_report.judge_func 同）。
  cmp="$(find "$d/report" -name comparison.json 2>/dev/null | sort | head -n1 || true)"
  # 非測試列：rc 0 只在 runner rc 0 且 comparison verdict PASS 時成立（證據＝comparison.json）。
  rc="$(python3 - "$d/rc" "${cmp:-/nonexistent}" <<'PY'
import json, sys
try:
    rc = int(open(sys.argv[1]).read().strip())
    v = json.load(open(sys.argv[2])).get("verdict")
except (OSError, ValueError):
    print(1); sys.exit()
print(0 if rc == 0 and v == "PASS" else 1)
PY
)"
  record_existing "func-test $c" "$rc" "$d/log" "$(argv_json func-test/runner.py --mode bridge --case "$c")" \
    --evidence "${cmp:-/nonexistent}"
done

# 13. 案例鎖
run_row "regen_lock --check" regen-lock-check --evidence "$RUN/logs/regen-lock-check.log" -- \
  python3 tests/acceptance/regen_lock.py --check
# 14. slice_2.3 必要案例涵蓋
run_row "cases.lock slice_2.3 coverage" coverage --evidence "$RUN/coverage.json" -- \
  python3 scripts/rust/p7_case_coverage.py --ab-dir "$AB" --out "$RUN/coverage.json"
# 15. 全套 pytest（p7_gate:python3-m-pytest-q-tests；任何失敗 → 該列 FAIL，l1_judge 規則）
mkdir -p "$RUN/pytest-full"
if [[ "$SKIP_FULL" == "1" ]]; then
  echo "--skip-full-pytest：未執行（不得作出口證據）" >"$RUN/logs/pytest-full.log"
else
  run_row "python3 -m pytest -q tests/" pytest-full --l1 "$RUN/pytest-full/l1-results.json" \
    --evidence "$RUN/pytest-full/junit.xml" -- \
    env SW_RESOURCE_REPORT="$RUN/pytest-full/l1-results.json" python3 -m pytest -q tests/ \
    --junitxml="$RUN/pytest-full/junit.xml"
fi
# 16. policy check
run_row "policy_check" policy-check --evidence "$RUN/logs/policy-check.log" -- python3 -m policy_check --repo .

# 報告與驗證（全部子程序結束後）。
mkdir -p "$(dirname "$OUT_MD")"
"${REPORT[@]}" build --results-dir "$RUN" --out "$OUT_MD"
set +e
"${REPORT[@]}" verify --results-dir "$RUN" --md "$OUT_MD" >"$RUN/logs/verify.log" 2>&1
VERIFY_RC=$?
set -e
echo "p7_gate.sh：報告 $OUT_MD；verify rc=$VERIFY_RC（$RUN/logs/verify.log）；run_p7_ab.sh rc=$AB_RC"
grep -q '^結論：PASS' "$OUT_MD" && [[ "$VERIFY_RC" == 0 ]] && exit 0
exit 1
