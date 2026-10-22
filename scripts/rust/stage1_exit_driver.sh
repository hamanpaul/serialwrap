#!/usr/bin/env bash
# Stage 1 出口驅動腳本（S1X G2）：Stage 1A＋1B 一併出口的可重現入口。
#
# 命令依據：plan 04 Task 19 Step 5、plan 05 Task 16 Step 4（lock／judge 不在本腳本修改範圍）。
# 執行順序（不可調換）：
#   1A：cargo 測試 → dev bundle → A/B 黑箱 → Linux cargo L1 → BB-proc → L1.5 Linux（bridge／scenarios）
#       → Windows cargo（win-test）→ Windows 黑箱（win-blackbox，產出 OutDir 與 bundle；1A 與 1B 共用此同一次執行，WB_DIR）
#   1B：cargo → sentinel bundle → 20groups → Linux cargo L1（1B）→ BB-proc（1B）→ L1.5 Linux（1B）
#       → no_onefile → bench 兩步 → Windows cargo（1B）
#       → Windows L1.5 producer 與 spike3（G3 wrapper）→ register_p4_evidence → check_p4_cases
#   完整 pytest：唯一一次，於 1B 證據登記之後執行（test_p4_required_cases_have_passing_evidence 與
#       test_cargo_workspace_tests_windows_native 會讀取 target/p4-evidence，過早執行會讀到過期證據）
#   gate（stage1a_gate.py）→ policy_check → tools.contracts
#
# 規則：
#   - 開頭檢查 git status 乾淨並記錄 HEAD；不改寫任何 tracked 檔（工具只寫 target/ 與 scratch 目錄）。
#   - 自測報告以 tools/uart_stdio_bridge/APPROVED_REVISION.json 的 tree_sha256 挑選，找不到即 exit 非 0，不退回舊報告。
#   - 報告路徑一律取工具自己印出的 run_id（win-test.sh、win-blackbox.sh、ab_blackbox.sh），不以 ls -t 猜測。
#   - 每步記錄 rc（$EV/rc.txt）後繼續；最後任一步 rc 非 0 即以 1 結束。
#
# 可覆寫環境變數：
#   RUN_ID               證據目錄名稱（預設 s1exit-<時間戳>）
#   SELFTEST_LINUX       Linux 自測報告（仍須核對核准 tree）
#   SELFTEST_WINDOWS     Windows 自測報告（仍須核對核准 tree）
#   WIN_L15_WRAPPER      Windows L1.5 wrapper（預設 scripts/rust/win-func-test.sh，G3 交付；需 --bundle）
#   SPIKE3_WRAPPER       spike3 Windows WAL wrapper（預設 scripts/rust/win-spike3.sh，G3 交付；一次跑四案）
#   SW_EDR_PRODUCT／SW_EDR_STATUS／SW_EDR_INTERCEPTIONS
#                        EDR 資料由擁有者主控台查得。未設即 unknown：spike3 工具將 PASS 降為 INSUFFICIENT_EVIDENCE
#                        （edr_downgrade），gate 判為 evidence-insufficient（整體 FAIL），而非 BLOCKED-OWNER；
#                        擁有者查得後設定 SW_EDR_* 重跑 spike3 方可 PASS（stage1a_gate._spike3_status 只對 PASS＋EDR 未知回 BLOCKED-OWNER）。

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
APPROVED_JSON="$REPO/tools/uart_stdio_bridge/APPROVED_REVISION.json"

# ── 工具函式 ───────────────────────────────────────────────────────────────

log() { echo "[$(date +%H:%M:%S)] $*" >> "$LOG"; }

# step <名稱> <命令…>：stdout／stderr 寫入 $EV/<名稱>.log，rc 記入 $EV/rc.txt 後繼續。
step() {
  local name="$1"; shift
  log "START $name"
  "$@" > "$EV/$name.log" 2>&1
  local rc=$?
  log "END $name rc=$rc"
  echo "$name $rc" >> "$EV/rc.txt"
  return 0
}

