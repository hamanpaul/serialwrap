"""Canary 狀態快照、WAL 增量收割與命令結果收斂（stdlib only）。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable

from tools.stage3 import canary_harvest, canary_policy, jsonio
from tools.stage3.proc import Runner, run_cmd, run_json
from tools.stage3.reason_codes import HarnessError

READONLY_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("daemon", "status"),
    ("session", "list"),
    ("cmd", "status"),
)
TERMINAL_RESULTS: frozenset[str] = frozenset({"done", "error", "timeout"})
_NOT_FOUND_CODES = frozenset({"CMD_NOT_FOUND"})
_RESULT_MAX_AGE_S = 24 * 60 * 60
_CLI_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class SessionObs:
    com: str
    state: str
    bridge_generation: int | None
    boot_quiet_remaining_s: float | None
    ready_reconfirm_pending: bool | None
    ready_reconfirm_failed: bool | None
    command_capable: bool | None
    platform: str | None
    profile: str | None
    last_error: str | None
    last_ready_at: str | None


@dataclass(frozen=True)
class Snapshot:
    t_wall: str
    host_id: str
    platform: str
    boot_id: str | None
    daemon_ok: bool
    daemon_pid: int | None
    daemon_version: str | None
    daemon_exe_sha256: str | None
    artifact_id: str | None
    artifact_check: str
    bundle_tree_sha256: str | None
    runtime_tree_sha256: str | None
    bundle_version: str | None
    daemon_implementation: str | None
    wal_dir: str | None
    wal_healthy: bool | None
    multi_open: bool | None
    sessions: tuple[SessionObs, ...]
    error: str | None


def _machine_id() -> str:
    """讀取本機識別值；呼叫端只保存其摘要，不保存原始識別值。"""
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                value, _kind = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
        except (ImportError, OSError, ValueError):
            return ""
    try:
        return Path("/etc/machine-id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return ""


def host_id() -> str:
    """依 hostname 與作業系統機器識別值產生固定 16 字元摘要。"""
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    material = f"{hostname}\0{_machine_id()}".encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()[:16]


def boot_id() -> str | None:
    """讀取作業系統開機身分；無法取得時回傳 None。"""
    if os.name != "nt":
        try:
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return None
        return value or None

    query = '*[System[Provider[@Name="Microsoft-Windows-Kernel-Boot"] and (EventID=27)]]'
    try:
        result = subprocess.run(
            ["wevtutil", "qe", "System", f"/q:{query}", "/c:1", "/rd:true", "/f:xml"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        root = ET.fromstring(result.stdout)
        record_id = None
        created = None
        for node in root.iter():
            name = node.tag.rsplit("}", 1)[-1]
            if name == "EventRecordID":
                record_id = (node.text or "").strip()
            elif name == "TimeCreated":
                created = node.attrib.get("SystemTime")
        if not record_id or not created:
            return None
        return f"{record_id}:{created}"
    except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError):
        return None


def daemon_exe_sha256(pid: int) -> str | None:
    """計算指定程序實際執行檔的 SHA-256；程序資訊不可讀時回傳 None。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if os.name != "nt":
        try:
            return jsonio.sha256_file(Path(os.path.realpath(f"/proc/{pid}/exe")))
        except OSError:
            return None

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    query_image = kernel32.QueryFullProcessImageNameW
    query_image.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    query_image.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not query_image(handle, 0, buffer, ctypes.byref(capacity)):
            return None
        return jsonio.sha256_file(Path(buffer.value))
    except OSError:
        return None
    finally:
        close_handle(handle)


def _tree_error(path: Path, reason: str = "non-regular file") -> HarnessError:
    return HarnessError("HARNESS_ARTIFACT_MISMATCH", f"{reason}: {Path(path).as_posix()}")


def _hash_regular_file(path: Path, *, display_path: Path | None = None) -> str:
    """雜湊一般檔案，並拒絕掃描期間被替換或改寫的檔案。"""
    label = display_path or path
    try:
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise _tree_error(label)
            digest = hashlib.sha256()
            size = 0
            for chunk in iter(lambda: stream.read(1 << 16), b""):
                size += len(chunk)
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ) or size != after.st_size:
            raise _tree_error(label, "file changed during hashing")
        return digest.hexdigest()
    except HarnessError:
        raise
    except OSError as exc:
        raise _tree_error(label, "cannot read file") from exc


