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
DAEMON_PATH="${SERIALWRAP_L15_RUST_DAEMON:-}"
BUILD_EXIT=""
BUILT_DAEMON_SHA256=""
DRY_RUN=0
SHARED_EVIDENCE=""

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
      printf '%s\n' '用法：scripts/rust/s22_gate.sh [--dry-run] [--shared-evidence DIR] [--run-id ID] [--out DIR] [--l1-results-dir DIR] [--windows-dir DIR] [--selftest-report FILE] [--daemon-path FILE] [--build-linux-exit N] [--built-daemon-sha256 SHA256]'
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
if [[ "$DRY_RUN" == "1" && -n "$SHARED_EVIDENCE" ]]; then
  echo "shared-evidence import：debug/release artifacts、cargo_fakes_filtered、可用的 s22 Windows 原生 L1（$SHARED_EVIDENCE）；selector/provenance 不符即 EVIDENCE_MISSING，不重跑。artifact provenance 不符：EVIDENCE_MISSING，不啟動本地建置。"
fi
if [[ "$DRY_RUN" == "1" ]]; then
  if [[ -z "$SHARED_EVIDENCE" ]]; then
    echo "執行：python3 scripts/rust/cargo_l1_results.py --platform linux -- --workspace --features test-fakes -- --skip cap_。"
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
  echo "執行：python3 func-test/runner.py <s22_gate_check.py 逐案列出的 L1.5 selector>（Python/Rust 各一列；需符合 daemon identity）。"
  echo "匯入：Windows L1 與 --windows-dir 的 owner 證據；shared 模式不啟動 Windows runner。"
  echo "執行：python3 scripts/rust/s22_gate_check.py --l1-results <DIR> --map tests/acceptance/slice22_evidence_map.yaml --lock tests/acceptance/l1_required.lock.json --l15-results <FILE> --dry-run。"
  exit 0
fi
SHARED_ARTIFACTS_VALID=0
SHARED_ARTIFACT_DIR="$RUN_DIR/shared-artifacts"
SHARED_ARTIFACT_ARGS=()
if [[ "$DRY_RUN" != "1" && -n "$SHARED_EVIDENCE" ]]; then
  if python3 scripts/rust/gate_shared_evidence.py import-artifacts --shared-evidence "$SHARED_EVIDENCE" \
      --out-dir "$SHARED_ARTIFACT_DIR" >"$RUN_DIR/logs/shared-artifacts.json" \
      2>"$RUN_DIR/logs/shared-artifacts.stderr"; then
    SHARED_ARTIFACTS_VALID=1
    [[ -n "$DAEMON_PATH" ]] || DAEMON_PATH="$SHARED_ARTIFACT_DIR/release_daemon"
    [[ -n "$BUILD_EXIT" ]] || BUILD_EXIT=0
    [[ -n "$BUILT_DAEMON_SHA256" ]] || BUILT_DAEMON_SHA256="$(sha256sum "$DAEMON_PATH" | cut -d' ' -f1)"
    export SERIALWRAP_L15_RUST_DAEMON="$DAEMON_PATH"
    SHARED_ARTIFACT_ARGS=(--artifact "debug_cli=$SHARED_ARTIFACT_DIR/debug_cli" \
      --artifact "debug_daemon=$SHARED_ARTIFACT_DIR/debug_daemon" \
      --artifact "release_cli=$SHARED_ARTIFACT_DIR/release_cli" \
      --artifact "release_daemon=$DAEMON_PATH")
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
    echo "EVIDENCE_MISSING：共用 cargo 證據拒收；本段不重跑。" >"$RUN_DIR/logs/shared-cargo-workspace.log"
    cat "$RUN_DIR/logs/shared-cargo-import.stderr" >>"$RUN_DIR/logs/shared-cargo-workspace.log" || true
  fi
fi