# fresh_or_fail <步驟名> <報告路徑>：產生報告的步驟之後呼叫（F3）。檔案須存在且 mtime 不早於本次開跑，
# 否則記入 rc.txt 為失敗（不得讓過期報告靜默進入登記與核對）。
fresh_or_fail() {
  local name="$1" path="$2"
  if [[ -f "$path" ]] && [[ "$(stat -c %Y "$path" 2>/dev/null || echo 0)" -ge "${RUN_START_EPOCH:-0}" ]]; then
    return 0
  fi
  log "STALE $name: 報告不存在或早於本次開跑（$path）"
  echo "fresh_$name 1" >> "$EV/rc.txt"
  return 1
}

# printed_path <步驟名> <ERE>：取出工具輸出中符合的最後一個路徑（即工具自己報告的 run_id 路徑）。
printed_path() {
  grep -oE "$2" "$EV/$1.log" 2>/dev/null | tail -n 1
}

# wait_for_path <路徑> ：最多等待約 15 秒。
wait_for_path() {
  local i
  for i in $(seq 1 150); do
    [[ -e "$1" ]] && return 0
    sleep 0.1
  done
  return 1
}

# stop_pid <pid>：結束由本腳本啟動的 throwaway 行程。
stop_pid() {
  if [[ -n "${1:-}" ]]; then
    kill "$1" 2>/dev/null
    wait "$1" 2>/dev/null
  fi
  return 0
}

