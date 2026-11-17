#!/usr/bin/env bash
# Slice 2.4 Linux 證據收集與出口報告；Windows／真機／長跑項只匯入擁有者既有證據。
# --candidate-bundle DIR：本輪候選 Linux bundle（mkbundle --release-bundle 輸出或安裝根；版本目錄＝bin/serialwrap
#   解析後的上兩層，須含 release-manifest.json 且 git_revision＝本 worktree HEAD）。指定後受測 CLI／daemon 取自該 bundle，
#   並寫入 RUN_DIR/artifacts/linux/candidate-bundle.json；長跑證據只有與它相同的 bundle／CLI／daemon 才能解除阻擋。
# --windows-run DIR：擁有者 Windows 原生 runner 產物（SHA256SUMS＋results-rust-windows.json；可附 bundle/ 版本目錄）。
# --longrun DIR：長跑證據（longrun-manifest.json＋<platform>/release-gate.json、baseline-release-gate.json、
#   supervision-attest.json、bench-lock.json、gate evidence_hashes 所列檔案；Windows 另含 excl/）。
#   由 contract_coverage.py --import-longrun 驗證後複製進 RUN_DIR/longrun/，判定時再重新驗證副本。
set -uo pipefail
export CARGO_INCREMENTAL=0

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-slice24"
RUN_DIR="${SW_SLICE24_RUN_DIR:-target/contract-coverage/$RUN_ID}"
WINDOWS_RUN=""
LONGRUN_DIR=""
CANDIDATE_BUNDLE=""
SLICE21_RUN=""
SLICE22_GATE=""
SLICE23_RUN=""
SHARED_EVIDENCE=""
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --shared-evidence)
      [[ $# -ge 2 ]] || { echo "slice24_exit.sh: --shared-evidence 缺少值" >&2; exit 2; }
      SHARED_EVIDENCE="$2"
      shift 2 ;;
    --run-id)
      [[ $# -ge 2 ]] || { echo "slice24_exit.sh: --run-id 缺少值" >&2; exit 2; }
      RUN_ID="$2"
      RUN_DIR="target/contract-coverage/$RUN_ID"
      shift 2 ;;
    --windows-run)
      [[ $# -ge 2 ]] || { echo "slice24_exit.sh: --windows-run 缺少值" >&2; exit 2; }
      WINDOWS_RUN="$2"
      shift 2 ;;
    --longrun)
      [[ $# -ge 2 ]] || { echo "slice24_exit.sh: --longrun 缺少值" >&2; exit 2; }
      LONGRUN_DIR="$2"
      shift 2 ;;
    --candidate-bundle)
      [[ $# -ge 2 ]] || { echo "slice24_exit.sh: --candidate-bundle 缺少值" >&2; exit 2; }
      CANDIDATE_BUNDLE="$2"
      shift 2 ;;
    --slice21-run)
      [[ $# -ge 2 ]] || { echo "slice24_exit.sh: --slice21-run 缺少值" >&2; exit 2; }
      SLICE21_RUN="$2"
      shift 2 ;;
    --slice22-gate)
      [[ $# -ge 2 ]] || { echo "slice24_exit.sh: --slice22-gate 缺少值" >&2; exit 2; }
      SLICE22_GATE="$2"
      shift 2 ;;
    --slice23-run)
      [[ $# -ge 2 ]] || { echo "slice24_exit.sh: --slice23-run 缺少值" >&2; exit 2; }
      SLICE23_RUN="$2"
      shift 2 ;;
    -h|--help)
      sed -n '2,9p' "$0"
      echo "用法：$0 [--dry-run] [--shared-evidence DIR] [--run-id ID] [--candidate-bundle DIR] [--windows-run DIR] [--longrun DIR] [--slice21-run DIR] [--slice22-gate FILE] [--slice23-run DIR]"
      exit 0 ;;
    *) echo "slice24_exit.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done

if [[ "$DRY_RUN" == "1" ]]; then
  echo "slice24_exit.sh dry-run：只列出計畫，不執行命令。"
  if [[ -n "$SHARED_EVIDENCE" ]]; then
    echo "shared-evidence import：debug/release artifacts、pytest_full、cargo_fakes_p7、cargo_workspace_default、cargo_contract_results、s24_ab（$SHARED_EVIDENCE）；候選 bundle 未指定 --candidate-bundle 時讀取 shared manifest，指定 --candidate-bundle DIR 時必須同 tree／manifest／CLI／daemon；SW_BUNDLE 指向該候選。selector/provenance/SHA 不符記 EVIDENCE_MISSING，FAIL 照傳且不重跑。artifact provenance 不符：EVIDENCE_MISSING，不啟動本地建置。"
    echo "執行：env CARGO_INCREMENTAL=0 cargo check --workspace --target x86_64-pc-windows-gnu。"
  else
    echo "執行：cargo build --workspace --bins；python3 scripts/rust/check_cfg_boundary.py。"
    echo "執行：cargo test --workspace --features test-fakes；cargo test --workspace；env CARGO_INCREMENTAL=0 cargo check --workspace --target x86_64-pc-windows-gnu。"
    echo "執行：python3 scripts/rust/import_cargo_results.py --label rust-linux --platform linux --include-cap posix_pty --include-cap process_rpc -- --workspace --features test-fakes。"
    echo "執行：python3 -m pytest -q tests/ --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native --deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence。"
    echo "執行：env SERIALWRAP_CLI_EXE=<CLI> SERIALWRAP_DAEMON_EXE=<DAEMON> SERIALWRAP_AB_LABEL=rust-linux python3 -m pytest -q tests/contract_blackbox tests/acceptance/l15/test_slice21_l15.py tests/acceptance/l15/test_slice23_l15.py tests/test_multiagent_e2e.py tests/test_human_agent_coexist.py tests/test_rust_contract_coverage.py tests/test_transfer_golden_fresh.py。"
  fi
  echo "匯入：--windows-run、--longrun、前置 slice 2.1～2.3 證據；執行 python3 -m tools.contracts check、python3 tests/acceptance/regen_lock.py --check、python3 -m policy_check --repo .。"
  echo "執行：python3 scripts/rust/slice_breaker.py --freeze tests/acceptance/breaker/slice_2.4.json；python3 scripts/rust/contract_coverage.py <候選 artifact 與前置 slice 輸入>。"
  exit 0
fi

if [[ ! "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "slice24_exit.sh: --run-id 只允許英數與 ._-" >&2
  exit 2
fi
if [[ "$RUN_DIR" != target/contract-coverage/* ]]; then
  echo "slice24_exit.sh: RUN_DIR 必須位於 target/contract-coverage/" >&2
  exit 2
fi
RUN_REAL="$(realpath -m "$RUN_DIR")"
RUN_ROOT_REAL="$(realpath -m target/contract-coverage)"
case "$RUN_REAL/" in
  "$RUN_ROOT_REAL"/*) ;;
  *) echo "slice24_exit.sh: RUN_DIR 解析後超出 target/contract-coverage/" >&2; exit 2 ;;
esac
if [[ -e "$RUN_DIR" ]]; then
  echo "slice24_exit.sh: $RUN_DIR 已存在，拒絕混合不同輪次；請改用新的 --run-id" >&2
  exit 2
fi

mkdir -p "$RUN_DIR/logs" "$RUN_DIR/artifacts"
# Cargo、pytest 與 importer 的所有可寫產物固定落在本 worktree 的 target/。
export CARGO_TARGET_DIR="$ROOT/target"
DRIVER_TSV="$RUN_DIR/driver.tsv"
OWNER_TSV="$RUN_DIR/owner-evidence.tsv"
: >"$DRIVER_TSV"
: >"$OWNER_TSV"
if source "$ROOT/scripts/rust/gate_prereqs.sh" --gate >"$RUN_DIR/logs/gate-prereqs.log" 2>&1; then
  :
else
  PREREQS_RC=$?
  printf 'gate_prereqs\t%s\n' "$PREREQS_RC" >>"$DRIVER_TSV"
  echo "slice24_exit.sh: gate_prereqs exit=$PREREQS_RC（$RUN_DIR/logs/gate-prereqs.log）" >&2
  exit "$PREREQS_RC"
fi

# 先前切片 gate 證據複製進本輪目錄後才匯入，保留輸入來源且讓報告可追溯。
PRIOR_CASE_ARGS=()
if [[ -n "$SLICE21_RUN" ]]; then
  PRIOR_SLICE21="$RUN_DIR/prior-slices/slice21"
  mkdir -p "$PRIOR_SLICE21"
  [[ -f "$SLICE21_RUN/report.json" ]] && cp -p -- "$SLICE21_RUN/report.json" "$PRIOR_SLICE21/report.json"
  [[ -f "$SLICE21_RUN/case_verdicts.json" ]] && cp -p -- "$SLICE21_RUN/case_verdicts.json" "$PRIOR_SLICE21/case_verdicts.json"
  PRIOR_CASE_ARGS+=(--slice21-run "$PRIOR_SLICE21")
fi
if [[ -n "$SLICE22_GATE" ]]; then
  PRIOR_SLICE22="$RUN_DIR/prior-slices/s22-gate.json"
  mkdir -p "$(dirname "$PRIOR_SLICE22")"
  [[ -f "$SLICE22_GATE" ]] && cp -p -- "$SLICE22_GATE" "$PRIOR_SLICE22"
  PRIOR_CASE_ARGS+=(--slice22-gate "$PRIOR_SLICE22")
fi
if [[ -n "$SLICE23_RUN" ]]; then
  PRIOR_SLICE23="$RUN_DIR/prior-slices/slice23"
  mkdir -p "$PRIOR_SLICE23"
  [[ -f "$SLICE23_RUN/results.json" ]] && cp -p -- "$SLICE23_RUN/results.json" "$PRIOR_SLICE23/results.json"
  [[ -f "$SLICE23_RUN/coverage.json" ]] && cp -p -- "$SLICE23_RUN/coverage.json" "$PRIOR_SLICE23/coverage.json"
  PRIOR_CASE_ARGS+=(--slice23-run "$PRIOR_SLICE23")
fi

LAST_RC=0
FAILURES=0
PYTHON_BASELINE_COVERAGE=""
RUST_BLACKBOX_COVERAGE=""

run_logged() {
  local name="$1"
  shift
  local log="$RUN_DIR/logs/$name.log"
  {
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
  } >"$log"
  "$@" >>"$log" 2>&1
  LAST_RC=$?
  printf '%s\t%s\n' "$name" "$LAST_RC" >>"$DRIVER_TSV"
  if [[ "$LAST_RC" -ne 0 ]]; then
    FAILURES=1
    echo "slice24_exit.sh: $name exit=$LAST_RC（$log）" >&2
  fi
  return 0
}

RUST_CLI="$RUN_DIR/artifacts/serialwrap"
RUST_DAEMON="$RUN_DIR/artifacts/serialwrapd"
SHARED_ARTIFACTS_VALID=0
SHARED_BUNDLE_INVALID=0
SHARED_ARTIFACT_DIR="$RUN_DIR/shared-artifact-import"
SHARED_ARTIFACT_ARGS=()
if [[ -n "$SHARED_EVIDENCE" ]]; then
  BUNDLE_CHECK_ARGS=(--shared-evidence "$SHARED_EVIDENCE")
  [[ -z "$CANDIDATE_BUNDLE" ]] || BUNDLE_CHECK_ARGS+=(--candidate-bundle "$CANDIDATE_BUNDLE")
  if python3 scripts/rust/gate_shared_evidence.py check-candidate-bundle "${BUNDLE_CHECK_ARGS[@]}" \
      >"$RUN_DIR/logs/shared-candidate-bundle.json" 2>"$RUN_DIR/logs/shared-candidate-bundle.stderr"; then
    RESOLVED_BUNDLE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("path", ""))' \
      "$RUN_DIR/logs/shared-candidate-bundle.json")"
    if [[ -n "$RESOLVED_BUNDLE" ]]; then
      CANDIDATE_BUNDLE="$RESOLVED_BUNDLE"
    else
      SHARED_BUNDLE_INVALID=1
      FAILURES=1
      echo "EVIDENCE_MISSING：共用證據未回傳有效 candidate bundle 路徑。" >"$RUN_DIR/logs/shared-candidate-bundle.stderr"
    fi
  else
    SHARED_BUNDLE_INVALID=1
    FAILURES=1
    CANDIDATE_BUNDLE=""
    echo "EVIDENCE_MISSING：共用 candidate bundle 缺失、不符 HEAD 或與外部指定候選不一致。" >>"$RUN_DIR/logs/shared-candidate-bundle.stderr"
  fi
fi
if [[ -n "$SHARED_EVIDENCE" ]]; then
  if python3 scripts/rust/gate_shared_evidence.py import-artifacts --shared-evidence "$SHARED_EVIDENCE" \
      --out-dir "$SHARED_ARTIFACT_DIR" >"$RUN_DIR/logs/shared-artifacts.json" \
      2>"$RUN_DIR/logs/shared-artifacts.stderr"; then
    cp -p "$SHARED_ARTIFACT_DIR/debug/serialwrap" "$RUST_CLI"
    cp -p "$SHARED_ARTIFACT_DIR/debug/serialwrapd" "$RUST_DAEMON"
    BUILD_RC=0
    SHARED_ARTIFACT_ARGS=(--artifact "debug_cli=$SHARED_ARTIFACT_DIR/debug/serialwrap" \
      --artifact "debug_daemon=$SHARED_ARTIFACT_DIR/debug/serialwrapd" \
      --artifact "release_cli=$SHARED_ARTIFACT_DIR/release/serialwrap" \
      --artifact "release_daemon=$SHARED_ARTIFACT_DIR/release/serialwrapd")
    SHARED_ARTIFACTS_VALID=1
    if [[ -z "$CANDIDATE_BUNDLE" ]]; then
      BUILD_RC=0
      printf 'build-linux-candidate\t0\n' >>"$DRIVER_TSV"
      printf 'shared artifact import\n' >"$RUN_DIR/logs/build-linux-candidate.log"
    fi
  else
    FAILURES=1
    echo "EVIDENCE_MISSING：共用 artifact provenance 不符。" >"$RUN_DIR/logs/build-linux-candidate.log"
    cat "$RUN_DIR/logs/shared-artifacts.stderr" >>"$RUN_DIR/logs/build-linux-candidate.log" || true
    BUILD_RC=3
    printf 'build-linux-candidate\t%s\n' "$BUILD_RC" >>"$DRIVER_TSV"
  fi
else
  run_logged build-linux-candidate cargo build --workspace --bins
  BUILD_RC="$LAST_RC"
  [[ "$BUILD_RC" != 0 ]] || { cp -p "$CARGO_TARGET_DIR/debug/serialwrap" "$RUST_CLI"; cp -p "$CARGO_TARGET_DIR/debug/serialwrapd" "$RUST_DAEMON"; }
fi
if [[ -n "$CANDIDATE_BUNDLE" ]]; then
  # 本輪候選＝指定 bundle：受測 CLI／daemon 直接取自其版本目錄（與 realhw deployed_content.install_dir 同解析），
  # 並記錄檔案清單與 tree 雜湊，供長跑 pinned bundle／CLI／daemon 交叉核對（P8X-e038dda-1）。
  CANDIDATE_CLI_REAL="$(realpath -e "$CANDIDATE_BUNDLE/bin/serialwrap" 2>/dev/null || true)"
  CANDIDATE_ROOT=""
  [[ -n "$CANDIDATE_CLI_REAL" ]] && CANDIDATE_ROOT="$(dirname "$(dirname "$CANDIDATE_CLI_REAL")")"
  if [[ -n "$CANDIDATE_ROOT" && -f "$CANDIDATE_ROOT/release-manifest.json" \
        && -f "$CANDIDATE_ROOT/bin/serialwrap" && -f "$CANDIDATE_ROOT/bin/serialwrapd" ]]; then
    # 候選 bundle 必須由本 worktree HEAD 建置，Cargo L1 結果才可歸屬同一候選（不同來源一律拒收）。
    run_logged linux-candidate-source python3 - "$CANDIDATE_ROOT/release-manifest.json" "$(git rev-parse HEAD)" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
revision = manifest.get("git_revision") if isinstance(manifest, dict) else None
if revision != sys.argv[2]:
    print(f"候選 bundle git_revision={revision!r} 不等於本 worktree HEAD {sys.argv[2]}")
    raise SystemExit(1)
print(f"候選 bundle git_revision={revision}")
PY
    if [[ "$LAST_RC" -eq 0 ]]; then
      RUST_CLI="$CANDIDATE_ROOT/bin/serialwrap"
      RUST_DAEMON="$CANDIDATE_ROOT/bin/serialwrapd"
      run_logged linux-candidate-bundle python3 scripts/rust/contract_coverage.py \
        --run-dir "$RUN_DIR" --record-candidate-bundle "linux=$CANDIDATE_ROOT"
    fi
  else
    printf 'linux-candidate-bundle\t1\n' >>"$DRIVER_TSV"
    FAILURES=1
    echo "slice24_exit.sh: --candidate-bundle 不是有效 bundle（需 bin/serialwrap 解析後的版本目錄含 release-manifest.json）" >&2
  fi
  if [[ ! -f "$RUN_DIR/artifacts/linux/candidate-bundle.json" ]]; then
    RUST_CLI="$RUN_DIR/artifacts/missing-candidate-cli"
    RUST_DAEMON="$RUN_DIR/artifacts/missing-candidate-daemon"
    echo "slice24_exit.sh: 候選 bundle 未記錄，後續結果不得計 PASS" >&2
  fi
elif [[ "$BUILD_RC" -eq 0 && -f "$RUST_CLI" && -f "$RUST_DAEMON" ]]; then
  :
else
  printf 'artifact_snapshot\t1\n' >>"$DRIVER_TSV"
  FAILURES=1
  echo "slice24_exit.sh: Rust CLI／daemon artifact 缺失，後續結果不得計 PASS" >&2
fi
if [[ "$SHARED_ARTIFACTS_VALID" == "1" && -n "$CANDIDATE_BUNDLE" ]]; then
  SHARED_ARTIFACT_ARGS=(--artifact "debug_cli=$SHARED_ARTIFACT_DIR/debug/serialwrap" \
    --artifact "debug_daemon=$SHARED_ARTIFACT_DIR/debug/serialwrapd" \
    --artifact "release_cli=$RUST_CLI" --artifact "release_daemon=$RUST_DAEMON")
fi

run_logged cfg-boundary python3 scripts/rust/check_cfg_boundary.py
if [[ -n "$SHARED_EVIDENCE" ]]; then
  if [[ "$SHARED_ARTIFACTS_VALID" == 1 ]] && python3 scripts/rust/gate_shared_evidence.py import-section \
      --shared-evidence "$SHARED_EVIDENCE" --gate slice24 --section cargo_fakes_p7 \
      --required-set p7_gate:cargo-test-workspace --platform linux \
      "${SHARED_ARTIFACT_ARGS[@]}" \
      --out-dir "$RUN_DIR/shared-import" >"$RUN_DIR/logs/shared-cargo-fakes.json" \
      2>"$RUN_DIR/logs/shared-cargo-fakes.stderr"; then
    cp "$RUN_DIR/shared-import/logs/cargo-fakes-p7.log" "$RUN_DIR/logs/cargo-workspace-test-fakes.log"
    LAST_RC="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit_code"])' "$RUN_DIR/logs/shared-cargo-fakes.json")"
    printf 'cargo-workspace-test-fakes\t%s\n' "$LAST_RC" >>"$DRIVER_TSV"
    [[ "$LAST_RC" == 0 ]] || FAILURES=1
  else
    echo "EVIDENCE_MISSING：共用 test-fakes 證據拒收；本段不重跑。" >"$RUN_DIR/logs/cargo-workspace-test-fakes.log"
    cat "$RUN_DIR/logs/shared-cargo-fakes.stderr" >>"$RUN_DIR/logs/cargo-workspace-test-fakes.log" 2>/dev/null || true
    printf 'cargo-workspace-test-fakes\t3\n' >>"$DRIVER_TSV"
    FAILURES=1
  fi
  if [[ "$SHARED_ARTIFACTS_VALID" == 1 ]] && python3 scripts/rust/gate_shared_evidence.py import-section \
      --shared-evidence "$SHARED_EVIDENCE" --gate slice24 --section cargo_workspace_default \
      --expected-command cargo_workspace_default --platform linux \
      "${SHARED_ARTIFACT_ARGS[@]}" \
      --out-dir "$RUN_DIR/shared-import" >"$RUN_DIR/logs/shared-cargo-default.json" \
      2>"$RUN_DIR/logs/shared-cargo-default.stderr"; then
    cp "$RUN_DIR/shared-import/logs/cargo-workspace-default.log" "$RUN_DIR/logs/cargo-workspace-test.log"
    LAST_RC="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit_code"])' "$RUN_DIR/logs/shared-cargo-default.json")"
    printf 'cargo-workspace-test\t%s\n' "$LAST_RC" >>"$DRIVER_TSV"
    [[ "$LAST_RC" == 0 ]] || FAILURES=1
  else
    echo "EVIDENCE_MISSING：共用 cargo workspace 證據拒收；本段不重跑。" >"$RUN_DIR/logs/cargo-workspace-test.log"
    cat "$RUN_DIR/logs/shared-cargo-default.stderr" >>"$RUN_DIR/logs/cargo-workspace-test.log" 2>/dev/null || true
    printf 'cargo-workspace-test\t3\n' >>"$DRIVER_TSV"
    FAILURES=1
  fi
else
  run_logged cargo-workspace-test-fakes cargo test --workspace --features test-fakes
  run_logged cargo-workspace-test cargo test --workspace
fi
# 交叉編譯僅記開發檢查，不會啟動 Windows host，也不作 Windows 原生證據。
run_logged cargo-windows-check env CARGO_INCREMENTAL=0 cargo check --workspace --target x86_64-pc-windows-gnu

if [[ -f "$RUST_CLI" && -f "$RUST_DAEMON" ]]; then
  if [[ -n "$SHARED_EVIDENCE" ]]; then
    if [[ "$SHARED_ARTIFACTS_VALID" == 1 ]] && python3 scripts/rust/gate_shared_evidence.py import-section \
        --shared-evidence "$SHARED_EVIDENCE" --gate slice24 --section cargo_contract_results \
        --expected-command cargo_contract_results --platform linux \
        "${SHARED_ARTIFACT_ARGS[@]}" \
        --out-dir "$RUN_DIR/shared-import" >"$RUN_DIR/logs/shared-cargo-contract-results.json" \
        2>"$RUN_DIR/logs/shared-cargo-contract-results.stderr"; then
      tar -xzf "$RUN_DIR/shared-import/slice24/cargo-results.tar.gz" -C "$RUN_DIR"
      cp "$RUN_DIR/shared-import/logs/cargo-contract-results.log" "$RUN_DIR/logs/rust-cargo-results.log"
      LAST_RC="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit_code"])' "$RUN_DIR/logs/shared-cargo-contract-results.json")"
      printf 'rust-cargo-results\t%s\n' "$LAST_RC" >>"$DRIVER_TSV"
      [[ "$LAST_RC" == 0 ]] || FAILURES=1
    else
      echo "EVIDENCE_MISSING：共用逐案 Cargo 結果拒收；本段不重跑。" >"$RUN_DIR/logs/rust-cargo-results.log"
      cat "$RUN_DIR/logs/shared-cargo-contract-results.stderr" >>"$RUN_DIR/logs/rust-cargo-results.log" 2>/dev/null || true
      printf 'rust-cargo-results\t3\n' >>"$DRIVER_TSV"
      FAILURES=1
    fi
  else
    run_logged rust-cargo-results env \
      SERIALWRAP_CLI_EXE="$RUST_CLI" \
      SERIALWRAP_DAEMON_EXE="$RUST_DAEMON" \
      python3 scripts/rust/import_cargo_results.py \
        --label rust-linux --platform linux --out-dir "$RUN_DIR" \
        --include-cap posix_pty --include-cap process_rpc \
        -- --workspace --features test-fakes
  fi
else
  printf 'rust-cargo-results\t1\n' >>"$DRIVER_TSV"
  FAILURES=1
fi

# N3（依 2026-11-12 rulings/s2.md 第 3 點）：Python 基線 pytest 參數與 CI 對齊。
if [[ -n "$SHARED_EVIDENCE" ]]; then
  if [[ "$SHARED_ARTIFACTS_VALID" == 1 ]] && python3 scripts/rust/gate_shared_evidence.py import-section \
      --shared-evidence "$SHARED_EVIDENCE" --gate slice24 --section pytest_full \
      --required-set p7_gate:python3-m-pytest-q-tests --platform linux \
      "${SHARED_ARTIFACT_ARGS[@]}" \
      --out-dir "$RUN_DIR/shared-import" >"$RUN_DIR/logs/shared-python-baseline.json" \
      2>"$RUN_DIR/logs/shared-python-baseline.stderr"; then
    PYTHON_BASELINE_COVERAGE="$RUN_DIR/shared-import/python-baseline-coverage"
    mkdir -p "$PYTHON_BASELINE_COVERAGE"
    tar -xzf "$RUN_DIR/shared-import/slice24/python-baseline-coverage.tar.gz" \
      -C "$PYTHON_BASELINE_COVERAGE" --strip-components=1
    cp "$RUN_DIR/shared-import/logs/pytest-full.log" "$RUN_DIR/logs/python-baseline.log"
    LAST_RC="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit_code"])' "$RUN_DIR/logs/shared-python-baseline.json")"
    printf 'python-baseline\t%s\n' "$LAST_RC" >>"$DRIVER_TSV"
    [[ "$LAST_RC" == 0 ]] || FAILURES=1
  else
    echo "EVIDENCE_MISSING：共用 pytest_full 證據拒收；本段不重跑。" >"$RUN_DIR/logs/python-baseline.log"
    cat "$RUN_DIR/logs/shared-python-baseline.stderr" >>"$RUN_DIR/logs/python-baseline.log" 2>/dev/null || true
    printf 'python-baseline\t3\n' >>"$DRIVER_TSV"
    FAILURES=1
  fi
else
  run_logged python-baseline env \
    SERIALWRAP_COVERAGE_RUN_DIR="$RUN_DIR" \
    SERIALWRAP_COVERAGE_LABEL=python-linux \
    python3 -m pytest -q tests/ \
    --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native \
    --deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence
fi

# Rust label 的 pytest 收集範圍：凡需 rust-linux 證據的 pytest nodeid（含 rust-l1 凍結案例的 pytest 節點），
# 其檔案都要在此陣列內並與本輪 artifact 一起執行（P8X-24c6b39-6；tests/test_rust_contract_coverage.py 把關）。
RUST_PYTEST_TARGETS=(
  tests/contract_blackbox
  tests/acceptance/l15/test_slice21_l15.py
  tests/acceptance/l15/test_slice23_l15.py
  tests/test_multiagent_e2e.py
  tests/test_human_agent_coexist.py
  tests/test_rust_contract_coverage.py
  tests/test_transfer_golden_fresh.py
)
if [[ -f "$RUST_CLI" && -f "$RUST_DAEMON" ]]; then
  if [[ -n "$SHARED_EVIDENCE" ]]; then
    S24_BUNDLE_ARGS=()
    [[ -z "$CANDIDATE_BUNDLE" ]] || S24_BUNDLE_ARGS+=(--candidate-bundle "$CANDIDATE_BUNDLE")
    if [[ "$SHARED_BUNDLE_INVALID" == 1 ]]; then
      echo "EVIDENCE_MISSING：shared s24_ab candidate bundle 驗證失敗；拒絕匯入。" >"$RUN_DIR/logs/rust-blackbox.log"
      printf 'rust-blackbox\t3\n' >>"$DRIVER_TSV"
      FAILURES=1
    elif [[ "$SHARED_ARTIFACTS_VALID" == 1 ]] && python3 scripts/rust/gate_shared_evidence.py import-section \
        --shared-evidence "$SHARED_EVIDENCE" --gate slice24 --section s24_ab \
        --expected-command s24_ab --platform linux "${S24_BUNDLE_ARGS[@]}" \
        "${SHARED_ARTIFACT_ARGS[@]}" \
        --out-dir "$RUN_DIR/shared-import" >"$RUN_DIR/logs/shared-rust-blackbox.json" \
        2>"$RUN_DIR/logs/shared-rust-blackbox.stderr"; then
      RUST_BLACKBOX_COVERAGE="$RUN_DIR/shared-import/rust-blackbox-coverage"
      mkdir -p "$RUST_BLACKBOX_COVERAGE"
      tar -xzf "$RUN_DIR/shared-import/slice24/coverage.tar.gz" \
        -C "$RUST_BLACKBOX_COVERAGE" --strip-components=1
      cp "$RUN_DIR/shared-import/slice24/s24-ab/junit.xml" "$RUN_DIR/rust-blackbox.junit.xml"
      cp "$RUN_DIR/shared-import/logs/s24-ab.log" "$RUN_DIR/logs/rust-blackbox.log"
      LAST_RC="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit_code"])' "$RUN_DIR/logs/shared-rust-blackbox.json")"
      printf 'rust-blackbox\t%s\n' "$LAST_RC" >>"$DRIVER_TSV"
      [[ "$LAST_RC" == 0 ]] || FAILURES=1
    else
      echo "EVIDENCE_MISSING：共用 Slice 2.4 A/B 證據拒收；本段不重跑。" >"$RUN_DIR/logs/rust-blackbox.log"
      cat "$RUN_DIR/logs/shared-rust-blackbox.stderr" >>"$RUN_DIR/logs/rust-blackbox.log" 2>/dev/null || true
      printf 'rust-blackbox\t3\n' >>"$DRIVER_TSV"
      FAILURES=1
    fi
  else
    S24_BUNDLE_ENV=()
    [[ -z "$CANDIDATE_BUNDLE" ]] || S24_BUNDLE_ENV+=("SW_BUNDLE=$CANDIDATE_BUNDLE")
    run_logged rust-blackbox env "${S24_BUNDLE_ENV[@]}" \
      SERIALWRAP_CLI_EXE="$RUST_CLI" \
      SERIALWRAP_DAEMON_EXE="$RUST_DAEMON" \
      SERIALWRAP_AB_LABEL=rust-linux \
      SERIALWRAP_COVERAGE_RUN_DIR="$RUN_DIR" \
      SERIALWRAP_COVERAGE_LABEL=rust-linux \
      python3 -m pytest -q "${RUST_PYTEST_TARGETS[@]}"
  fi
else
  printf 'rust-blackbox\t1\n' >>"$DRIVER_TSV"
  FAILURES=1
fi

# Windows 證據只接受既有原生 runner 產物；此 Linux 腳本不呼叫 Windows 程序。
if [[ -n "$WINDOWS_RUN" ]]; then
  python3 - "$WINDOWS_RUN" "$RUN_DIR" <<'PY'
import hashlib
import json
import shutil
import sys
from pathlib import Path, PureWindowsPath

source = Path(sys.argv[1]).resolve()
run_dir = Path(sys.argv[2]).resolve()
manifest = source / "SHA256SUMS"
results = source / "results-rust-windows.json"
if not source.is_dir() or not manifest.is_file() or not results.is_file():
    print("EVIDENCE_MISSING: Windows runner 需提供 SHA256SUMS 與 results-rust-windows.json")
    raise SystemExit(0)

listed = {}
try:
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, name = line.partition("  ")
        if not name:
            digest, _, name = line.partition(" ")
        name = name.lstrip("* ")
        if len(digest) != 64 or not name:
            raise ValueError("SHA256SUMS 格式錯誤")
        candidate = (source / name).resolve()
        if not candidate.is_relative_to(source) or not candidate.is_file():
            raise ValueError(f"SHA256SUMS 路徑無效：{name}")
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != digest.lower():
            raise ValueError(f"SHA256SUMS 雜湊不符：{name}")
        listed[digest.lower()] = candidate
    document = json.loads(results.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Windows 結果根節點不是 object")
    if document.get("label") != "rust-windows" or document.get("platform") != "windows":
        raise ValueError("Windows 結果 label/platform 不符")
    if document.get("native_windows") is not True or document.get("native_build_link") is not True:
        raise ValueError("結果未證明 Windows 原生 build/link 與實跑")
    artifacts_dir = run_dir / "artifacts" / "windows"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    # 判定器以 runner 自己的 SHA256SUMS 核對 Windows artifact，不套用 Linux 預期雜湊（P8X-24c6b39-5）。
    shutil.copyfile(manifest, artifacts_dir / "SHA256SUMS")
    for key in ("cli", "daemon"):
        digest = document.get(f"{key}_sha256")
        candidate = listed.get(digest)
        if candidate is None:
            raise ValueError(f"{key} artifact 不在已驗證的 SHA256SUMS")
        source_name = document.get(f"{key}_exe")
        suffix = PureWindowsPath(str(source_name)).suffix or candidate.suffix
        copied = artifacts_dir / f"serialwrap-{key}{suffix}"
        shutil.copyfile(candidate, copied)
        if hashlib.sha256(copied.read_bytes()).hexdigest() != digest:
            raise ValueError(f"複製後 {key} artifact 雜湊不符")
        document[f"{key}_exe"] = str(copied)
    destination = run_dir / "results-rust-windows.json"
    destination.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Windows 原生證據已匯入：{destination}")
except (OSError, ValueError, json.JSONDecodeError) as exc:
    print(f"EVIDENCE_MISSING: Windows 原生證據拒收：{exc}")
PY
  WINDOWS_IMPORT_RC=$?
  if [[ "$WINDOWS_IMPORT_RC" -eq 0 && -f "$RUN_DIR/results-rust-windows.json" ]]; then
    printf 'windows-native\tIMPORTED_PENDING_JUDGE\n' >>"$OWNER_TSV"
  else
    printf 'windows-native\tEVIDENCE_MISSING\n' >>"$OWNER_TSV"
  fi
  # Windows 候選 bundle（runner 附的 bundle/ 版本目錄）：判定時其 bin/*.exe 須等於 results-rust-windows 的受測 artifact。
  if [[ -f "$WINDOWS_RUN/bundle/release-manifest.json" ]]; then
    if python3 scripts/rust/contract_coverage.py --run-dir "$RUN_DIR" \
        --record-candidate-bundle "windows=$WINDOWS_RUN/bundle" >"$RUN_DIR/logs/windows-candidate-bundle.log" 2>&1; then
      printf 'windows-candidate-bundle\tRECORDED_PENDING_JUDGE\n' >>"$OWNER_TSV"
    else
      printf 'windows-candidate-bundle\tREJECTED（見 %s）\n' "$RUN_DIR/logs/windows-candidate-bundle.log" >>"$OWNER_TSV"
    fi
  else
    printf 'windows-candidate-bundle\tEVIDENCE_MISSING\n' >>"$OWNER_TSV"
  fi
else
  printf 'windows-native\tEVIDENCE_MISSING\n' >>"$OWNER_TSV"
  printf 'windows-candidate-bundle\tEVIDENCE_MISSING\n' >>"$OWNER_TSV"
fi
if [[ -z "$CANDIDATE_BUNDLE" ]]; then
  # 未指定候選 bundle 時本輪受測的是 debug build，長跑（pinned release bundle）無法綁定本輪候選。
  printf 'linux-candidate-bundle\tEVIDENCE_MISSING（未指定 --candidate-bundle；長跑無法綁定本輪候選）\n' >>"$OWNER_TSV"
fi

# 長跑證據（計畫 Task 15 步驟 8）：驗證全部成立才匯入並解除 lr-mixed 的擁有者阻擋；
# 缺漏、竄改或拒收的證據照實記錄，lr-mixed 維持 BLOCKED-OWNER。TestPilot、真機 COM 與 USB-UART 由擁有者另行執行。
if [[ -n "$LONGRUN_DIR" ]]; then
  LONGRUN_LOG="$RUN_DIR/logs/longrun-import.log"
  python3 scripts/rust/contract_coverage.py --run-dir "$RUN_DIR" --import-longrun "$LONGRUN_DIR" >"$LONGRUN_LOG" 2>&1
  LONGRUN_RC=$?
  case "$LONGRUN_RC" in
    0) printf 'longrun\tIMPORTED_PENDING_JUDGE（%s）\n' "$(grep -E '^longrun ' "$LONGRUN_LOG" | tr '\n' ' ')" >>"$OWNER_TSV" ;;
    1) printf 'longrun\tREJECTED（見 %s）\n' "$LONGRUN_LOG" >>"$OWNER_TSV" ;;
    *) printf 'longrun\tEVIDENCE_MISSING（匯入失敗 exit=%s，見 %s）\n' "$LONGRUN_RC" "$LONGRUN_LOG" >>"$OWNER_TSV" ;;
  esac
else
  printf 'longrun\tBLOCKED-OWNER\n' >>"$OWNER_TSV"
fi
printf 'physical_uart_testpilot\tBLOCKED-OWNER\n' >>"$OWNER_TSV"
printf 'windows_com_supervisor_runtime\tBLOCKED-OWNER\n' >>"$OWNER_TSV"

run_logged contracts-check python3 -m tools.contracts check
run_logged regen-lock-check python3 tests/acceptance/regen_lock.py --check
run_logged slice-breaker python3 scripts/rust/slice_breaker.py \
  --freeze tests/acceptance/breaker/slice_2.4.json \
  --snapshots tests/acceptance/breaker/slice_2.4.snapshots.ndjson \
  --today "$(date -u +%F)"
run_logged policy-check python3 -m policy_check --repo .

JSON_REPORT="$RUN_DIR/slice24.json"
MD_REPORT="$RUN_DIR/slice24.md"
# 預期雜湊只對 Linux 候選 artifact；Windows 結果以其 runner SHA256SUMS 核對。
CONTRACT_COVERAGE_ARGS=(
  --run-dir "$RUN_DIR" \
  --expect-platform linux \
  --expect-git-revision "$(git rev-parse HEAD)" \
  --expect-cli-sha256 "$(sha256sum "$RUST_CLI" 2>/dev/null | cut -d' ' -f1)" \
  --expect-daemon-sha256 "$(sha256sum "$RUST_DAEMON" 2>/dev/null | cut -d' ' -f1)" \
  --json "$JSON_REPORT" \
  --report "$MD_REPORT"
)
if [[ -n "$PYTHON_BASELINE_COVERAGE" ]]; then
  CONTRACT_COVERAGE_ARGS+=(--merge-results-from "$PYTHON_BASELINE_COVERAGE")
fi
if [[ -n "$RUST_BLACKBOX_COVERAGE" ]]; then
  CONTRACT_COVERAGE_ARGS+=(--merge-results-from "$RUST_BLACKBOX_COVERAGE")
fi
CONTRACT_COVERAGE_ARGS+=("${PRIOR_CASE_ARGS[@]}")
run_logged contract-coverage python3 scripts/rust/contract_coverage.py "${CONTRACT_COVERAGE_ARGS[@]}"
CONTRACT_COVERAGE_RC="$LAST_RC"

if [[ -f "$JSON_REPORT" ]]; then
  python3 - "$JSON_REPORT" "$MD_REPORT" "$DRIVER_TSV" "$OWNER_TSV" "$RUN_DIR" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path
import yaml

json_path, markdown_path, driver_path, owner_path, run_dir = map(Path, sys.argv[1:])
report = json.loads(json_path.read_text(encoding="utf-8"))
counts = Counter(report.get("case_verdicts", {}).values())
cases = yaml.safe_load((Path("tests/acceptance/cases.yaml")).read_text(encoding="utf-8"))["cases"]
windows_cases = [case for case in cases if "windows" in case.get("platforms", [])]
windows_counts = Counter(report.get("case_verdicts", {}).get(case["case_id"], "EVIDENCE_MISSING") for case in windows_cases)
lines = [
    "",
    "## Linux gate 命令狀態",
    "",
    "| 項目 | exit | 狀態 |",
    "|---|---:|---|",
]
for row in driver_path.read_text(encoding="utf-8").splitlines():
    if not row.strip():
        continue
    name, code = row.split("\t", 1)
    state = "PASS" if code == "0" else "FAIL"
    lines.append(f"| `{name}` | {code} | **{state}** |")
lines.extend([
    "",
    "## 擁有者證據輸入",
    "",
])
for row in owner_path.read_text(encoding="utf-8").splitlines():
    if row.strip():
        name, status = row.split("\t", 1)
        lines.append(f"- `{name}`：**{status}**")
lines.extend(["", "Windows 凍結案例：" + ", ".join(f"{key}={windows_counts.get(key, 0)}" for key in ("PASS", "FAIL", "EVIDENCE_MISSING", "BLOCKED-OWNER")), ""])
markdown_path.write_text(markdown_path.read_text(encoding="utf-8") + "\n".join(lines), encoding="utf-8")
print("Slice 2.4 凍結案例：" + ", ".join(f"{key}={counts.get(key, 0)}" for key in ("PASS", "FAIL", "EVIDENCE_MISSING", "BLOCKED-OWNER")))
print(f"契約 ID：covered={len(report.get('covered', []))} missing={sum(len(rows) for rows in report.get('missing', {}).values())} failed={len(report.get('failed', []))}")
print(f"BLOCKED-OWNER={len(report.get('blocked_owner', {}))} Windows native cases: " + ", ".join(f"{key}={windows_counts.get(key, 0)}" for key in ("PASS", "FAIL", "EVIDENCE_MISSING", "BLOCKED-OWNER")))
PY
else
  echo "slice24_exit.sh: 未產生 slice24.json；請檢查 $RUN_DIR/logs/contract-coverage.log" >&2
  FAILURES=1
fi

echo "slice24 JSON: $JSON_REPORT"
echo "slice24 gate report: $MD_REPORT"
if [[ "$CONTRACT_COVERAGE_RC" -ne 0 || "$FAILURES" -ne 0 ]]; then
  exit 1
fi
exit 0