if [[ "$DRY_RUN" != "1" && -n "$SHARED_EVIDENCE" && "$SHARED_ARTIFACTS_VALID" == "1" ]]; then
  for section in windows_cap_windows_com windows_cap_windows_com_daemon; do
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
      printf 'EVIDENCE_MISSING：共用 Windows L1 拒收；本段不重跑。\n' >"$RUN_DIR/logs/import-$section.log"
      cat "$RUN_DIR/logs/shared-$section-import.stderr" >>"$RUN_DIR/logs/import-$section.log" || true
    fi
  done
fi

set +e
SHARED_EVIDENCE_FLAG=0
[[ -n "$SHARED_EVIDENCE" ]] && SHARED_EVIDENCE_FLAG=1
python3 - "$ROOT" "$RUN_DIR" "$L1_DIR" "$WINDOWS_DIR" "$SELFTEST_REPORT" "$DAEMON_PATH" \
  "$BUILD_EXIT" "$BUILT_DAEMON_SHA256" "$DRY_RUN" "$SHARED_EVIDENCE_FLAG" <<'PY'
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
    sections = ("win_test",) if shared_evidence else (
        "win_test", "windows_cap_windows_com", "windows_cap_windows_com_daemon")
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

def l15_rows() -> list[dict]:
    refs = sorted({
        ref
        for entry in (emap.get("cases") or {}).values()
        for ref in (entry or {}).get("tests") or []
        if isinstance(ref, str) and ref.startswith("func-test/runner.py ")
    })
    rows: list[dict] = []
    selftest_ready = selftest_report is not None and selftest_report.is_file()
    for index, ref in enumerate(refs, start=1):
        tokens = shlex.split(ref)
        bridge = "--mode" in tokens and tokens[tokens.index("--mode") + 1] == "bridge"
        argv = [sys.executable, "func-test/runner.py", *tokens[1:]]
        if bridge and selftest_report is not None:
            argv.extend(["--selftest-report", str(selftest_report)])
        daemon_specs = [
            ("python", "SERIALWRAP_L15_PY_DAEMON"),
            ("rust", "SERIALWRAP_L15_RUST_DAEMON"),
        ]
        valid_paths = []
        for implementation, env_name in daemon_specs:
            raw_path = os.environ.get(env_name)
            path = pathlib.Path(raw_path).resolve() if raw_path else None
            valid = path is not None and path.is_file() and path.is_relative_to(root)
            valid_paths.append(path if valid else None)
        distinct_paths = all(path is not None for path in valid_paths) and valid_paths[0] != valid_paths[1]
        for implementation, env_name in daemon_specs:
            raw_path = os.environ.get(env_name)
            path = pathlib.Path(raw_path).resolve() if raw_path else None
            valid = path is not None and path.is_file() and path.is_relative_to(root) and distinct_paths
            if not valid:
                rows.append({"ref": ref, "implementation": implementation, "outcome": "failed", "exit_code": 127,
                             "reason": f"{env_name} 必須是 worktree 內存在且身分獨立的 daemon artifact"})
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
            if bridge and not selftest_ready:
                rows.append({"ref": ref, "implementation": implementation, "outcome": "unchecked", "exit_code": None,
                             "daemon_path": str(path.relative_to(root)), "daemon_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "reason": "缺少符合資格的 SERIALWRAP_L15_SELFTEST_REPORT"})
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            env = os.environ.copy()
            env["SERIALWRAP_GATE"] = "1"
            env["SERIALWRAP_DAEMON_EXE"] = str(path)
            label = f"l15-{index:02d}-{implementation}"
            rc = run_logged(label, argv, env)
            same_artifact = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest
            if not same_artifact and rc == 0:
                rc = 1
            rows.append({"ref": ref, "implementation": implementation,
                         "outcome": "passed" if rc == 0 and same_artifact else "failed", "exit_code": rc,
                         "daemon_path": str(path.relative_to(root)), "daemon_sha256": digest,
                         "log": f"logs/{label}.log"})
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
