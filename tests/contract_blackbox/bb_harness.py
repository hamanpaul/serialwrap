"""黑箱契約 harness 公開介面。

提供黑箱測試所需的 CLI 執行、環境隔離、A/B 選擇器解析與跨平台輸出處理。
stdlib-only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

NL: bytes = b"\r\n" if os.name == "nt" else b"\n"

# 本行程內 isolated_env 登記的 (隔離根, env)；conftest 於每個案例 call 階段結束時據以記錄 config_digest
# （見 config_digest.py）。env 保存同一物件參考，夾具之後的修改會反映在摘要中。
ISOLATED_ROOTS: list[tuple[Path, dict[str, str]]] = []


@dataclass(frozen=True)
class CliResult:
    rc: int
    stdout: bytes
    stderr: bytes


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def ab_env() -> dict[str, str]:
    try:
        from ab_env import AB_ENV
        return dict(AB_ENV)
    except ImportError:
        tests_dir = str(repo_root() / "tests")
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        try:
            from ab_env import AB_ENV
            return dict(AB_ENV)
        except ImportError:
            keys = ("SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE", "SERIALWRAP_AB_LABEL", "SW_BASELINE_PYTHON")
            return {k: os.environ[k] for k in keys if k in os.environ}


def ab_label() -> str:
    return ab_env().get("SERIALWRAP_AB_LABEL", "python-baseline")


def is_rust() -> bool:
    return ab_label().startswith("rust")


def python_baseline_prefix() -> list[str]:
    root = repo_root()
    if os.name == "nt":
        baseline = ab_env().get("SW_BASELINE_PYTHON") or sys.executable
        return [baseline, str(root / "serialwrap.py")]
    return [str(root / "serialwrap")]


def python_baseline_interpreter() -> list[str]:
    if os.name == "nt":
        return [ab_env().get("SW_BASELINE_PYTHON") or sys.executable]
    py = shutil.which("python3")
    return [py if py else sys.executable]


def cli_prefix() -> list[str]:
    cli = ab_env().get("SERIALWRAP_CLI_EXE")
    if cli == "/bin/serialwrap" and not Path("/bin/serialwrap").exists():
        sw_bundle = os.environ.get("SW_BUNDLE")
        if sw_bundle:
            cli = str(Path(sw_bundle) / "bin" / "serialwrap")
    if not cli:
        return python_baseline_prefix()
    if cli.endswith(".py"):
        interpreter = ab_env().get("SW_BASELINE_PYTHON") if os.name == "nt" else None
        return [interpreter or sys.executable, cli]
    return [cli]


def daemon_prefix() -> list[str]:
    d = ab_env().get("SERIALWRAP_DAEMON_EXE")
    if d:
        if d.endswith(".py"):
            return [sys.executable, d]
        return [d]
    if os.name == "nt":
        return [sys.executable, str(repo_root() / "serialwrapd.py")]
    return [str(repo_root() / "tests" / "contract_blackbox" / "bin" / "py-serialwrapd")]


def isolated_env(tmp: Path, **overrides: str | None) -> dict[str, str]:
    e = dict(os.environ)
    # 先刪除外層 env 中所有 SERIALWRAP_ 開頭的變數
    for k in list(e.keys()):
        if k.startswith("SERIALWRAP_"):
            del e[k]
    # SC-28（P6 Task 0，07 D-11）：console backend 選擇 env 明列於刪除清單，前綴規則日後若放寬仍不得穿透
    for k in ("SERIALWRAP_CONSOLE_BACKEND",):
        e.pop(k, None)

    # 設置 9 個目錄變數
    dirs = {
        "SERIALWRAP_STATE_DIR": tmp / "state",
        "SERIALWRAP_RUN_DIR": tmp / "run",
        "SERIALWRAP_WAL_DIR": tmp / "wal",
        "SERIALWRAP_CONFIG_DIR": tmp / "config",
        "SERIALWRAP_LOG_DIR": tmp / "blog",
        "SERIALWRAP_EVENTS_DIR": tmp / "events.d",
        "SERIALWRAP_EVENTS_RUNTIME_DIR": tmp / "events-rt",
        "SERIALWRAP_BY_ID_DIR": tmp / "by-id",
        "SERIALWRAP_BY_PATH_DIR": tmp / "by-path",
    }
    for var, p in dirs.items():
        p.mkdir(parents=True, exist_ok=True)
        e[var] = str(p)

    # 設置 HOME、XDG 目錄與環境變數
    (tmp / "home").mkdir(parents=True, exist_ok=True)
    (tmp / "xdg-config").mkdir(parents=True, exist_ok=True)
    (tmp / "xdg-state").mkdir(parents=True, exist_ok=True)
    e["HOME"] = str(tmp / "home")
    if os.name == "nt":
        e["USERPROFILE"] = str(tmp / "home")
    e["XDG_CONFIG_HOME"] = str(tmp / "xdg-config")
    e["XDG_STATE_HOME"] = str(tmp / "xdg-state")
    for k in (
        "XDG_RUNTIME_DIR",
        "XDG_DATA_HOME",
        "PYTHONUNBUFFERED",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PYTHONLEGACYWINDOWSSTDIO",
        "PYTHONWARNINGS",
        "PYTHONDEVMODE",
        "PYTHONFAULTHANDLER",
        "HOMEDRIVE",
        "HOMEPATH",
    ):
        e.pop(k, None)
    e["COLUMNS"] = "80"
    e["LANG"] = "C.UTF-8"
    e["LC_ALL"] = "C.UTF-8"
    e["PYTHONDONTWRITEBYTECODE"] = "1"

    # overrides 處理：值為 None 則刪除該 key，否則覆寫
    for k, v in overrides.items():
        if v is None:
            e.pop(k, None)
        else:
            e[k] = str(v)
    ISOLATED_ROOTS.append((tmp, e))
    return e


def run_cli(
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    stdin: bytes | None = None,
    timeout_s: float = 60.0,
    merge_stderr: bool = False,
    cwd: Path | None = None,
) -> CliResult:
    cmd = cli_prefix() + list(args)
    stderr_target = subprocess.STDOUT if merge_stderr else subprocess.PIPE
    proc = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL if stdin is None else None,
        input=stdin,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=stderr_target,
        timeout=timeout_s,
        cwd=str(cwd) if cwd else None,
    )
    return CliResult(
        rc=proc.returncode,
        stdout=proc.stdout or b"",
        stderr=b"" if merge_stderr else (proc.stderr or b""),
    )


def stdout_lines(r: CliResult) -> list[str]:
    text = r.stdout.decode("utf-8", errors="replace")
    nl = "\r\n" if os.name == "nt" else "\n"
    if not text:
        return []
    if text.endswith(nl):
        text = text[:-len(nl)]
    return text.split(nl)
