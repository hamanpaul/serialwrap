#!/usr/bin/env bash
# S1X G3：以 WSL interop 在 Windows 本機磁碟執行 L1.5 bridge 情境（COM20:COM21），產出 stage1a_gate 的 `--l15-report windows=` 來源。
#
# argv 依 docs/rust-refactor/plans/04-stage1a-rust-cli.md Step 5 的 Windows 形式（不帶 --case，執行全部 scenarios）：
#   py -3.12 func-test/runner.py --mode bridge --scenarios --daemon-com COM20 --bridge-com COM21 \
#     --transport-mode virtual_com0com --selftest-report <Windows 自測報告> --report-dir <run>\report
# 只使用 COM20（serialwrap／受測端）與 COM21（bridge／測試端）；本 wrapper 不接受其他 COM 參數，
# 也不接觸既有的隔離排程工作、其 serialwrapd，或其他 Windows 行程。
# runner 的 SERIALWRAP_CLI_EXE 指向 staging 中複製的 bundle（<bundle>\bin\serialwrap.exe）。
#
# 用法：
#   scripts/rust/win-func-test.sh --bundle <WSL 路徑：G4 匯出的 Windows dev bundle> \
#     --selftest-report <WSL 路徑：Windows 自測報告 JSON> --report-dir <WSL 輸出目錄>
#   scripts/rust/win-func-test.sh --print-argv      # 僅輸出 runner argv（佔位路徑），不執行 interop
# 環境變數：SW_WIN_TIMEOUT（runner 逾時秒數，預設 5400）、SW_WIN_KEEP=1（保留 Windows staging）。
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

選項：
  --bundle <目錄>            G4 匯出的 Windows dev bundle（需含 bin/serialwrap.exe 與 bin/current-release.json）
  --selftest-report <檔案>   Windows bridge 自測報告（verdict PASS、win-serial、virtual_com0com、COM20:COM21）
  --report-dir <目錄>        輸出目錄（report/ 內容、meta 與 runner 日誌複製於此）
  --print-argv               僅輸出 runner argv（JSON 陣列，路徑為佔位），不執行
USAGE
}

# runner_argv <自測報告 Windows 路徑> <報告目錄 Windows 路徑>：設定 RUNNER_ARGV（plan 04 Step 5 的 Windows 形式）。
runner_argv() {
  RUNNER_ARGV=(-3.12 func-test/runner.py --mode bridge --scenarios
    --daemon-com "$DAEMON_COM" --bridge-com "$BRIDGE_COM" --transport-mode "$TRANSPORT_MODE"
    --selftest-report "$1" --report-dir "$2")
}

bundle=""
selftest=""
report_dir=""
print_argv=0
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
    -h|--help) usage; exit 0 ;;
    *) sw_win_die "未知參數 $1（本 wrapper 固定 COM20:COM21，不接受 COM 參數）" ;;
  esac
done

if [[ "$print_argv" -eq 1 ]]; then
  runner_argv '<RUN>\selftest-report.json' '<RUN>\report'
  python3 -c 'import json, sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))' "${RUNNER_ARGV[@]}"
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
selftest_sha="$(sha256sum "$selftest" | cut -d' ' -f1)"
cli_sha="$(sha256sum "$bundle/bin/serialwrap.exe" | cut -d' ' -f1)"

sw_win_write_env \
  "PYTHONUTF8=1" \
  "PYTHONDONTWRITEBYTECODE=1" \
  "TMP=$SW_WIN_RUN_WIN\\tmp" \
  "TEMP=$SW_WIN_RUN_WIN\\tmp" \
  "SERIALWRAP_CLI_EXE=$SW_WIN_RUN_WIN\\bundle\\bin\\serialwrap.exe" \
  "SERIALWRAP_TEST_DEVICE_ALLOWLIST=$DAEMON_COM"

runner_argv "$SW_WIN_RUN_WIN\\selftest-report.json" "$SW_WIN_RUN_WIN\\report"
echo "win-func-test: staging=$SW_WIN_RUN_UNIX（HEAD=$SW_WIN_HEAD，git_clean=$SW_WIN_GIT_CLEAN）"
sw_win_run_child "$RUNNER_TIMEOUT_S" "${RUNNER_ARGV[@]}"
child_rc="$SW_WIN_CHILD_RC"
sw_win_collect_child_logs "$SW_WIN_RUN_UNIX/logs/runner"

mkdir -p -- "$report_dir"
cp -a -- "$SW_WIN_RUN_UNIX/report/." "$report_dir/"
mkdir -p -- "$report_dir/runner-logs"
for f in "$SW_WIN_RUN_UNIX"/logs/runner.*; do
  [[ -e "$f" ]] && cp -- "$f" "$report_dir/runner-logs/"
done

python3 - "$report_dir/win-func-test-meta.json" "$run_id" "$SW_WIN_HEAD" "$SW_WIN_TREE" "$SW_WIN_GIT_CLEAN" \
  "$selftest" "$selftest_sha" "$cli_sha" "$child_rc" "$RUNNER_TIMEOUT_S" "$DAEMON_COM" "$BRIDGE_COM" \
  "$TRANSPORT_MODE" "${RUNNER_ARGV[@]}" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

(out, run_id, head, tree, git_clean, selftest, selftest_sha, cli_sha, child_rc, timeout_s,
 daemon_com, bridge_com, transport) = sys.argv[1:14]
argv = sys.argv[14:]
selftest_doc = json.loads(Path(selftest).read_text(encoding="utf-8"))
meta = {
    "schema_version": "win-func-test-meta.v1",
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
    "runner_argv": argv,
    "runner_timeout_s": int(timeout_s),
    "runner_exit": int(child_rc),
}
Path(out).write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

if [[ ! -f "$report_dir/func-test-report.json" ]]; then
  echo "win-func-test: runner 未產生 func-test-report.json（exit=$child_rc）" >&2
  [[ "$child_rc" -ne 0 ]] || child_rc=125
fi
echo "win-func-test: report_dir=$report_dir exit=$child_rc"
exit "$child_rc"
