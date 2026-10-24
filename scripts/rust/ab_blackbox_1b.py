#!/usr/bin/env python3
"""依 P4 凍結案例執行 Stage 1B Python／Rust CLI 黑箱 A/B。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
# 以腳本模式執行時 sys.path[0] 為 scripts/rust，須補上 repo 根才能 import scripts.rust.* 與 sw_core（同 capture_cli_help.py 慣例）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
LOCK = ROOT / "tests/acceptance/p4_required.lock.json"
CASE_MAP = ROOT / "tests/acceptance/p4_case_map.yaml"
COMPARE = ROOT / "scripts/rust/ab_compare.py"
APPLICABILITY = ROOT / "tests/contract_blackbox/data/applicability.json"
CONFIG_DIGEST = ROOT / "tests/contract_blackbox/config_digest.py"
PY_DAEMON = ROOT / ("serialwrapd.py" if os.name == "nt" else "tests/contract_blackbox/bin/py-serialwrapd")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        return _sha256_file(path)
    for item in sorted(path.rglob("*"), key=lambda p: p.relative_to(path).as_posix()):
        if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc":
            digest.update(item.relative_to(path).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(item.read_bytes())
    return digest.hexdigest()


def _frozen_cases() -> tuple[list[dict[str, Any]], list[str]]:
    data = json.loads(LOCK.read_text(encoding="utf-8"))
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("p4_required.lock.json 沒有凍結案例")
    ids: list[str] = []
    nodes: list[str] = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
            raise ValueError("p4_required.lock.json 含無效 case_id")
        ids.append(case["case_id"])
        tests = case.get("tests")
        if not isinstance(tests, list):
            raise ValueError(f"凍結案例 {case['case_id']} 缺少 tests 清單")
        nodes.extend(
            node for node in tests
            if isinstance(node, str) and node.startswith("tests/contract_blackbox/") and "::" in node
        )
    if len(ids) != len(set(ids)):
        raise ValueError("p4_required.lock.json 有重複 case_id")
    if len(nodes) != len(set(nodes)):
        raise ValueError("p4_required.lock.json 有重複 pytest nodeid")
    if not nodes:
        raise ValueError("凍結案例沒有可由 pytest 比較的黑箱 nodeid")
    # fail-closed：凍結節點不得包含 A/B 外層測試本身，子輪不會遞迴；此處以斷言擋住任何回歸。
    if any(node.startswith("tests/contract_blackbox/test_cli_mgmt_ab_suite.py") for node in nodes):
        raise ValueError("凍結節點含 test_cli_mgmt_ab_suite.py：A/B executor 子輪不得遞迴執行外層測試")
    return cases, sorted(nodes)


def _frozen_case_map(lock_cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """讀取 skip 登記前先核對 P4 map 的凍結摘要，避免未核准地擴張 allowlist。"""
    from scripts.rust.check_p4_cases import frozen_digest

    data = yaml.safe_load(CASE_MAP.read_text(encoding="utf-8")) or {}
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or any(not isinstance(case, dict) for case in cases):
        raise ValueError("P4 case map 缺少有效 cases 清單")
    current = {case.get("case_id"): case for case in cases}
    if len(current) != len(cases):
        raise ValueError("P4 case map 含重複 case_id")
    for frozen in lock_cases:
        case = current.get(frozen["case_id"])
        if case is None or frozen_digest(case) != frozen.get("sha256"):
            raise ValueError(f"P4 case map 凍結欄位與 lock 不符：{frozen['case_id']}")
    return cases


def _approved_python_skips(
    lock_cases: list[dict[str, Any]],
    case_map: list[dict[str, Any]],
    platform: str,
    selected_nodes: list[str],
) -> dict[str, str]:
    """登記的 Python 輪 skip 只核准給帶 `rust_only` marker 的節點。

    `skip_registered` 不得整案套用：同一登記案例內的共用節點（無 `rust_only`）必須在 A 輪實際執行，
    並與 B 輪一併由比較器判定。共用節點若被 skip 或 xfail，`_junit_required` 會判為失敗。
    """
    current = {case["case_id"]: case for case in case_map}
    selected = set(selected_nodes)
    approved: dict[str, str] = {}
    for frozen in lock_cases:
        case = current[frozen["case_id"]]
        registered = case.get("skip_registered") or {}
        if "python" not in registered and platform not in registered:
            continue
        for node in frozen.get("tests", []):
            if node not in selected or "rust_only" not in _markers(node):
                continue
            approved[node] = "rust-only contract"
    return approved


def _markers(nodeid: str) -> set[str]:
    path_text, *segments = nodeid.split("::")
    path = ROOT / path_text
    if not path.is_file() or not segments:
        return set()
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return set()
    scope = tree.body
    owners: list[ast.AST] = [tree]
    for segment in segments:
        name = segment.split("[", 1)[0]
        found = next(
            (item for item in scope
             if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
             and item.name == name),
            None,
        )
        if found is None:
            return set()
        owners.append(found)
        scope = getattr(found, "body", [])
    result: set[str] = set()
    for owner in owners:
        expressions: list[ast.AST] = list(getattr(owner, "decorator_list", []))
        for statement in getattr(owner, "body", []):
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                if any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
                    expressions.append(statement.value)
        for expression in expressions:
            for item in ast.walk(expression):
                if (isinstance(item, ast.Attribute) and isinstance(item.value, ast.Attribute)
                        and item.value.attr == "mark"):
                    result.add(item.attr)
    return result


def _applicable_nodes(nodes: list[str], platform: str) -> list[str]:
    """該平台要執行的凍結 pytest 節點。

    - windows：凍結 s1b_20groups selector（markexpr）收集到者，與 register／check 共用 `platform_applicability`
      規則；不收集者（例如依賴 /proc 與 sh 的 test_daemon_restart_ab）不在 Windows 執行。
    - linux：維持 HEAD 的負向標記過濾（僅排除 windows_native）。pidns_rig 標記不得把 test_daemon_restart_ab
      排出 Linux A/B，否則 Linux A/B 覆蓋縮減（Stage 1 收尾審查回報，見 CHG-S1X-P4-APPL-01）。
    """
    for node in nodes:
        if not (ROOT / node.split("::", 1)[0]).is_file():
            raise ValueError(f"凍結 pytest 來源不存在：{node}")
    if platform == "windows":
        from scripts.rust.check_p4_cases import platform_applicability

        applicability = platform_applicability(nodes, cwd=ROOT, platforms=("windows",))
        selected = [node for node in nodes if "windows" in applicability[node]]
    else:
        selected = [node for node in nodes if "windows_native" not in _markers(node)]
    if not selected:
        raise ValueError(f"平台 {platform} 沒有可執行的凍結 pytest 案例")
    return selected


def _run(argv: list[str], *, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=ROOT, env=env, capture_output=True, text=True,
        timeout=timeout, check=False,
    )


def _host_pid_ns() -> str | None:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def _pid_ns_of(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/ns/pid")
    except OSError:
        return None


def _namespace_members(namespace: str) -> list[int]:
    """host /proc 上屬於指定 pid namespace 的行程（只計可讀取者；殭屍行程已無 ns 連結，視為已結束）。"""
    members: list[int] = []
    for proc_dir in Path("/proc").iterdir():
        if proc_dir.name.isdigit() and _pid_ns_of(int(proc_dir.name)) == namespace:
            members.append(int(proc_dir.name))
    return sorted(members)


def _release_check(pidlog: Path, out: Path, timeout_s: float = 10.0) -> dict[str, Any]:
    """A 輪釋放檢查（POSIX）。

    pidns_rig 子行程（位於外層 unshare 建立的不同 pid namespace）寫入的 pid 為 namespace 內部編號，不得拿去查
    host 的 /proc/<pid>（回歸：host 恰有同號行程時 B 輪被誤判而未啟動）。因此列上 `pid_ns` 與 host 不同者，改以
    「host /proc 上無任何行程屬於該 namespace」判定釋放；仍有成員即 unreaped，並列出其 host pid。
    與 host 同 namespace 的列（含未附 pid_ns 的舊格式列）照原邏輯檢查 /proc/<pid>；外層 wrapper 的 host pid 屬此類。
    """
    if os.name == "nt":
        return _release_check_windows(pidlog, out, timeout_s)

    import fcntl

    host_ns = _host_pid_ns()
    parse_errors: list[str] = []
    pids: list[int] = []
    foreign_namespaces: set[str] = set()
    sockets: list[str] = []
    locks: list[str] = []
    workdirs: list[str] = []
    if not pidlog.is_file():
        parse_errors.append("A 輪沒有產生 PID 資源日誌")
    else:
        for number, line in enumerate(pidlog.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("不是 JSON 物件")
                row_ns = str(row["pid_ns"]) if row.get("pid_ns") else None
                if row_ns and host_ns and row_ns != host_ns:
                    foreign_namespaces.add(row_ns)
                else:
                    if row.get("pid"):
                        pids.append(int(row["pid"]))
                    pids.extend(int(pid) for pid in row.get("children", []) if pid)
                sockets.extend(str(value) for value in (row.get("socket"),) if value)
                locks.extend(str(value) for value in (row.get("lock"),) if value)
                workdirs.extend(str(row[key]) for key in ("run_dir", "state_dir") if row.get(key))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                parse_errors.append(f"PID 日誌第 {number} 行無效：{exc}")
    if not pids and not foreign_namespaces and not parse_errors:
        parse_errors.append("A 輪 PID 日誌沒有受測行程")

    deadline = time.monotonic() + timeout_s
    alive: list[int] = []
    for pid in sorted(set(pids)):
        while time.monotonic() < deadline:
            try:
                stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            except OSError:
                break
            tail = stat[stat.rfind(")") + 2 :].split()
            if tail and tail[0] in {"Z", "X", "x"}:
                break
            time.sleep(0.05)
        else:
            alive.append(pid)

    namespace_members: set[int] = set()
    for namespace in sorted(foreign_namespaces):
        members_deadline = time.monotonic() + timeout_s
        members = _namespace_members(namespace)
        while members and time.monotonic() < members_deadline:
            time.sleep(0.05)
            members = _namespace_members(namespace)
        namespace_members.update(members)
    alive.extend(sorted(namespace_members))

    remaining_sockets = [
        socket[7:] if socket.startswith("unix://") else socket
        for socket in set(sockets)
        if Path(socket[7:] if socket.startswith("unix://") else socket).exists()
    ]
    held_locks: list[str] = []
    for lock in sorted(set(locks)):
        if not Path(lock).exists():
            continue
        try:
            fd = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        except OSError:
            held_locks.append(lock)

    held_fds: list[str] = []
    normalized_dirs = [os.path.realpath(path) for path in set(workdirs) if Path(path).exists()]
    tracked_pids = set(pids) | namespace_members
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit() or int(proc_dir.name) == os.getpid():
            continue
        fd_dir = proc_dir / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.path.realpath(os.readlink(fd))
            except OSError:
                continue
            if any(target.startswith(path) for path in normalized_dirs):
                held_fds.append(f"pid {proc_dir.name} 持有 {target}")
                break
            if int(proc_dir.name) in tracked_pids and target.startswith("/dev/pts/"):
                held_fds.append(f"A 輪 pid {proc_dir.name} 仍持有 {target}")
                break
    result = {
        "ok": not (alive or remaining_sockets or held_locks or held_fds or parse_errors),
        "pids_reaped": not alive,
        "sockets_cleaned": not remaining_sockets,
        "locks_free": not held_locks,
        "fds_released": not held_fds,
        "parse_errors": parse_errors,
        "unreaped_pids": alive,
        "remaining_sockets": remaining_sockets,
        "held_locks": held_locks,
        "held_fds": held_fds,
        "pidns_foreign_namespaces": sorted(foreign_namespaces),
        "pidns_unreaped_pids": sorted(namespace_members),
    }
    out.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return result


def _release_check_windows(pidlog: Path, out: Path, timeout_s: float) -> dict[str, Any]:
    """Windows A/B 輪以行程結束、TCP/AF_UNIX endpoint 與 msvcrt byte lock 核對釋放。"""
    import ctypes
    import errno
    import msvcrt
    from ctypes import wintypes
    from urllib.parse import urlsplit

    parse_errors: list[str] = []
    pids: list[int] = []
    sockets: list[str] = []
    locks: list[str] = []
    if not pidlog.is_file():
        parse_errors.append("A 輪沒有產生 PID 資源日誌")
    else:
        for number, line in enumerate(pidlog.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("不是 JSON 物件")
                if row.get("pid"):
                    pids.append(int(row["pid"]))
                pids.extend(int(pid) for pid in row.get("children", []) if pid)
                sockets.extend(str(value) for value in (row.get("socket"),) if value)
                locks.extend(str(value) for value in (row.get("lock"),) if value)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                parse_errors.append(f"PID 日誌第 {number} 行無效：{exc}")
    if not pids and not parse_errors:
        parse_errors.append("A 輪 PID 日誌沒有受測行程")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    def process_state(pid: int) -> tuple[str, str | None]:
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            code = ctypes.get_last_error()
            if code == 87:  # ERROR_INVALID_PARAMETER: 行程不存在
                return "exited", None
            return "unknown", f"OpenProcess({pid}) winerror={code}"
        try:
            result = kernel32.WaitForSingleObject(handle, 0)
            if result == 0:  # WAIT_OBJECT_0
                return "exited", None
            if result == 258:  # WAIT_TIMEOUT
                return "alive", None
            return "unknown", f"WaitForSingleObject({pid}) result={result} winerror={ctypes.get_last_error()}"
        finally:
            kernel32.CloseHandle(handle)

    deadline = time.monotonic() + timeout_s
    pending = set(pids)
    alive: set[int] = set()
    process_errors: list[str] = []
    while pending and time.monotonic() < deadline:
        for pid in tuple(pending):
            state, error = process_state(pid)
            if state == "exited":
                pending.remove(pid)
                alive.discard(pid)
            elif state == "unknown":
                pending.remove(pid)
                process_errors.append(error or f"無法核對 A 輪行程 {pid}")
            else:
                alive.add(pid)
        if pending:
            time.sleep(0.05)
    alive.update(pending)
    parse_errors.extend(process_errors)

    remaining_sockets: list[str] = []
    for endpoint in sorted(set(sockets)):
        if endpoint.startswith("tcp://"):
            parsed = urlsplit(endpoint)
            if not parsed.hostname or parsed.port is None:
                parse_errors.append(f"無效 TCP endpoint：{endpoint}")
                continue
            try:
                with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
                    remaining_sockets.append(endpoint)
            except OSError as exc:
                winerror = getattr(exc, "winerror", None)
                if exc.errno == errno.ECONNREFUSED or winerror == 10061:
                    continue
                parse_errors.append(f"TCP endpoint 探測不確定 {endpoint}：{exc}")
            continue
        path_text = endpoint[7:] if endpoint.startswith("unix://") else endpoint
        if Path(path_text).exists():
            remaining_sockets.append(path_text)

    held_locks: list[str] = []
    for lock in sorted(set(locks)):
        if not Path(lock).exists():
            continue
        fd: int | None = None
        try:
            fd = os.open(lock, os.O_RDWR | os.O_BINARY)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            held_locks.append(lock)
        finally:
            if fd is not None:
                os.close(fd)

    result = {
        "ok": not (alive or remaining_sockets or held_locks or parse_errors),
        "pids_reaped": not alive and not process_errors,
        "sockets_cleaned": not remaining_sockets,
        "locks_free": not held_locks,
        "fds_released": not alive and not process_errors,
        "process_query_errors": process_errors,
        "parse_errors": parse_errors,
        "unreaped_pids": sorted(alive),
        "remaining_sockets": remaining_sockets,
        "held_locks": held_locks,
        "handle_release_method": "A 輪追蹤行程結束後由 Windows 關閉其行程 handle",
    }
    out.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return result


def resolve_manifest(cli: Path) -> Path:
    """依 bundle 佈局定位 release-manifest.json（Linux 版本化佈局與 Windows dev bundle 皆可）。

    順序：`bin/current-release.json` 指向的 `versions/<V>/`（只取目錄名段，Windows 反斜線不直接用）→
    `<bundle>/release-manifest.json` → `<bundle>/versions/*/` 唯一者。全部不存在即 ValueError，不得退回 git HEAD。
    """
    from pathlib import PureWindowsPath

    bundle = cli.parent.parent
    candidates: list[Path] = []
    pointer = cli.parent / "current-release.json"
    if pointer.is_file():
        try:
            raw = str(json.loads(pointer.read_text(encoding="utf-8"))["dir"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"current-release.json 無法解析：{exc}") from exc
        candidates.append(bundle / "versions" / PureWindowsPath(raw).name / "release-manifest.json")
    candidates.append(bundle / "release-manifest.json")
    versions = sorted((bundle / "versions").glob("*/release-manifest.json")) if (bundle / "versions").is_dir() else []
    if len(versions) == 1:
        candidates.append(versions[0])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError(f"找不到 release-manifest.json（bundle={bundle}）；不得以 git HEAD 代替")


def _config_digest_module() -> Any:
    """以路徑載入 tests/contract_blackbox/config_digest.py（stdlib-only；不經 sys.path，避免與 harness 同名模組衝突）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("ab_config_digest", CONFIG_DIGEST)
    if spec is None or spec.loader is None:
        raise ValueError(f"無法載入設定摘要模組：{CONFIG_DIGEST}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config_summary(log: Path, out: Path) -> dict[str, Any]:
    """彙整單一輪（A 或 B）的設定紀錄，逐案例摘要寫入 `out`；紀錄缺失或為空即 ValueError（fail-closed）。"""
    summary = _config_digest_module().aggregate(log)
    out.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return summary


def _lock_applicability_sha256() -> str:
    """凍結 lock 與 applicability 檔的雜湊（與設定內容無關，獨立保留於身分）。"""
    return hashlib.sha256(LOCK.read_bytes() + APPLICABILITY.read_bytes()).hexdigest()


def _bundle_identity(
    cli: Path, platform: str, cases_sha: str, evidence: list[Path], *, config_digest: str,
) -> dict[str, Any]:
    from sw_core.release_manifest import package_tree_sha256

    manifest_path = resolve_manifest(cli)
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    daemon = manifest.get("components", {}).get("daemon", {})
    package = daemon.get("package", {})
    runtime = daemon.get("runtime", {})
    daemon_tree = package.get("tree_sha256") or package_tree_sha256()
    runtime_path = Path(sys.executable).resolve()
    runtime_sha = _sha256_file(runtime_path)
    git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    git_revision = manifest.get("git_revision")
    if git_revision != git_head:
        raise ValueError(f"release-manifest git_revision={git_revision!r} 與 HEAD={git_head} 不符")
    contract_revision = manifest.get("contract_revision") or subprocess.check_output(
        ["git", "rev-parse", "HEAD:contracts"], cwd=ROOT, text=True
    ).strip()
    return {
        "cli_path": str(cli.resolve()),
        "cli_sha256": _sha256_file(cli),
        "cli_implementation": "rust" if manifest.get("components", {}).get("cli", {}).get("implementation") == "rust" else "python",
        "daemon_version": manifest.get("bundle_version") or (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "daemon_git_revision": git_revision,
        "daemon_tree_sha256": daemon_tree,
        "daemon_runtime_sha256": runtime.get("sha256") or runtime_sha,
        "contract_revision": contract_revision,
        "case_list_sha256": cases_sha,
        "tool_revision": f"{git_revision}+{_tree_sha256(ROOT / 'tests/contract_blackbox')}",
        "platform": platform,
        "config_digest": config_digest,
        "lock_applicability_sha256": _lock_applicability_sha256(),
        "dut_identity": "P2 黑箱測試目標",
        "evidence_files": [
            {"path": str(path.resolve()), "sha256": _sha256_file(path)}
            for path in evidence if path.is_file()
        ],
    }


def _baseline_identity(
    cli: Path, platform: str, cases_sha: str, evidence: list[Path], *, config_digest: str,
) -> dict[str, Any]:
    """A 輪（Python 基線）身分：基線不是 bundle，不要求 release-manifest；git_revision 取 HEAD。"""
    from sw_core.release_manifest import package_tree_sha256

    git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    contract_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD:contracts"], cwd=ROOT, text=True
    ).strip()
    return {
        "cli_path": str(cli.resolve()),
        "cli_sha256": _sha256_file(cli),
        "cli_implementation": "python",
        "daemon_version": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
        "daemon_git_revision": git_head,
        "daemon_tree_sha256": package_tree_sha256(),
        "daemon_runtime_sha256": _sha256_file(Path(sys.executable).resolve()),
        "contract_revision": contract_revision,
        "case_list_sha256": cases_sha,
        "tool_revision": f"{git_head}+{_tree_sha256(ROOT / 'tests/contract_blackbox')}",
        "platform": platform,
        "config_digest": config_digest,
        "lock_applicability_sha256": _lock_applicability_sha256(),
        "dut_identity": "P2 黑箱測試目標",
        "evidence_files": [
            {"path": str(path.resolve()), "sha256": _sha256_file(path)}
            for path in evidence if path.is_file()
        ],
    }


def _required_line(node: str) -> str:
    """required 清單須與 ab_compare 正規化形式相同（<檔名>.py::<函式>，去除類別與參數化後綴），否則比對會回報 required-missing。"""
    path_text, *segments = node.split("::")
    func = segments[-1].split("[", 1)[0] if segments else ""
    return f"{Path(path_text).name}::{func}"


def _write_identity(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _junit_matches(cases: dict[str, dict[str, str]], node: str) -> list[dict[str, str]]:
    """JUnit 中與凍結 pytest 節點對應的列；呼叫端須確認恰為一列才可判定結果。"""
    path_text, *segments = node.split("::")
    module = path_text.removesuffix(".py").replace("/", ".")
    expected_class = ".".join([module, *segments[:-1]])
    expected_name = segments[-1].split("[", 1)[0] if segments else ""
    exact_name = bool(segments and "[" in segments[-1])
    matches = []
    for cid, info in cases.items():
        actual_class, separator, actual_name = cid.rpartition("::")
        if not separator or actual_class != expected_class:
            continue
        name_matches = actual_name == segments[-1] if exact_name else actual_name.split("[", 1)[0] == expected_name
        if name_matches:
            matches.append(info)
    return matches


def _junit_required(
    junit: Path,
    required: list[str],
    *,
    approved_skips: dict[str, str] | None = None,
) -> list[str]:
    from scripts.rust.ab_compare import _parse_junit

    cases, errors = _parse_junit(junit)
    failures = list(errors)
    approved_skips = approved_skips or {}
    for node in sorted(set(required)):
        matches = _junit_matches(cases, node)
        if len(matches) != 1:
            failures.append(f"必要節點 {node} 執行次數為 {len(matches)}")
            continue
        outcome = matches[0]["outcome"]
        message = matches[0]["message"]
        if outcome == "xfail":
            failures.append(f"必要節點 {node} 出現 xfail")
        elif outcome == "skip":
            if approved_skips.get(node) != message:
                failures.append(f"必要節點 {node} 有未核准 skip：{message}")
        elif outcome != "pass":
            failures.append(f"必要節點 {node} 結果不是 pass：{outcome}")
    return failures


_RUST_ONLY_RELEASE_REASON = "全部選取節點為 rust_only，A 輪僅收集並 skip，未啟動受測行程"


def _a_round_spawned_nothing(
    a_junit: Path,
    a_pidlog: Path,
    a_returncode: int,
    selected: list[str],
    approved: dict[str, str],
) -> bool:
    """A 輪是否可證明「未啟動任何受測行程」（fail-closed：任一條件不成立即 False）。

    條件：每個選取節點皆為 `_approved_python_skips` 核准的 rust_only；A 輪 exit 0；JUnit 可解析且每個
    選取節點恰一列，結果為核准理由的 skip（不得為 pass、xfail 或其他理由的 skip）；PID 日誌不存在或無任何非空白列。
    """
    from scripts.rust.ab_compare import _parse_junit

    if a_returncode != 0 or not all(node in approved for node in selected):
        return False
    if not a_junit.is_file():
        return False
    try:
        if a_pidlog.is_file() and any(line.strip() for line in a_pidlog.read_text(encoding="utf-8").splitlines()):
            return False
    except (OSError, UnicodeError):
        return False
    cases, errors = _parse_junit(a_junit)
    if errors:
        return False
    for node in selected:
        matches = _junit_matches(cases, node)
        if len(matches) != 1:
            return False
        if matches[0]["outcome"] != "skip" or matches[0]["message"] != approved[node]:
            return False
    return True


def _a_round_release(
    *,
    a_junit: Path,
    a_pidlog: Path,
    a_returncode: int,
    selected: list[str],
    approved: dict[str, str],
    out: Path,
) -> dict[str, Any]:
    """A 輪釋放前置判斷（Linux 與 Windows 共用；平台差異仍在 `_release_check` 內）。

    依 plan 05 Task 16（第 3523 行）：rust_only 案例只跑 B 並驗絕對契約。全 rust_only 選取時 A 輪沒有受測行程需要
    釋放，`_release_check` 的「缺 PID 日誌即失敗」不適用，此時記為 ok 並照常啟動 B 輪。其餘情況一律沿用 `_release_check`。
    """
    if not _a_round_spawned_nothing(a_junit, a_pidlog, a_returncode, selected, approved):
        return _release_check(a_pidlog, out)
    result: dict[str, Any] = {
        "ok": True,
        "a_round_spawned": False,
        "reason": _RUST_ONLY_RELEASE_REASON,
        "selected_nodes": sorted(selected),
        "pids_reaped": True,
        "sockets_cleaned": True,
        "locks_free": True,
        "fds_released": True,
        "parse_errors": [],
        "unreaped_pids": [],
        "remaining_sockets": [],
        "held_locks": [],
        "held_fds": [],
    }
    out.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return result


def execute(*, cli: Path, out_dir: Path, platform: str, timeout: int) -> int:
    if platform not in {"linux", "windows"}:
        raise ValueError(f"不支援的平台：{platform}")
    if platform != ("windows" if os.name == "nt" else "linux"):
        raise ValueError(f"runner 平台標籤 {platform} 與本機平台不符")
    if not cli.is_file():
        raise ValueError(f"Rust CLI 不存在：{cli}")
    if not PY_DAEMON.is_file():
        raise ValueError(f"Python daemon fixture 不存在：{PY_DAEMON}")
    out_dir.mkdir(parents=True, exist_ok=False)
    a_junit, b_junit = out_dir / "a-python.xml", out_dir / "b-rust.xml"
    a_resource, b_resource = out_dir / "a-resource-report.json", out_dir / "b-resource-report.json"
    a_pidlog, b_pidlog = out_dir / "a.pids", out_dir / "b.pids"
    required_path = out_dir / "required.txt"
    release_path = out_dir / "release-check.json"
    compare_path = out_dir / "ab-report.json"
    identity_a_path, identity_b_path = out_dir / "identity-a.json", out_dir / "identity-b.json"
    a_config_log, b_config_log = out_dir / "a-config.jsonl", out_dir / "b-config.jsonl"
    config_a_path, config_b_path = out_dir / "config-digest-a.json", out_dir / "config-digest-b.json"

    lock_cases, frozen_nodes = _frozen_cases()
    case_map = _frozen_case_map(lock_cases)
    selected_nodes = _applicable_nodes(frozen_nodes, platform)
    approved_a_skips = _approved_python_skips(lock_cases, case_map, platform, selected_nodes)
    required_path.write_text("\n".join(sorted({_required_line(node) for node in selected_nodes})) + "\n", encoding="utf-8")
    base_env = dict(os.environ)
    base_env["SERIALWRAP_DAEMON_EXE"] = str(PY_DAEMON)

    collect = _run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *selected_nodes],
        env={**base_env, "SERIALWRAP_AB_LABEL": "python-baseline"}, timeout=timeout,
    )
    collected = {line.strip() for line in collect.stdout.splitlines() if "::" in line}
    missing = sorted(set(selected_nodes) - collected)
    if collect.returncode != 0 or missing:
        raise RuntimeError(f"凍結節點無法收集：rc={collect.returncode} missing={missing}\n{collect.stdout[-2000:]}\n{collect.stderr[-2000:]}")

    a_env = dict(base_env)
    a_env.pop("SERIALWRAP_CLI_EXE", None)
    a_env.pop("SERIALWRAP_RELEASE_MANIFEST", None)
    a_env.update({
        "SERIALWRAP_AB_LABEL": "python-baseline",
        "SW_AB_PIDLOG": str(a_pidlog),
        "SW_RESOURCE_REPORT": str(a_resource),
        "SW_AB_CONFIGLOG": str(a_config_log),
    })
    a_command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *selected_nodes, f"--junitxml={a_junit}"]
    a_result = _run(a_command, env=a_env, timeout=timeout)
    a_status_errors = (
        _junit_required(a_junit, selected_nodes, approved_skips=approved_a_skips)
        if a_junit.is_file()
        else ["A 輪缺少 JUnit"]
    )

    release = _a_round_release(
        a_junit=a_junit, a_pidlog=a_pidlog, a_returncode=a_result.returncode,
        selected=selected_nodes, approved=approved_a_skips, out=release_path,
    )
    if not release.get("ok"):
        report = {
            "result": "fail", "reason": "A 輪資源釋放檢查失敗", "a_exit_code": a_result.returncode,
            "a_status_errors": a_status_errors, "release_check": release,
            "case_ids": [case["case_id"] for case in lock_cases], "pytest_nodes": selected_nodes,
        }
        compare_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        summary = {
            "schema_version": "stage1b-ab.v1", "result": "fail", "platform": platform,
            "case_ids": [case["case_id"] for case in lock_cases], "pytest_nodes": selected_nodes,
            "commands": {"a": a_command, "release_check": str(release_path), "b": None, "compare": None},
            "exit_codes": {"a": a_result.returncode, "b": None, "compare": None},
            "a_status_errors": a_status_errors, "b_status_errors": ["A 輪資源釋放失敗，B 輪未啟動"],
            "release_check": release, "counts": {}, "ab_report": report,
            "logs": {"a_stdout": a_result.stdout[-4000:], "a_stderr": a_result.stderr[-4000:]},
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"result": "fail", "summary": str(out_dir / "summary.json"), "counts": {}}, ensure_ascii=False, sort_keys=True))
        return 1

    b_env = dict(base_env)
    b_env.update({
        "SERIALWRAP_CLI_EXE": str(cli.resolve()),
        "SERIALWRAP_RELEASE_MANIFEST": str(resolve_manifest(cli)),
        "SW_BUNDLE": str(cli.parent.parent),
        "SERIALWRAP_AB_LABEL": "rust-1b",
        "SW_AB_PIDLOG": str(b_pidlog),
        "SW_RESOURCE_REPORT": str(b_resource),
        "SW_AB_CONFIGLOG": str(b_config_log),
    })
    b_command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *selected_nodes, f"--junitxml={b_junit}"]
    b_result = _run(b_command, env=b_env, timeout=timeout)
    b_status_errors = _junit_required(b_junit, selected_nodes) if b_junit.is_file() else ["B 輪缺少 JUnit"]

    package_tree = __import__("sw_core.release_manifest", fromlist=["package_tree_sha256"]).package_tree_sha256()
    from scripts.rust.ab_compare import _parse_junit

    parsed_a, parse_errors_a = _parse_junit(a_junit)
    parsed_b, parse_errors_b = _parse_junit(b_junit)
    cases_sha_a = hashlib.sha256("\n".join(sorted(parsed_a)).encode("utf-8")).hexdigest()
    cases_sha_b = hashlib.sha256("\n".join(sorted(parsed_b)).encode("utf-8")).hexdigest()
    identity_evidence = [required_path, release_path, a_junit, b_junit, a_resource, b_resource]
    identity_a_cli = ROOT / ("serialwrap.py" if platform == "windows" else "serialwrap")
    # 設定摘要缺失（紀錄不存在或為空）不以例外中止：記入 config_errors、整體判 fail，summary 仍照常產生。
    config_errors: list[str] = []
    config_digests: dict[str, str] = {}
    for side, log, summary_path in (("a", a_config_log, config_a_path), ("b", b_config_log, config_b_path)):
        try:
            config_digests[side] = _config_summary(log, summary_path)["digest"]
        except ValueError as exc:
            config_errors.append(f"{side.upper()} 輪設定摘要缺失：{exc}")
            config_digests[side] = ""
    identity_a = _baseline_identity(
        identity_a_cli, platform, cases_sha_a,
        identity_evidence + [config_a_path, a_config_log], config_digest=config_digests["a"],
    )
    identity_b = _bundle_identity(
        cli, platform, cases_sha_b,
        identity_evidence + [config_b_path, b_config_log], config_digest=config_digests["b"],
    )
    identity_b["cli_implementation"] = "rust"
    identity_b["daemon_tree_sha256"] = package_tree
    identity_b["daemon_runtime_sha256"] = _sha256_file(Path(sys.executable).resolve())
    _write_identity(identity_a_path, identity_a)
    _write_identity(identity_b_path, identity_b)

    compare_command = [
        sys.executable, str(COMPARE), "--a", str(a_junit), "--b", str(b_junit),
        "--a-label", "python", "--b-label", "rust-1b", "--out", str(compare_path),
        "--identity-a", str(identity_a_path), "--identity-b", str(identity_b_path),
        "--applicability", str(APPLICABILITY), "--required", str(required_path),
    ]
    compare_result = _run(compare_command, env=base_env, timeout=timeout)
    comparison: dict[str, Any] = {}
    if compare_path.is_file():
        try:
            comparison = json.loads(compare_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            comparison = {}
    success = (
        a_result.returncode == 0 and b_result.returncode == 0 and not a_status_errors
        and not b_status_errors and not parse_errors_a and not parse_errors_b and not config_errors
        and compare_result.returncode == 0
        and comparison.get("result") == "pass"
        and not comparison.get("errors")
        and bool(comparison.get("cases"))
        and all(row.get("class") in {"ok", "rust-only-ok", "not-applicable"}
                for row in comparison["cases"])
    )
    summary = {
        "schema_version": "stage1b-ab.v1",
        "result": "pass" if success else "fail",
        "platform": platform,
        "case_ids": [case["case_id"] for case in lock_cases],
        "pytest_nodes": selected_nodes,
        "commands": {"a": a_command, "release_check": str(release_path), "b": b_command, "compare": compare_command},
        "exit_codes": {"a": a_result.returncode, "b": b_result.returncode, "compare": compare_result.returncode},
        "a_status_errors": a_status_errors,
        "b_status_errors": b_status_errors,
        "config_errors": config_errors,
        "release_check": release,
        "counts": comparison.get("counts", {}),
        "ab_report": comparison,
        "logs": {"a_stdout": a_result.stdout[-4000:], "a_stderr": a_result.stderr[-4000:],
                 "b_stdout": b_result.stdout[-4000:], "b_stderr": b_result.stderr[-4000:],
                 "compare_stdout": compare_result.stdout[-2000:], "compare_stderr": compare_result.stderr[-2000:]},
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": summary["result"], "summary": str(out_dir / "summary.json"), "counts": summary["counts"]}, ensure_ascii=False, sort_keys=True))
    return 0 if success else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="執行 P4 凍結的 Stage 1B 黑箱 A/B")
    parser.add_argument("--cli", type=Path, help="Rust dev bundle CLI；預設讀 SERIALWRAP_CLI_EXE")
    parser.add_argument("--out", type=Path, required=True, help="新建的結果目錄")
    parser.add_argument("--platform", choices=("linux", "windows"), default="windows" if os.name == "nt" else "linux")
    parser.add_argument("--timeout", type=int, default=3600, help="每輪 pytest 的逾時秒數")
    args = parser.parse_args(argv)
    raw_cli = args.cli or (Path(os.environ["SERIALWRAP_CLI_EXE"]) if os.environ.get("SERIALWRAP_CLI_EXE") else None)
    if raw_cli is None:
        parser.error("必須提供 --cli 或 SERIALWRAP_CLI_EXE")
    out_dir = args.out.resolve()
    # 結果目錄必須為新建；既有目錄（含先前成功 run 的 summary.json）一律拒絕，且不寫入任何檔案。
    if out_dir.exists():
        print(f"Stage 1B A/B 拒絕覆寫既有結果目錄：{out_dir}", file=sys.stderr)
        return 2
    try:
        return execute(cli=raw_cli.resolve(), out_dir=out_dir, platform=args.platform, timeout=args.timeout)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        # 失敗摘要只寫 summary.error.json，不得覆蓋 summary.json（gate 讀取的成功／失敗依據）。
        out_dir.mkdir(parents=True, exist_ok=True)
        failure = {"schema_version": "stage1b-ab.v1", "result": "fail", "error": f"{type(exc).__name__}: {exc}"}
        (out_dir / "summary.error.json").write_text(json.dumps(failure, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(f"Stage 1B A/B 失敗：{failure['error']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
