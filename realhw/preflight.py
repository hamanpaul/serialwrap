"""preflight：收集（I/O）與判定（純函式）分離，判定可單測。"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ModuleNotFoundError:  # Windows 由 msvcrt 提供 bench lock。
    fcntl = None
from . import drivers
from . import frontends, procobs


@dataclasses.dataclass(frozen=True)
class Checks:
    git_behind: int
    doctor_ok: bool
    boards_ready: list[str]
    boards_expected: list[str]
    tools_missing: list[str]
    leaked_daemons: list[str]
    other_pytest: bool
    state_polluted: bool
    benchlock_ok: bool = True
    external_testpilot: tuple[str, ...] = ()
    win_daemon_present: bool = False
    win_daemon_holds: tuple[str, ...] = ()
    version_problem: str | None = None
    exe_identity: dict[str, Any] = dataclasses.field(default_factory=dict)
    cross_os_guard: str = "unconfigured"
    cross_os_detail: str | None = None


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
MIN_DEPLOYED: tuple[int, int, int] = (0, 2, 3)


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def version_gate(cli: str, daemon: str) -> str | None:
    """pinned CLI 與 daemon 版本必須一致；訊息維持 regression 原有契約。"""
    a = _VERSION_RE.search(cli or "")
    b = _VERSION_RE.search(daemon or "")
    if a is None or b is None:
        return f"版本解析失敗：cli={cli!r} daemon={daemon!r}（#154 gate）"
    va = tuple(map(int, a.groups()))
    vb = tuple(map(int, b.groups()))
    if va != vb:
        return (
            f"client↔daemon 版本不齊：cli={'.'.join(map(str, va))} "
            f"daemon={'.'.join(map(str, vb))}（#154 gate，suite-refuse）"
        )
    return None


def resolve_serialwrap_exe(cfg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """解析設定或 PATH 中的 CLI，並回報是否明確 pin 及其雜湊。"""
    configured_value = str(cfg.get("serialwrap_exe") or "").strip()
    configured = configured_value or None
    raw_path = os.path.expanduser(configured_value) if configured_value else shutil.which("serialwrap")
    if not raw_path:
        return "serialwrap", {"configured": configured, "resolved": None,
                               "sha256": None, "pinned": bool(configured)}
    resolved = os.path.realpath(raw_path)
    digest: str | None = None
    try:
        hasher = hashlib.sha256()
        with open(resolved, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
    except OSError:
        pass
    return resolved, {"configured": configured, "resolved": resolved,
                      "sha256": digest, "pinned": bool(configured)}


@dataclasses.dataclass(frozen=True)
class Capabilities:
    remote_capability: bool
    deployed_version: str
    docker: bool


def missing_capabilities(caps: Capabilities, *,
                         minimum: tuple[int, int, int] = MIN_DEPLOYED) -> dict[str, str]:
    missing: dict[str, str] = {}
    if not caps.remote_capability:
        missing["remote_capability"] = "remote_capability_missing"
    ver = parse_version(caps.deployed_version)
    if ver is None or ver < minimum:
        missing["deployed_recent"] = "deployed_daemon_stale"
    if not caps.docker:
        missing["docker"] = "docker_unavailable"
    return missing


def collect_capabilities(sw) -> Capabilities:
    remote_ok = bool(sw.run("remote", "status").get("ok"))
    version = sw.run("--version").get("_raw", "")
    docker_ok = False
    if shutil.which("docker"):
        try:
            docker_ok = _run(["docker", "info"], timeout=20).returncode == 0
        except (subprocess.SubprocessError, OSError):
            docker_ok = False
    return Capabilities(remote_capability=remote_ok, deployed_version=version, docker=docker_ok)


def evaluate(c: Checks) -> tuple[bool, list[str]]:
    problems: list[str] = []
    ok = True
    if c.git_behind > 0:
        problems.append(f"警告：本機落後 origin/main {c.git_behind} commits（不擋，但報告會記錄）")
    if not c.doctor_ok:
        ok = False
        problems.append("serialwrap doctor 未通過")
    missing = [b for b in c.boards_expected if b not in c.boards_ready]
    if missing:
        ok = False
        attributed = [b for b in missing if b in c.win_daemon_holds]
        plain = [b for b in missing if b not in c.win_daemon_holds]
        if plain:
            problems.append(f"板卡未 READY：{','.join(plain)}")
        if attributed:
            problems.append(
                f"板卡未 READY：{','.join(attributed)}（歸因 windows_daemon_holds_device："
                "Windows 端 serialwrapd 持有該裝置的 exclusive handle，usbipd 拒絕匯出；"
                "先於 Windows 端 `serialwrap.exe device release` 再重跑）")
    for t in c.tools_missing:
        ok = False
        problems.append(f"工具不可用：{t}")
    if c.leaked_daemons:
        ok = False
        problems.append(f"殘留 daemon：{','.join(c.leaked_daemons)}（先清理，見 checklist 前置B）")
    if c.other_pytest:
        ok = False
        problems.append("偵測到 pytest 執行中——本套件與單元測試互斥（#120 live guard 會誤判本套件操作為 FAIL）")
    if c.state_polluted:
        ok = False
        problems.append("live state.json 含 /tmp/sw-* 污染哨兵（先清理）")
    if not c.benchlock_ok:
        ok = False
        problems.append("benchlock：~/.local/state/serialwrap/bench.lock 被他者持有（另一場 reliability／wifi_llapi run？）——bench 互斥、整場拒跑")
    if c.external_testpilot:
        ok = False
        problems.append("偵測到進行中的外部 testpilot run（bench 互斥、整場拒跑）："
                        + "；".join(c.external_testpilot))
    if c.version_problem:
        ok = False
        problems.append(c.version_problem)
    if c.cross_os_guard == "held":
        ok = False
        problems.append(f"跨 OS bench 守衛：{c.cross_os_detail or '另一側仍持有本 bench 裝置'}")
    if "process_scan_unavailable" in (
        c.leaked_daemons + list(c.external_testpilot)
    ):
        ok = False
        problems.append("process_scan_unavailable：無法列舉行程，無法證明 bench 沒有衝突工作")
    return ok, problems


def _run(argv: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _git_behind(repo_root: Path) -> int:
    """git fetch 後回傳 HEAD..origin/main 落後 commit 數（失敗回 0）。"""
    try:
        _run(["git", "-C", str(repo_root), "fetch", "-q", "origin"], timeout=60)
        cp = _run(["git", "-C", str(repo_root), "rev-list", "--count", "HEAD..origin/main"])
        return int(cp.stdout.strip() or "0")
    except (subprocess.SubprocessError, ValueError, OSError):
        return 0


def _doctor_ok(sw) -> bool:
    """doctor 全綠判定；advisory 項（run_doctor 蓋章 `advisory: true`，如 #148 的
    shell/daemon WAL_DIR 不一致 WARN）不視為 FAIL——WARN 誤觸 suite-refuse 會讓
    有 legacy env export 的 bench 永遠拒跑。"""
    doc = sw.run("doctor")
    checks = doc.get("checks") or []
    return bool(checks) and all(
        c.get("ok", False) or c.get("advisory", False) for c in checks
    )


def _tools_missing(cfg: dict, *, os_name: str | None = None) -> list[str]:
    platform_name = os.name if os_name is None else os_name
    if platform_name == "nt" or platform_name.startswith("win"):
        # Windows 不具備 POSIX bench 工具；個別 case 由 capability SKIP gate 處理。
        return []
    missing: list[str] = []
    for tool in ("tmux", "minicom"):
        if not shutil.which(tool):
            missing.append(tool)
    if not Path(cfg["usbipd_exe"]).exists():
        missing.append("usbipd")
    try:
        if _run(["sudo", "-n", "true"]).returncode != 0:
            missing.append("sudo-nopasswd")
    except (subprocess.SubprocessError, OSError):
        missing.append("sudo-nopasswd")
    return missing


def _leaked_daemons() -> list[str]:
    """回傳殘留 throwaway/pytest-iso daemon；無法列舉時回報證據不足。"""
    processes = procobs.list_processes()
    if processes is None:
        return ["process_scan_unavailable"]
    out: list[str] = []
    for process in processes:
        if process.get("pid") == os.getpid():
            continue
        name = str(process.get("name") or "")
        cmdline = str(process.get("cmdline") or "")
        if re.search(r"sw-coexis|sw-pytest-iso", cmdline, re.I):
            out.append(f"{process.get('pid')} {cmdline}")
        elif not cmdline and re.fullmatch(r"serialwrapd(?:\.exe)?", name, re.I):
            out.append("process_scan_unavailable")
    return out


def _other_pytest() -> bool:
    """偵測其他 pytest 行程；排除目前 preflight 行程。"""
    processes = procobs.list_processes()
    if processes is None:
        return True
    me = os.getpid()
    for process in processes:
        if process.get("pid") == me:
            continue
        name = str(process.get("name") or "")
        cmdline = str(process.get("cmdline") or "")
        if re.search(r"pytest", cmdline, re.I) or (
            not cmdline and re.fullmatch(r"pytest(?:\.exe)?", name, re.I)
        ):
            return True
    return False


def resolve_state_dir(cfg: dict[str, Any], *, os_name: str | None = None) -> Path:
    """解析產品 XDG state 根目錄，允許 testbed 明確覆寫。"""
    configured = str(cfg.get("state_dir") or "").strip()
    if configured:
        return Path(os.path.expanduser(configured))
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(os.path.expanduser(base)) / "serialwrap"


def _state_polluted(cfg: dict[str, Any] | None = None, *, os_name: str | None = None) -> bool:
    """live state.json 的 bindings 值含 /tmp/ 污染哨兵。"""
    state = resolve_state_dir(cfg or {}, os_name=os_name) / "state.json"
    if not state.exists():
        return False
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    bindings = data.get("bindings") or {}
    return "/tmp/" in json.dumps(bindings, ensure_ascii=False)


class BenchLockConfigError(RuntimeError):
    """Windows bench 未提供跨工具共用的 lock path。"""


def bench_lock_path(cfg: dict[str, Any] | None = None, *, os_name: str | None = None) -> Path:
    """解析 Linux 共用 lock 預設值或 testbed 指定的跨 OS lock 路徑。"""
    values = cfg or {}
    configured = str(values.get("bench_lock_path") or "").strip()
    if configured:
        return Path(os.path.expanduser(configured))
    platform_name = os.name if os_name is None else os_name
    if platform_name == "nt" or platform_name.startswith("win"):
        raise BenchLockConfigError("Windows testbed 必須設定 bench_lock_path")
    return Path.home() / ".local/state/serialwrap/bench.lock"


def acquire_benchlock(lock_path: Path) -> int | None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        os.close(fd)
        return None
    return fd


def _external_testpilot() -> list[str]:
    """偵測其他 testpilot run；列舉失敗視為證據不足。"""
    processes = procobs.list_processes()
    if processes is None:
        return ["process_scan_unavailable"]
    me = os.getpid()
    out: list[str] = []
    for process in processes:
        cmdline = str(process.get("cmdline") or "")
        if process.get("pid") == me:
            continue
        name = str(process.get("name") or "")
        if not cmdline and re.fullmatch(r"testpilot(?:\.exe)?", name, re.I):
            out.append("process_scan_unavailable")
        elif re.search(r"(?:^|[\\/])?testpilot(?:\.exe)?[\"']?\s+ru(?:n)?\b",
                       cmdline, re.I):
            out.append(f"{process.get('pid')} {cmdline}")
    return out


def cross_os_holder_check(cfg: dict[str, Any], boards: list[str], *,
                          run: Callable[..., Any] = subprocess.run) -> tuple[str, str | None]:
    """探測另一 OS 是否仍持有本 bench 的任一實體裝置。"""
    argv = cfg.get("cross_os_probe_argv") or []
    if not argv:
        return "unconfigured", None
    try:
        cp = run(list(argv), capture_output=True, text=True, timeout=10)
        if getattr(cp, "returncode", 1) != 0:
            raise OSError((getattr(cp, "stderr", "") or "probe command failed").strip())
        payload = json.loads(getattr(cp, "stdout", "") or "")
        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            raise ValueError("probe JSON 必須包含 sessions 陣列")
        requested_boards = {str(item) for item in boards}
        bench_boards = [board for board in cfg.get("boards", [])
                        if not requested_boards or str(board.get("com")) in requested_boards]
        for session in sessions:
            state = str(session.get("state") or "").upper()
            if state in {"DETACHED", "RELEASED"}:
                continue
            com = str(session.get("com") or "")
            device = str(session.get("device_by_id") or session.get("device") or "")
            if not device:
                raise ValueError(f"無法核對 {com} 的裝置身分")
            mapped = False
            for board in bench_boards:
                serial = str(board.get("serial") or "")
                expected = str(board.get("device_by_id") or "")
                if not serial and not expected:
                    continue
                mapped = True
                if ((serial and serial.casefold() in device.casefold())
                        or (expected and expected.casefold() == device.casefold())):
                    return "held", (f"另一側 daemon 的 {com} 仍持有 bench 裝置 "
                                     f"{device}（state={state}）")
            if not mapped:
                raise ValueError(f"無法以序號或 device_by_id 對應 {com} 的裝置 {device}")
            raise ValueError(f"另一側 daemon 的 {com} 持有未對應 bench 裝置 {device}")
        return "clear", None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError) as exc:
        return "unknown", f"另一 OS probe 無法確認：{exc}"


def platform_missing_caps(*, os_name: str | None = None, frontend_ok: bool,
                          cfg: dict[str, Any] | None = None,
                          which: Callable[[str], str | None] = shutil.which) -> dict[str, str]:
    """回傳目前平台缺少的 case 能力，以 capability reason code 表示。"""
    platform_name = os.name if os_name is None else os_name
    if platform_name == "nt" or platform_name.startswith("win"):
        missing = {key: f"cap_missing:{key}" for key in
                   ("tmux", "posix_throwaway_daemon", "usbipd", "supervisor_runtime",
                    "posix_orphan_semantics")}
    else:
        missing: dict[str, str] = {}
        if not which("tmux"):
            missing["tmux"] = "cap_missing:tmux"
        settings = cfg or {}
        cli = str(settings.get("serialwrap_exe") or "").strip()
        if cli:
            cli_path = Path(os.path.expanduser(cli))
            if cli_path.parent == Path("."):
                resolved_cli = which(cli)
                daemon_path = Path(resolved_cli).with_name("serialwrapd") if resolved_cli else None
            else:
                daemon_path = cli_path.with_name("serialwrapd")
            daemon_available = bool(daemon_path and daemon_path.is_file())
        else:
            resolved_cli = which("serialwrap")
            daemon_path = Path(resolved_cli).with_name("serialwrapd") if resolved_cli else None
            daemon_available = bool(daemon_path and daemon_path.is_file()) or bool(which("serialwrapd"))
        if not daemon_available:
            missing["posix_throwaway_daemon"] = "cap_missing:posix_throwaway_daemon"
        usbipd = str(settings.get("usbipd_exe") or "").strip()
        if usbipd:
            usbipd_path = Path(os.path.expanduser(usbipd))
            usbipd_available = usbipd_path.is_file() if usbipd_path.parent != Path(".") else bool(which(usbipd))
        else:
            usbipd_available = bool(which("usbipd"))
        if not usbipd_available:
            missing["usbipd"] = "cap_missing:usbipd"
        if not which("systemctl"):
            missing["supervisor_runtime"] = "cap_missing:supervisor_runtime"
        try:
            frontend_kind = frontends.select_frontend_kind(settings, os_name=platform_name)
        except frontends.FrontendError:
            frontend_kind = "unavailable"
        if frontend_kind != "tmux":
            missing["posix_orphan_semantics"] = "cap_missing:posix_orphan_semantics"
    if not frontend_ok:
        missing["human_frontend"] = "cap_missing:human_frontend"
    return missing


def collect(cfg: dict, sw, repo_root, *, benchlock_ok: bool = True, win=None) -> Checks:
    """I/O 收集層——逐項落地，回傳 Checks；evaluate 由單測釘住。

    git_behind：git -C <repo_root> fetch -q origin && git rev-list --count HEAD..origin/main
    doctor_ok：sw.run("doctor") 解析各 check 全綠
    boards_ready：sw.sessions() 中 state==READY 的 com；boards_expected 來自 cfg["boards"]
    tools_missing：依作業系統檢查必要工具；平台專屬缺項交由 capability gate。
    leaked_daemons／pytest／testpilot：透過 procobs.list_processes() 觀測。
    state_polluted：依 resolve_state_dir(cfg) 讀取 state.json。
    """
    boards_expected = [b["com"] for b in cfg["boards"]]
    boards_ready = [s.get("com") for s in sw.sessions() if s.get("state") == "READY"]
    win_present = bool(win is not None and win.available())
    win_holds: list[str] = []
    if win_present:
        held = win.held_devices()
        for b in cfg["boards"]:
            if drivers.match_held_for_serial(held, b.get("serial", "")) is not None:
                win_holds.append(b["com"])
    return Checks(
        git_behind=_git_behind(Path(repo_root)),
        doctor_ok=_doctor_ok(sw),
        boards_ready=[c for c in boards_ready if c],
        boards_expected=boards_expected,
        tools_missing=_tools_missing(cfg),
        leaked_daemons=_leaked_daemons(),
        other_pytest=_other_pytest(),
        state_polluted=_state_polluted(cfg),
        benchlock_ok=benchlock_ok,
        external_testpilot=tuple(_external_testpilot()),
        win_daemon_present=win_present,
        win_daemon_holds=tuple(win_holds),
    )
