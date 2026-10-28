#!/usr/bin/env bash
# S1X G3／OD-19：以 WSL interop 在 Windows 本機磁碟執行 spike3 四案，產出 stage1a_gate 的 `--spike3-evidence` 來源。
#
# 案例：win-wal-av-rotate、win-wal-av-unlink、win-wal-av-reader-handle、win-wal-f12-progress。
# argv：py -3.12 scripts/stage0/spike3_win_wal.py --case <id> --evidence-dir <run>\spike3
#
# OD-19：防毒產品名與狀態由 spike3 工具自動唯讀查詢（SecurityCenter2），不接受 --edr-* 自填參數，
# wrapper 亦不傳遞任何 EDR 值；各案在 scripts/stage0/av_sim.py 注入下執行。
# 證據複製到 <EV>/spike3/<case_id>.json；子行程輸出保存於 <EV>/spike3/logs/<case_id>.{stdout,stderr}；
# 非 PASS 案例殘留的 WAL 暫存目錄複製到 <EV>/spike3/diag/<case_id>/（診斷用，不計證據）。
# 暫存目錄以 TMP／TEMP 導向本次 staging，結束即由 staging 清除。
#
# 用法：
#   scripts/rust/win-spike3.sh --evidence-dir <WSL 目錄>
#   scripts/rust/win-spike3.sh --print-argv   # 僅輸出各案 argv（佔位路徑），不執行 interop
# 環境變數：SW_WIN_CASE_TIMEOUT（每案逾時秒數，預設 900）、SW_WIN_KEEP=1（保留 Windows staging）。
set -euo pipefail

SW_WIN_TOOL=win-spike3.sh
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
# shellcheck source=scripts/rust/win-stage-lib.sh
source "$ROOT/scripts/rust/win-stage-lib.sh"

SPIKE3_CASES=(win-wal-av-rotate win-wal-av-unlink win-wal-av-reader-handle win-wal-f12-progress)
CASE_TIMEOUT_S="${SW_WIN_CASE_TIMEOUT:-900}"

usage() {
  cat <<'USAGE'
用法：
  scripts/rust/win-spike3.sh --evidence-dir <目錄>
  scripts/rust/win-spike3.sh --print-argv

選項：
  --evidence-dir <目錄>       證據輸出根目錄（寫入 <目錄>/spike3/<case_id>.json）
  --print-argv                僅輸出各案 argv（JSON），不執行
USAGE
}

# spike3_argv <case_id> <證據目錄 Windows 路徑>：設定 RUNNER_ARGV（spike3 工具 argv）。
spike3_argv() {
  RUNNER_ARGV=(-3.12 scripts/stage0/spike3_win_wal.py --case "$1" --evidence-dir "$2")
}

evidence_dir=""
print_argv=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --evidence-dir)
      [[ $# -ge 2 ]] || sw_win_die "--evidence-dir 缺少參數"
      evidence_dir="$2"; shift 2 ;;
    --print-argv) print_argv=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) sw_win_die "未知參數 $1" ;;
  esac
done

if [[ "$print_argv" -eq 1 ]]; then
  python3 - "${SPIKE3_CASES[@]}" <<'PY'
import json
import sys

cases = sys.argv[1:]
rows = [["-3.12", "scripts/stage0/spike3_win_wal.py", "--case", c, "--evidence-dir", "<RUN>\\spike3"]
        for c in cases]
print(json.dumps(rows, ensure_ascii=False))
PY
  exit 0
fi

[[ -n "$evidence_dir" ]] || sw_win_die "缺少 --evidence-dir"
sw_win_require_interop
trap sw_win_cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_id="$(date +%y%m%d-%H%M%S)-$$"
sw_win_stage_create "$run_id"
sw_win_write_launcher
sw_win_write_env \
  "PYTHONUTF8=1" \
  "PYTHONDONTWRITEBYTECODE=1" \
  "TMP=$SW_WIN_RUN_WIN\\tmp" \
  "TEMP=$SW_WIN_RUN_WIN\\tmp"
echo "win-spike3: staging=$SW_WIN_RUN_UNIX（HEAD=$SW_WIN_HEAD，git_clean=$SW_WIN_GIT_CLEAN）"

ev_spike="$evidence_dir/spike3"
mkdir -p -- "$ev_spike"
results=()
for cid in "${SPIKE3_CASES[@]}"; do
  spike3_argv "$cid" "$SW_WIN_RUN_WIN\\spike3"
  sw_win_run_child "$CASE_TIMEOUT_S" "${RUNNER_ARGV[@]}"
  results+=("$cid=$SW_WIN_CHILD_RC")
  sw_win_collect_child_logs "$ev_spike/logs/$cid"
  # 非 PASS 案例會保留 swspike3-* 暫存目錄供檢視；移到證據旁的 diag/，不計為證據。
  for d in "$SW_WIN_RUN_UNIX"/tmp/swspike3-*; do
    [[ -d "$d" ]] || continue
    mkdir -p -- "$ev_spike/diag/$cid"
    mv -- "$d" "$ev_spike/diag/$cid/"
  done
done

copied=0
for cid in "${SPIKE3_CASES[@]}"; do
  rm -f -- "$ev_spike/$cid.json"
  if [[ -f "$SW_WIN_RUN_UNIX/spike3/$cid.json" ]]; then
    cp -- "$SW_WIN_RUN_UNIX/spike3/$cid.json" "$ev_spike/$cid.json"
    copied=$((copied + 1))
  fi
done

python3 - "$ev_spike/run-meta.json" "$run_id" "$SW_WIN_HEAD" "$SW_WIN_TREE" "$SW_WIN_GIT_CLEAN" \
  "${#SPIKE3_CASES[@]}" "$CASE_TIMEOUT_S" "${results[@]}" <<'PY'
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

out, run_id, head, tree, git_clean, n_cases, timeout_s = sys.argv[1:8]
results = sys.argv[8:]
ev = Path(out).parent
files = {}
for path in sorted(ev.glob("win-wal-*.json")):
    files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
meta = {
    "schema_version": "win-spike3-run-meta.v1",
    "run_id": run_id,
    "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "git_head": head,
    "git_tree": tree,
    "git_clean": git_clean == "true",
    "case_count": int(n_cases),
    "case_timeout_s": int(timeout_s),
    "case_exit_codes": dict(item.split("=", 1) for item in results),
    "evidence_sha256": files,
}
Path(out).write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

for cid in "${SPIKE3_CASES[@]}"; do
  if [[ -f "$ev_spike/$cid.json" ]]; then
    verdict="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("verdict"))' "$ev_spike/$cid.json")"
  else
    verdict="MISSING"
  fi
  echo "win-spike3: $cid verdict=$verdict"
done
echo "win-spike3: evidence_dir=$ev_spike copied=$copied/${#SPIKE3_CASES[@]}"
[[ "$copied" -eq "${#SPIKE3_CASES[@]}" ]] || exit 1
exit 0