def deployed_tree_digests(bundle_dir: Path) -> tuple[str, str]:
    """重新雜湊實際 bundle 樹，回傳全樹與排除根目錄索引檔後的執行期樹摘要。"""
    root = Path(bundle_dir)

    def relative(path: Path) -> Path:
        try:
            value = path.relative_to(root).as_posix()
        except ValueError:
            value = path.name
        return Path(value or ".")

    rows: list[tuple[str, str]] = []
    try:
        root_stat = root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode):
            raise _tree_error(Path("."))

        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    children = sorted(entries, key=lambda entry: entry.name)
            except OSError as exc:
                raise _tree_error(relative(directory), "cannot scan directory") from exc
            child_dirs: list[Path] = []
            for entry in children:
                path = Path(entry.path)
                rel = path.relative_to(root).as_posix()
                try:
                    item_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise _tree_error(Path(rel), "cannot stat file") from exc
                if stat.S_ISLNK(item_stat.st_mode):
                    raise _tree_error(Path(rel))
                if stat.S_ISDIR(item_stat.st_mode):
                    child_dirs.append(path)
                    continue
                if not stat.S_ISREG(item_stat.st_mode):
                    raise _tree_error(Path(rel))
                rows.append((rel, _hash_regular_file(path, display_path=Path(rel))))
            pending.extend(reversed(child_dirs))
    except HarnessError:
        raise
    except OSError as exc:
        raise _tree_error(Path("."), "cannot scan directory") from exc

    rows.sort(key=lambda row: row[0])
    bundle_text = "".join(f"{rel}\0{digest}\n" for rel, digest in rows)
    runtime_text = "".join(
        f"{rel}\0{digest}\n"
        for rel, digest in rows
        if rel not in {"release-manifest.json", "SHA256SUMS"}
    )
    return (
        hashlib.sha256(bundle_text.encode("utf-8")).hexdigest(),
        hashlib.sha256(runtime_text.encode("utf-8")).hexdigest(),
    )


def _load_manifest(path: Path) -> tuple[dict, bytes] | None:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return value, raw


def _manifest_location(cli_exe: Path, layout: str) -> Path | None:
    cli = Path(os.path.realpath(cli_exe))
    if layout == "windows":
        pointer = cli.parent / "current-release.json"
        try:
            current = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(current, dict) or not isinstance(current.get("dir"), str):
            return None
        bundle = Path(current["dir"])
        if not bundle.is_absolute():
            bundle = pointer.parent / bundle
        return bundle / "release-manifest.json"
    if layout == "linux":
        return cli.parent.parent / "release-manifest.json"
    return None


def _validate_windows_entry_dir(cli_exe: Path) -> None:
    """拒絕 Windows 穩定入口目錄中可被載入的未宣告檔案。"""
    entry_dir = Path(os.path.realpath(cli_exe)).parent
    allowed = {"serialwrap.exe", "serialwrapd.exe", "current-release.json"}
    try:
        with os.scandir(entry_dir) as entries:
            actual = {entry.name.casefold(): entry for entry in entries}
    except OSError as exc:
        raise _tree_error(Path("bin"), "cannot scan stable entry directory") from exc

    for name in sorted(set(actual) - allowed):
        raise _tree_error(Path(name), "unexpected stable entry")
    for name in sorted(allowed - set(actual)):
        raise _tree_error(Path(name), "missing stable entry")
    for name, entry in actual.items():
        try:
            item_stat = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise _tree_error(Path(name), "cannot stat stable entry") from exc
        if not stat.S_ISREG(item_stat.st_mode):
            raise _tree_error(Path(name))


def resolve_artifact(
    cli_exe: Path,
    *,
    daemon_pid: int | None,
    daemon_version: str | None,
    daemon_sha: Callable[[int], str | None] = daemon_exe_sha256,
    layout: str | None = None,
) -> tuple[str | None, dict | None, str, tuple[str, str] | None]:
    """以 manifest、CLI、daemon 與部署樹四項證據核實快照的 artifact 歸屬。"""
    selected_layout = layout or ("windows" if os.name == "nt" else "linux")
    manifest_path = _manifest_location(Path(cli_exe), selected_layout)
    if manifest_path is None:
        return None, None, "unresolved", None
    loaded = _load_manifest(manifest_path)
    if loaded is None:
        return None, None, "unresolved", None
    manifest, raw_manifest = loaded
    components = manifest.get("components")
    if not isinstance(components, dict):
        return None, manifest, "unresolved", None
    cli_component = components.get("cli")
    daemon_component = components.get("daemon")
    if not isinstance(cli_component, dict) or not isinstance(daemon_component, dict):
        return None, manifest, "unresolved", None

    expected_cli_sha = cli_component.get("sha256")
    try:
        actual_cli_sha = jsonio.sha256_file(Path(os.path.realpath(cli_exe)))
    except OSError:
        actual_cli_sha = None
    if not isinstance(expected_cli_sha, str) or actual_cli_sha != expected_cli_sha:
        return None, manifest, "cli_mismatch", None

    bundle_version = manifest.get("bundle_version")
    if not isinstance(bundle_version, str) or daemon_version != bundle_version:
        return None, manifest, "version_mismatch", None

    launcher = daemon_component.get("launcher")
    if isinstance(launcher, dict):
        expected_daemon_sha = launcher.get("sha256")
    else:
        runtime = daemon_component.get("runtime")
        expected_daemon_sha = runtime.get("sha256") if isinstance(runtime, dict) else None
    try:
        actual_daemon_sha = daemon_sha(daemon_pid) if daemon_pid is not None else None
    except (OSError, ValueError, TypeError):
        actual_daemon_sha = None
    if not isinstance(expected_daemon_sha, str) or actual_daemon_sha != expected_daemon_sha:
        return None, manifest, "daemon_mismatch", None

    if selected_layout == "windows":
        try:
            _validate_windows_entry_dir(Path(cli_exe))
        except HarnessError:
            return None, manifest, "tree_invalid", None

    try:
        bundle_tree, runtime_tree = deployed_tree_digests(manifest_path.parent)
    except HarnessError:
        return None, manifest, "tree_invalid", None
    return jsonio.sha256_bytes(raw_manifest), manifest, "verified", (bundle_tree, runtime_tree)


