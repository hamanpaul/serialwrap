#!/usr/bin/env bash
# Slice 2.2 出口證據編排：補跑鎖定的 Python parity、驗證 L1.5 daemon 身分與候選建置、匯入 Windows 證據並判定。
set -euo pipefail
export CARGO_INCREMENTAL=0

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
cd "$ROOT"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-s22"
OUT_ROOT="$ROOT/target/s22-gate"
L1_DIR=""
WINDOWS_DIR="${SW_GATE_S22_WINDOWS_DIR:-}"
SELFTEST_REPORT="${SERIALWRAP_L15_SELFTEST_REPORT:-}"
CLI_PATH=""
DAEMON_PATH=""
BUILD_EXIT=""
BUILT_DAEMON_SHA256=""
DRY_RUN=0
SHARED_EVIDENCE=""
SHARED_REJECTED_EVIDENCE=()

mark_shared_rejected() {
  local rejected="$1/$2" current
  for current in "${SHARED_REJECTED_EVIDENCE[@]}"; do
    [[ "$current" == "$rejected" ]] && return 0
  done
  SHARED_REJECTED_EVIDENCE+=("$rejected")
  echo "EVIDENCE_MISSING：共用證據拒收：$rejected" >&2
}

while (($#)); do
  case "$1" in
    --run-id) [[ $# -ge 2 ]] || { echo "s22_gate.sh: --run-id 缺少值" >&2; exit 2; }; RUN_ID="$2"; shift 2 ;;
    --out) [[ $# -ge 2 ]] || { echo "s22_gate.sh: --out 缺少值" >&2; exit 2; }; OUT_ROOT="$2"; shift 2 ;;
    --l1-results-dir) [[ $# -ge 2 ]] || { echo "s22_gate.sh: --l1-results-dir 缺少值" >&2; exit 2; }; L1_DIR="$2"; shift 2 ;;
    --windows-dir) [[ $# -ge 2 ]] || { echo "s22_gate.sh: --windows-dir 缺少值" >&2; exit 2; }; WINDOWS_DIR="$2"; shift 2 ;;
    --selftest-report) [[ $# -ge 2 ]] || { echo "s22_gate.sh: --selftest-report 缺少值" >&2; exit 2; }; SELFTEST_REPORT="$2"; shift 2 ;;
    --daemon-path) [[ $# -ge 2 ]] || { echo "s22_gate.sh: --daemon-path 缺少值" >&2; exit 2; }; DAEMON_PATH="$2"; shift 2 ;;
    --build-linux-exit) [[ $# -ge 2 ]] || { echo "s22_gate.sh: --build-linux-exit 缺少值" >&2; exit 2; }; BUILD_EXIT="$2"; shift 2 ;;
    --built-daemon-sha256) [[ $# -ge 2 ]] || { echo "s22_gate.sh: --built-daemon-sha256 缺少值" >&2; exit 2; }; BUILT_DAEMON_SHA256="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --shared-evidence) [[ $# -ge 2 ]] || { echo "s22_gate.sh: --shared-evidence 缺少值" >&2; exit 2; }; SHARED_EVIDENCE="$2"; shift 2 ;;
    -h|--help)
      printf '%s\n' '用法：scripts/rust/s22_gate.sh [--dry-run] --shared-evidence DIR [--run-id ID] [--out DIR] [--l1-results-dir DIR] [--selftest-report FILE]'
      printf '%s\n' '正式執行必須使用共用證據；不支援非共用正式模式。--windows-dir 不用於共用模式。'
      exit 0 ;;
    *) echo "s22_gate.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done
export SERIALWRAP_L15_RUST_DAEMON="$DAEMON_PATH"

if ! OUT_ROOT="$(realpath -m -- "$OUT_ROOT")"; then
  echo "s22_gate.sh: 無法解析 --out 路徑：$OUT_ROOT" >&2
  exit 2
fi
TARGET_ROOT="$(realpath -m -- "$ROOT/target")"
case "$OUT_ROOT" in
  "$TARGET_ROOT"|"$TARGET_ROOT"/*) ;;
  *) echo "s22_gate.sh: --out 必須位於本 worktree 的 target/ 底下：$OUT_ROOT" >&2; exit 2 ;;
esac

if [[ "$DRY_RUN" != "1" && -z "$SHARED_EVIDENCE" ]]; then
  echo "s22_gate.sh: 非共用模式的正式執行已停用；必須提供 --shared-evidence。" >&2
  exit 2
fi
if [[ "$DRY_RUN" != "1" && -n "$SHARED_EVIDENCE" ]]; then
  # 共用模式只使用經 manifest 驗證並匯入的 release CLI／daemon，不接受外層路徑覆寫。
  CLI_PATH=""
  DAEMON_PATH=""
  BUILD_EXIT=""
  BUILT_DAEMON_SHA256=""
  unset SERIALWRAP_L15_RUST_DAEMON
fi
export SERIALWRAP_L15_RUST_DAEMON="$DAEMON_PATH"

[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "s22_gate.sh: --run-id 只允許英數與 ._-" >&2; exit 2; }
RUN_DIR="$OUT_ROOT/$RUN_ID"
REPORT="$RUN_DIR/s22-gate.json"
[[ ! -e "$REPORT" ]] || { echo "s22_gate.sh: $REPORT 已存在，拒絕覆寫" >&2; exit 2; }
mkdir -p "$RUN_DIR/logs"
[[ -n "$L1_DIR" ]] || L1_DIR="$RUN_DIR/l1-results"
if [[ "$DRY_RUN" == "1" && -n "$SHARED_EVIDENCE" ]]; then
  echo "shared-evidence import：debug/release artifacts、cargo_fakes_filtered、windows_l1:s22_gate:win_test 與兩個 Windows 能力 L1（$SHARED_EVIDENCE）；selector/provenance 不符即 EVIDENCE_MISSING，不重跑。artifact provenance 不符：EVIDENCE_MISSING，不啟動本地建置。"
fi
if [[ "$DRY_RUN" == "1" ]]; then
  if [[ -z "$SHARED_EVIDENCE" ]]; then
    echo "正式執行必須提供 --shared-evidence；非共用正式模式拒絕執行。"
  fi
  python3 - "$ROOT/tests/acceptance/l1_required.lock.json" <<'PY'
import json
import shlex
import sys
from pathlib import Path

lock = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for section in ("parity_py", "py_parity"):
    selector = lock["sets"][f"s22_gate:{section}"]["linux"]["selector"]["argv"]
    print("執行：python3 -m pytest " + shlex.join(selector))
PY
  echo "執行：以 s22_gate_check.py --plan 推導並執行 console_conformance_pty；blackbox_rust 逐字使用鎖定 selector，僅附加 --junitxml，並以共用 release CLI／daemon 執行。"
  echo "執行：python3 func-test/runner.py <s22_gate_check.py 逐案列出的 L1.5 selector>（Python/Rust 各一列；需符合 daemon identity）。"
  echo "匯入：共用 manifest 的 windows_l1:s22_gate:win_test 與兩組 Windows 能力 L1；shared 模式不讀 --windows-dir、不啟動 Windows runner。"
  echo "執行：python3 scripts/rust/s22_gate_check.py --l1-results <DIR> --map tests/acceptance/slice22_evidence_map.yaml --lock tests/acceptance/l1_required.lock.json --l15-results <FILE> --dry-run。"
fi
SHARED_ARTIFACTS_VALID=0
SHARED_RELEASE_BUNDLE_VALID=0
SHARED_ARTIFACT_DIR="$RUN_DIR/shared-artifacts"
SHARED_ARTIFACT_ARGS=()
if [[ "$DRY_RUN" != "1" && -n "$SHARED_EVIDENCE" ]]; then
  if python3 scripts/rust/gate_shared_evidence.py import-artifacts --shared-evidence "$SHARED_EVIDENCE" \
      --out-dir "$SHARED_ARTIFACT_DIR" >"$RUN_DIR/logs/shared-artifacts.json" \
      2>"$RUN_DIR/logs/shared-artifacts.stderr"; then
    SHARED_ARTIFACTS_VALID=1
    SHARED_ARTIFACT_ARGS=(--artifact "debug_cli=$SHARED_ARTIFACT_DIR/debug/serialwrap" \
      --artifact "debug_daemon=$SHARED_ARTIFACT_DIR/debug/serialwrapd" \
      --artifact "release_cli=$SHARED_ARTIFACT_DIR/release/serialwrap" \
      --artifact "release_daemon=$SHARED_ARTIFACT_DIR/release/serialwrapd")
    if python3 - "$ROOT" "$SHARED_EVIDENCE" "$SHARED_ARTIFACT_DIR" \
        >"$RUN_DIR/logs/shared-release-bundle.json" 2>"$RUN_DIR/logs/shared-release-bundle.stderr" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
from scripts.rust.gate_shared_evidence import check_candidate_bundle

shared = Path(sys.argv[2]).resolve()
imported = Path(sys.argv[3]).resolve()
checked = check_candidate_bundle(shared)
if checked.get("status") != "PASS":
    print(json.dumps(checked, ensure_ascii=False, sort_keys=True))
    raise SystemExit(3)
manifest = json.loads((shared / "manifest.json").read_text(encoding="utf-8"))
artifacts = manifest["provenance"]["artifacts"]
identity = checked["identity"]
for name, identity_key, relative in (
    ("release_cli", "cli_sha256", "release/serialwrap"),
    ("release_daemon", "daemon_sha256", "release/serialwrapd"),
):
    expected = identity[identity_key]
    recorded = artifacts[name]["sha256"]
    actual = hashlib.sha256((imported / relative).read_bytes()).hexdigest()
    if recorded != expected or actual != expected:
        print(f"{name} 摘要與同 HEAD Stage 2 bundle 不符", file=sys.stderr)
        raise SystemExit(3)
print(json.dumps({"status": "PASS", "bundle_path": checked["path"], "identity": identity},
                  ensure_ascii=False, sort_keys=True))
PY
    then
      SHARED_RELEASE_BUNDLE_VALID=1
      CLI_PATH="$SHARED_ARTIFACT_DIR/release/serialwrap"
      DAEMON_PATH="$SHARED_ARTIFACT_DIR/release/serialwrapd"
      BUILD_EXIT=0
      BUILT_DAEMON_SHA256="$(sha256sum "$DAEMON_PATH" | cut -d' ' -f1)"
      export SERIALWRAP_L15_RUST_DAEMON="$DAEMON_PATH"
    else
      mark_shared_rejected s22_gate:console_conformance_pty linux
      mark_shared_rejected s22_gate:blackbox_rust linux
    fi
  else
    mark_shared_rejected s22_gate:cargo_workspace linux
    mark_shared_rejected s22_gate:console_conformance_pty linux
    mark_shared_rejected s22_gate:blackbox_rust linux
    mark_shared_rejected s22_gate:win_test windows
    mark_shared_rejected s22_gate:windows_cap_windows_com windows
    mark_shared_rejected s22_gate:windows_cap_windows_com_daemon windows
  fi
fi
if [[ "$DRY_RUN" != "1" && -n "$SHARED_EVIDENCE" ]]; then
  IMPORT_DIR="$RUN_DIR/shared-import"
  IMPORT_JSON="$RUN_DIR/logs/shared-cargo-import.json"
  mkdir -p "$L1_DIR/cargo_workspace/linux"
  if [[ "$SHARED_ARTIFACTS_VALID" == "1" ]] && python3 scripts/rust/gate_shared_evidence.py import-section --shared-evidence "$SHARED_EVIDENCE" \
      --gate s22 --section cargo_fakes_filtered --required-set s22_gate:cargo_workspace --platform linux \
      "${SHARED_ARTIFACT_ARGS[@]}" --out-dir "$IMPORT_DIR" \
      >"$IMPORT_JSON" 2>"$RUN_DIR/logs/shared-cargo-import.stderr"; then
    cp "$IMPORT_DIR/s22_gate/cargo_workspace/linux/resource-report.json" \
      "$L1_DIR/cargo_workspace/linux/resource-report.json"
    cp "$IMPORT_DIR/logs/cargo-fakes-filtered.log" "$RUN_DIR/logs/shared-cargo-workspace.log"
  else
    mark_shared_rejected s22_gate:cargo_workspace linux
    echo "EVIDENCE_MISSING：共用 cargo 證據拒收；本段不重跑。" >"$RUN_DIR/logs/shared-cargo-workspace.log"
    if [[ -f "$RUN_DIR/logs/shared-cargo-import.stderr" ]]; then
      cat "$RUN_DIR/logs/shared-cargo-import.stderr" >>"$RUN_DIR/logs/shared-cargo-workspace.log" || true
    fi
  fi
fi

if [[ "$DRY_RUN" != "1" && -n "$SHARED_EVIDENCE" && "$SHARED_RELEASE_BUNDLE_VALID" == "1" ]]; then
  for section in win_test windows_cap_windows_com windows_cap_windows_com_daemon; do
    set_id="s22_gate:$section"
    import_dir="$RUN_DIR/shared-import-$section"
    result="$RUN_DIR/logs/shared-$section-import.json"
    if python3 scripts/rust/gate_shared_evidence.py import-section \
        --shared-evidence "$SHARED_EVIDENCE" --gate s22 --section "windows_l1:$set_id" \
        --required-set "$set_id" --platform windows "${SHARED_ARTIFACT_ARGS[@]}" \
        --out-dir "$import_dir" >"$result" 2>"$RUN_DIR/logs/shared-$section-import.stderr"; then
      report="$L1_DIR/$section/windows/resource-report.json"
      mkdir -p "$(dirname "$report")"
      cp "$import_dir/l1-results.json" "$report"
      printf '已匯入 shared-evidence windows_l1:%s；未啟動 Windows runner.\n' "$set_id" \
        >"$RUN_DIR/logs/import-$section.log"
    else
      mark_shared_rejected "$set_id" windows
      printf 'EVIDENCE_MISSING：共用 Windows L1 拒收；本段不重跑。\n' >"$RUN_DIR/logs/import-$section.log"
      cat "$RUN_DIR/logs/shared-$section-import.stderr" >>"$RUN_DIR/logs/import-$section.log" || true
    fi
  done
elif [[ "$DRY_RUN" != "1" && -n "$SHARED_EVIDENCE" ]]; then
  mark_shared_rejected s22_gate:blackbox_rust linux
  mark_shared_rejected s22_gate:win_test windows
  mark_shared_rejected s22_gate:windows_cap_windows_com windows
  mark_shared_rejected s22_gate:windows_cap_windows_com_daemon windows
fi

set +e
SHARED_EVIDENCE_FLAG=0
[[ -n "$SHARED_EVIDENCE" ]] && SHARED_EVIDENCE_FLAG=1
REJECTED_SHARED_EVIDENCE_ARG="$(IFS=,; printf '%s' "${SHARED_REJECTED_EVIDENCE[*]}")"
python3 - "$ROOT" "$RUN_DIR" "$L1_DIR" "$WINDOWS_DIR" "$SELFTEST_REPORT" "$DAEMON_PATH" \
  "$BUILD_EXIT" "$BUILT_DAEMON_SHA256" "$DRY_RUN" "$SHARED_EVIDENCE_FLAG" \
  "$REJECTED_SHARED_EVIDENCE_ARG" "$SHARED_EVIDENCE" "$CLI_PATH" "$SHARED_RELEASE_BUNDLE_VALID" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import yaml

root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "func-test"))
import runner as func_test_runner
from scripts.rust.l15_artifact import l15_artifact_problem

run_dir = pathlib.Path(sys.argv[2]).resolve()
l1_dir = pathlib.Path(sys.argv[3]).resolve()
windows_dir = pathlib.Path(sys.argv[4]).resolve() if sys.argv[4] else None
selftest_report = pathlib.Path(sys.argv[5]).resolve() if sys.argv[5] else None
daemon_path = pathlib.Path(sys.argv[6]).resolve() if sys.argv[6] else None
build_exit = int(sys.argv[7]) if sys.argv[7] else None
built_sha = sys.argv[8] or None
dry_run = sys.argv[9] == "1"
shared_evidence = sys.argv[10] == "1"
rejected_shared_evidence = [value for value in sys.argv[11].split(",") if value]
shared_evidence_path = pathlib.Path(sys.argv[12]).resolve() if sys.argv[12] else None
cli_path = pathlib.Path(sys.argv[13]).resolve() if sys.argv[13] else None
shared_release_bundle_valid = sys.argv[14] == "1"
if cli_path is not None and not cli_path.is_relative_to(root):
    print(f"EVIDENCE_MISSING: 候選 CLI 必須位於此 worktree：{cli_path}", file=sys.stderr)
    cli_path = None
if daemon_path is not None and not daemon_path.is_relative_to(root):
    print(f"EVIDENCE_MISSING: 候選 daemon 必須位於此 worktree：{daemon_path}", file=sys.stderr)
    daemon_path = None
lock_path = root / "tests/acceptance/l1_required.lock.json"
map_path = root / "tests/acceptance/slice22_evidence_map.yaml"
lock = json.loads(lock_path.read_text(encoding="utf-8"))
emap = yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}
logs_dir = run_dir / "logs"
l1_dir.mkdir(parents=True, exist_ok=True)

def run_logged(label: str, argv: list[str], env: dict[str, str], *, preamble: list[str] | None = None,
               timeout: float | None = None) -> int:
    log = logs_dir / f"{label}.log"
    with log.open("w", encoding="utf-8") as stream:
        stream.write("+ " + shlex.join(argv) + "\n")
        for line in preamble or []:
            stream.write(line + "\n")
        stream.flush()
        try:
            proc = subprocess.run(argv, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                  timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            stream.write(f"逾時（{timeout} 秒）：{exc}\n")
            return 124
        except OSError as exc:
            stream.write(f"{type(exc).__name__}: {exc}\n")
            return 127
    return proc.returncode if proc.returncode >= 0 else 128 - proc.returncode

selftest_source = "explicit" if selftest_report is not None else "missing"
selftest_eligibility = "INSUFFICIENT_EVIDENCE"
selftest_ready = False
if not dry_run:
    if selftest_report is None and shared_evidence_path is not None:
        shared_report = shared_evidence_path / "work/p7-ab/bridge-selftest/selftest-report.json"
        if shared_report.is_file():
            eligibility, eligible = func_test_runner.evaluate_bridge_eligibility(shared_report)
            if eligible:
                selftest_report = shared_report.resolve()
                selftest_source = "shared"
                selftest_eligibility, selftest_ready = eligibility, True
    if selftest_report is None:
        # 依 run_p7_ab.sh：`python3 -m uart_stdio_bridge selftest --endpoint posix-pty`。
        selftest_source = "generated"
        selftest_dir = run_dir / "bridge-selftest"
        selftest_dir.mkdir(parents=True, exist_ok=True)
        selftest_env = os.environ.copy()
        bridge_src = str(root / "tools/uart_stdio_bridge/src")
        selftest_env["PYTHONPATH"] = bridge_src + (os.pathsep + selftest_env["PYTHONPATH"] if selftest_env.get("PYTHONPATH") else "")
        selftest_rc = run_logged("bridge-selftest", [
            sys.executable, "-m", "uart_stdio_bridge", "selftest", "--endpoint", "posix-pty",
            "--out", str(selftest_dir),
        ], selftest_env)
        generated_report = selftest_dir / "selftest-report.json"
        if generated_report.is_file():
            selftest_report = generated_report.resolve()
        (logs_dir / "bridge-selftest.exit").write_text(f"{selftest_rc}\n", encoding="utf-8")
    if selftest_report is not None:
        selftest_eligibility, selftest_ready = func_test_runner.evaluate_bridge_eligibility(selftest_report)

def import_windows_reports() -> None:
    # 共用模式只接受下方從 manifest 匯入的 windows_l1；不讀另一個來源的 --windows-dir。
    if shared_evidence or windows_dir is None:
        return
    sections = ("win_test", "windows_cap_windows_com", "windows_cap_windows_com_daemon")
    for section in sections:
        source = windows_dir / section / "windows" / "resource-report.json"
        if not source.is_file():
            (logs_dir / f"import-{section}.log").write_text(
                f"EVIDENCE_MISSING: 未提供明確路徑 {source}\n", encoding="utf-8")
            continue
        destination = l1_dir / section / "windows" / "resource-report.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
        (logs_dir / f"import-{section}.log").write_text(
            f"已匯入明確指定的 Windows L1：{source}\n", encoding="utf-8")

def run_python_parity(section: str) -> None:
    set_id = f"s22_gate:{section}"
    spec = lock["sets"][set_id]["linux"]
    selector = spec["selector"]
    report = l1_dir / section / "linux" / "resource-report.json"
    junit = l1_dir / section / "linux" / "junit.xml"
    report.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["SW_RESOURCE_REPORT"] = str(report)
    argv = [sys.executable, "-m", "pytest", *selector["argv"], f"--junitxml={junit}"]
    rc = run_logged(section, argv, env)
    (logs_dir / f"{section}.exit").write_text(f"{rc}\n", encoding="utf-8")

def run_console_conformance_pty() -> None:
    """以 gate checker --plan 的 argv 執行鎖定的 posix_pty 集合。"""
    report = l1_dir / "console_conformance_pty" / "linux" / "resource-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    plan_argv = [sys.executable, "scripts/rust/s22_gate_check.py", "--plan", "--l1-results", str(l1_dir)]
    plan_log = logs_dir / "console_conformance_pty-plan.log"
    with plan_log.open("w", encoding="utf-8") as stream:
        stream.write("+ " + shlex.join(plan_argv) + "\n")
        try:
            plan_proc = subprocess.run(plan_argv, cwd=root, capture_output=True, text=True,
                                       timeout=60, check=False)
            stream.write(plan_proc.stdout or "")
            if plan_proc.stderr:
                stream.write(plan_proc.stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            stream.write(f"{type(exc).__name__}: {exc}\n")
            plan_proc = None
    cargo_argv = None
    if plan_proc is not None and plan_proc.returncode == 0:
        try:
            plan = json.loads(plan_proc.stdout)
            row = next(item for item in plan if item.get("set_id") == "s22_gate:console_conformance_pty"
                       and item.get("platform") == "linux")
            planned = row.get("collect_argv")
            if isinstance(planned, list) and all(isinstance(arg, str) for arg in planned):
                cargo_argv = planned
        except (ValueError, StopIteration, TypeError):
            cargo_argv = None
    if cargo_argv is None:
        (logs_dir / "console_conformance_pty.exit").write_text("127\n", encoding="utf-8")
        return
    rc = run_logged("console_conformance_pty", cargo_argv, os.environ.copy())
    (logs_dir / "console_conformance_pty.exit").write_text(f"{rc}\n", encoding="utf-8")

def run_blackbox_rust() -> None:
    """以鎖定 selector、共用 Stage 2 release CLI／daemon 執行 Slice 2.1＋2.2 黑箱。"""
    if not shared_evidence or not shared_release_bundle_valid or cli_path is None or daemon_path is None:
        return
    set_id = "s22_gate:blackbox_rust"
    selector = lock["sets"][set_id]["linux"]["selector"]
    report = l1_dir / "blackbox_rust" / "linux" / "resource-report.json"
    junit = l1_dir / "blackbox_rust" / "linux" / "junit.xml"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.unlink(missing_ok=True)
    env = os.environ.copy()
    env["SERIALWRAP_AB_LABEL"] = "rust"
    env["SERIALWRAP_CLI_EXE"] = str(cli_path)
    env["SERIALWRAP_DAEMON_EXE"] = str(daemon_path)
    env["SW_RESOURCE_REPORT"] = str(report)
    argv = [sys.executable, "-m", "pytest", *selector["argv"], f"--junitxml={junit}"]
    cli_sha = hashlib.sha256(cli_path.read_bytes()).hexdigest()
    daemon_sha = hashlib.sha256(daemon_path.read_bytes()).hexdigest()
    rc = run_logged("blackbox_rust", argv, env, preamble=[
        f"SERIALWRAP_CLI_EXE={cli_path} CLI SHA256={cli_sha}",
        f"SERIALWRAP_DAEMON_EXE={daemon_path} daemon SHA256={daemon_sha}",
    ])
    same_artifacts = (cli_path.is_file() and hashlib.sha256(cli_path.read_bytes()).hexdigest() == cli_sha
                      and daemon_path.is_file() and hashlib.sha256(daemon_path.read_bytes()).hexdigest() == daemon_sha)
    if not same_artifacts and rc == 0:
        rc = 1
        with (logs_dir / "blackbox_rust.log").open("a", encoding="utf-8") as stream:
            stream.write("EVIDENCE_MISSING: 測試期間共用 CLI／daemon artifact SHA256 改變。\n")
    (logs_dir / "blackbox_rust.exit").write_text(f"{rc}\n", encoding="utf-8")

def l15_rows() -> list[dict]:
    refs = sorted({
        ref
        for section in ("cases", "parity_rows")
        for entry in (emap.get(section) or {}).values()
        for ref in (entry or {}).get("tests") or []
        if isinstance(ref, str) and ref.startswith("func-test/runner.py ")
    })
    rows: list[dict] = []
    for index, ref in enumerate(refs, start=1):
        tokens = shlex.split(ref)
        bridge = "--mode" in tokens and tokens[tokens.index("--mode") + 1] == "bridge"
        argv = [sys.executable, "func-test/runner.py", *tokens[1:]]
        if bridge and selftest_report is not None:
            argv.extend(["--selftest-report", str(selftest_report)])
        daemon_specs = [
            ("python", "SERIALWRAP_L15_PY_DAEMON",
             os.environ.get("SERIALWRAP_L15_PY_DAEMON") or
             str(root / "tests/contract_blackbox/bin/py-serialwrapd")),
            ("rust", "SERIALWRAP_L15_RUST_DAEMON",
             os.environ.get("SERIALWRAP_L15_RUST_DAEMON") or None),
        ]
        configured_paths = {
            implementation: pathlib.Path(raw_path).resolve() if raw_path else None
            for implementation, _env_name, raw_path in daemon_specs
        }
        same_daemon_path = (
            configured_paths["python"] is not None
            and configured_paths["rust"] is not None
            and configured_paths["python"] == configured_paths["rust"]
        )
        for implementation, env_name, raw_path in daemon_specs:
            path = configured_paths[implementation]
            if path is None or not path.is_file():
                rows.append({"ref": ref, "implementation": implementation, "outcome": "unchecked",
                             "exit_code": None,
                             "reason": f"{env_name} 的 daemon 不存在，未執行此 L1.5 列"})
                continue
            if not path.is_relative_to(root):
                rows.append({"ref": ref, "implementation": implementation, "outcome": "failed", "exit_code": 127,
                             "reason": f"{env_name} 必須指向 worktree 內的 daemon artifact"})
                continue
            if same_daemon_path:
                rows.append({"ref": ref, "implementation": implementation, "outcome": "failed", "exit_code": 127,
                             "daemon_path": str(path.relative_to(root)),
                             "daemon_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "reason": "Python 與 Rust L1.5 必須使用身分獨立的 daemon artifact"})
                continue
            identity_problem = l15_artifact_problem(
                implementation,
                path,
                candidate_path=daemon_path if implementation == "rust" else None,
                build_linux_exit=build_exit if implementation == "rust" else None,
                candidate_sha256=built_sha if implementation == "rust" else None,
            )
            if identity_problem:
                rows.append({"ref": ref, "implementation": implementation, "outcome": "unchecked", "exit_code": None,
                             "daemon_path": str(path.relative_to(root)),
                             "daemon_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "reason": identity_problem})
                continue
            if implementation == "rust" and (cli_path is None or not cli_path.is_file()
                                               or not cli_path.is_relative_to(root)):
                rows.append({"ref": ref, "implementation": implementation, "outcome": "failed", "exit_code": 127,
                             "daemon_path": str(path.relative_to(root)),
                             "daemon_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "reason": "Rust L1.5 必須使用 worktree 內共用 Stage 2 release CLI"})
                continue
            if bridge and not selftest_ready:
                # 未提供報告仍是證據不足；已提供但資格不合格維持既有失敗效果，不得由預檢降級成 unchecked。
                report_exists = selftest_report is not None and selftest_report.is_file()
                rows.append({"ref": ref, "implementation": implementation,
                             "outcome": "failed" if report_exists else "unchecked",
                             "exit_code": 1 if report_exists else None,
                             "daemon_path": str(path.relative_to(root)), "daemon_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "reason": f"bridge selftest 資格未通過（{selftest_eligibility}）：{selftest_report or '<missing>'}"})
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            rust_cli_sha = hashlib.sha256(cli_path.read_bytes()).hexdigest() if implementation == "rust" else None
            env = os.environ.copy()
            env.pop("SERIALWRAP_CLI_EXE", None)
            env["SERIALWRAP_GATE"] = "1"
            env["SERIALWRAP_DAEMON_EXE"] = str(path)
            if implementation == "rust":
                env["SERIALWRAP_CLI_EXE"] = str(cli_path)
            label = f"l15-{index:02d}-{implementation}"
            report_dir = run_dir / "l15" / label
            run_argv = [*argv, "--report-dir", str(report_dir)]
            rc = run_logged(label, run_argv, env, timeout=900)
            same_artifact = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest
            same_cli = (implementation != "rust" or
                        (cli_path.is_file() and hashlib.sha256(cli_path.read_bytes()).hexdigest() == rust_cli_sha))
            if not same_artifact or not same_cli:
                if rc == 0:
                    rc = 1
            row = {"ref": ref, "implementation": implementation,
                   "outcome": "passed" if rc == 0 and same_artifact and same_cli else "failed", "exit_code": rc,
                   "daemon_path": str(path.relative_to(root)), "daemon_sha256": digest,
                   "log": f"logs/{label}.log"}
            if implementation == "rust":
                row["cli_path"] = str(cli_path.relative_to(root))
                row["cli_sha256"] = rust_cli_sha
            rows.append(row)
    return rows

if not dry_run:
    import_windows_reports()
    run_python_parity("parity_py")
    run_python_parity("py_parity")
    if shared_release_bundle_valid:
        run_console_conformance_pty()
        run_blackbox_rust()
    runs = l15_rows()
else:
    runs = []

l15_path = run_dir / "l15-results.json"
l15_path.write_text(json.dumps({"format": "s22-l15-results.v1", "runs": runs,
                                "bridge_selftest_report": str(selftest_report) if selftest_report else None,
                                "bridge_selftest_eligibility": selftest_eligibility},
                              ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
report = run_dir / "s22-gate.json"
argv = [
    sys.executable, "scripts/rust/s22_gate_check.py",
    "--l1-results", str(l1_dir), "--map", str(map_path), "--lock", str(lock_path),
    "--l15-results", str(l15_path), "--out", str(report),
]
if daemon_path is not None:
    argv.extend(["--daemon-path", str(daemon_path)])
if cli_path is not None:
    argv.extend(["--cli-path", str(cli_path)])
if shared_evidence and shared_release_bundle_valid:
    argv.extend(["--shared-release-bundle", "--shared-release-info",
                 str(run_dir / "logs/shared-release-bundle.json")])
if build_exit is not None:
    argv.extend(["--build-linux-exit", str(build_exit)])
if built_sha:
    argv.extend(["--built-daemon-sha256", built_sha])
if selftest_report is not None:
    argv.extend(["--bridge-selftest-report", str(selftest_report)])
for evidence in rejected_shared_evidence:
    argv.extend(["--evidence-missing", evidence])
if dry_run:
    argv.append("--dry-run")
checker_rc = run_logged("s22-gate-check", argv, os.environ.copy())
print(f"s22_gate.sh: report={report} checker_exit={checker_rc}")
raise SystemExit(0 if dry_run else checker_rc)
PY
RC=$?
set -e
exit "$RC"
