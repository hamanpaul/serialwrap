"""preflight：收集（I/O）與判定（純函式）分離，判定可單測。"""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
from pathlib import Path


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
        problems.append(f"板卡未 READY：{','.join(missing)}")
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


def collect(cfg: dict, sw, repo_root) -> Checks:
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
    return Checks(
        git_behind=_git_behind(Path(repo_root)),
        doctor_ok=_doctor_ok(sw),
        boards_ready=[c for c in boards_ready if c],
        boards_expected=boards_expected,
        tools_missing=_tools_missing(cfg),
        leaked_daemons=_leaked_daemons(),
        other_pytest=_other_pytest(),
        state_polluted=_state_polluted(),
    )
