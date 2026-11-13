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

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-slice24"
RUN_DIR="${SW_SLICE24_RUN_DIR:-target/contract-coverage/$RUN_ID}"
WINDOWS_RUN=""
LONGRUN_DIR=""
CANDIDATE_BUNDLE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
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
    -h|--help)
      sed -n '2,9p' "$0"
      echo "用法：$0 [--run-id ID] [--candidate-bundle DIR] [--windows-run DIR] [--longrun DIR]"
      exit 0 ;;
    *) echo "slice24_exit.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done

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
LAST_RC=0
FAILURES=0

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

# 先快照不帶 test-fakes 的 daemon/CLI；後續 Cargo 測試不得改寫本輪受測 artifact。
run_logged build-linux-candidate cargo build --workspace --bins
BUILD_RC="$LAST_RC"
RUST_CLI="$RUN_DIR/artifacts/serialwrap"
RUST_DAEMON="$RUN_DIR/artifacts/serialwrapd"
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
elif [[ "$BUILD_RC" -eq 0 && -f "$CARGO_TARGET_DIR/debug/serialwrap" && -f "$CARGO_TARGET_DIR/debug/serialwrapd" ]]; then
  cp -p "$CARGO_TARGET_DIR/debug/serialwrap" "$RUST_CLI"
  cp -p "$CARGO_TARGET_DIR/debug/serialwrapd" "$RUST_DAEMON"
else
  printf 'artifact_snapshot\t1\n' >>"$DRIVER_TSV"
  FAILURES=1
  echo "slice24_exit.sh: Rust CLI／daemon artifact 缺失，後續結果不得計 PASS" >&2
fi

run_logged cfg-boundary python3 scripts/rust/check_cfg_boundary.py
run_logged cargo-workspace-test-fakes cargo test --workspace --features test-fakes
run_logged cargo-workspace-test cargo test --workspace
# 交叉編譯僅記開發檢查，不會啟動 Windows host，也不作 Windows 原生證據。
run_logged cargo-windows-check cargo check --workspace --target x86_64-pc-windows-gnu

if [[ -f "$RUST_CLI" && -f "$RUST_DAEMON" ]]; then
  run_logged rust-cargo-results env \
    SERIALWRAP_CLI_EXE="$RUST_CLI" \
    SERIALWRAP_DAEMON_EXE="$RUST_DAEMON" \
    python3 scripts/rust/import_cargo_results.py \
      --label rust-linux --platform linux --out-dir "$RUN_DIR" \
      --include-cap posix_pty --include-cap process_rpc \
      -- --workspace --features test-fakes
else
  printf 'rust-cargo-results\t1\n' >>"$DRIVER_TSV"
  FAILURES=1
fi

# N3（依 2026-11-12 rulings/s2.md 第 3 點）：Python 基線 pytest 參數與 CI 對齊。
run_logged python-baseline env \
  SERIALWRAP_COVERAGE_RUN_DIR="$RUN_DIR" \
  SERIALWRAP_COVERAGE_LABEL=python-linux \
  python3 -m pytest -q tests/ \
  --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native \
  --deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence

# Rust label 的 pytest 收集範圍：凡需 rust-linux 證據的 pytest nodeid（含 rust-l1 凍結案例的 pytest 節點），
# 其檔案都要在此陣列內並與本輪 artifact 一起執行（P8X-24c6b39-6；tests/test_rust_contract_coverage.py 把關）。
RUST_PYTEST_TARGETS=(
  tests/contract_blackbox
  tests/test_multiagent_e2e.py
  tests/test_human_agent_coexist.py
  tests/test_rust_contract_coverage.py
  tests/test_transfer_golden_fresh.py
)
if [[ -f "$RUST_CLI" && -f "$RUST_DAEMON" ]]; then
  run_logged rust-blackbox env \
    SERIALWRAP_CLI_EXE="$RUST_CLI" \
    SERIALWRAP_DAEMON_EXE="$RUST_DAEMON" \
    SERIALWRAP_AB_LABEL=rust-linux \
    SERIALWRAP_COVERAGE_RUN_DIR="$RUN_DIR" \
    SERIALWRAP_COVERAGE_LABEL=rust-linux \
    python3 -m pytest -q "${RUST_PYTEST_TARGETS[@]}"
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
run_logged contract-coverage python3 scripts/rust/contract_coverage.py \
  --run-dir "$RUN_DIR" \
  --expect-platform linux \
  --expect-cli-sha256 "$(sha256sum "$RUST_CLI" 2>/dev/null | cut -d' ' -f1)" \
  --expect-daemon-sha256 "$(sha256sum "$RUST_DAEMON" 2>/dev/null | cut -d' ' -f1)" \
  --json "$JSON_REPORT" \
  --report "$MD_REPORT"
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
