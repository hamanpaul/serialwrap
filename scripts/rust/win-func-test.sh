#!/usr/bin/env bash
# S1X G3：以 WSL interop 在 Windows 本機磁碟執行 L1.5 bridge 情境（COM20:COM21），產出 stage1a_gate 的 `--l15-report windows=` 來源。
#
# 每次呼叫於同一 staging、同一 bundle、同一自測報告下執行兩次 runner：
#   1. 情境（scenarios）：py -3.12 func-test/runner.py --mode bridge --scenarios --daemon-com COM20 --bridge-com COM21 \
#        --transport-mode virtual_com0com --selftest-report <Windows 自測報告> --report-dir <run>\report
#      報告：<report-dir>/func-test-report.json（plan 04 Step 5 的 Windows 形式，argv 不變）。
#   2. 基本案例（basic）：同上但不帶 --scenarios（func-test/cases 中 bridge.applicable 的案例）。
#      報告：<report-dir>/basic/func-test-report.json。
#      理由（plan 04 Step 5 S1X 修訂）：cases.lock.json 宣告 brcm-01／ev-01／ha-05／sm-01 的平台為 [linux, windows]，
#      gate 5c-iv 要求每個宣告平台都有列；只跑情境時 Windows 永遠缺這些列。
# 只使用 COM20（serialwrap／受測端）與 COM21（bridge／測試端）；本 wrapper 不接受其他 COM 參數，
# 也不接觸既有的隔離排程工作、其 serialwrapd，或其他 Windows 行程。
# runner 的 SERIALWRAP_CLI_EXE 指向 staging 中複製的 bundle（<bundle>\bin\serialwrap.exe）。
# 編碼：只設定 PYTHONIOENCODING=utf-8（runner 自身的 stdio）；不設定 PYTHONUTF8，使 bridge 子行程呼叫 git／powershell
# 時依系統 locale（Windows 為 cp950）解碼，避免 0xAA（Big5「版」）等位元組在 reader thread 拋出 UnicodeDecodeError。
#
# 用法：
#   scripts/rust/win-func-test.sh --bundle <WSL 路徑：G4 匯出的 Windows dev bundle> \
#     --selftest-report <WSL 路徑：Windows 自測報告 JSON> --report-dir <WSL 輸出目錄>
#   scripts/rust/win-func-test.sh --print-argv          # 僅輸出情境 runner argv（佔位路徑），不執行 interop
#   scripts/rust/win-func-test.sh --print-basic-argv    # 僅輸出基本案例 runner argv（佔位路徑），不執行 interop
# 環境變數：SW_WIN_TIMEOUT（每次 runner 的逾時秒數，預設 5400）、SW_WIN_KEEP=1（保留 Windows staging）。
set -euo pipefail

SW_WIN_TOOL=win-func-test.sh
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
# shellcheck source=scripts/rust/win-stage-lib.sh
source "$ROOT/scripts/rust/win-stage-lib.sh"

DAEMON_COM=COM20
BRIDGE_COM=COM21
TRANSPORT_MODE=virtual_com0com
RUNNER_TIMEOUT_S="${SW_WIN_TIMEOUT:-5400}"
RUNNER_ARGV=()

usage() {
  cat <<'USAGE'
用法：
  scripts/rust/win-func-test.sh --bundle <目錄> --selftest-report <JSON> --report-dir <目錄>
  scripts/rust/win-func-test.sh --print-argv
  scripts/rust/win-func-test.sh --print-basic-argv

選項：
  --bundle <目錄>            G4 匯出的 Windows dev bundle（需含 bin/serialwrap.exe 與 bin/current-release.json）
  --selftest-report <檔案>   Windows bridge 自測報告（verdict PASS、win-serial、virtual_com0com、COM20:COM21）
  --report-dir <目錄>        輸出目錄（情境報告與 meta 於此；基本案例報告於 basic/；runner 日誌於 runner-logs/）
  --print-argv               僅輸出情境 runner argv（JSON 陣列，路徑為佔位），不執行
  --print-basic-argv         僅輸出基本案例 runner argv（JSON 陣列，路徑為佔位），不執行
USAGE
}

# runner_argv <自測報告 Windows 路徑> <報告目錄 Windows 路徑> [basic]：設定 RUNNER_ARGV。
# 無第三參數為情境（plan 04 Step 5 的 Windows 形式）；basic 為基本案例（不帶 --scenarios）。
runner_argv() {
  local scenario_flag=(--scenarios)
  if [[ "${3:-}" == basic ]]; then
    scenario_flag=()
  fi
  RUNNER_ARGV=(-3.12 func-test/runner.py --mode bridge ${scenario_flag[@]+"${scenario_flag[@]}"}
    --daemon-com "$DAEMON_COM" --bridge-com "$BRIDGE_COM" --transport-mode "$TRANSPORT_MODE"
    --selftest-report "$1" --report-dir "$2")
}

