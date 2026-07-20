"""preflight：收集（I/O）與判定（純函式）分離，判定可單測。"""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import drivers


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


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
MIN_DEPLOYED: tuple[int, int, int] = (0, 2, 3)


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


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
    doc = sw.run("doctor")
    checks = doc.get("checks") or []
    return bool(checks) and all(c.get("ok", False) for c in checks)


def _tools_missing(cfg: dict) -> list[str]:
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
    """pgrep character-class 防 self-match；回傳殘留 throwaway/pytest-iso daemon 行。"""
    cp = _run(["pgrep", "-af", "sw-coexis[t]|sw-pytest-iso"])
    return [ln for ln in cp.stdout.splitlines() if ln.strip()]


def _other_pytest() -> bool:
    """偵測其他 pytest 行程（character-class 防 self-match，排除自身 pid）。"""
    cp = _run(["pgrep", "-af", "pytes[t]"])
    me = os.getpid()
    for ln in cp.stdout.splitlines():
        parts = ln.split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid != me:
            return True
    return False


def _state_polluted() -> bool:
    """live state.json 的 bindings 值含 /tmp/ 污染哨兵。"""
    state = Path.home() / ".local/state/serialwrap/state.json"
    if not state.exists():
        return False
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    bindings = data.get("bindings") or {}
    return "/tmp/" in json.dumps(bindings, ensure_ascii=False)


def bench_lock_path() -> Path:
    return Path.home() / ".local/state/serialwrap/bench.lock"


def acquire_benchlock(lock_path: Path) -> int | None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _external_testpilot() -> list[str]:
    cp = _run(["pgrep", "-af", r"testpilot ru[n]"])
    me = os.getpid()
    out: list[str] = []
    for ln in cp.stdout.splitlines():
        parts = ln.split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid != me and ln.strip():
            out.append(ln)
    return out


def collect(cfg: dict, sw, repo_root, *, benchlock_ok: bool = True, win=None) -> Checks:
    """I/O 收集層——逐項落地，回傳 Checks；evaluate 由單測釘住。

    git_behind：git -C <repo_root> fetch -q origin && git rev-list --count HEAD..origin/main
    doctor_ok：sw.run("doctor") 解析各 check 全綠
    boards_ready：sw.sessions() 中 state==READY 的 com；boards_expected 來自 cfg["boards"]
    tools_missing：shutil.which("tmux"/"minicom")、Path(cfg["usbipd_exe"]).exists()、sudo -n true
    leaked_daemons：pgrep -af 'sw-coexis[t]|sw-pytest-iso'（character class 防 self-match）
    other_pytest：pgrep -af 'pytes[t]'（排除自身 pid）
    state_polluted：讀 ~/.local/state/serialwrap/state.json，bindings 值含 "/tmp/" 即 True
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
        state_polluted=_state_polluted(),
        benchlock_ok=benchlock_ok,
        external_testpilot=tuple(_external_testpilot()),
        win_daemon_present=win_present,
        win_daemon_holds=tuple(win_holds),
    )
