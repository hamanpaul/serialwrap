#!/usr/bin/env bash
# Slice 2.3（P7）出口 gate 編排（08-slice23-login-events.md Task 19 Step 3）。
#
# 用法：scripts/rust/p7_gate.sh [--dry-run] [--shared-evidence DIR] [--run-id ID] [--out-md PATH] [--skip-full-pytest]
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
export CARGO_INCREMENTAL=0
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_ID="$(date +%y%m%d-%H%M%S)"
OUT_MD=""
SKIP_FULL=0
DRY_RUN=0
SHARED_EVIDENCE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --shared-evidence) [[ $# -ge 2 ]] || { echo "p7_gate.sh: --shared-evidence 缺少值" >&2; exit 2; }; SHARED_EVIDENCE="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    --out-md) OUT_MD="$2"; shift 2 ;;
    --skip-full-pytest) SKIP_FULL=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "p7_gate.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done
[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "p7_gate.sh: --run-id 只允許英數與 ._-" >&2; exit 2; }
if [[ "$DRY_RUN" == "1" ]]; then
  echo "p7_gate.sh dry-run：只列出計畫，不執行命令。"
  if [[ -n "$SHARED_EVIDENCE" ]]; then
    echo "shared-evidence import：debug/release artifacts、cargo_fakes_p7、pytest_full、p7_ab 與 Windows L1（$SHARED_EVIDENCE）；selector/provenance/SHA 不符記 EVIDENCE_MISSING，FAIL 照傳且不重跑。artifact provenance 不符：EVIDENCE_MISSING，不啟動本地建置。"
  else
    echo "執行：cargo build --workspace --bins；cargo build --release -p sw-daemon -p sw-cli --target x86_64-unknown-linux-gnu。"
    echo "執行：python3 scripts/rust/cargo_l1_results.py --platform linux -- --workspace --features test-fakes。"
    echo "執行：bash scripts/rust/run_p7_ab.sh --run-id <ID> --run-dir <RUN>/ab --out <RUN>/p7-ab.md --rust-cli <CLI> --rust-daemon <DAEMON> --od59-contract。"
    echo "執行：bash scripts/rust/win-test.sh --package sw-pal --package sw-core --package sw-daemon -- --features test-fakes -- --skip cap_。"
    echo "執行：bash scripts/rust/win-test.sh --caps windows_com --package sw-daemon -- --test cap_windows_com_p7 --features test-fakes -- --ignored cap_windows_com__（僅允許已核准 com0com pair）。"
    echo "執行：python3 -m pytest -q tests/ --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native --deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence。"
  fi
  echo "執行：python3 scripts/rust/check_cfg_boundary.py；python3 tests/acceptance/regen_lock.py --check；python3 scripts/rust/p7_case_coverage.py；python3 -m policy_check --repo .。"
  echo "執行：python3 scripts/rust/p7_gate_report.py build 與 verify；prior slice、func-test、coverage 段依目前輸入補齊。"
  exit 0
fi
RUN="$ROOT/target/p7-gate/$RUN_ID"
[[ ! -e "$RUN/results.json" ]] || { echo "p7_gate.sh: $RUN 已存在，拒絕覆寫（請換 --run-id）" >&2; exit 2; }
mkdir -p "$RUN/logs"
[[ -n "$OUT_MD" ]] || OUT_MD="$ROOT/docs/rust-refactor/evidence/p7-gate-$(date +%y%m%d).md"
cd "$ROOT"
TARGET_DIR="${CARGO_TARGET_DIR:-$ROOT/target}"
[[ "$TARGET_DIR" == /* ]] || TARGET_DIR="$ROOT/$TARGET_DIR"
source "$ROOT/scripts/rust/gate_prereqs.sh" --gate
REPORT=(python3 scripts/rust/p7_gate_report.py)
# 受測 artifact 於建置後立即快照到 run 目錄：之後的 cargo L1 列（--features test-fakes）與全套 pytest 會重建
# target/debug/ 的同名執行檔，若直接引用會讓黑箱跑到 test-fakes 版、verify 的 sha256 也對不上（第 12 輪實跑揭露）。
RUST_CLI="$RUN/artifacts/serialwrap"
RUST_D="$RUN/artifacts/serialwrapd"
PRIOR_RELEASE_D="$RUN/prior-slice-artifacts/release/serialwrapd"

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

SHARED_WIN_RC=""
import_shared_windows_l1() {  # import_shared_windows_l1 <required-set> <destination>
  local set_id="$1" destination="$2" result="$RUN/logs/shared-windows-${set_id}.json"
  [[ "$SHARED_ARTIFACTS_VALID" == "1" ]] || return 3
  if ! python3 scripts/rust/gate_shared_evidence.py import-section --shared-evidence "$SHARED_EVIDENCE" \
      --gate p7 --section "windows_l1:$set_id" --required-set "$set_id" --platform windows \
      "${SHARED_ARTIFACT_ARGS[@]}" --out-dir "$destination" \
      >"$result" 2>"$RUN/logs/shared-windows-${set_id}.stderr"; then
    return 3
  fi
  SHARED_WIN_RC="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit_code"])' "$result")"
  return 0
}

mkdir -p "$RUN/artifacts" "$RUN/prior-slice-artifacts/release"
SHARED_ARTIFACTS_VALID=0
SHARED_ARTIFACT_ARGS=()
if [[ -n "$SHARED_EVIDENCE" ]]; then
  if python3 scripts/rust/gate_shared_evidence.py import-artifacts \
      --shared-evidence "$SHARED_EVIDENCE" --out-dir "$RUN/shared-artifact-import" \
      >"$RUN/logs/shared-artifacts.json" 2>"$RUN/logs/shared-artifacts.stderr"; then
    cp -p "$RUN/shared-artifact-import/debug/serialwrap" "$RUST_CLI"
    cp -p "$RUN/shared-artifact-import/debug/serialwrapd" "$RUST_D"
    cp -p "$RUN/shared-artifact-import/release/serialwrapd" "$PRIOR_RELEASE_D"
    SHARED_ARTIFACT_ARGS=(--artifact "debug_cli=$RUN/shared-artifact-import/debug/serialwrap" \
      --artifact "debug_daemon=$RUN/shared-artifact-import/debug/serialwrapd" \
      --artifact "release_cli=$RUN/shared-artifact-import/release/serialwrap" \
      --artifact "release_daemon=$RUN/shared-artifact-import/release/serialwrapd")
    SHARED_ARTIFACTS_VALID=1
  else
    echo "EVIDENCE_MISSING：共用 artifact provenance 不符；不在本 gate 重建。" >"$RUN/logs/shared-artifacts.log"
  fi
else
  CARGO_TARGET_DIR="$TARGET_DIR" CARGO_INCREMENTAL=0 cargo build --workspace --bins >"$RUN/logs/build.log" 2>&1 || {
    echo "p7_gate.sh: cargo build 失敗（$RUN/logs/build.log）" >&2; exit 1;
  }
  cp -p "$TARGET_DIR/debug/serialwrap" "$RUST_CLI"
  cp -p "$TARGET_DIR/debug/serialwrapd" "$RUST_D"
  CARGO_TARGET_DIR="$TARGET_DIR" CARGO_INCREMENTAL=0 cargo build --release -p sw-daemon -p sw-cli \
    --target x86_64-unknown-linux-gnu >"$RUN/logs/build-prior-slice-release.log" 2>&1 || {
    echo "p7_gate.sh: slice21 同類型 release 建置失敗（$RUN/logs/build-prior-slice-release.log）" >&2; exit 1;
  }
  cp -p "$TARGET_DIR/x86_64-unknown-linux-gnu/release/serialwrapd" "$PRIOR_RELEASE_D"
fi
SHARED_WIN_NONCOM_IMPORTED=0
SHARED_WIN_COM_IMPORTED=0
SHARED_WIN_NONCOM_RC=""
SHARED_WIN_COM_RC=""
if [[ -n "$SHARED_EVIDENCE" && "$SHARED_ARTIFACTS_VALID" == "1" ]]; then
  mkdir -p "$RUN/win-non-com" "$RUN/win-com"
  if import_shared_windows_l1 s21_core "$RUN/shared-windows-s21-core"; then
    cp "$RUN/shared-windows-s21-core/l1-results.json" "$RUN/win-non-com/l1-results.json"
    SHARED_WIN_NONCOM_IMPORTED=1
    SHARED_WIN_NONCOM_RC="$SHARED_WIN_RC"
  fi
  if import_shared_windows_l1 p7_gate:win-test-sh-caps-windows-com "$RUN/shared-windows-p7-com"; then
    cp "$RUN/shared-windows-p7-com/l1-results.json" "$RUN/win-com/l1-results.json"
    SHARED_WIN_COM_IMPORTED=1
    SHARED_WIN_COM_RC="$SHARED_WIN_RC"
  fi
fi
CAPS=()
if ! [[ -n "${WSL_DISTRO_NAME:-}" || -e /proc/sys/fs/binfmt_misc/WSLInterop ]]; then CAPS+=(windows_native windows_com); fi
[[ -n "${SW_TEST_COM_PAIR:-}" ]] || CAPS+=(windows_com)
if [[ -n "$SHARED_EVIDENCE" ]]; then
  [[ "$SHARED_WIN_NONCOM_IMPORTED" == "1" ]] || CAPS+=(windows_native)
  [[ "$SHARED_WIN_COM_IMPORTED" == "1" ]] || CAPS+=(windows_com)
fi
FILTERED_CAPS=()
for cap in "${CAPS[@]}"; do
  [[ "$cap" == windows_native && "$SHARED_WIN_NONCOM_IMPORTED" == "1" ]] && continue
  [[ "$cap" == windows_com && "$SHARED_WIN_COM_IMPORTED" == "1" ]] && continue
  if [[ -n "$SHARED_EVIDENCE" && " ${FILTERED_CAPS[*]} " == *" $cap "* ]]; then continue; fi
  FILTERED_CAPS+=("$cap")
done
CAPS=("${FILTERED_CAPS[@]}")
"${REPORT[@]}" init --results-dir "$RUN" --run-id "$RUN_ID" --caps-missing "${CAPS[@]}" \
  --artifact "$RUST_CLI" "$RUST_D" --prior-slice-artifact serialwrapd_release "$PRIOR_RELEASE_D"

# OD-87：記錄 2.3 的實際斷路器狀態；豁免只解除 gate 阻擋，評估輸出仍進入可驗證報告。
set +e
python3 scripts/rust/slice_breaker.py evaluate-gate \
  --freeze tests/acceptance/breaker/slice_2.3.json \
  --snapshots tests/acceptance/breaker/slice_2.3.snapshots.ndjson \
  --out "$RUN/breaker.json" >"$RUN/logs/breaker.log" 2>&1
BREAKER_RC=$?
set -e
"${REPORT[@]}" record-breaker --results-dir "$RUN" --breaker-json "$RUN/breaker.json" --exit-code "$BREAKER_RC"

# 1. cfg 邊界
run_row "check_cfg_boundary" check-cfg-boundary --evidence "$RUN/logs/check-cfg-boundary.log" -- \
  python3 scripts/rust/check_cfg_boundary.py
# 2. Linux L1（p7_gate:cargo-test-workspace）
mkdir -p "$RUN/cargo-test-workspace"
if [[ -n "$SHARED_EVIDENCE" ]]; then
  SHARED_CARGO_JSON="$RUN/logs/shared-cargo-p7-import.json"
  if [[ "$SHARED_ARTIFACTS_VALID" == 1 ]] && python3 scripts/rust/gate_shared_evidence.py import-section \
      --shared-evidence "$SHARED_EVIDENCE" --gate p7 --section cargo_fakes_p7 \
      --required-set p7_gate:cargo-test-workspace --platform linux \
      "${SHARED_ARTIFACT_ARGS[@]}" --out-dir "$RUN/shared-import" \
      >"$SHARED_CARGO_JSON" 2>"$RUN/logs/shared-cargo-p7-import.stderr"; then
    cp "$RUN/shared-import/p7_gate/cargo-test-workspace/l1-results.json" "$RUN/cargo-test-workspace/l1-results.json"
    cp "$RUN/shared-import/logs/cargo-fakes-p7.log" "$RUN/logs/cargo-test-workspace.log"
    SHARED_RC="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit_code"])' "$SHARED_CARGO_JSON")"
    record_existing "cargo test --workspace" "$SHARED_RC" "$RUN/logs/cargo-test-workspace.log" \
      '["cargo","test","--workspace","--features","test-fakes"]' --l1 "$RUN/cargo-test-workspace/l1-results.json"
  else
    echo "EVIDENCE_MISSING：共用 cargo 證據拒收；本段不重跑。" >"$RUN/logs/cargo-test-workspace.log"
    cat "$RUN/logs/shared-cargo-p7-import.stderr" >>"$RUN/logs/cargo-test-workspace.log" 2>/dev/null || true
    record_existing "cargo test --workspace" - "$RUN/logs/cargo-test-workspace.log" \
      '["cargo","test","--workspace","--features","test-fakes"]'
  fi
else
  run_row "cargo test --workspace" cargo-test-workspace --l1 "$RUN/cargo-test-workspace/l1-results.json" -- \
    python3 scripts/rust/cargo_l1_results.py --platform linux --out "$RUN/cargo-test-workspace/l1-results.json" \
    -- --workspace --features test-fakes
fi
# 3／4. Windows 原生（non-COM 與 COM pair）；能力缺失時只記錄，由報告判 BLOCKED-OWNER。
win_l1() {  # win_l1 <dir> <該次 win-test log>：只匯入本次命令明列的 report
  local dir="$1" log="$2" rep l1
  if ! rep="$(python3 - "$log" "$ROOT/target/win-test-report" <<'PY'
import pathlib, sys
log = pathlib.Path(sys.argv[1])
root = pathlib.Path(sys.argv[2]).resolve()
paths = []
for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
    candidate = pathlib.Path(line.strip())
    if candidate.is_absolute() and candidate.parent.resolve() == root and candidate.is_file():
        if candidate.name.endswith(".json") and not candidate.name.startswith(("l1-results-", "summary-")):
            paths.append(candidate)
print(paths[-1] if paths else "")
PY
)"; then rep=""; fi
  [[ -n "$rep" ]] || { echo "本次 win-test 未輸出 report path" >>"$log"; return 0; }
  if ! l1="$(python3 - "$rep" <<'PY'
import json, pathlib, sys
report = pathlib.Path(sys.argv[1])
name = json.loads(report.read_text(encoding="utf-8")).get("l1_results")
print(report.parent / name if isinstance(name, str) and name else "")
PY
)"; then l1=""; fi
  if [[ -n "$l1" && -f "$l1" ]]; then
    cp "$l1" "$dir/l1-results.json"
  else
    echo "本次 win-test report 未包含可讀 l1_results" >>"$log"
  fi
  return 0
}
mkdir -p "$RUN/win-non-com" "$RUN/win-com"
# 兩列的 win-test.sh 參數（tests/test_p7_gate_win_argv.py 以 `--dry-run` 核對可被 win-test.sh 解析）。
# COM 列依 OD-53 為能力集合（rulings/p7.md 第 6 輪 SPEC-GAP #5）：test-binary 參數以 `--ignored cap_windows_com__`
# 明確只執行三個 `#[ignore = "cap:windows_com"]` 案例、`--caps windows_com` 實跑；必要集合
# `p7_gate:win-test-sh-caps-windows-com`（windows）以同一 argv 為 selector，於原生 Windows 收集登記（P7-LW-9）。
WIN_NONCOM_ARGS=(--package sw-pal --package sw-core --package sw-daemon -- --features test-fakes -- --skip cap_)
WIN_COM_ARGS=(--caps windows_com --package sw-daemon -- --test cap_windows_com_p7 --features test-fakes -- --ignored cap_windows_com__)
if [[ -n "$SHARED_EVIDENCE" ]]; then
  if [[ "$SHARED_WIN_NONCOM_IMPORTED" == "1" ]]; then
    echo "已匯入 shared-evidence windows_l1:s21_core；未啟動 Windows runner。" >"$RUN/logs/win-non-com.log"
    record_existing "win-test.sh (non-COM)" "$SHARED_WIN_NONCOM_RC" "$RUN/logs/win-non-com.log" \
      "$(argv_json scripts/rust/win-test.sh "${WIN_NONCOM_ARGS[@]}")" --l1 "$RUN/win-non-com/l1-results.json" --platform-windows
  else
    echo "EVIDENCE_MISSING：共用 s21_core Windows L1 缺失或 provenance／selector 不符；本段不重跑。" >"$RUN/logs/win-non-com.log"
    cat "$RUN/logs/shared-windows-s21_core.stderr" >>"$RUN/logs/win-non-com.log" 2>/dev/null || true
    record_existing "win-test.sh (non-COM)" - "$RUN/logs/win-non-com.log" \
      "$(argv_json scripts/rust/win-test.sh "${WIN_NONCOM_ARGS[@]}")" --platform-windows
  fi
elif [[ " ${CAPS[*]} " == *" windows_native "* ]]; then
  echo "caps_missing: windows_native（非 WSL interop 環境；LOCAL-WINDOWS-TODO）" >"$RUN/logs/win-non-com.log"
  record_existing "win-test.sh (non-COM)" - "$RUN/logs/win-non-com.log" '["scripts/rust/win-test.sh"]'
else
  exec_logged win-non-com bash scripts/rust/win-test.sh "${WIN_NONCOM_ARGS[@]}"
  win_l1 "$RUN/win-non-com" "$RUN/logs/win-non-com.log"
  record_existing "win-test.sh (non-COM)" "$LAST_RC" "$RUN/logs/win-non-com.log" \
    "$(argv_json scripts/rust/win-test.sh "${WIN_NONCOM_ARGS[@]}")" --l1 "$RUN/win-non-com/l1-results.json" --platform-windows
fi
if [[ -n "$SHARED_EVIDENCE" ]]; then
  if [[ "$SHARED_WIN_COM_IMPORTED" == "1" ]]; then
    echo "已匯入 shared-evidence windows_l1:p7_gate:win-test-sh-caps-windows-com；未啟動 Windows runner。" >"$RUN/logs/win-com.log"
    record_existing "win-test.sh --caps windows_com" "$SHARED_WIN_COM_RC" "$RUN/logs/win-com.log" \
      "$(argv_json scripts/rust/win-test.sh "${WIN_COM_ARGS[@]}")" --l1 "$RUN/win-com/l1-results.json" --platform-windows
  else
    echo "EVIDENCE_MISSING：共用 p7 Windows COM L1 缺失或 provenance／selector 不符；本段不重跑。" >"$RUN/logs/win-com.log"
    cat "$RUN/logs/shared-windows-p7_gate:win-test-sh-caps-windows-com.stderr" >>"$RUN/logs/win-com.log" 2>/dev/null || true
    record_existing "win-test.sh --caps windows_com" - "$RUN/logs/win-com.log" \
      "$(argv_json scripts/rust/win-test.sh "${WIN_COM_ARGS[@]}")" --platform-windows
  fi
elif [[ " ${CAPS[*]} " == *" windows_com "* ]]; then
  echo "caps_missing: windows_com（未設 SW_TEST_COM_PAIR 或無 Windows；B-06 擁有者 host COM20:COM21）" >"$RUN/logs/win-com.log"
  record_existing "win-test.sh --caps windows_com" - "$RUN/logs/win-com.log" '["scripts/rust/win-test.sh","--caps","windows_com"]'
else
  # win-test.sh 只在 `--` 之後接受 cargo 參數（審查 P7-cd7acf0-5）。
  exec_logged win-com bash scripts/rust/win-test.sh "${WIN_COM_ARGS[@]}"
  win_l1 "$RUN/win-com" "$RUN/logs/win-com.log"
  record_existing "win-test.sh --caps windows_com" "$LAST_RC" "$RUN/logs/win-com.log" \
    "$(argv_json scripts/rust/win-test.sh "${WIN_COM_ARGS[@]}")" --l1 "$RUN/win-com/l1-results.json" --platform-windows
fi

# 5～12. 黑箱 A/B、先前切片必要回歸、func-test 兩案（run_p7_ab.sh 一次產出，逐階段成列）。
AB="$RUN/ab"
AB_MD="$RUN/p7-ab.md"
if [[ -n "$SHARED_EVIDENCE" ]]; then
  mkdir -p "$AB"
  SHARED_AB_JSON="$RUN/logs/shared-p7-ab-import.json"
  if [[ "$SHARED_ARTIFACTS_VALID" == 1 ]] && python3 scripts/rust/gate_shared_evidence.py import-section \
      --shared-evidence "$SHARED_EVIDENCE" --gate p7 --section p7_ab --expected-command p7_ab --platform linux \
      "${SHARED_ARTIFACT_ARGS[@]}" \
      --out-dir "$RUN/shared-import" >"$SHARED_AB_JSON" 2>"$RUN/logs/shared-p7-ab-import.stderr"; then
    tar -xzf "$RUN/shared-import/p7_gate/ab.tar.gz" -C "$AB"
    [[ ! -f "$AB/p7-ab.md" ]] || cp "$AB/p7-ab.md" "$AB_MD"
    cp "$RUN/shared-import/logs/p7-ab.log" "$RUN/logs/run-p7-ab.log"
    AB_RC="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit_code"])' "$SHARED_AB_JSON")"
  else
    echo "EVIDENCE_MISSING：共用 p7 A/B 證據拒收；本段不重跑。" >"$RUN/logs/run-p7-ab.log"
    cat "$RUN/logs/shared-p7-ab-import.stderr" >>"$RUN/logs/run-p7-ab.log" 2>/dev/null || true
    AB_RC=3
  fi
else
  set +e
  bash scripts/rust/run_p7_ab.sh --run-id "$RUN_ID" --run-dir "$AB" --out "$AB_MD" --rust-cli "$RUST_CLI" \
    --od59-contract --rust-daemon "$RUST_D" >"$RUN/logs/run-p7-ab.log" 2>&1
  AB_RC=$?
  set -e
fi
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
phase_row "OD-59 control latency 500x3 A" od59-contract-a
phase_row "OD-59 control latency 500x3 B" od59-contract-b
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
# 14. slice_2.3 必要案例涵蓋。2.1／2.2 先前切片案例不在本 gate 重跑，以對應切片 gate 的逐案證據判定（rulings/p7.md
#     SPEC-GAP #4 (ii)）：P7_SLICE21_GATE_RUN＝slice21 gate 的 run 目錄（report.json＋case_verdicts.json）、
#     P7_S22_GATE_JSON＝s22_gate_check.py 輸出（須含 identity）；未設或檔案不存在時該切片案例記缺漏（不得 PASS）。
#     匯入證據的 git_revision 與受測 daemon sha256 須等於本 run 的 results.json（審查 P7-38c150c-3），否則不採用。
#     證據檔複製進 run 目錄後才引用，sha256 隨該列記錄。
COV_ARGS=(--ab-dir "$AB" --out "$RUN/coverage.json" --cargo-l1 "$RUN/cargo-test-workspace/l1-results.json"
          --p7-results "$RUN/results.json")
COV_EV=(--evidence "$RUN/coverage.json")
mkdir -p "$RUN/prior-slices"
if [[ -n "${P7_SLICE21_GATE_RUN:-}" && -f "${P7_SLICE21_GATE_RUN}/report.json" ]]; then
  mkdir -p "$RUN/prior-slices/slice21"
  cp -p "$P7_SLICE21_GATE_RUN/report.json" "$RUN/prior-slices/slice21/report.json"
  COV_EV+=(--evidence "$RUN/prior-slices/slice21/report.json")
  if [[ -f "$P7_SLICE21_GATE_RUN/case_verdicts.json" ]]; then
    cp -p "$P7_SLICE21_GATE_RUN/case_verdicts.json" "$RUN/prior-slices/slice21/case_verdicts.json"
    COV_EV+=(--evidence "$RUN/prior-slices/slice21/case_verdicts.json")
  fi
  COV_ARGS+=(--slice21-run "$RUN/prior-slices/slice21")
fi
if [[ -n "${P7_S22_GATE_JSON:-}" && -f "${P7_S22_GATE_JSON}" ]]; then
  cp -p "$P7_S22_GATE_JSON" "$RUN/prior-slices/s22-gate.json"
  COV_ARGS+=(--slice22-gate "$RUN/prior-slices/s22-gate.json")
  COV_EV+=(--evidence "$RUN/prior-slices/s22-gate.json")
fi
run_row "cases.lock slice_2.3 coverage" coverage "${COV_EV[@]}" -- \
  python3 scripts/rust/p7_case_coverage.py "${COV_ARGS[@]}"
# 15. 全套 pytest（N3；依 2026-11-12 rulings/s2.md 第 3 點，比照 CI 排除兩項 Stage 1 證據測試；其他失敗 → FAIL）
mkdir -p "$RUN/pytest-full"
FULL_PYTEST_ROW='python3 -m pytest -q tests/ --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native --deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence'
if [[ "$SKIP_FULL" == "1" ]]; then
  echo "--skip-full-pytest：未執行（不得作出口證據）" >"$RUN/logs/pytest-full.log"
elif [[ -n "$SHARED_EVIDENCE" ]]; then
  SHARED_PYTEST_JSON="$RUN/logs/shared-pytest-import.json"
  if [[ "$SHARED_ARTIFACTS_VALID" == 1 ]] && python3 scripts/rust/gate_shared_evidence.py import-section \
      --shared-evidence "$SHARED_EVIDENCE" --gate p7 --section pytest_full \
      --required-set p7_gate:python3-m-pytest-q-tests --platform linux \
      "${SHARED_ARTIFACT_ARGS[@]}" --out-dir "$RUN/shared-import" \
      >"$SHARED_PYTEST_JSON" 2>"$RUN/logs/shared-pytest-import.stderr"; then
    mkdir -p "$RUN/pytest-full"
    cp "$RUN/shared-import/p7_gate/pytest-full/l1-results.json" "$RUN/pytest-full/l1-results.json"
    cp "$RUN/shared-import/p7_gate/pytest-full/junit.xml" "$RUN/pytest-full/junit.xml"
    cp "$RUN/shared-import/logs/pytest-full.log" "$RUN/logs/pytest-full.log"
    SHARED_RC="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit_code"])' "$SHARED_PYTEST_JSON")"
    record_existing "$FULL_PYTEST_ROW" "$SHARED_RC" "$RUN/logs/pytest-full.log" \
      "$(argv_json -m pytest -q tests/ --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native --deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence)" \
      --l1 "$RUN/pytest-full/l1-results.json" --evidence "$RUN/pytest-full/junit.xml"
  else
    echo "EVIDENCE_MISSING：共用 pytest 證據拒收；本段不重跑。" >"$RUN/logs/pytest-full.log"
    cat "$RUN/logs/shared-pytest-import.stderr" >>"$RUN/logs/pytest-full.log" 2>/dev/null || true
    record_existing "$FULL_PYTEST_ROW" - "$RUN/logs/pytest-full.log" \
      "$(argv_json -m pytest -q tests/ --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native --deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence)"
  fi
else
  run_row "python3 -m pytest -q tests/ --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native --deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence" pytest-full --l1 "$RUN/pytest-full/l1-results.json" \
    --evidence "$RUN/pytest-full/junit.xml" -- \
    env SW_RESOURCE_REPORT="$RUN/pytest-full/l1-results.json" python3 -m pytest -q tests/ \
    --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native \
    --deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence \
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
