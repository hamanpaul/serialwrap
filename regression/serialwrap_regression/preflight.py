"""preflight：重用 realhw 收集件＋回歸 plugin 專屬 gate（client↔daemon 版本對齊，#154）。

工具檢查裁剪為 tmux/minicom（本 plugin 無 usbipd／sudo 需求）；benchlock 與
reliability／wifi_llapi 共用同一把（bench 互斥、整場拒跑）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    root = Path(__file__).resolve().parents[2]
    if not (root / "realhw" / "preflight.py").is_file():
        raise RuntimeError(
            f"serialwrap_regression 僅支援 editable 安裝；REPO_ROOT={root} 下找不到 realhw/。"
            "請從 repo root 執行 pip install -e regression/"
        )
    return root


REPO_ROOT: Path = _repo_root()


def ensure_realhw_importable() -> Path:
    import sys

    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return REPO_ROOT


def version_gate(cli_version: str, daemon_version: str) -> str | None:
    """pinned CLI 與 daemon 版本必須一致；解析失敗或不齊回問題字串（suite-refuse）。"""
    ensure_realhw_importable()
    from realhw.preflight import parse_version

    a, b = parse_version(cli_version), parse_version(daemon_version)
    if a is None or b is None:
        return f"版本解析失敗：cli={cli_version!r} daemon={daemon_version!r}（#154 gate）"
    if a != b:
        return (
            f"client↔daemon 版本不齊：cli={'.'.join(map(str, a))} "
            f"daemon={'.'.join(map(str, b))}（#154 gate，suite-refuse）"
        )
    return None


def stale_client_note(path_version: str, pinned_version: str) -> str | None:
    """PATH 上 serialwrap 與 pinned 不一致時的診斷 note（不擋——本 plugin 不用 PATH）。"""
    ensure_realhw_importable()
    from realhw.preflight import parse_version

    a, b = parse_version(path_version), parse_version(pinned_version)
    if a is None or b is None or a == b:
        return None
    return (
        f"警告：PATH 上 serialwrap={'.'.join(map(str, a))} 與 pinned "
        f"{'.'.join(map(str, b))} 不一致（不擋；#154 stale client 徵兆）"
    )


def daemon_version_probe(sw: Any) -> str:
    """從 daemon pid 的 cmdline 找到其 venv python，以 importlib.metadata 取套件版本。"""
    pid = sw.run("daemon", "status").get("pid")
    if not pid:
        return ""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        python = cmdline[0].decode()
    except (OSError, IndexError, UnicodeDecodeError):
        return ""
    cp = subprocess.run(
        [python, "-c", "import importlib.metadata as m; print(m.version('serialwrap'))"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return (cp.stdout or "").strip()


def _tools_missing() -> list[str]:
    return [t for t in ("tmux", "minicom") if not shutil.which(t)]


def run_preflight(cfg: dict[str, Any]) -> dict[str, Any]:
    """收集＋判定；回 {ok, problems, notes, missing_caps, deployed_version, benchlock_fd}。"""
    ensure_realhw_importable()
    from realhw import drivers, preflight as rp

    exe = str(cfg["serialwrap_exe"])
    sw = drivers.SwCli(exe=exe)
    lock_fd = rp.acquire_benchlock(rp.bench_lock_path())
    try:
        boards_expected = [b["com"] for b in cfg["boards"]]
        boards_ready = [
            s.get("com") for s in sw.sessions() if s.get("state") == "READY" and s.get("com")
        ]
        checks = rp.Checks(
            git_behind=rp._git_behind(REPO_ROOT),
            doctor_ok=rp._doctor_ok(sw),
            boards_ready=boards_ready,
            boards_expected=boards_expected,
            tools_missing=_tools_missing(),
            leaked_daemons=rp._leaked_daemons(),
            other_pytest=rp._other_pytest(),
            state_polluted=rp._state_polluted(),
            benchlock_ok=lock_fd is not None,  # 注入實際結果（跨-plan 簽章教訓：勿留預設）
            external_testpilot=tuple(rp._external_testpilot()),
        )
        ok, problems = rp.evaluate(checks)

        cli_version = str(sw.run("--version").get("_raw", "")).strip()
        gate = version_gate(cli_version, daemon_version_probe(sw))
        if gate is not None:
            ok = False
            problems.append(gate)

        notes: list[str] = []
        path_exe = shutil.which("serialwrap")
        if path_exe and os.path.realpath(path_exe) != os.path.realpath(exe):
            path_version = str(
                drivers.SwCli(exe=path_exe).run("--version").get("_raw", "")
            ).strip()
            note = stale_client_note(path_version, cli_version)
            if note:
                notes.append(note)

        return {
            "ok": bool(ok),
            "problems": list(problems),
            "notes": notes,
            "missing_caps": {},
            "deployed_version": cli_version,
            "benchlock_fd": lock_fd,
        }
    except Exception:
        if lock_fd is not None:
            os.close(lock_fd)
        raise
