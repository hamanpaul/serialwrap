#!/usr/bin/env bash
# Slice 2.2 出口證據編排：補跑鎖定的 Python parity、產生 L1.5 執行清單、匯入明確指定的 Windows 證據並判定。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-s22"
OUT_ROOT="$ROOT/target/s22-gate"
L1_DIR=""
WINDOWS_DIR="${SW_GATE_S22_WINDOWS_DIR:-}"
SELFTEST_REPORT="${SERIALWRAP_L15_SELFTEST_REPORT:-}"
DAEMON_PATH="${SERIALWRAP_L15_RUST_DAEMON:-}"
BUILD_EXIT=""
BUILT_DAEMON_SHA256=""
DRY_RUN=0

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
    -h|--help)
      printf '%s\n' '用法：scripts/rust/s22_gate.sh [--dry-run] [--run-id ID] [--out DIR] [--l1-results-dir DIR] [--windows-dir DIR] [--selftest-report FILE] [--daemon-path FILE] [--build-linux-exit N] [--built-daemon-sha256 SHA256]'
      exit 0 ;;
    *) echo "s22_gate.sh: 未知參數 $1" >&2; exit 2 ;;
  esac
done

[[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "s22_gate.sh: --run-id 只允許英數與 ._-" >&2; exit 2; }
RUN_DIR="$OUT_ROOT/$RUN_ID"
REPORT="$RUN_DIR/s22-gate.json"
[[ ! -e "$REPORT" ]] || { echo "s22_gate.sh: $REPORT 已存在，拒絕覆寫" >&2; exit 2; }
mkdir -p "$RUN_DIR/logs"
[[ -n "$L1_DIR" ]] || L1_DIR="$RUN_DIR/l1-results"

set +e
python3 - "$ROOT" "$RUN_DIR" "$L1_DIR" "$WINDOWS_DIR" "$SELFTEST_REPORT" "$DAEMON_PATH" \
  "$BUILD_EXIT" "$BUILT_DAEMON_SHA256" "$DRY_RUN" <<'PY'
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
run_dir = pathlib.Path(sys.argv[2]).resolve()
l1_dir = pathlib.Path(sys.argv[3]).resolve()
windows_dir = pathlib.Path(sys.argv[4]).resolve() if sys.argv[4] else None
selftest_report = pathlib.Path(sys.argv[5]).resolve() if sys.argv[5] else None
daemon_path = pathlib.Path(sys.argv[6]).resolve() if sys.argv[6] else None
build_exit = int(sys.argv[7]) if sys.argv[7] else None
built_sha = sys.argv[8] or None
dry_run = sys.argv[9] == "1"
if daemon_path is not None and not daemon_path.is_relative_to(root):
    print(f"EVIDENCE_MISSING: 候選 daemon 必須位於此 worktree：{daemon_path}", file=sys.stderr)
    daemon_path = None
lock_path = root / "tests/acceptance/l1_required.lock.json"
map_path = root / "tests/acceptance/slice22_evidence_map.yaml"
lock = json.loads(lock_path.read_text(encoding="utf-8"))
emap = yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}
logs_dir = run_dir / "logs"
l1_dir.mkdir(parents=True, exist_ok=True)

def run_logged(label: str, argv: list[str], env: dict[str, str]) -> int:
    log = logs_dir / f"{label}.log"
    with log.open("w", encoding="utf-8") as stream:
        stream.write("+ " + shlex.join(argv) + "\n")
        stream.flush()
        try:
            proc = subprocess.run(argv, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT, check=False)
        except OSError as exc:
            stream.write(f"{type(exc).__name__}: {exc}\n")
            return 127
    return proc.returncode if proc.returncode >= 0 else 128 - proc.returncode

def import_windows_reports() -> None:
    if windows_dir is None:
        return
    for section in ("win_test", "windows_cap_windows_com", "windows_cap_windows_com_daemon"):
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

def l15_rows() -> list[dict]:
    refs = sorted({
        ref
        for entry in (emap.get("cases") or {}).values()
        for ref in (entry or {}).get("tests") or []
        if isinstance(ref, str) and ref.startswith("func-test/runner.py ")
    })
    rows: list[dict] = []
    selftest_ready = selftest_report is not None and selftest_report.is_file()
    daemon_paths = [pathlib.Path(os.environ[name]).resolve() for name in
                    ("SERIALWRAP_L15_PY_DAEMON", "SERIALWRAP_L15_RUST_DAEMON") if os.environ.get(name)]
    pinned_daemons = len(daemon_paths) == 2 and all(
        path.is_file() and path.is_relative_to(root) for path in daemon_paths
    )
    for index, ref in enumerate(refs, start=1):
        tokens = shlex.split(ref)
        bridge = "--mode" in tokens and tokens[tokens.index("--mode") + 1] == "bridge"
        if not pinned_daemons:
            rows.append({"ref": ref, "outcome": "unchecked", "exit_code": None,
                         "reason": "缺少 worktree 內明確指定的 SERIALWRAP_L15_PY_DAEMON／SERIALWRAP_L15_RUST_DAEMON"})
            continue
        if bridge and not selftest_ready:
            rows.append({"ref": ref, "outcome": "unchecked", "exit_code": None,
                         "reason": "缺少符合資格的 SERIALWRAP_L15_SELFTEST_REPORT"})
            continue
        argv = [sys.executable, "func-test/runner.py", *tokens[1:]]
        if bridge and selftest_report is not None:
            argv.extend(["--selftest-report", str(selftest_report)])
        env = os.environ.copy()
        env["SERIALWRAP_GATE"] = "1"
        rc = run_logged(f"l15-{index:02d}", argv, env)
        rows.append({"ref": ref, "outcome": "passed" if rc == 0 else "failed", "exit_code": rc,
                     "log": f"logs/l15-{index:02d}.log"})
    return rows

if not dry_run:
    import_windows_reports()
    run_python_parity("parity_py")
    run_python_parity("py_parity")
    runs = l15_rows()
else:
    runs = []

l15_path = run_dir / "l15-results.json"
l15_path.write_text(json.dumps({"format": "s22-l15-results.v1", "runs": runs},
                              ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
report = run_dir / "s22-gate.json"
argv = [
    sys.executable, "scripts/rust/s22_gate_check.py",
    "--l1-results", str(l1_dir), "--map", str(map_path), "--lock", str(lock_path),
    "--l15-results", str(l15_path), "--out", str(report),
]
if daemon_path is not None:
    argv.extend(["--daemon-path", str(daemon_path)])
if build_exit is not None:
    argv.extend(["--build-linux-exit", str(build_exit)])
if built_sha:
    argv.extend(["--built-daemon-sha256", built_sha])
if dry_run:
    argv.append("--dry-run")
checker_rc = run_logged("s22-gate-check", argv, os.environ.copy())
print(f"s22_gate.sh: report={report} checker_exit={checker_rc}")
raise SystemExit(0 if dry_run else checker_rc)
PY
RC=$?
set -e
exit "$RC"
