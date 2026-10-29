#!/usr/bin/env bash
# Slice 2.1 出口 gate（P5-T22；計畫 docs/rust-refactor/plans/06-slice21-daemon-core.md Task 22 Step 3）。
#
# 用法：scripts/rust/slice21_gate.sh [--dry-run] [--evidence-run] [--with-com-pair] [--run-id ID] [--out DIR]
#   產出 <DIR>/<ID>/report.json（預設 DIR=target/slice21-gate），schema 見 tests/acceptance/slice21_gate_report.schema.json。
#
# 原則：
# - 本腳本只負責「執行並記錄」每段 exit 與 l1-results；section 狀態與 verdict 一律由
#   scripts/rust/slice21_gate_judge.py 計算（gate 腳本本身不判定）。
# - --dry-run 只產生報告骨架與 BLOCKED-OWNER 清單，各 section EVIDENCE_MISSING，verdict 永遠 INCOMPLETE。
# - 需 Windows 原生、com0com、bench 或擁有者的證據一律 EVIDENCE_MISSING／BLOCKED-OWNER，不得標 PASS。
# - §5.3 開工前凍結檢查：event_latency 未凍結時拒絕開跑（INSUFFICIENT_EVIDENCE，exit 3），不得以 skip 或 PASS 代替。
#   --evidence-run：凍結檢查未通過時仍執行各 section 以取得證據（第三輪新增）；preflight 照實寫入報告，judge 依
#   `event_latency_frozen: false` 一律判 INCOMPLETE（永不 READY），結束碼同 INCOMPLETE（3）。
# 結束碼：--dry-run 一律 0；否則 READY_FOR_OWNER_EVIDENCE→0、FAIL→1、INCOMPLETE→3、參數或工具錯誤→2。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JUDGE="$ROOT/scripts/rust/slice21_gate_judge.py"
DRY_RUN=0
EVIDENCE_RUN=0
WITH_COM_PAIR=0
RUN_ID=""
OUT_DIR="$ROOT/target/slice21-gate"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --evidence-run) EVIDENCE_RUN=1; shift ;;
    --with-com-pair) WITH_COM_PAIR=1; shift ;;
    --run-id)
      [[ $# -ge 2 ]] || { echo "slice21_gate.sh: --run-id 缺少值" >&2; exit 2; }
      RUN_ID="$2"; shift 2 ;;
    --out)
      [[ $# -ge 2 ]] || { echo "slice21_gate.sh: --out 缺少值" >&2; exit 2; }
      OUT_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "slice21_gate.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done

[[ -n "$RUN_ID" ]] || RUN_ID="$(date +%y%m%d-%H%M%S)"
if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "slice21_gate.sh: --run-id 只允許英數與 ._-：$RUN_ID" >&2
  exit 2
fi
RUN_DIR="$OUT_DIR/$RUN_ID"
if [[ -e "$RUN_DIR/report.json" ]]; then
  echo "slice21_gate.sh: $RUN_DIR/report.json 已存在，拒絕覆寫（請換 --run-id）" >&2
  exit 2
fi
mkdir -p "$RUN_DIR/sections" "$RUN_DIR/logs"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cd "$ROOT"

LINUX_TARGET="x86_64-unknown-linux-gnu"
WIN_TARGET="x86_64-pc-windows-gnu"
RUST_CLI="$ROOT/target/$LINUX_TARGET/release/serialwrap"
RUST_D="$ROOT/target/$LINUX_TARGET/release/serialwrapd"
WIN_D="$ROOT/target/$WIN_TARGET/release/serialwrapd.exe"

record() {  # record <name> [record-section 參數…]
  local name="$1"; shift
  python3 "$JUDGE" record-section --run-dir "$RUN_DIR" --name "$name" "$@"
}

# 執行一段命令：stdout／stderr 寫入 logs/<name>.log，回傳值存入全域 LAST_RC（不中斷整體 gate）。
LAST_RC=0
run_logged() {  # run_logged <log 名稱> <命令…>
  local log="$RUN_DIR/logs/$1.log"; shift
  printf '+ %q' "$@" >"$log"; printf '\n' >>"$log"
  set +e
  "$@" >>"$log" 2>&1
  LAST_RC=$?
  set -e
  return 0
}

# §5.3 開工前凍結檢查（event_latency 凍結與審閱紀錄、無 pending_owner_decision）。
python3 - "$ROOT/contracts/resource_limits.yaml" "$RUN_DIR/preflight.json" <<'PY'
import json, sys
import yaml

src, out = sys.argv[1], sys.argv[2]
reasons = []
try:
    doc = yaml.safe_load(open(src, encoding="utf-8")) or {}
except Exception as exc:  # noqa: BLE001
    doc = {}
    reasons.append(f"resource_limits.yaml 無法讀取：{exc}")
ev = doc.get("event_latency") or {}
if ev.get("freeze_status") != "frozen":
    reasons.append(f"event_latency.freeze_status={ev.get('freeze_status')!r}（須為 frozen）")
for key in ("reviewed_by", "reviewed_at", "review_conclusion"):
    if not ev.get(key):
        reasons.append(f"event_latency.{key} 未附入")
if ev.get("reviewed_by") and ev.get("reviewed_by") == ev.get("owner"):
    reasons.append("event_latency.reviewed_by 不得等於 owner")
def walk(node):
    if isinstance(node, dict):
        od = node.get("owner_decision")
        if isinstance(od, dict) and od.get("status") == "pending_owner_decision":
            yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)
pending = list(walk(doc.get("limits") or {}))
if pending:
    reasons.append(f"limits 內仍有 {len(pending)} 列 owner_decision.status: pending_owner_decision（NC-P5-01）")
json.dump({"event_latency_frozen": not reasons, "reasons": reasons}, open(out, "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
PY
PREFLIGHT_OK="$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1]))["event_latency_frozen"]))' "$RUN_DIR/preflight.json")"

assemble() {  # assemble [額外 assemble 參數…]
  local extra=()
  [[ "$DRY_RUN" == "1" ]] && extra+=(--dry-run)
  python3 "$JUDGE" assemble --run-dir "$RUN_DIR" --run-id "$RUN_ID" --started-at "$STARTED_AT" \
    --preflight "$RUN_DIR/preflight.json" "${extra[@]}" "$@"
}

exit_for_verdict() {
  local v
  v="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["verdict"])' "$RUN_DIR/report.json")"
  echo "slice21_gate.sh: verdict=$v（$RUN_DIR/report.json）"
  if [[ "$DRY_RUN" == "1" ]]; then exit 0; fi
  case "$v" in
    READY_FOR_OWNER_EVIDENCE) exit 0 ;;
    FAIL) exit 1 ;;
    *) exit 3 ;;
  esac
}

com_reason="未提供 SW_TEST_COM_PAIR／--with-com-pair（BO-S21-01：擁有者 host COM20:COM21）"

if [[ "$DRY_RUN" == "1" || ( "$PREFLIGHT_OK" != "1" && "$EVIDENCE_RUN" != "1" ) ]]; then
  why="dry-run：未執行"
  [[ "$DRY_RUN" == "1" ]] || why="§5.3 開工前凍結檢查未通過：拒絕開跑（INSUFFICIENT_EVIDENCE）"
  record cfg_boundary --reason "$why"
  record linux_l1 --required-set s21_core --reason "$why"
  record linux_cap_posix_pty --required-set s21_gate:linux_cap_posix_pty --reason "$why"
  record linux_cap_sys_admin --required-set s21_gate:cap_sys_admin --reason "$why"
  record linux_cap_windows_native --required-set s21_gate:cap_windows_native --reason "$why"
  record linux_cap_interactive_desktop --required-set s21_gate:cap_interactive_desktop --reason "$why"
  record linux_cap_supervisor_runtime --required-set s21_gate:cap_supervisor_runtime --reason "$why"
  record py_parity --required-set s21_gate:py_parity_full --reason "$why"
  record windows_native_l1 --required-set s21_core --reason "$why；需原生 Windows（win-test.sh）"
  record windows_cap_windows_com --required-set s21_gate:windows_cap_windows_com --reason "$com_reason"
  record blackbox_python --required-set s21_gate:blackbox_python --reason "$why"
  record blackbox_rust --required-set s21_gate:blackbox_rust --reason "$why"
  record existing_suites_rust --required-set s21_gate:existing_suites_rust --reason "$why"
  record l15_ab --reason "$why"
  record event_latency --reason "$why"
  record wal_s1w_perf --reason "$why；Windows 原生與 EDR 下正式量測屬 BO-S21-14"
  record case_uptake --reason "$why"
  record pytest_full --required-set s21_gate:pytest_full --reason "$why"
  record policy_check --reason "$why"
  assemble --cli-path "$RUST_CLI" --daemon-path "$RUST_D"
  if [[ "$DRY_RUN" != "1" ]]; then
    echo "slice21_gate.sh: INSUFFICIENT_EVIDENCE：$(python3 -c 'import json,sys; print("；".join(json.load(open(sys.argv[1]))["reasons"]))' "$RUN_DIR/preflight.json")" >&2
  fi
  exit_for_verdict
fi

# 1. cfg 邊界
run_logged cfg_boundary python3 scripts/rust/check_cfg_boundary.py
record cfg_boundary --command "python3 scripts/rust/check_cfg_boundary.py" --exit "$LAST_RC" \
  --log logs/cfg_boundary.log --section-verdict "$([[ $LAST_RC == 0 ]] && echo PASS || echo FAIL)"

# 2. Linux L1（s21_core）
# F108（編排者裁決）：linux_l1 以非 root 執行。euid==0 時須以 SW_GATE_L1_USER（使用者名稱）經 setpriv 降權執行；
# 無法降權即不執行、記 EVIDENCE_MISSING 並要求非 root 補跑（不得記 PASS，也不得以 root 專屬失敗記 FAIL）。
mkdir -p "$RUN_DIR/linux_l1"
l1_cmd=(python3 scripts/rust/cargo_l1_results.py --platform linux --out "$RUN_DIR/linux_l1/l1-results.json"
  -- --workspace --features test-fakes -- --skip cap_)
l1_euid="$(id -u)"
l1_run=1
if [[ "$l1_euid" == 0 ]]; then
  if [[ -n "${SW_GATE_L1_USER:-}" ]] && command -v setpriv >/dev/null 2>&1 && id -u "$SW_GATE_L1_USER" >/dev/null 2>&1; then
    l1_euid="$(id -u "$SW_GATE_L1_USER")"
    chown -R "$SW_GATE_L1_USER" "$RUN_DIR/linux_l1"
    l1_cmd=(setpriv --reuid="$l1_euid" --regid="$(id -g "$SW_GATE_L1_USER")" --init-groups
      env HOME="$(getent passwd "$SW_GATE_L1_USER" | cut -d: -f6)" "${l1_cmd[@]}")
  else
    l1_run=0
  fi
fi
if [[ "$l1_run" == 1 ]]; then
  run_logged linux_l1 "${l1_cmd[@]}"
  record linux_l1 --required-set s21_core --command "cargo_l1_results.py --platform linux -- --workspace --features test-fakes -- --skip cap_" \
    --exit "$LAST_RC" --log logs/linux_l1.log --l1-results linux_l1/l1-results.json --run-euid "$l1_euid"
else
  record linux_l1 --required-set s21_core --section-verdict EVIDENCE_MISSING --run-euid 0 \
    --reason "F108：gate 以 root 執行且無法降權（未設 SW_GATE_L1_USER 或無 setpriv）；linux_l1 須以非 root 補跑"
fi

# 3. Linux cap_posix_pty
mkdir -p "$RUN_DIR/linux_cap_posix_pty"
run_logged linux_cap_posix_pty python3 scripts/rust/cargo_l1_results.py --platform linux --caps posix_pty \
  --out "$RUN_DIR/linux_cap_posix_pty/l1-results.json" -- -p sw-pal --features test-fakes
record linux_cap_posix_pty --required-set s21_gate:linux_cap_posix_pty \
  --command "cargo_l1_results.py --platform linux --caps posix_pty -- -p sw-pal --features test-fakes" \
  --exit "$LAST_RC" --log logs/linux_cap_posix_pty.log --l1-results linux_cap_posix_pty/l1-results.json

# 3b. OD-55 linux 能力集合（P5 第 6 輪 SPEC-GAP #2，編排者裁決）：先探測能力，具備才以 --caps <cap> 實跑；
#     否則記 EVIDENCE_MISSING 並附探測證據（所需能力、處置與實跑方式由 judge CAPABILITY_SECTIONS 補上），不得記 PASS。
cap_probe() {  # cap_probe <cap>：印出 available:<證據> 或 unavailable:<證據>
  local eff st
  case "$1" in
    sys_admin)
      eff="$(awk '/^CapEff:/{print $2}' /proc/self/status 2>/dev/null || true)"
      if [[ "$(id -u)" == 0 && -n "$eff" ]] && (( (16#$eff >> 21) & 1 )); then
        echo "available:euid=0 CapEff=$eff"
      else
        echo "unavailable:euid=$(id -u) CapEff=${eff:-unknown}"
      fi ;;
    supervisor_runtime)
      st="$(systemctl --user is-system-running 2>/dev/null || true)"
      if [[ "$st" == running || "$st" == degraded ]]; then
        echo "available:systemctl --user is-system-running=$st"
      else
        echo "unavailable:systemctl --user is-system-running=${st:-unavailable}"
      fi ;;
    *)
      echo "unavailable:linux 主機（$(uname -s)）無法提供 cap_$1（需原生 Windows）" ;;
  esac
}
for cap in sys_admin windows_native interactive_desktop supervisor_runtime; do
  sec="linux_cap_$cap"
  crate=sw-pal; [[ "$cap" == windows_native ]] && crate=sw-daemon
  probe="$(cap_probe "$cap")"
  if [[ "$probe" == available:* ]]; then
    mkdir -p "$RUN_DIR/$sec"
    run_logged "$sec" python3 scripts/rust/cargo_l1_results.py --platform linux --caps "$cap" \
      --out "$RUN_DIR/$sec/l1-results.json" -- -p "$crate" --features test-fakes
    record "$sec" --required-set "s21_gate:cap_$cap" \
      --command "cargo_l1_results.py --platform linux --caps $cap -- -p $crate --features test-fakes" \
      --exit "$LAST_RC" --log "logs/$sec.log" --l1-results "$sec/l1-results.json" --capability-probe "$cap=$probe"
  else
    record "$sec" --required-set "s21_gate:cap_$cap" --capability-probe "$cap=$probe" \
      --reason "能力探測：${probe#unavailable:}；未執行 --caps $cap"
  fi
done

# 4／5. Windows 原生 L1 與 COM pair（win-test.sh；報告的 l1_results 複製進本 run）
win_l1_into() {  # win_l1_into <section>：取 win-test 最新報告的 l1_results
  local sec="$1" rep l1
  rep="$(ls -t "$ROOT"/target/win-test-report/*.json 2>/dev/null | grep -v -e '/l1-results-' -e '/summary-' | head -n1 || true)"
  [[ -n "$rep" ]] || return 0
  l1="$(python3 -c 'import json,sys,os; d=json.load(open(sys.argv[1])); print(os.path.join(os.path.dirname(sys.argv[1]), d.get("l1_results") or ""))' "$rep")"
  [[ -f "$l1" ]] && cp "$l1" "$RUN_DIR/$sec/l1-results.json"
  return 0
}
mkdir -p "$RUN_DIR/windows_native_l1" "$RUN_DIR/windows_cap_windows_com"
run_logged windows_native_l1 bash scripts/rust/win-test.sh --package sw-pal --package sw-core --package sw-daemon -- --features test-fakes -- --skip cap_
win_l1_into windows_native_l1
win_l1_arg=()
[[ -f "$RUN_DIR/windows_native_l1/l1-results.json" ]] && win_l1_arg=(--l1-results windows_native_l1/l1-results.json)
record windows_native_l1 --required-set s21_core --command "scripts/rust/win-test.sh --package sw-pal --package sw-core --package sw-daemon" \
  --exit "$LAST_RC" --log logs/windows_native_l1.log "${win_l1_arg[@]}"
if [[ "$WITH_COM_PAIR" == "1" && -n "${SW_TEST_COM_PAIR:-}" ]]; then
  run_logged windows_cap_windows_com bash scripts/rust/win-test.sh --caps windows_com --package sw-pal -- --features test-fakes
  win_l1_into windows_cap_windows_com
  com_arg=()
  [[ -f "$RUN_DIR/windows_cap_windows_com/l1-results.json" ]] && com_arg=(--l1-results windows_cap_windows_com/l1-results.json)
  record windows_cap_windows_com --required-set s21_gate:windows_cap_windows_com \
    --command "scripts/rust/win-test.sh --caps windows_com --package sw-pal" --exit "$LAST_RC" \
    --log logs/windows_cap_windows_com.log "${com_arg[@]}"
else
  record windows_cap_windows_com --required-set s21_gate:windows_cap_windows_com --reason "$com_reason"
fi

# 6. release artifact（identity 與黑箱用）
# 方法審閱 [High]（2026-10-29）：建置前先移除舊 artifact，建置失敗時不得留下舊 binary 被後續 section 與量測證據
# 綁到目前 HEAD；Linux／Windows 建置結果分開保存，建置成功當下記錄 daemon sha256（event_latency 量測核對用）。
rm -f "$RUST_D" "$RUST_CLI"
run_logged build_linux cargo build --release -p sw-daemon -p sw-cli --target "$LINUX_TARGET"
BUILD_LINUX_RC="$LAST_RC"
BUILT_D_SHA=""
if [[ "$BUILD_LINUX_RC" == 0 && -f "$RUST_D" ]]; then
  BUILT_D_SHA="$(sha256sum "$RUST_D" | cut -d' ' -f1)"
fi
run_logged build_windows cargo build --release -p sw-daemon -p sw-cli --target "$WIN_TARGET"
BUILD_WINDOWS_RC="$LAST_RC"

pytest_l1() {  # pytest_l1 <section> <log> <env…> -- <pytest 參數…>：帶 SW_RESOURCE_REPORT 與 junit
  local sec="$1" log="$2"; shift 2
  local envs=()
  while [[ $# -gt 0 && "$1" != "--" ]]; do envs+=("$1"); shift; done
  shift
  mkdir -p "$RUN_DIR/$sec"
  run_logged "$log" env "${envs[@]}" SW_RESOURCE_REPORT="$RUN_DIR/$sec/l1-results.json" \
    python3 -m pytest "$@" --junitxml="$RUN_DIR/$sec/junit.xml"
}

# 7. 黑箱 A/B（Python 基線與 Rust）
pytest_l1 blackbox_python blackbox_python -- -q tests/contract_blackbox/slice21 -m cap_posix_pty
record blackbox_python --required-set s21_gate:blackbox_python --command "pytest -q tests/contract_blackbox/slice21 -m cap_posix_pty（Python）" \
  --exit "$LAST_RC" --log logs/blackbox_python.log --l1-results blackbox_python/l1-results.json
# Rust 標籤須以 ELF CLI 執行（contract_blackbox conftest 的 artifact 身分檢查；第三輪 gate 實跑 46/46 因此 fail）。
pytest_l1 blackbox_rust blackbox_rust SERIALWRAP_CLI_EXE="$RUST_CLI" SERIALWRAP_DAEMON_EXE="$RUST_D" SERIALWRAP_AB_LABEL=rust-s21 -- \
  -q tests/contract_blackbox/slice21 -m cap_posix_pty
record blackbox_rust --required-set s21_gate:blackbox_rust --command "SERIALWRAP_CLI_EXE=<rust cli> SERIALWRAP_DAEMON_EXE=<rust> pytest -q tests/contract_blackbox/slice21 -m cap_posix_pty" \
  --exit "$LAST_RC" --log logs/blackbox_rust.log --l1-results blackbox_rust/l1-results.json

# 8. Task 20 既有套件改指 Rust（兩條 pytest 合併為一次收集＋三次單 case runner）
pytest_l1 existing_suites_rust existing_suites_rust SERIALWRAP_CLI_EXE="$RUST_CLI" SERIALWRAP_DAEMON_EXE="$RUST_D" -- \
  -q tests/test_multiagent_e2e.py tests/test_human_agent_coexist.py \
  -k "test_five_agents_three_rounds_no_conflict or t2_wal_reset_seq_resets_to_zero or t3_wal_current_seq_matches_live or t4_bind_idempotent_ready_session"
suites_rc="$LAST_RC"
extra_checks=()
for c in sm-01-attach-to-ready ha-05-concurrent-submit sm-04-device-unplug; do
  run_logged "func_$c" env SERIALWRAP_CLI_EXE="$RUST_CLI" SERIALWRAP_DAEMON_EXE="$RUST_D" \
    python3 func-test/runner.py --case "$c" --report-dir "$RUN_DIR/func/rust/$c"
  extra_checks+=(--extra-check "func-test:$c=$LAST_RC")
done
record existing_suites_rust --required-set s21_gate:existing_suites_rust \
  --command "Task 20 既有套件（pytest＋func-test/runner.py --case ×3）" --exit "$suites_rc" \
  --log logs/existing_suites_rust.log --l1-results existing_suites_rust/l1-results.json "${extra_checks[@]}"

# 9. Task 21 L1.5 與 event_latency
# bridge 自測報告（SERIALWRAP_L15_SELFTEST_REPORT）須存在且依 Task 8 正式資格判定合格；否則屬證據缺失，
# 不得跑成 FAIL（審查 b7cd852 #2）。判定沿用 func-test/runner.py 的 evaluate_bridge_eligibility。
l15_qualified=0
if [[ -n "${SERIALWRAP_L15_SELFTEST_REPORT:-}" && -f "${SERIALWRAP_L15_SELFTEST_REPORT}" ]]; then
  l15_qualified="$(python3 -c 'import sys; sys.path.insert(0, "func-test"); import runner; print(int(runner.evaluate_bridge_eligibility(sys.argv[1])[1]))' \
    "$SERIALWRAP_L15_SELFTEST_REPORT" 2>/dev/null || echo 0)"
fi
if [[ "$l15_qualified" != "1" ]]; then
  record l15_ab --exit 0 --section-verdict EVIDENCE_MISSING \
    --reason "SERIALWRAP_L15_SELFTEST_REPORT 未提供或 bridge 自測報告不合格（Task 8 資格判定）：L1.5 A/B 未執行"
elif [[ -f tests/acceptance/l15/test_slice21_l15.py ]]; then
  mkdir -p "$RUN_DIR/l15_ab"
  # 逐案 outcome（l1-results.v1）供 case_verdicts 彙整（OD-60／OWNER-BATCH #8）。
  run_logged l15_ab env SERIALWRAP_GATE=1 SERIALWRAP_L15_PY_DAEMON="$ROOT/tests/contract_blackbox/bin/py-serialwrapd" \
    SERIALWRAP_L15_RUST_DAEMON="$RUST_D" SW_RESOURCE_REPORT="$RUN_DIR/l15_ab/l1-results.json" \
    python3 -m pytest -q tests/acceptance/l15/test_slice21_l15.py
  # pytest 全綠但仍有 L1.5 待裁決缺口（情境斷言不完整，PA-OD-L15-*）時不計 PASS（審查 c0d221f #3）。
  l15_rc="$LAST_RC"
  l15_gaps="$(python3 -c 'import sys, yaml; d=yaml.safe_load(open(sys.argv[1])); print(",".join(x["id"] for x in d.get("pending_decisions") or [] if str(x.get("id","")).startswith("PA-OD-L15")))' \
    tests/acceptance/slice21_gate_expect.yaml 2>/dev/null || echo "PA-OD-L15-?")"
  if [[ "$l15_rc" != 0 ]]; then l15_verdict=FAIL; elif [[ -n "$l15_gaps" ]]; then l15_verdict=EVIDENCE_MISSING; else l15_verdict=PASS; fi
  record l15_ab --command "SERIALWRAP_GATE=1 pytest -q tests/acceptance/l15/test_slice21_l15.py" --exit "$l15_rc" \
    --log logs/l15_ab.log --section-verdict "$l15_verdict" --evidence l15_ab/l1-results.json \
    ${l15_gaps:+--reason "L1.5 待裁決缺口未結：$l15_gaps（slice21-l15-gaps.md）"}
else
  record l15_ab --reason "tests/acceptance/l15/test_slice21_l15.py 不存在（P5-T21 未完成）"
fi
# OD-45：event_latency = L1 結構 gate（五事件 l1_gate 須在 linux_l1 passed）＋外部量測三項（RPC 送出 → RPC 回應；
#        idle 與 flood 20000 B/s；Rust 為判定對象，Python 基線以診斷樣本數另記供對照，不計入判定）。
mkdir -p "$RUN_DIR/event_latency"
evlat_bind=()
# 只有本 run 建置成功且 artifact 仍為建置當下的 sha256 時，才明示建置來源（否則量測報告不綁來源 → 證據不足）。
if [[ -n "$BUILT_D_SHA" ]]; then
  evlat_bind=(--artifact-source-commit "$(git -C "$ROOT" rev-parse HEAD)" --artifact-build-run-id "$RUN_ID" --artifact-sha256 "$BUILT_D_SHA")
fi
run_logged event_latency python3 scripts/rust/event_latency_measure.py --daemon "$RUST_D" --cli "$RUST_CLI" --label rust \
  --l1-results "$RUN_DIR/linux_l1/l1-results.json" --run-id "$RUN_ID" \
  "${evlat_bind[@]}" --out "$RUN_DIR/event_latency/rust.json" --work-dir "$RUN_DIR/event_latency/work"
evlat_rc="$LAST_RC"
# 工具結束碼：PASS 0、FAIL 1、INSUFFICIENT_EVIDENCE 3。證據不足照實記 EVIDENCE_MISSING（exit 0），不得記成 FAIL 或 PASS。
evlat_exit="$evlat_rc"; [[ "$evlat_rc" == 3 ]] && evlat_exit=0
run_logged event_latency_python python3 scripts/rust/event_latency_measure.py --daemon "$ROOT/tests/contract_blackbox/bin/py-serialwrapd" \
  --label python-diagnostic --samples "${SW_EVLAT_PY_SAMPLES:-50}" --l1-results "$RUN_DIR/linux_l1/l1-results.json" \
  --out "$RUN_DIR/event_latency/python-diagnostic.json" --work-dir "$RUN_DIR/event_latency/work-py" || true
evlat_verdict="$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1]))["verdict"]; print({"PASS":"PASS","FAIL":"FAIL"}.get(v,"EVIDENCE_MISSING"))' \
  "$RUN_DIR/event_latency/rust.json" 2>/dev/null || echo EVIDENCE_MISSING)"
evlat_reason="$(python3 -c 'import json,sys; print("；".join(json.load(open(sys.argv[1])).get("reasons") or []) or "—")' \
  "$RUN_DIR/event_latency/rust.json" 2>/dev/null || echo "rust.json 未產生")"
rm -rf "$RUN_DIR/event_latency/work" "$RUN_DIR/event_latency/work-py"
record event_latency --command "python3 scripts/rust/event_latency_measure.py --label rust --l1-results linux_l1/l1-results.json" \
  --exit "$evlat_exit" --log logs/event_latency.log --section-verdict "$evlat_verdict" --evidence event_latency/rust.json \
  --reason "OD-45（L1 結構 gate＋外部量測 stop_request／command_cancel／release_request）：$evlat_reason"

# 9 補：§1.2 WAL 效能（超標記 FAIL 並保留樣本，不得改列觀察值）
# OD-61：Linux 門檻以擁有者 WSL2 主機量測為準。編排者於本機量測後以
#   SW_GATE_WAL_S1W_IMPORT=<wal_s1w.json> SW_GATE_WAL_S1W_HOST=<主機類型標註> 匯入；本機（雲端 vm）量測照跑但只作診斷。
#   判定由 slice21_gate_judge.py wal-s1w-verdict 依可追溯性與主機類型決定（不得以雲端結果判 PASS／FAIL）。
mkdir -p "$RUN_DIR/wal_s1w_perf"
run_logged wal_s1w_perf cargo run --release -p sw-core --example wal_s1w -- --out "$RUN_DIR/wal_s1w_perf/wal_s1w.json" \
  --wal-root "$RUN_DIR/wal_s1w_perf/walroot"
s1w_rc="$LAST_RC"
rm -rf "$RUN_DIR/wal_s1w_perf/walroot"
s1w_args=()
[[ -f "$RUN_DIR/wal_s1w_perf/wal_s1w.json" ]] && s1w_args+=(--local "$RUN_DIR/wal_s1w_perf/wal_s1w.json")
s1w_evidence=wal_s1w_perf/wal_s1w.json
# SW_GATE_WAL_S1W_IMPORT 可列多輪證據（以 : 分隔，如三輪），judge 取最差判定；OD-66：WSL2 證據另以
# SW_GATE_WAL_S1W_PY_BASELINE 匯入同主機同時段的 Python 基線（wal_microbench.py），門檻依主機類型選並記入 verdict.json。
if [[ -n "${SW_GATE_WAL_S1W_IMPORT:-}" ]]; then
  s1w_n=0
  IFS=':' read -r -a s1w_imports <<<"$SW_GATE_WAL_S1W_IMPORT"
  for s1w_src in "${s1w_imports[@]}"; do
    s1w_n=$((s1w_n + 1))
    cp "$s1w_src" "$RUN_DIR/wal_s1w_perf/wal_s1w.imported-$s1w_n.json" 2>/dev/null || true
    s1w_args+=(--import "$RUN_DIR/wal_s1w_perf/wal_s1w.imported-$s1w_n.json")
  done
  s1w_args+=(--host-label "${SW_GATE_WAL_S1W_HOST:-}")
  s1w_evidence=wal_s1w_perf/verdict.json
fi
if [[ -n "${SW_GATE_WAL_S1W_PY_BASELINE:-}" ]]; then
  cp "$SW_GATE_WAL_S1W_PY_BASELINE" "$RUN_DIR/wal_s1w_perf/python-baseline.imported.json" 2>/dev/null || true
  s1w_args+=(--python-baseline "$RUN_DIR/wal_s1w_perf/python-baseline.imported.json")
fi
python3 "$JUDGE" wal-s1w-verdict "${s1w_args[@]}" --out "$RUN_DIR/wal_s1w_perf/verdict.json" >/dev/null 2>>"$RUN_DIR/logs/wal_s1w_perf.log" || true
s1w_verdict="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["verdict"])' "$RUN_DIR/wal_s1w_perf/verdict.json" 2>/dev/null || echo EVIDENCE_MISSING)"
s1w_reason="$(python3 -c 'import json,sys; print("；".join(json.load(open(sys.argv[1]))["reasons"]) or "—")' "$RUN_DIR/wal_s1w_perf/verdict.json" 2>/dev/null || echo "wal-s1w-verdict 未產生")"
s1w_exit="$s1w_rc"; [[ "$s1w_verdict" == EVIDENCE_MISSING ]] && s1w_exit=0
record wal_s1w_perf --command "cargo run --release -p sw-core --example wal_s1w（＋SW_GATE_WAL_S1W_IMPORT 匯入）" --exit "$s1w_exit" \
  --log logs/wal_s1w_perf.log --section-verdict "$s1w_verdict" --evidence "$s1w_evidence" \
  --reason "Linux 門檻依主機類型（OD-66：WSL2 另訂門檻＋不劣於 Python 基線；原生 §1.2）：$s1w_reason" \
  --reason "Windows 原生與 EDR 下正式量測屬 BO-S21-14（EVIDENCE_MISSING）"

# 10. Windows 無 COM 黑箱（BO-S21-13 承接 Windows 全套；此處只保留紀錄，不計入 section）。
#     注意：win-blackbox.ps1 目前仍為 P3 介面，-Suite／-Daemon 尚未支援；失敗只留 log，不影響判定。
if command -v pwsh.exe >/dev/null 2>&1 && [[ -f "$WIN_D" ]]; then
  run_logged windows_nocom pwsh.exe -File scripts/rust/win-blackbox.ps1 -Suite slice21-nocom -Daemon "$WIN_D"
fi

# 10b. Python 基線 parity（P5 第 6 輪 SPEC-GAP #1，編排者裁決）：以 s21_gate:py_parity_full 的完整 selector 實跑；
#      舊集合 s21_gate:py_parity 不改不刪，仍由 pytest_full 承擔（兩者皆由 gate 執行）。
mapfile -t pyp_argv < <(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["sets"]["s21_gate:py_parity_full"]["linux"]["selector"]["argv"]))' \
  tests/acceptance/l1_required.lock.json 2>/dev/null || true)
if [[ "${#pyp_argv[@]}" -gt 0 ]]; then
  pytest_l1 py_parity py_parity -- "${pyp_argv[@]}"
  record py_parity --required-set s21_gate:py_parity_full --command "pytest <s21_gate:py_parity_full selector.argv>" \
    --exit "$LAST_RC" --log logs/py_parity.log --l1-results py_parity/l1-results.json
else
  record py_parity --required-set s21_gate:py_parity_full --reason "l1_required.lock.json 無 s21_gate:py_parity_full.linux selector"
fi

# 11. 全套 pytest（s21_gate:pytest_full＋s21_gate:py_parity）
pytest_l1 pytest_full pytest_full -- -q tests/
record pytest_full --required-set s21_gate:pytest_full --command "SW_RESOURCE_REPORT=… python3 -m pytest -q tests/" \
  --exit "$LAST_RC" --log logs/pytest_full.log --l1-results pytest_full/l1-results.json

# 11b. 承接率：case_verdicts.json 由 Task 20／21 的逐案證據（l15_ab 兩邊、移出 l15_ab 者的承擔測試、func-test）彙整，
#     每案取最差判定（FAIL＞EVIDENCE_MISSING＞PASS），缺證據即 EVIDENCE_MISSING（OD-60／OWNER-BATCH #8）；須在承擔測試
#     （pytest_full 等）之後彙整。此處的 PASS 只表示「輸入已備齊」；judge 另以 slice_breaker.evaluate 判定斷路器
#     （tripped／OD-02 → FAIL、證據不足 → EVIDENCE_MISSING；審查 b7cd852 #1），assemble 於正式實跑附加本次快照。
cv_args=()
run_logged case_verdicts python3 "$JUDGE" case-verdicts --run-dir "$RUN_DIR"
if [[ "$LAST_RC" == 0 && -f "$RUN_DIR/case_verdicts.json" ]]; then
  record case_uptake --command "slice21_gate_judge.py case-verdicts＋slice_breaker.acceptance_rate" --exit 0 --section-verdict PASS \
    --evidence case_verdicts.json --log logs/case_verdicts.log
  cv_args=(--case-verdicts "$RUN_DIR/case_verdicts.json" --append-breaker-snapshot)
else
  record case_uptake --reason "case_verdicts.json 彙整失敗（exit $LAST_RC）" --section-verdict EVIDENCE_MISSING --exit 0 \
    --log logs/case_verdicts.log
fi

# 12. policy check
run_logged policy_check python3 -m policy_check --repo .
record policy_check --command "python3 -m policy_check --repo ." --exit "$LAST_RC" --log logs/policy_check.log \
  --section-verdict "$([[ $LAST_RC == 0 ]] && echo PASS || echo FAIL)"

# 9c. identity（CLI／daemon sha256、版本）＋判定
# F139：serialwrapd 依 Python 1:1 不提供 --version；版本取自 throwaway daemon 的 daemon.status RPC 回應 `version`。
daemon_version=""
if [[ -x "$RUST_D" ]]; then
  daemon_version="$(python3 scripts/rust/probe_daemon_version.py --daemon "$RUST_D" --cli "$RUST_CLI" \
    --work-dir "$RUN_DIR/daemon_version" 2>>"$RUN_DIR/logs/daemon_version.log" | tail -n1 || true)"
fi
ver_args=()
[[ -n "$daemon_version" ]] && ver_args=(--daemon-version "$daemon_version")
assemble --cli-path "$RUST_CLI" --daemon-path "$RUST_D" "${ver_args[@]}" "${cv_args[@]}" \
  --build-linux-exit "$BUILD_LINUX_RC" --build-windows-exit "$BUILD_WINDOWS_RC" --built-daemon-sha256 "$BUILT_D_SHA"
exit_for_verdict
