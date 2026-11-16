#!/usr/bin/env bash
# 在單一候選 HEAD 產生 S2 gate 可共用的證據。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
OUT_ROOT="$ROOT/target/s2-shared-evidence"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
WINDOWS_L1=""
DRY_RUN=0
while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --out) [[ $# -ge 2 ]] || { echo "gate_shared.sh: --out 缺少值" >&2; exit 2; }; OUT_ROOT="$2"; shift 2 ;;
    --run-id) [[ $# -ge 2 ]] || { echo "gate_shared.sh: --run-id 缺少值" >&2; exit 2; }; RUN_ID="$2"; shift 2 ;;
    --windows-l1) [[ $# -ge 2 ]] || { echo "gate_shared.sh: --windows-l1 缺少值" >&2; exit 2; }; WINDOWS_L1="$2"; shift 2 ;;
    -h|--help) echo "用法：scripts/rust/gate_shared.sh [--dry-run] [--out DIR] [--run-id ID] [--windows-l1 DIR]"; exit 0 ;;
    *) echo "gate_shared.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done
[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "gate_shared.sh: --run-id 只允許英數與 ._-" >&2; exit 2; }
if [[ "$OUT_ROOT" != /* ]]; then OUT_ROOT="$ROOT/$OUT_ROOT"; fi
OUT_ROOT="$(realpath -m -- "$OUT_ROOT")"
RUN_DIR="$OUT_ROOT/$RUN_ID"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "gate_shared.sh dry-run：候選 artifact 與共用證據目錄 $RUN_DIR"
  echo "執行：source scripts/rust/gate_prereqs.sh --gate；失敗記錄到 <LOG_DIR>/driver.tsv 並停止。"
  echo "執行：cargo build --workspace --bins；cargo build --workspace --bins --release --target x86_64-unknown-linux-gnu。"
  echo "執行：env SW_RESOURCE_REPORT=<L1> SERIALWRAP_COVERAGE_RUN_DIR=<COVERAGE> SERIALWRAP_COVERAGE_LABEL=python-linux python3 -m pytest -q tests/ --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native --deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence --junitxml=<JUNIT>。"
  echo "執行：cargo test --workspace；python3 scripts/rust/cargo_l1_results.py --platform linux --out <L1> -- --workspace --features test-fakes -- --skip cap_。"
  echo "執行：python3 scripts/rust/cargo_l1_results.py --platform linux --out <L1> -- --workspace --features test-fakes；python3 scripts/rust/import_cargo_results.py --label rust-linux --platform linux --include-cap posix_pty --include-cap process_rpc -- --workspace --features test-fakes。"
  echo "執行：bash scripts/rust/run_p7_ab.sh --run-id <ID> --run-dir <AB> --out <REPORT> --rust-cli <CLI> --rust-daemon <DAEMON> --od59-contract。"
  echo "執行：env SERIALWRAP_CLI_EXE=<CLI> SERIALWRAP_DAEMON_EXE=<DAEMON> SERIALWRAP_AB_LABEL=rust-linux python3 -m pytest -q tests/contract_blackbox tests/acceptance/l15/test_slice21_l15.py tests/acceptance/l15/test_slice23_l15.py tests/test_multiagent_e2e.py tests/test_human_agent_coexist.py tests/test_rust_contract_coverage.py tests/test_transfer_golden_fresh.py。"
  echo "匯入：--windows-l1 <dir>/<required-set>/l1-results.json；只讀取編排者提供的原生 L1，不啟動 Windows runner。"
  echo "輸出 manifest.json：HEAD、乾淨工作樹、debug/release CLI 與 daemon SHA256、selector argv、evidence SHA256。"
  exit 0
fi

cd "$ROOT"
export TMPDIR="$(mktemp -d /tmp/sw.XXXXXX)"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$ROOT/target}"
[[ "$CARGO_TARGET_DIR" == /* ]] || CARGO_TARGET_DIR="$ROOT/$CARGO_TARGET_DIR"
export CARGO_TARGET_DIR CARGO_INCREMENTAL=0
set -o pipefail
[[ ! -e "$RUN_DIR/manifest.json" ]] || { echo "gate_shared.sh: $RUN_DIR 已有 manifest，拒絕覆寫" >&2; exit 2; }
python3 - "$ROOT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.rust.gate_shared_evidence import is_worktree_clean
if not is_worktree_clean(Path(sys.argv[1])):
    print("EVIDENCE_MISSING: 產生共用證據前工作樹必須乾淨")
    raise SystemExit(3)
PY

LOG_DIR="$ROOT/target/s2-par/gpz/gate-shared/$RUN_ID"
mkdir -p "$LOG_DIR"
if source "$ROOT/scripts/rust/gate_prereqs.sh" --gate >"$LOG_DIR/gate-prereqs.log" 2>&1; then
  PREREQS_RC=0
else
  PREREQS_RC=$?
fi
printf 'gate_prereqs\t%s\n' "$PREREQS_RC" >"$LOG_DIR/driver.tsv"
if [[ "$PREREQS_RC" -ne 0 ]]; then
  echo "gate_shared.sh: gate_prereqs exit=$PREREQS_RC（$LOG_DIR/gate-prereqs.log）" >&2
  exit "$PREREQS_RC"
fi
FAILURES=0
LAST_RC=0
run_capture() {
  local log="$1"; shift
  set +e
  "$@" 2>&1 | tee "$log"
  local statuses=("${PIPESTATUS[@]}")
  set -e
  LAST_RC="${statuses[0]}"
  [[ "$LAST_RC" == 0 ]] || FAILURES=1
}

run_capture "$LOG_DIR/build-debug.log" cargo build --workspace --bins
[[ "$LAST_RC" == 0 ]] || exit "$LAST_RC"
run_capture "$LOG_DIR/build-release.log" cargo build --workspace --bins --release --target x86_64-unknown-linux-gnu
[[ "$LAST_RC" == 0 ]] || exit "$LAST_RC"
python3 scripts/rust/gate_shared_evidence.py init --shared-evidence "$RUN_DIR" \
  --artifact "debug_cli=$CARGO_TARGET_DIR/debug/serialwrap" \
  --artifact "debug_daemon=$CARGO_TARGET_DIR/debug/serialwrapd" \
  --artifact "release_cli=$CARGO_TARGET_DIR/x86_64-unknown-linux-gnu/release/serialwrap" \
  --artifact "release_daemon=$CARGO_TARGET_DIR/x86_64-unknown-linux-gnu/release/serialwrapd" \
  >"$LOG_DIR/manifest-init.json"

mapfile -d '' -t PYTEST_ARGV < <(python3 - <<'PY'
import json
from pathlib import Path
lock=json.loads(Path("tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
for item in lock["sets"]["s21_gate:pytest_full"]["linux"]["selector"]["argv"]:
    print(item, end="\0")
PY
)
PYTEST_DATA="$RUN_DIR/work/pytest-full"
mkdir -p "$PYTEST_DATA"
mkdir -p "$RUN_DIR/coverage"
run_capture "$LOG_DIR/pytest-full.log" env \
  SW_RESOURCE_REPORT="$PYTEST_DATA/l1-results.json" \
  SERIALWRAP_COVERAGE_RUN_DIR="$RUN_DIR/coverage" SERIALWRAP_COVERAGE_LABEL=python-linux \
  python3 -m pytest "${PYTEST_ARGV[@]}" --junitxml="$PYTEST_DATA/junit.xml"
tar -czf "$RUN_DIR/work/python-baseline-coverage.tar.gz" -C "$RUN_DIR" coverage
python3 scripts/rust/gate_shared_evidence.py record-section --shared-evidence "$RUN_DIR" --section pytest_full \
  --argv-json "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${PYTEST_ARGV[@]}")" \
  --exit-code "$LAST_RC" --required-set s21_gate:pytest_full --required-set p7_gate:python3-m-pytest-q-tests \
  --file logs/pytest-full.log="$LOG_DIR/pytest-full.log" \
  --file s21_gate/pytest-full/l1-results.json="$PYTEST_DATA/l1-results.json" \
  --file p7_gate/pytest-full/l1-results.json="$PYTEST_DATA/l1-results.json" \
  --file s21_gate/pytest-full/junit.xml="$PYTEST_DATA/junit.xml" \
  --file p7_gate/pytest-full/junit.xml="$PYTEST_DATA/junit.xml" \
  --file slice24/python-baseline-coverage.tar.gz="$RUN_DIR/work/python-baseline-coverage.tar.gz"

run_capture "$LOG_DIR/cargo-workspace-default.log" cargo test --workspace
python3 scripts/rust/gate_shared_evidence.py record-section --shared-evidence "$RUN_DIR" \
  --section cargo_workspace_default --argv-json '["--workspace"]' --exit-code "$LAST_RC" \
  --file logs/cargo-workspace-default.log="$LOG_DIR/cargo-workspace-default.log"

FILTERED_ARGV=(--workspace --features test-fakes -- --skip cap_)
FILTERED_L1="$RUN_DIR/work/cargo-fakes-filtered.json"
run_capture "$LOG_DIR/cargo-fakes-filtered.log" python3 scripts/rust/cargo_l1_results.py --platform linux \
  --out "$FILTERED_L1" -- "${FILTERED_ARGV[@]}"
python3 scripts/rust/gate_shared_evidence.py record-section --shared-evidence "$RUN_DIR" --section cargo_fakes_filtered \
  --argv-json "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${FILTERED_ARGV[@]}")" \
  --exit-code "$LAST_RC" --required-set s21_core --required-set s22_gate:cargo_workspace \
  --file logs/cargo-fakes-filtered.log="$LOG_DIR/cargo-fakes-filtered.log" \
  --file s21_gate/linux-l1/l1-results.json="$FILTERED_L1" \
  --file s22_gate/cargo_workspace/linux/resource-report.json="$FILTERED_L1"

P7_ARGV=(--workspace --features test-fakes)
P7_L1="$RUN_DIR/work/cargo-fakes-p7.json"
run_capture "$LOG_DIR/cargo-fakes-p7.log" python3 scripts/rust/cargo_l1_results.py --platform linux \
  --out "$P7_L1" -- "${P7_ARGV[@]}"
python3 scripts/rust/gate_shared_evidence.py record-section --shared-evidence "$RUN_DIR" --section cargo_fakes_p7 \
  --argv-json "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${P7_ARGV[@]}")" \
  --exit-code "$LAST_RC" --required-set p7_gate:cargo-test-workspace \
  --file logs/cargo-fakes-p7.log="$LOG_DIR/cargo-fakes-p7.log" \
  --file p7_gate/cargo-test-workspace/l1-results.json="$P7_L1"

CARGO_RESULTS_DIR="$RUN_DIR/work/s24-cargo-results"
mkdir -p "$CARGO_RESULTS_DIR"
CARGO_RESULTS_ARGV=(--include-cap posix_pty --include-cap process_rpc --workspace --features test-fakes)
run_capture "$LOG_DIR/cargo-contract-results.log" env \
  SERIALWRAP_CLI_EXE="$RUN_DIR/artifacts/debug_cli" SERIALWRAP_DAEMON_EXE="$RUN_DIR/artifacts/debug_daemon" \
  python3 scripts/rust/import_cargo_results.py --label rust-linux --platform linux \
  --out-dir "$CARGO_RESULTS_DIR" --include-cap posix_pty --include-cap process_rpc \
  -- --workspace --features test-fakes
tar -czf "$RUN_DIR/work/s24-cargo-results.tar.gz" -C "$CARGO_RESULTS_DIR" .
python3 scripts/rust/gate_shared_evidence.py record-section --shared-evidence "$RUN_DIR" \
  --section cargo_contract_results --argv-json "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${CARGO_RESULTS_ARGV[@]}")" \
  --exit-code "$LAST_RC" --file slice24/cargo-results.tar.gz="$RUN_DIR/work/s24-cargo-results.tar.gz" \
  --file logs/cargo-contract-results.log="$LOG_DIR/cargo-contract-results.log"

P7_AB="$RUN_DIR/work/p7-ab"
mkdir -p "$P7_AB"
run_capture "$LOG_DIR/p7-ab.log" bash scripts/rust/run_p7_ab.sh --run-id "$RUN_ID" --run-dir "$P7_AB" \
  --out "$RUN_DIR/work/p7-ab.md" --rust-cli "$RUN_DIR/artifacts/debug_cli" \
  --rust-daemon "$RUN_DIR/artifacts/debug_daemon" --od59-contract
cp "$RUN_DIR/work/p7-ab.md" "$P7_AB/p7-ab.md" 2>/dev/null || true
tar -czf "$RUN_DIR/work/p7-ab.tar.gz" -C "$P7_AB" .
python3 scripts/rust/gate_shared_evidence.py record-section --shared-evidence "$RUN_DIR" --section p7_ab \
  --argv-json "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' bash scripts/rust/run_p7_ab.sh \
    --run-id "$RUN_ID" --run-dir "$P7_AB" --out "$RUN_DIR/work/p7-ab.md" \
    --rust-cli "$RUN_DIR/artifacts/debug_cli" --rust-daemon "$RUN_DIR/artifacts/debug_daemon" --od59-contract)" \
  --exit-code "$LAST_RC" \
  --file p7_gate/ab.tar.gz="$RUN_DIR/work/p7-ab.tar.gz" --file logs/p7-ab.log="$LOG_DIR/p7-ab.log"

S24_TARGETS=(tests/contract_blackbox tests/acceptance/l15/test_slice21_l15.py \
  tests/acceptance/l15/test_slice23_l15.py tests/test_multiagent_e2e.py \
  tests/test_human_agent_coexist.py tests/test_rust_contract_coverage.py tests/test_transfer_golden_fresh.py)
S24_DATA="$RUN_DIR/work/s24-ab"
mkdir -p "$S24_DATA"
run_capture "$LOG_DIR/s24-ab.log" env \
  SERIALWRAP_CLI_EXE="$RUN_DIR/artifacts/debug_cli" SERIALWRAP_DAEMON_EXE="$RUN_DIR/artifacts/debug_daemon" \
  SERIALWRAP_AB_LABEL=rust-linux SERIALWRAP_COVERAGE_RUN_DIR="$RUN_DIR/coverage" \
  SERIALWRAP_COVERAGE_LABEL=rust-linux python3 -m pytest -q "${S24_TARGETS[@]}" --junitxml="$S24_DATA/junit.xml"
tar -czf "$RUN_DIR/work/s24-coverage.tar.gz" -C "$RUN_DIR" coverage
python3 scripts/rust/gate_shared_evidence.py record-section --shared-evidence "$RUN_DIR" --section s24_ab \
  --argv-json "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' -q "${S24_TARGETS[@]}")" \
  --exit-code "$LAST_RC" --file slice24/coverage.tar.gz="$RUN_DIR/work/s24-coverage.tar.gz" \
  --file logs/s24-ab.log="$LOG_DIR/s24-ab.log" --file slice24/s24-ab/junit.xml="$S24_DATA/junit.xml"

if [[ -n "$WINDOWS_L1" ]]; then
  [[ -d "$WINDOWS_L1" ]] || { echo "EVIDENCE_MISSING: Windows L1 目錄不存在：$WINDOWS_L1" >&2; exit 3; }
  mapfile -t WINDOWS_SET_IDS < <(python3 - <<'PY'
import json
from pathlib import Path
lock = json.loads(Path("tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
for set_id, platforms in sorted(lock["sets"].items()):
    if "windows" in platforms:
        print(set_id)
PY
)
  imported_windows=0
  for set_id in "${WINDOWS_SET_IDS[@]}"; do
    source="$WINDOWS_L1/$set_id/l1-results.json"
    [[ -f "$source" ]] || continue
    if python3 scripts/rust/gate_shared_evidence.py record-windows-l1 \
        --shared-evidence "$RUN_DIR" --windows-l1 "$WINDOWS_L1" --required-set "$set_id" \
        2>&1 | tee -a "$LOG_DIR/windows-l1-import.log"; then
      imported_windows=$((imported_windows + 1))
    else
      FAILURES=1
    fi
  done
  echo "Windows L1 匯入：${imported_windows} 個鎖定集合；未提供的集合保留 EVIDENCE_MISSING。"
fi

echo "共用證據已寫入：$RUN_DIR/manifest.json"
echo "各 gate 仍由原有 judge 判定；共享段的 FAIL 保留原 exit code。"
exit "$FAILURES"