def _maybe_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _maybe_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _maybe_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _maybe_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _session_observations(value: object) -> tuple[SessionObs, ...]:
    if not isinstance(value, list):
        return ()
    rows: list[SessionObs] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        rows.append(
            SessionObs(
                com=str(item.get("com") or ""),
                state=str(item.get("state") or ""),
                bridge_generation=_maybe_int(item.get("bridge_generation")),
                boot_quiet_remaining_s=_maybe_float(item.get("boot_quiet_remaining_s")),
                ready_reconfirm_pending=_maybe_bool(item.get("ready_reconfirm_pending")),
                ready_reconfirm_failed=_maybe_bool(item.get("ready_reconfirm_failed")),
                command_capable=_maybe_bool(item.get("command_capable")),
                platform=_maybe_str(item.get("platform")),
                profile=_maybe_str(item.get("profile")),
                last_error=_maybe_str(item.get("last_error")),
                last_ready_at=_maybe_str(item.get("last_ready_at")),
            )
        )
    return tuple(rows)


def _run_error(result, body: dict | None, label: str) -> str:
    if result.timed_out:
        return f"{label}_timeout"
    if result.rc != 0:
        return f"{label}_failed:rc={result.rc}"
    if body is None:
        return f"{label}_invalid_json"
    return f"{label}_not_ok"


def _daemon_status_error(result, body: dict | None) -> str:
    """把明確的連線拒絕與 endpoint 消失編成事故偵測器使用的死機標記。"""
    if result.timed_out:
        return "daemon_status_timeout"
    code = body.get("error_code") if isinstance(body, dict) else None
    if code == "SOCKET_ERROR" or "SOCKET_ERROR" in result.stderr:
        message = body.get("message", "") if isinstance(body, dict) else ""
        detail = f"{message} {result.stderr}".lower()
        if "connection refused" in detail or re.search(r"\berrno\s*111\b", detail) or re.search(r"\bwinerror\s*10061\b", detail):
            return "connect_refused"
        if (
            "no such file or directory" in detail
            or re.search(r"\berrno\s*2\b", detail)
            or re.search(r"\bwinerror\s*2\b", detail)
        ):
            return "not_running"
    return _run_error(result, body, "daemon_status")