# json_array <參數...>：印出 JSON 陣列（供 meta 使用）。
json_array() {
  python3 -c 'import json, sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))' "$@"
}

bundle=""
selftest=""
report_dir=""
print_argv=0
print_basic=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle)
      [[ $# -ge 2 ]] || sw_win_die "--bundle 缺少參數"
      bundle="$2"; shift 2 ;;
    --selftest-report)
      [[ $# -ge 2 ]] || sw_win_die "--selftest-report 缺少參數"
      selftest="$2"; shift 2 ;;
    --report-dir)
      [[ $# -ge 2 ]] || sw_win_die "--report-dir 缺少參數"
      report_dir="$2"; shift 2 ;;
    --print-argv) print_argv=1; shift ;;
    --print-basic-argv) print_basic=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) sw_win_die "未知參數 $1（本 wrapper 固定 COM20:COM21，不接受 COM 參數）" ;;
  esac
done

if [[ "$print_argv" -eq 1 ]]; then
  runner_argv '<RUN>\selftest-report.json' '<RUN>\report'
  json_array "${RUNNER_ARGV[@]}"
  exit 0
fi
if [[ "$print_basic" -eq 1 ]]; then
  runner_argv '<RUN>\selftest-report.json' '<RUN>\report-basic' basic
  json_array "${RUNNER_ARGV[@]}"
  exit 0
fi

[[ -n "$bundle" ]] || sw_win_die "缺少 --bundle（G4 匯出的 Windows dev bundle）"
[[ -f "$bundle/bin/serialwrap.exe" && -f "$bundle/bin/current-release.json" ]] \
  || sw_win_die "bundle 不完整（需 bin/serialwrap.exe 與 bin/current-release.json）：$bundle"
[[ -n "$selftest" && -f "$selftest" ]] || sw_win_die "找不到 Windows 自測報告：$selftest"
[[ -n "$report_dir" ]] || sw_win_die "缺少 --report-dir"

# 自測報告須為本 wrapper 所用的 pair 與 transport；runner 會再以核准 revision 判定，此處僅先行擋下明顯錯配。
python3 - "$selftest" <<'PY' || sw_win_die "自測報告不符（需 verdict PASS、complete、win-serial、virtual_com0com、COM20:COM21）"
import json
import sys

report = json.load(open(sys.argv[1], encoding="utf-8"))
ok = (
    report.get("verdict") == "PASS"
    and report.get("complete") is True
    and report.get("endpoint") == "win-serial"
    and report.get("transport_mode") == "virtual_com0com"
    and (report.get("platform") or {}).get("com_pair") == ["COM20", "COM21"]
)
sys.exit(0 if ok else 2)
PY

sw_win_require_interop
trap sw_win_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_id="$(date +%y%m%d-%H%M%S)-$$"
sw_win_stage_create "$run_id"
sw_win_write_launcher

cp -a -- "$bundle" "$SW_WIN_RUN_UNIX/bundle"
cp -- "$selftest" "$SW_WIN_RUN_UNIX/selftest-report.json"
mkdir -p -- "$SW_WIN_RUN_UNIX/report-basic"
selftest_sha="$(sha256sum "$selftest" | cut -d' ' -f1)"
cli_sha="$(sha256sum "$bundle/bin/serialwrap.exe" | cut -d' ' -f1)"

sw_win_write_env \
  "PYTHONIOENCODING=utf-8" \
  "PYTHONDONTWRITEBYTECODE=1" \
  "TMP=$SW_WIN_RUN_WIN\\tmp" \
  "TEMP=$SW_WIN_RUN_WIN\\tmp" \
  "SERIALWRAP_CLI_EXE=$SW_WIN_RUN_WIN\\bundle\\bin\\serialwrap.exe" \
  "SERIALWRAP_TEST_DEVICE_ALLOWLIST=$DAEMON_COM"

echo "win-func-test: staging=$SW_WIN_RUN_UNIX（HEAD=$SW_WIN_HEAD，git_clean=$SW_WIN_GIT_CLEAN）"

# 第一次：情境（argv 與 plan 04 Step 5 Windows 形式逐字相同）。
runner_argv "$SW_WIN_RUN_WIN\\selftest-report.json" "$SW_WIN_RUN_WIN\\report"
scenario_argv=("${RUNNER_ARGV[@]}")
sw_win_run_child "$RUNNER_TIMEOUT_S" "${RUNNER_ARGV[@]}"
scenario_rc="$SW_WIN_CHILD_RC"
sw_win_collect_child_logs "$SW_WIN_RUN_UNIX/logs/runner"
echo "win-func-test: 情境執行結束 exit=$scenario_rc"

# 第二次：基本案例（同一 staging；不因情境失敗而略過）。
runner_argv "$SW_WIN_RUN_WIN\\selftest-report.json" "$SW_WIN_RUN_WIN\\report-basic" basic
basic_argv=("${RUNNER_ARGV[@]}")
sw_win_run_child "$RUNNER_TIMEOUT_S" "${RUNNER_ARGV[@]}"
basic_rc="$SW_WIN_CHILD_RC"
sw_win_collect_child_logs "$SW_WIN_RUN_UNIX/logs/runner-basic"
echo "win-func-test: 基本案例執行結束 exit=$basic_rc"

mkdir -p -- "$report_dir"
cp -a -- "$SW_WIN_RUN_UNIX/report/." "$report_dir/"
mkdir -p -- "$report_dir/basic"
cp -a -- "$SW_WIN_RUN_UNIX/report-basic/." "$report_dir/basic/"
mkdir -p -- "$report_dir/runner-logs"
for f in "$SW_WIN_RUN_UNIX"/logs/runner*.*; do
  if [[ -e "$f" ]]; then
    cp -- "$f" "$report_dir/runner-logs/"
  fi
done

python3 - "$report_dir/win-func-test-meta.json" "$run_id" "$SW_WIN_HEAD" "$SW_WIN_TREE" "$SW_WIN_GIT_CLEAN" \
  "$selftest" "$selftest_sha" "$cli_sha" "$RUNNER_TIMEOUT_S" "$DAEMON_COM" "$BRIDGE_COM" \
  "$TRANSPORT_MODE" "$scenario_rc" "$basic_rc" "$(json_array "${scenario_argv[@]}")" "$(json_array "${basic_argv[@]}")" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

(out, run_id, head, tree, git_clean, selftest, selftest_sha, cli_sha, timeout_s,
 daemon_com, bridge_com, transport, scenario_rc, basic_rc, scenario_argv, basic_argv) = sys.argv[1:17]
selftest_doc = json.loads(Path(selftest).read_text(encoding="utf-8"))
meta = {
    "schema_version": "win-func-test-meta.v2",
    "run_id": run_id,
    "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "git_head": head,
    "git_tree": tree,
    "git_clean": git_clean == "true",
    "com_pair": {"daemon": daemon_com, "bridge": bridge_com},
    "transport_mode": transport,
    "selftest_report_sha256": selftest_sha,
    "tool_tree_sha256": (selftest_doc.get("tool") or {}).get("tree_sha256"),
    "cli_exe_sha256": cli_sha,
    "runner_timeout_s": int(timeout_s),
    # 與 v1 相容的頂層欄位指向情境執行；完整兩次執行見 runs。
    "runner_argv": json.loads(scenario_argv),
    "runner_exit": int(scenario_rc),
    "runs": {
        "scenarios": {
            "runner_argv": json.loads(scenario_argv),
            "runner_exit": int(scenario_rc),
            "report": "func-test-report.json",
        },
        "basic": {
            "runner_argv": json.loads(basic_argv),
            "runner_exit": int(basic_rc),
            "report": "basic/func-test-report.json",
        },
    },
}
Path(out).write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

rc_scenario="$scenario_rc"
rc_basic="$basic_rc"
if [[ ! -f "$report_dir/func-test-report.json" ]]; then
  echo "win-func-test: 情境 runner 未產生 func-test-report.json（exit=$scenario_rc）" >&2
  [[ "$rc_scenario" -ne 0 ]] || rc_scenario=125
fi
if [[ ! -f "$report_dir/basic/func-test-report.json" ]]; then
  echo "win-func-test: 基本案例 runner 未產生 basic/func-test-report.json（exit=$basic_rc）" >&2
  [[ "$rc_basic" -ne 0 ]] || rc_basic=125
fi
echo "win-func-test: report_dir=$report_dir scenarios_exit=$rc_scenario basic_exit=$rc_basic"
if [[ "$rc_scenario" -ne 0 ]]; then
  exit "$rc_scenario"
fi
exit "$rc_basic"