# select_selftest <linux|windows> [覆寫路徑]
# 印出 tool.tree_sha256 等於核准 tree 且 verdict 為 PASS 的自測報告路徑；找不到或覆寫不合格即回傳 1。
# 被略過的候選寫入 stderr（由呼叫端導入 driver.log）。
select_selftest() {
  python3 - "$APPROVED_JSON" "$1" "${2:-}" <<'PY'
import glob
import json
import os
import sys
from pathlib import Path

approved_path, kind, override = sys.argv[1], sys.argv[2], sys.argv[3]
approved = json.loads(Path(approved_path).read_text(encoding="utf-8"))["tree_sha256"]


def check(path):
    """回傳 (created_at, 原因)；合格時原因為空字串，不合格時 created_at 為 None。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"無法讀取（{exc}）"
    tree = (doc.get("tool") or {}).get("tree_sha256")
    if tree != approved:
        return None, f"tool.tree_sha256={str(tree)[:12]} 不等於核准 tree {approved[:12]}"
    if doc.get("verdict") != "PASS":
        return None, f"verdict={doc.get('verdict')!r} 不是 PASS"
    return str(doc.get("created_at", "")), ""


if override:
    created, why = check(override)
    if created is None:
        print(f"select_selftest: 覆寫報告不合格 {override}：{why}", file=sys.stderr)
        sys.exit(1)
    print(override)
    sys.exit(0)

pattern = os.path.join(os.path.expanduser("~"), "b-log", "rust-refactor", "p2",
                       f"selftest-{kind}*", "selftest-report.json")
hits = []
for path in sorted(glob.glob(pattern)):
    created, why = check(path)
    if created is None:
        print(f"select_selftest: 略過 {path}：{why}", file=sys.stderr)
        continue
    hits.append((created, path))
if not hits:
    print(f"select_selftest: 找不到 {kind} 核准 tree {approved[:12]} 且 PASS 的自測報告（搜尋 {pattern}）",
          file=sys.stderr)
    sys.exit(1)
hits.sort()
print(hits[-1][1])
PY
}

# copy_win_test_l1 <win-test 報告.json> <目的檔>：依報告的 l1_results 欄位複製同一 run 的 l1-results。
copy_win_test_l1() {
  local rep="$1" dest="$2" name
  if [[ ! -f "$rep" ]]; then
    log "WARN win-test 報告不存在: $rep"
    return 1
  fi
  name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["l1_results"])' "$rep")" || return 1
  # cp -p 保留來源 mtime，使 fresh_or_fail 能判定 Windows 端報告是否為本次產生（F3）。
  cp -p "$(dirname "$rep")/$name" "$dest"
}

# iso_vars <scratch>：印出隔離環境變數（每行一個 KEY=VALUE），供 env -i 使用。
iso_vars() {
  local d="$1"
  printf '%s\n' "PATH=$PATH" "LANG=C.UTF-8" "HOME=$d/home" \
    "SERIALWRAP_STATE_DIR=$d/state" "SERIALWRAP_RUN_DIR=$d/run" "SERIALWRAP_CONFIG_DIR=$d/config" \
    "SERIALWRAP_WAL_DIR=$d/state/wal" "SERIALWRAP_LOG_DIR=$d/blog" "SERIALWRAP_EVENTS_DIR=$d/events.d" \
    "SERIALWRAP_EVENTS_RUNTIME_DIR=$d/events-rt" "SERIALWRAP_BY_ID_DIR=$d/by-id" "SERIALWRAP_BY_PATH_DIR=$d/by-path"
}

# AF_UNIX 路徑上限約 107 bytes；證據目錄位於長的 repo 路徑下，socket 一律放在短路徑的私有暫存目錄
# （$XDG_RUNTIME_DIR 或 $TMPDIR／/tmp 下的 mktemp 目錄），結束後刪除；日誌另存於 $EV/bench/。

# bench_linux <短路徑暫存目錄>：啟動隔離 throwaway serialwrapd，量測 Linux 暖快取往返，結束後關閉。
bench_linux() {
  local base="$1" d pid="" ISO=()
  d="$base/d"
  mkdir -p "$d/home" "$d/state" "$d/run" "$d/config" "$d/blog" "$d/events.d" "$d/events-rt" "$d/by-id" "$d/by-path" "$d/profile"
  mapfile -t ISO < <(iso_vars "$d")
  env -i "${ISO[@]}" "$SW_BUNDLE/bin/serialwrapd" --profile-dir "$d/profile" --socket "$d/s.sock" \
    --lock "$d/s.lock" > "$d/daemon.out" 2> "$d/daemon.err" &
  pid=$!
  if wait_for_path "$d/s.sock"; then
    step bench_linux_wsl2 env -i "${ISO[@]}" python3 scripts/rust/bench_cli_roundtrip.py \
      --cli "$SW_BUNDLE/bin/serialwrap" --endpoint "$d/s.sock" --platform linux \
      --json-out "$REPO/target/p4-bench-linux-wsl2.json"
    cp "$REPO/target/p4-bench-linux-wsl2.json" "$EV/bench/" 2>/dev/null
    fresh_or_fail bench_linux_wsl2 "$REPO/target/p4-bench-linux-wsl2.json"
  else
    log "WARN bench_linux: 隔離 daemon socket 未就緒"
    echo "bench_linux_wsl2 1" >> "$EV/rc.txt"
  fi
  stop_pid "$pid"
  cp "$d/daemon.err" "$EV/bench/daemon.err" 2>/dev/null
  return 0
}

# bench_cli_only <短路徑暫存目錄>：啟動 bb_stubd 替身端點，量測 CLI 歸因（REFERENCE_ONLY），結束後關閉。
bench_cli_only() {
  local base="$1" w pid="" ISO=()
  w="$base/w"
  mkdir -p "$w" "$base/home" "$base/state" "$base/run" "$base/config" "$base/blog" "$base/events.d" "$base/events-rt" "$base/by-id" "$base/by-path"
  mapfile -t ISO < <(iso_vars "$base")
  printf '%s\n' '{"default":[{"reply":{"ok":true,"running":true,"stub":"bb_stubd"}}]}' > "$base/script.json"
  python3 "$REPO/tests/contract_blackbox/bb_stubd.py" --script "$base/script.json" --workdir "$w" \
    > "$base/stub.out" 2> "$base/stub.err" &
  pid=$!
  if wait_for_path "$w/ready"; then
    step bench_cli_only_stub env -i "${ISO[@]}" python3 scripts/rust/bench_cli_roundtrip.py \
      --cli "$SW_BUNDLE/bin/serialwrap" --endpoint "$w/stubd.sock" --cli-only-stub \
      --json-out "$REPO/target/p4-bench-cli-only.json"
    cp "$REPO/target/p4-bench-cli-only.json" "$EV/bench/" 2>/dev/null
    fresh_or_fail bench_cli_only_stub "$REPO/target/p4-bench-cli-only.json"
  else
    log "WARN bench_cli_only: bb_stubd 未就緒"
    echo "bench_cli_only_stub 1" >> "$EV/rc.txt"
  fi
  stop_pid "$pid"
  cp "$base/stub.err" "$EV/bench/stub.err" 2>/dev/null
  return 0
}

# ── 主流程 ─────────────────────────────────────────────────────────────────

main() {
  set -uo pipefail
  cd "$REPO" || exit 1

  local RUN_ID EV LOG DEV_BUNDLE P4 HEAD_SHA DIRTY BENCH_BASE
  local SELFTEST_LINUX_PATH SELFTEST_WINDOWS_PATH
  local AB_REPORT WT1A_REPORT WT1B_REPORT WB_REPORT WB_DIR WIN_CLI WIN_UNIT_JUNIT
  local WIN_L15_WRAPPER SPIKE3_WRAPPER GATE_ARGS=() c rc_fail

  RUN_ID="${RUN_ID:-s1exit-$(date +%Y%m%dT%H%M%S)}"
  EV="$REPO/target/stage1a-evidence/$RUN_ID"
  LOG="$EV/driver.log"
  DEV_BUNDLE="$REPO/target/dev-bundle"
  P4="$REPO/target/p4-evidence"
  # SW_BUNDLE 必須 export：test_fresh_install_blackbox 等測試以此環境變數找 sentinel bundle（plan 05 Task 16 Step 4）。
  export SW_BUNDLE="$REPO/target/dev-bundle-sentinel"
  WIN_L15_WRAPPER="${WIN_L15_WRAPPER:-$REPO/scripts/rust/win-func-test.sh}"
  SPIKE3_WRAPPER="${SPIKE3_WRAPPER:-$REPO/scripts/rust/win-spike3.sh}"
  export SW_EDR_PRODUCT="${SW_EDR_PRODUCT:-unknown}"
  export SW_EDR_STATUS="${SW_EDR_STATUS:-unknown}"
  export SW_EDR_INTERCEPTIONS="${SW_EDR_INTERCEPTIONS:-unknown}"

  mkdir -p "$EV/l1/linux/blackbox" "$EV/l1/linux/cargo" "$EV/l1/linux/pytest-full" \
           "$EV/l1/windows/blackbox" "$EV/l1/windows/cargo" "$EV/spike3" "$EV/bench"
  : > "$EV/rc.txt"
  echo "$RUN_ID" > "$EV/RUN_ID"
  RUN_START_EPOCH="$(date +%s)"

  # 開頭：工作樹必須乾淨，並記錄 HEAD（register_p4_evidence 與 gate 的 provenance 皆對應此 revision）。
  HEAD_SHA="$(git rev-parse HEAD)"
  DIRTY="$(git status --porcelain --untracked-files=normal)"
  echo "$HEAD_SHA" > "$EV/HEAD"
  log "RUN_ID=$RUN_ID HEAD=$HEAD_SHA EV=$EV SW_BUNDLE=$SW_BUNDLE"
  if [[ -n "$DIRTY" ]]; then
    printf '%s\n' "$DIRTY" > "$EV/dirty-at-start.txt"
    log "ABORT 工作樹不乾淨（見 dirty-at-start.txt）"
    echo "stage1_exit_driver: 工作樹不乾淨，拒絕執行（見 $EV/dirty-at-start.txt）" >&2
    exit 2
  fi

  # 證據隔離（Stage 1 收尾審查 F3）：既有 target/p4-evidence 與 target/p4-bench-*.json 整個搬到本次證據目錄（搬不刪），
  # 避免 cargo_l1_results／copy 步驟失敗時，舊報告被 register 與 check 採用（_merge_records 只追加、不清除）。
  mkdir -p "$EV/p4-prev"
  if [[ -e "$P4" ]]; then
    mv "$P4" "$EV/p4-prev/p4-evidence"
    log "隔離既有 p4-evidence → $EV/p4-prev/p4-evidence"
  fi
  mkdir -p "$P4"
  for f in "$REPO"/target/p4-bench-*.json; do
    [[ -e "$f" ]] || continue
    mv "$f" "$EV/p4-prev/"
    log "隔離既有 bench 報告 → $EV/p4-prev/$(basename "$f")"
  done
  mkdir -p "$P4/l1/linux/cargo" "$P4/l1/windows/cargo" "$P4/l1/linux/20groups" "$P4/l1/windows/20groups"

  # EDR 未知警示（F2）：spike3 四列在 EDR 未知時為 evidence-insufficient（整體 FAIL），不是 BLOCKED-OWNER。
  if [[ "$SW_EDR_PRODUCT" == unknown || "$SW_EDR_STATUS" == unknown ]]; then
    log "WARN EDR 產品或狀態未知：spike3 將 PASS 降為 INSUFFICIENT_EVIDENCE，gate 判為 evidence-insufficient（FAIL），須擁有者查得 SW_EDR_* 後重跑"
    echo "WARN EDR 未知：spike3 四列將為 evidence-insufficient（整體 FAIL）" >&2
  fi

  # 自測報告：核准 tree 對應者；找不到即結束，不退回舊報告（S1X：舊 tree 9eaed6de 的報告不得進證據）。
  SELFTEST_LINUX_PATH="$(select_selftest linux "${SELFTEST_LINUX:-}" 2>>"$LOG")" || {
    log "FATAL 無 Linux 核准自測報告"; echo "stage1_exit_driver: 無 Linux 核准自測報告" >&2; exit 1; }
  SELFTEST_WINDOWS_PATH="$(select_selftest windows "${SELFTEST_WINDOWS:-}" 2>>"$LOG")" || {
    log "FATAL 無 Windows 核准自測報告"; echo "stage1_exit_driver: 無 Windows 核准自測報告" >&2; exit 1; }
  log "SELFTEST linux=$SELFTEST_LINUX_PATH"
  log "SELFTEST windows=$SELFTEST_WINDOWS_PATH"

  # ── Stage 1A（plan 04 Task 19 Step 5）──────────────────────────────────
  step ci_workflow_test python3 -m pytest -q tests/test_rust_ci_workflow.py
  step cfg_dep_boundary bash -c 'python3 scripts/rust/check_cfg_boundary.py && python3 scripts/rust/check_dep_direction.py'
  step cargo_test_all_features cargo test --workspace --all-features
  step bundle_dev_release scripts/rust/mkbundle.sh --dev --profile release --out "$DEV_BUNDLE"
  step ab_blackbox_release scripts/rust/ab_blackbox.sh --profile release --l1-out "$EV/l1"
  AB_REPORT="$(printed_path ab_blackbox_release '/[^[:space:]]*target/ab-report/[^[:space:]]+\.json')"
  log "ab-report=${AB_REPORT:-<未取得>}（取自 ab_blackbox.sh 的「A/B 比較完成」輸出）"
  step cargo_l1_linux_1a python3 scripts/rust/cargo_l1_results.py --platform linux \
    --out "$EV/l1/linux/cargo/resource-report.json" -- --workspace --all-features
  fresh_or_fail cargo_l1_linux_1a "$EV/l1/linux/cargo/resource-report.json"
  step bbproc_multiagent_coexist env SERIALWRAP_CLI_EXE="$DEV_BUNDLE/bin/serialwrap" \
    python3 -m pytest -q tests/test_multiagent_e2e.py tests/test_human_agent_coexist.py
  step l15_linux_bridge env SERIALWRAP_CLI_EXE="$DEV_BUNDLE/bin/serialwrap" \
    python3 func-test/runner.py --mode bridge --selftest-report "$SELFTEST_LINUX_PATH" --report-dir "$EV/func-test"
  step l15_linux_bridge_scenarios env SERIALWRAP_CLI_EXE="$DEV_BUNDLE/bin/serialwrap" \
    python3 func-test/runner.py --mode bridge --scenarios --selftest-report "$SELFTEST_LINUX_PATH" \
    --report-dir "$EV/func-test-scenarios"
  step win_cargo_1a scripts/rust/win-test.sh --package sw-proto --package sw-pal --package sw-cli
  WT1A_REPORT="$(printed_path win_cargo_1a '/[^[:space:]]*target/win-test-report/[0-9A-Za-z_-]+\.json')"
  copy_win_test_l1 "$WT1A_REPORT" "$EV/l1/windows/cargo/resource-report.json" 2>>"$LOG" \
    || log "WARN win cargo 1A l1 複製失敗（report=${WT1A_REPORT:-<未取得>}）"
  fresh_or_fail win_cargo_1a "$EV/l1/windows/cargo/resource-report.json"
  step win_blackbox_1a env SW_TEST_COM_PAIR=COM20:COM21 scripts/rust/win-blackbox.sh
  WB_REPORT="$(printed_path win_blackbox_1a '/[^[:space:]]*target/win-blackbox/[^/[:space:]]+/report\.json')"
  if [[ -n "$WB_REPORT" ]]; then
    WB_DIR="$(dirname "$WB_REPORT")"
  else
    WB_DIR="$EV/missing-win-blackbox"
    WB_REPORT="$WB_DIR/report.json"
    log "WARN win-blackbox 未取得 report 路徑；gate 將判定證據不足"
  fi
  log "win-blackbox dir=$WB_DIR"
  if [[ -d "$WB_DIR/l1/windows/blackbox" ]]; then
    cp -r "$WB_DIR/l1/windows/blackbox/." "$EV/l1/windows/blackbox/" 2>>"$LOG" \
      || log "WARN win blackbox 1A l1 複製失敗"
  fi

  # ── Stage 1B（plan 05 Task 16 Step 4）──────────────────────────────────
  step cargo_test_workspace cargo test --workspace
  step cargo_build_release cargo build --workspace --release
  step win_cargo_1b scripts/rust/win-test.sh --l1-set s1b_cargo --package sw-pal --package sw-cli
  WT1B_REPORT="$(printed_path win_cargo_1b '/[^[:space:]]*target/win-test-report/[0-9A-Za-z_-]+\.json')"
  mkdir -p "$P4/l1/windows/cargo"
  copy_win_test_l1 "$WT1B_REPORT" "$P4/l1/windows/cargo/resource-report.json" 2>>"$LOG" \
    || log "WARN win cargo 1B l1 複製失敗（report=${WT1B_REPORT:-<未取得>}）"
  fresh_or_fail win_cargo_1b "$P4/l1/windows/cargo/resource-report.json"
  step bundle_sentinel scripts/rust/mkbundle.sh --dev --delegate-sentinel --out "$SW_BUNDLE"
  # P4 目錄已於開頭隔離（F3），此處不再需要 ab-suite 改名；20groups 報告一律為本次產生。
  step suite_20groups_linux env SERIALWRAP_CLI_EXE="$SW_BUNDLE/bin/serialwrap" \
    SERIALWRAP_DAEMON_EXE="$SW_BUNDLE/bin/serialwrapd" SERIALWRAP_AB_LABEL=rust-1b \
    SW_RESOURCE_REPORT="$P4/l1/linux/20groups/resource-report.json" \
    python3 -m pytest tests/contract_blackbox -m "core_portable or posix_only" \
    --junitxml="$P4/l1/linux/20groups/junit.xml"
  fresh_or_fail suite_20groups_linux_junit "$P4/l1/linux/20groups/junit.xml"
  fresh_or_fail suite_20groups_linux "$P4/l1/linux/20groups/resource-report.json"
  step cargo_l1_linux_1b python3 scripts/rust/cargo_l1_results.py --platform linux \
    --out "$P4/l1/linux/cargo/resource-report.json" -- --workspace
  fresh_or_fail cargo_l1_linux_1b "$P4/l1/linux/cargo/resource-report.json"
  step bbproc_1b env SERIALWRAP_CLI_EXE="$SW_BUNDLE/bin/serialwrap" \
    python3 -m pytest -q tests/test_multiagent_e2e.py tests/test_human_agent_coexist.py
  step l15_linux_1b_bridge env SERIALWRAP_CLI_EXE="$SW_BUNDLE/bin/serialwrap" \
    python3 func-test/runner.py --mode bridge --selftest-report "$SELFTEST_LINUX_PATH" \
    --report-dir "$P4/l15-linux"
  step l15_linux_1b_scenarios env SERIALWRAP_CLI_EXE="$SW_BUNDLE/bin/serialwrap" \
    python3 func-test/runner.py --mode bridge --scenarios --selftest-report "$SELFTEST_LINUX_PATH" \
    --report-dir "$P4/l15-linux-scenarios"
  step no_onefile_extract python3 scripts/rust/check_no_onefile_extract.py --cli "$SW_BUNDLE/bin/serialwrap" --runs 100
  BENCH_BASE="$(mktemp -d "${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/s1x-bench.XXXXXX")" || BENCH_BASE=""
  if [[ -n "$BENCH_BASE" ]]; then
    bench_linux "$BENCH_BASE"
    bench_cli_only "$BENCH_BASE"
    rm -rf -- "$BENCH_BASE"
  else
    log "WARN 無法建立 bench socket 暫存目錄"
    echo "bench_linux_wsl2 1" >> "$EV/rc.txt"
    echo "bench_cli_only_stub 1" >> "$EV/rc.txt"
  fi
  if [[ -d "$WB_DIR/l1/windows/blackbox" ]]; then
    cp -r "$WB_DIR/l1/windows/blackbox/." "$P4/l1/windows/20groups/" 2>>"$LOG" \
      || log "WARN p4 windows 20groups 複製失敗"
  fi

  # Windows L1.5（win-func-test.sh，COM20:COM21，argv 依 plan 04 Step 5 Windows 形式、不帶 --case）與 spike3
  # （win-spike3.sh 一次跑四案，證據寫入 $EV/spike3/<case>.json）。兩者需要 G4 匯出的 Windows dev bundle（$WB_DIR/bundle）；
  # bundle 缺漏時 wrapper 直接失敗，gate 判為證據不足。
  step win_l15_func_test "$WIN_L15_WRAPPER" --bundle "$WB_DIR/bundle" \
    --selftest-report "$SELFTEST_WINDOWS_PATH" --report-dir "$EV/func-test-windows"
  step spike3_win_wal "$SPIKE3_WRAPPER" --evidence-dir "$EV" \
    --edr-product "$SW_EDR_PRODUCT" --edr-status "$SW_EDR_STATUS" --edr-interceptions "$SW_EDR_INTERCEPTIONS"

  # 登記與核對（register 要求乾淨工作樹；Windows CLI 與 junit 取自 win-blackbox 的 OutDir 佈局）。
  WIN_CLI="$WB_DIR/bundle/bin/serialwrap.exe"
  WIN_UNIT_JUNIT="$WB_DIR/l1/windows/blackbox/junit.xml"
  step register_p4_evidence python3 scripts/rust/register_p4_evidence.py \
    --linux-cli "$SW_BUNDLE/bin/serialwrap" --linux-unit-junit "$P4/l1/linux/20groups/junit.xml" \
    --l1-dir "$P4/l1" --windows-cli "$WIN_CLI" --windows-unit-junit "$WIN_UNIT_JUNIT"
  step check_p4_cases python3 scripts/rust/check_p4_cases.py --check

  # 唯一一次完整 pytest（同一 HEAD；JUnit 與 resource report 於 1B 登記之後產生）。
  step pytest_full_junit env PYTHONPATH=scripts/rust SW_REQUIRE_CARGO=1 \
    SW_RESOURCE_REPORT="$EV/l1/linux/pytest-full/resource-report.json" \
    python3 -m pytest -q tests/ -p junit_provenance --junitxml="$EV/pytest-full.xml"
  fresh_or_fail pytest_full_junit "$EV/pytest-full.xml"
  fresh_or_fail pytest_full_report "$EV/l1/linux/pytest-full/resource-report.json"

  # 出口 gate：L1.5 含 Linux 與 Windows、spike3、明確的 A/B 報告與 Windows 黑箱來源。
  GATE_ARGS=(--out "$EV/stage1a-gate.json" --l1-results "$EV/l1" --pytest-junit-report "$EV/pytest-full.xml"
    --ab-report "${AB_REPORT:-$EV/ab-report-missing.json}"
    --l15-report "linux=$EV/func-test/func-test-report.json"
    --l15-report "linux=$EV/func-test-scenarios/func-test-report.json"
    --l15-report "windows=$EV/func-test-windows/func-test-report.json"
    --spike3-evidence "$EV/spike3"
    --win-report "$WB_REPORT" --windows-com "$WB_DIR")
  log "GATE_ARGS=${GATE_ARGS[*]}"
  step stage1a_gate python3 scripts/rust/stage1a_gate.py "${GATE_ARGS[@]}"
  step policy_check python3 -m policy_check --repo .
  step tools_contracts python3 -m tools.contracts check
  step git_status_end bash -c 'git status --short; echo "HEAD=$(git rev-parse HEAD)"'

  rc_fail="$(awk '$2 != 0' "$EV/rc.txt" | wc -l)"
  log "DRIVER DONE failed_steps=$rc_fail"
  echo "DONE" >> "$EV/rc.txt"
  [[ "$rc_fail" -eq 0 ]]
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