def take_snapshot(
    cli_exe: Path,
    *,
    platform: str,
    run: Runner = run_cmd,
    now: Callable[[], str] = jsonio.utc_now_iso,
) -> Snapshot:
    """以唯讀 CLI 命令取得 daemon、session 與實際部署內容快照。"""
    if platform not in canary_policy.PLATFORMS:
        raise ValueError(f"unknown platform: {platform!r}")
    wall = now()
    common = {"t_wall": wall, "host_id": host_id(), "platform": platform, "boot_id": boot_id()}

    try:
        status_result, status = run_json(run, [str(cli_exe), "daemon", "status"], timeout=_CLI_TIMEOUT_S)
    except Exception as exc:
        error = f"daemon_status_error:{type(exc).__name__}"
        return Snapshot(
            **common, daemon_ok=False, daemon_pid=None, daemon_version=None, daemon_exe_sha256=None,
            artifact_id=None, artifact_check="unresolved", bundle_tree_sha256=None, runtime_tree_sha256=None,
            bundle_version=None, daemon_implementation=None, wal_dir=None, wal_healthy=None,
            multi_open=None, sessions=(), error=error,
        )

    if status_result.timed_out or status_result.rc != 0 or status is None or status.get("ok") is not True:
        return Snapshot(
            **common, daemon_ok=False, daemon_pid=None, daemon_version=None, daemon_exe_sha256=None,
            artifact_id=None, artifact_check="unresolved", bundle_tree_sha256=None, runtime_tree_sha256=None,
            bundle_version=None, daemon_implementation=None, wal_dir=None, wal_healthy=None,
            multi_open=None, sessions=(), error=_daemon_status_error(status_result, status),
        )

    pid = _maybe_int(status.get("pid"))
    version = _maybe_str(status.get("version"))
    running = status.get("running") is True
    if not running:
        return Snapshot(
            **common, daemon_ok=False, daemon_pid=pid, daemon_version=version, daemon_exe_sha256=None,
            artifact_id=None, artifact_check="unresolved", bundle_tree_sha256=None, runtime_tree_sha256=None,
            bundle_version=None, daemon_implementation=None, wal_dir=None, wal_healthy=None,
            multi_open=_maybe_bool(status.get("multi_open")), sessions=(), error="not_running",
        )

    errors: list[str] = []
    try:
        sessions_result, sessions_doc = run_json(run, [str(cli_exe), "session", "list"], timeout=_CLI_TIMEOUT_S)
    except Exception as exc:
        sessions_result, sessions_doc = None, None
        errors.append(f"session_list_error:{type(exc).__name__}")
    if sessions_result is not None and (
        sessions_result.timed_out or sessions_result.rc != 0 or sessions_doc is None or sessions_doc.get("ok") is not True
    ):
        errors.append(_run_error(sessions_result, sessions_doc, "session_list"))
    session_rows = _session_observations(sessions_doc.get("sessions") if sessions_doc else None)

    wal_path = status.get("wal_path")
    wal_dir = str(Path(wal_path).parent) if isinstance(wal_path, str) and wal_path else None
    wal = status.get("wal") if isinstance(status.get("wal"), dict) else {}
    wal_healthy = _maybe_bool(wal.get("healthy"))
    daemon_sha = daemon_exe_sha256(pid) if pid is not None else None
    artifact_id, manifest, artifact_check, trees = resolve_artifact(
        Path(cli_exe),
        daemon_pid=pid,
        daemon_version=version,
        daemon_sha=daemon_exe_sha256,
        layout="windows" if platform == "windows" else "linux",
    )
    bundle_tree = trees[0] if trees is not None else None
    runtime_tree = trees[1] if trees is not None else None
    components = manifest.get("components") if isinstance(manifest, dict) else None
    daemon_component = components.get("daemon") if isinstance(components, dict) else None
    daemon_impl = daemon_component.get("implementation") if isinstance(daemon_component, dict) else None
    bundle_version = manifest.get("bundle_version") if isinstance(manifest, dict) else None

    return Snapshot(
        **common,
        daemon_ok=True,
        daemon_pid=pid,
        daemon_version=version,
        daemon_exe_sha256=daemon_sha,
        artifact_id=artifact_id,
        artifact_check=artifact_check,
        bundle_tree_sha256=bundle_tree,
        runtime_tree_sha256=runtime_tree,
        bundle_version=_maybe_str(bundle_version),
        daemon_implementation=_maybe_str(daemon_impl),
        wal_dir=wal_dir,
        wal_healthy=wal_healthy,
        multi_open=_maybe_bool(status.get("multi_open")),
        sessions=session_rows,
        error=";".join(errors) if errors else None,
    )


def _record_id(record: dict) -> dict:
    record["record_id"] = jsonio.record_id(record)
    return record


def _append_record(ledger_dir: Path, record: dict) -> None:
    jsonio.append_ndjson(Path(ledger_dir) / "ledger.ndjson", _record_id(record))


def _read_ledger_partial(ledger_dir: Path) -> tuple[list[dict], list[int]]:
    path = Path(ledger_dir) / "ledger.ndjson"
    if not path.exists():
        return [], []
    return jsonio.read_ndjson(path)


def _read_ledger(ledger_dir: Path) -> list[dict]:
    records, malformed = _read_ledger_partial(ledger_dir)
    if malformed:
        raise HarnessError("HARNESS_LEDGER_CORRUPT", f"malformed ledger lines: {malformed[:10]}")
    return records


def _record_snapshot(snapshot: Snapshot) -> dict:
    return _record_id({"type": "snapshot", **asdict(snapshot)})


def _load_seen(ledger_dir: Path, records: list[dict]) -> set[str]:
    seen: set[str] = set()
    path = Path(ledger_dir) / "seen.txt"
    try:
        if path.exists():
            with path.open("r", encoding="ascii") as stream:
                seen.update(line.strip() for line in stream if re.fullmatch(r"[0-9a-f]{32}\s*", line))
    except (OSError, UnicodeError):
        pass
    seen.update(
        str(record["cmd_id"])
        for record in records
        if record.get("type") == "submit" and isinstance(record.get("cmd_id"), str)
        and re.fullmatch(r"[0-9a-f]{32}", record["cmd_id"])
    )
    return seen


def _load_pending_file(ledger_dir: Path) -> dict[str, str]:
    path = Path(ledger_dir) / "pending_results.json"
    pending: dict[str, str] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError("HARNESS_LEDGER_CORRUPT", f"pending results unreadable: {type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise HarnessError("HARNESS_LEDGER_CORRUPT", "pending results root must be an object")
    for cmd_id, wall in raw.items():
        if isinstance(cmd_id, str) and re.fullmatch(r"[0-9a-f]{32}", cmd_id) and isinstance(wall, str):
            pending[cmd_id] = wall
    return pending


def _load_pending(ledger_dir: Path, records: list[dict]) -> dict[str, str]:
    pending = _load_pending_file(ledger_dir)

    results = {
        str(record.get("cmd_id"))
        for record in records
        if record.get("type") == "submit_result" and isinstance(record.get("cmd_id"), str)
    }
    for record in records:
        if record.get("type") != "submit":
            continue
        cmd_id = record.get("cmd_id")
        wall = record.get("wall_ts")
        if isinstance(cmd_id, str) and isinstance(wall, str) and cmd_id not in results:
            pending.setdefault(cmd_id, wall)
    for cmd_id in results:
        pending.pop(cmd_id, None)
    return pending


def _save_pending(ledger_dir: Path, pending: dict[str, str]) -> None:
    jsonio.atomic_write_text(Path(ledger_dir) / "pending_results.json", jsonio.dumps_report(pending))


def _save_seen(ledger_dir: Path, cmd_ids: list[str]) -> None:
    if not cmd_ids:
        return
    path = Path(ledger_dir) / "seen.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="ascii", newline="\n") as stream:
        for cmd_id in cmd_ids:
            stream.write(cmd_id + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _sorted_pending(pending: dict[str, str]) -> list[str]:
    return [cmd_id for cmd_id, _wall in sorted(pending.items(), key=lambda item: _pending_order_key(item[0], item[1]))]


def _pending_order_key(cmd_id: str, wall: str) -> tuple[int, object, str]:
    try:
        parsed = jsonio.parse_iso(wall)
    except (TypeError, ValueError):
        return (1, wall, cmd_id)
    return (0, parsed, cmd_id)


def _load_poll_cursor(ledger_dir: Path) -> tuple[str, str] | None:
    path = Path(ledger_dir) / "pending_poll_state.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    wall, cmd_id = raw.get("last_first_seen"), raw.get("last_cmd_id")
    if isinstance(wall, str) and isinstance(cmd_id, str) and re.fullmatch(r"[0-9a-f]{32}", cmd_id):
        return cmd_id, wall
    return None


def _load_last_polled_ids(ledger_dir: Path) -> list[str]:
    path = Path(ledger_dir) / "pending_poll_state.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    ids = raw.get("last_polled_ids") if isinstance(raw, dict) else None
    if not isinstance(ids, list):
        return []
    return [item for item in ids if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{32}", item)]


def _save_poll_state(
    ledger_dir: Path,
    *,
    selected: list[str],
    last_polled: tuple[str, str] | None,
) -> None:
    state: dict[str, object] = {"last_polled_ids": selected}
    cursor = last_polled or _load_poll_cursor(ledger_dir)
    if cursor is not None:
        state.update({"last_cmd_id": cursor[0], "last_first_seen": cursor[1]})
    jsonio.atomic_write_text(Path(ledger_dir) / "pending_poll_state.json", jsonio.dumps_report(state))


def _pending_poll_window(ledger_dir: Path, pending: dict[str, str], max_items: int) -> list[str]:
    ordered = _sorted_pending(pending)
    if not ordered or max_items <= 0:
        return []
    cursor = _load_poll_cursor(ledger_dir)
    if cursor is None:
        start = 0
    else:
        cursor_key = _pending_order_key(cursor[0], cursor[1])
        start = next(
            (index for index, cmd_id in enumerate(ordered) if _pending_order_key(cmd_id, pending[cmd_id]) > cursor_key),
            0,
        )
    return (ordered[start:] + ordered[:start])[:max_items]


def _save_collector_context(ledger_dir: Path, *, host: str, platform: str) -> None:
    context = {"host_id": host, "platform": platform}
    jsonio.atomic_write_text(Path(ledger_dir) / "collector_context.json", jsonio.dumps_report(context))


def _load_collector_context(ledger_dir: Path) -> tuple[str, str]:
    path = Path(ledger_dir) / "collector_context.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raw = None
    if isinstance(raw, dict):
        host = raw.get("host_id")
        platform = raw.get("platform")
        if isinstance(host, str) and host and platform in canary_policy.PLATFORMS:
            return host, platform
    return host_id(), "windows" if os.name == "nt" else "linux"


def _not_found_code(code: object) -> str | None:
    if isinstance(code, str) and code in _NOT_FOUND_CODES:
        return "CMD_NOT_FOUND"
    return None


def _command_result(run: Runner, cli_exe: Path, cmd_id: str):
    return run_json(
        run,
        [str(cli_exe), "cmd", "status", "--cmd-id", cmd_id],
        timeout=_CLI_TIMEOUT_S,
    )


def _result_record(
    ledger_dir: Path,
    *,
    cmd_id: str,
    status: str,
    error_code: str | None,
    reason: str | None,
    platform: str,
    host: str,
    wall: str,
) -> None:
    record = {
        "type": "submit_result",
        "host_id": host,
        "platform": platform,
        "cmd_id": cmd_id,
        "status": status,
        "error_code": error_code,
        "t_wall": wall,
    }
    if reason is not None:
        record["reason"] = reason
    _append_record(ledger_dir, record)


def _anomaly_record(
    ledger_dir: Path,
    *,
    platform: str,
    host: str,
    wall: str,
    detail: str,
    kind: str | None = None,
    cmd_id: str | None = None,
) -> None:
    record = {
        "type": "harvest_anomaly",
        "host_id": host,
        "platform": platform,
        "detail": detail,
        "t_wall": wall,
    }
    if kind is not None:
        record["kind"] = kind
    if cmd_id is not None:
        record["cmd_id"] = cmd_id
    _append_record(ledger_dir, record)


def _record_ledger_integrity_anomalies(
    ledger_dir: Path,
    records: list[dict],
    malformed_lines: list[int],
    *,
    platform: str,
    host: str,
    wall: str,
) -> int:
    """逐筆記下壞行一次，讓 collector 可續跑且 gate 仍看得到完整性問題。"""
    recorded = {
        row.get("detail")
        for row in records
        if row.get("type") == "harvest_anomaly" and isinstance(row.get("detail"), str)
    }
    count = 0
    for line in malformed_lines:
        detail = f"malformed_ledger_line:{line}"
        if detail in recorded:
            continue
        _anomaly_record(ledger_dir, platform=platform, host=host, wall=wall, detail=detail)
        recorded.add(detail)
        count += 1
    return count


def _stale(first_seen: str, now_wall: str) -> bool:
    try:
        age = (jsonio.parse_iso(now_wall) - jsonio.parse_iso(first_seen)).total_seconds()
    except (TypeError, ValueError):
        return False
    return age > _RESULT_MAX_AGE_S


def _poll_pending(
    cli_exe: Path,
    ledger_dir: Path,
    pending: dict[str, str],
    *,
    policy: canary_policy.CanaryPolicy,
    platform: str,
    host: str,
    run: Runner,
    now: Callable[[], str],
) -> tuple[int, int]:
    processed = 0
    anomalies = 0
    max_items = policy.result_poll_max_per_cycle
    selected = _pending_poll_window(ledger_dir, pending, max_items)
    last_polled = (selected[-1], pending[selected[-1]]) if selected else None
    for cmd_id in selected:
        current_wall = now()
        try:
            result, body = _command_result(run, cli_exe, cmd_id)
        except Exception as exc:
            _anomaly_record(
                ledger_dir,
                platform=platform,
                host=host,
                wall=current_wall,
                detail=f"cmd_status_error:{type(exc).__name__}",
            )
            anomalies += 1
            if _stale(pending[cmd_id], current_wall):
                _result_record(
                    ledger_dir,
                    cmd_id=cmd_id,
                    status="unknown",
                    error_code=None,
                    reason="STALE_NON_TERMINAL",
                    platform=platform,
                    host=host,
                    wall=current_wall,
                )
                pending.pop(cmd_id, None)
                processed += 1
            continue

        command = body.get("command") if isinstance(body, dict) else None
        command = command if isinstance(command, dict) else {}
        error_code = command.get("error_code") or (body.get("error_code") if isinstance(body, dict) else None)
        missing = _not_found_code(error_code)
        status = command.get("status") if isinstance(command.get("status"), str) else None
        if not result.timed_out and body is not None and (body.get("ok") is True or missing is not None):
            if status in TERMINAL_RESULTS:
                _result_record(
                    ledger_dir, cmd_id=cmd_id, status=status,
                    error_code=error_code if isinstance(error_code, str) else None,
                    reason=None, platform=platform, host=host, wall=current_wall,
                )
                pending.pop(cmd_id, None)
                processed += 1
                continue
            if status in {"canceled", "interactive"}:
                _result_record(
                    ledger_dir,
                    cmd_id=cmd_id,
                    status="unknown",
                    error_code=status.upper(),
                    reason=None,
                    platform=platform,
                    host=host,
                    wall=current_wall,
                )
                pending.pop(cmd_id, None)
                processed += 1
                continue
            if missing is not None:
                _result_record(
                    ledger_dir, cmd_id=cmd_id, status="unknown", error_code=missing,
                    reason=missing, platform=platform, host=host, wall=current_wall,
                )
                pending.pop(cmd_id, None)
                processed += 1
                if missing == "CMD_NOT_FOUND":
                    _anomaly_record(
                        ledger_dir,
                        platform=platform,
                        host=host,
                        wall=current_wall,
                        detail=f"result_evicted:{cmd_id}",
                        kind="result_evicted",
                        cmd_id=cmd_id,
                    )
                    anomalies += 1
                continue

        if _stale(pending[cmd_id], current_wall):
            _result_record(
                ledger_dir,
                cmd_id=cmd_id,
                status="unknown",
                error_code=error_code if isinstance(error_code, str) else None,
                reason="STALE_NON_TERMINAL",
                platform=platform,
                host=host,
                wall=current_wall,
            )
            pending.pop(cmd_id, None)
            processed += 1
    _save_poll_state(ledger_dir, selected=selected, last_polled=last_polled)
    _save_pending(ledger_dir, pending)
    return processed, anomalies


@lru_cache(maxsize=1)
def collector_revision() -> str:
    """回傳執行 zipapp 的檔案摘要或目前來源版本；無法讀取時回傳 unknown。"""
    argv0 = Path(sys.argv[0]) if sys.argv else Path()
    try:
        if argv0.suffix.lower() == ".pyz" and argv0.is_file():
            return jsonio.sha256_file(argv0)
    except OSError:
        pass
    repo_root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="ascii",
            errors="replace",
            timeout=5,
        )
        revision = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
            return revision.lower()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def write_policy_ref(
    ledger_dir: Path,
    policy: canary_policy.CanaryPolicy,
    *,
    now: Callable[[], str] = jsonio.utc_now_iso,
    platform: str | None = None,
) -> None:
    """在帳本尾端登錄目前凍結政策與 collector 版本。"""
    selected_platform = platform or ("windows" if os.name == "nt" else "linux")
    host = host_id()
    revision = collector_revision()
    records, _malformed = _read_ledger_partial(Path(ledger_dir))
    previous = next((row for row in reversed(records) if row.get("type") == "policy_ref"), None)
    if (
        previous is not None
        and previous.get("policy_sha256") == policy.sha256
        and previous.get("collector_revision") == revision
    ):
        return
    _append_record(
        Path(ledger_dir),
        {
            "type": "policy_ref",
            "host_id": host,
            "platform": selected_platform,
            "policy_sha256": policy.sha256,
            "status": policy.status,
            "frozen_at": policy.frozen_at,
            "decision_ref": policy.decision_ref,
            "collector_revision": revision,
            "t_wall": now(),
        },
    )


def _append_seen(ledger_dir: Path, cmd_ids: list[str]) -> None:
    _save_seen(Path(ledger_dir), cmd_ids)


def collect_once(
    cli_exe: Path,
    ledger_dir: Path,
    policy: canary_policy.CanaryPolicy,
    *,
    platform: str,
    run: Runner = run_cmd,
    now: Callable[[], str] = jsonio.utc_now_iso,
) -> dict:
    """寫入一次 snapshot、增量 WAL 紀錄與有界命令結果。"""
    if policy.status != "frozen":
        raise HarnessError("HARNESS_CANARY_POLICY_NOT_FROZEN", "policy is draft")
    if policy.frozen_at is None:
        raise HarnessError("HARNESS_CANARY_POLICY_NOT_FROZEN", "frozen policy has no frozen_at")
    if platform not in canary_policy.PLATFORMS:
        raise ValueError(f"unknown platform: {platform!r}")
    ledger_dir = Path(ledger_dir)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    _save_poll_state(ledger_dir, selected=[], last_polled=None)
    write_policy_ref(ledger_dir, policy, now=now, platform=platform)

    snapshot = take_snapshot(cli_exe, platform=platform, run=run, now=now)
    records, malformed_lines = _read_ledger_partial(ledger_dir)
    _append_record(ledger_dir, _record_snapshot(snapshot))
    _save_collector_context(ledger_dir, host=snapshot.host_id, platform=platform)
    base = {"snapshot_ok": snapshot.daemon_ok, "new_submits": 0, "results": 0, "signals": 0, "anomalies": 0}
    base["anomalies"] += _record_ledger_integrity_anomalies(
        ledger_dir,
        records,
        malformed_lines,
        platform=platform,
        host=snapshot.host_id,
        wall=snapshot.t_wall,
    )
    if not snapshot.daemon_ok:
        return base
    if not snapshot.wal_dir:
        _anomaly_record(
            ledger_dir, platform=platform, host=snapshot.host_id, wall=snapshot.t_wall,
            detail="wal_path_missing",
        )
        base["anomalies"] += 1
        return base

    cursor_path = ledger_dir / "cursor.json"
    try:
        cursor = canary_harvest.load_cursor(cursor_path)
        harvested = canary_harvest.harvest(
            Path(snapshot.wal_dir),
            cursor,
            rules=canary_harvest.HarvestRules.from_policy(policy),
        )
    except Exception as exc:
        _anomaly_record(
            ledger_dir, platform=platform, host=snapshot.host_id, wall=now(),
            detail=f"harvest_error:{type(exc).__name__}",
        )
        base["anomalies"] += 1
        return base

    seen = _load_seen(ledger_dir, records)
    pending = _load_pending(ledger_dir, records)
    new_seen: list[str] = []
    frozen_at = jsonio.parse_iso(policy.frozen_at)
    for observation in harvested.submits:
        if jsonio.parse_iso(observation.wall_ts) < frozen_at:
            continue
        if observation.cmd_id in seen:
            continue
        submit_record = _record_id(
            {
                "type": "submit",
                "host_id": snapshot.host_id,
                "platform": platform,
                "cmd_id": observation.cmd_id,
                "com": observation.com,
                "source": observation.source,
                "wall_ts": observation.wall_ts,
                "seq": observation.seq,
                "wal_file": observation.wal_file,
            }
        )
        _append_record(ledger_dir, submit_record)
        new_seen.append(observation.cmd_id)
        seen.add(observation.cmd_id)
        pending.setdefault(observation.cmd_id, observation.wall_ts)
    _append_seen(ledger_dir, new_seen)

    for signal in harvested.signals:
        if jsonio.parse_iso(signal.wall_ts) < frozen_at:
            continue
        record = asdict(signal)
        record.update({"type": "wal_signal", "host_id": snapshot.host_id, "platform": platform})
        _append_record(ledger_dir, record)
        base["signals"] += 1
    for detail in harvested.anomalies:
        _anomaly_record(
            ledger_dir, platform=platform, host=snapshot.host_id, wall=now(), detail=detail,
        )
        base["anomalies"] += 1

    _save_pending(ledger_dir, pending)
    canary_harvest.save_cursor(cursor_path, harvested.cursor)
    base["new_submits"] = len(new_seen)
    result_count, result_anomalies = _poll_pending(
        Path(cli_exe), ledger_dir, pending,
        policy=policy, platform=platform, host=snapshot.host_id, run=run, now=now,
    )
    base["results"] = result_count
    base["anomalies"] += result_anomalies
    return base


def drain_results_once(
    cli_exe: Path,
    ledger_dir: Path,
    policy: canary_policy.CanaryPolicy,
    *,
    run: Runner = run_cmd,
    now: Callable[[], str] = jsonio.utc_now_iso,
) -> int:
    """只收斂 pending 命令結果，不重新解析帳本、不呼叫 snapshot 或讀取 WAL。"""
    if policy.status != "frozen":
        raise HarnessError("HARNESS_CANARY_POLICY_NOT_FROZEN", "policy is draft")
    ledger_dir = Path(ledger_dir)
    pending = _load_pending_file(ledger_dir)
    if not pending:
        _save_pending(ledger_dir, pending)
        return 0
    host, platform = _load_collector_context(ledger_dir)
    _processed, _anomalies = _poll_pending(
        Path(cli_exe), ledger_dir, pending,
        policy=policy, platform=platform, host=host, run=run, now=now,
    )
    return len(pending)


def _loop_anomaly(ledger_dir: Path, policy: canary_policy.CanaryPolicy, platform: str, exc: Exception) -> None:
    code = exc.code if isinstance(exc, HarnessError) else type(exc).__name__
    _anomaly_record(
        ledger_dir,
        platform=platform,
        host=host_id(),
        wall=jsonio.utc_now_iso(),
        detail=f"collector_exception:{code}",
    )


def collect_loop(
    cli_exe: Path,
    ledger_dir: Path,
    policy: canary_policy.CanaryPolicy,
    *,
    platform: str,
    stop: threading.Event,
    run: Runner = run_cmd,
    now: Callable[[], str] = jsonio.utc_now_iso,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """週期收集 Canary 資料，並在兩次快照間持續排空 pending 結果。"""
    if policy.status != "frozen":
        raise HarnessError("HARNESS_CANARY_POLICY_NOT_FROZEN", "policy is draft")
    if platform not in canary_policy.PLATFORMS:
        raise ValueError(f"unknown platform: {platform!r}")
    interval = float(policy.collector_interval_s)
    while not stop.is_set():
        cycle_start = monotonic()
        checked: set[str] = set()
        try:
            collect_once(cli_exe, ledger_dir, policy, platform=platform, run=run, now=now)
            checked.update(_load_last_polled_ids(Path(ledger_dir)))
        except Exception as exc:
            _loop_anomaly(Path(ledger_dir), policy, platform, exc)
        deadline = cycle_start + interval
        while not stop.is_set():
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            try:
                pending = _load_pending_file(Path(ledger_dir))
            except Exception as exc:
                _loop_anomaly(Path(ledger_dir), policy, platform, exc)
                pending = {}
            has_pending = bool(pending)
            if has_pending:
                try:
                    drain_results_once(cli_exe, ledger_dir, policy, run=run, now=now)
                    checked.update(_load_last_polled_ids(Path(ledger_dir)))
                    pending = _load_pending_file(Path(ledger_dir))
                    unpolled = set(pending) - checked
                    delay = min(1.0, remaining) if unpolled else remaining
                except Exception as exc:
                    _loop_anomaly(Path(ledger_dir), policy, platform, exc)
                    delay = remaining
            else:
                delay = remaining
            if delay <= 0:
                break
            if sleep is time.sleep:
                stop.wait(delay)
            else:
                sleep(delay)

