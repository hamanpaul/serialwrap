"""Slice 2.1 黑箱案例的 pytest 設定（P5-T20，紅燈階段）。

本檔只含 Task 20 新增的共用 fixture：
- `cred_daemon`：COM0 只回 `login: `、帳密環境變數未設 → `CREDENTIALS_UNRESOLVED`（SC-34、f10）
- `banner_target`：開機即送 U-Boot autoboot banner（AUTOBOOT_QUIET 情境）
- `profile_variant`：以 harness 的 `SHELL_TARGET_YAML` 為底，逐鍵替換產生變體 profile
- 單元 marker `cap_posix_pty` 註冊（Slice 2.1 以 `-m cap_posix_pty` 選取）

依賴（由 Task 15 harness 提供，本檔不重複定義）：`harness.start_daemon`、`harness.SHELL_TARGET_YAML`、
`slice21_daemon_factory`、`shell_daemon`、`artifact_identity`。所有依賴皆於 fixture 內延遲匯入，
使缺少 harness 時整個 `tests/` 仍可收集。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

import pytest

# P5-T15：本目錄為套件（__init__.py），pytest 不再把本目錄放進 sys.path；保留 `from harness import ...`
# 的頂層匯入形式（本檔與 Tasks 20～22 案例共用）。
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

UBOOT_BANNER: bytes = b"\r\nU-Boot 2024.01\r\nHit any key to stop autoboot:  3 \r\n"
UNSET_USER_ENV = "SW_T20_UNSET_USER"   # 測試環境不得設定；未設即 CREDENTIALS_UNRESOLVED
UNSET_PASS_ENV = "SW_T20_UNSET_PASS"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "cap_posix_pty: 需要 POSIX PTY pair（Slice 2.1 以 -m cap_posix_pty 選取）",
    )


@pytest.fixture
def profile_variant():
    """回傳 `make(**subs)`：以 SHELL_TARGET_YAML 為底替換同名頂層鍵值（單行 `key: value`）。

    不存在於底稿的鍵會插在第一行 `prompt_regex:` 之後（沿用其縮排）；底稿無 `prompt_regex:` 即失敗。
    """

    def _make(**subs: str) -> str:
        from harness import SHELL_TARGET_YAML  # Task 15 harness

        lines = SHELL_TARGET_YAML.splitlines()
        out: list[str] = []
        seen: set[str] = set()
        for line in lines:
            body = line.lstrip()
            key = body.split(":", 1)[0]
            if key in subs and ":" in body:
                indent = line[: len(line) - len(body)]
                out.append(f"{indent}{key}: {subs[key]}")
                seen.add(key)
            else:
                out.append(line)
        missing = [k for k in subs if k not in seen]
        if missing:
            anchor = next((i for i, ln in enumerate(out) if ln.lstrip().startswith("prompt_regex:")), None)
            if anchor is None:
                raise AssertionError("SHELL_TARGET_YAML 缺少 prompt_regex 錨點，無法產生 profile 變體")
            indent = out[anchor][: len(out[anchor]) - len(out[anchor].lstrip())]
            for k in missing:
                anchor += 1
                out.insert(anchor, f"{indent}{k}: {subs[k]}")
        return "\n".join(out) + "\n"

    return _make


@pytest.fixture
def cred_daemon(tmp_path: Path, profile_variant):
    """COM0 以 login 模式啟動，帳密環境變數未設；FakeTarget 只回 `login: `。"""
    from harness import start_daemon  # Task 15 harness

    profile = profile_variant(user_env=UNSET_USER_ENV, pass_env=UNSET_PASS_ENV)
    d = start_daemon(tmp_path / "cred", login=True, profile_yaml=profile)
    try:
        yield d
    finally:
        d.stop()


@pytest.fixture
def banner_target(tmp_path: Path):
    """開機即送 U-Boot autoboot banner（之後才是 shell prompt）的 COM0。"""
    from harness import start_daemon  # Task 15 harness

    d = start_daemon(tmp_path / "banner", login=False, boot_banner=UBOOT_BANNER)
    try:
        yield d
    finally:
        d.stop()


# ---------------------------------------------------------------------------
# P5-T15：harness fixtures（python_daemon_exe／slice21_daemon_factory／artifact_identity／shell_daemon）。
# ---------------------------------------------------------------------------


@pytest.fixture
def python_daemon_exe() -> list[str]:
    """Python 基線 daemon（arch §9.2；P3 交付的 wrapper）。"""
    root = Path(__file__).resolve().parents[3]
    if os.name == "nt":
        return [sys.executable, str(root / "serialwrapd.py")]
    return [str(root / "tests" / "contract_blackbox" / "bin" / "py-serialwrapd")]


@pytest.fixture
def artifact_identity() -> dict:
    """受測 artifact 身分；harness.start_daemon 於每次啟動後以 /proc/<pid>/exe 交叉檢查（不符即 pytest.fail）。"""
    from bb_harness import ab_label, cli_prefix, daemon_prefix

    return {"cli": cli_prefix(), "daemon": daemon_prefix(), "label": ab_label()}


@pytest.fixture
def slice21_daemon_factory(tmp_path: Path, request) -> Callable[..., object]:
    """回傳 `make(root, *, daemon=None, reuse_dirs=False, profile_yaml=…, prompt=…, extra_env=None, **kw)`；teardown 一律 stop。"""
    from harness import SHELL_TARGET_YAML, start_daemon

    made: list = []

    def _make(root: Path, *, daemon: list[str] | None = None, reuse_dirs: bool = False,
              profile_yaml: str = SHELL_TARGET_YAML, prompt: str = "root@t:/# ",
              extra_env: dict[str, str] | None = None, **kw):
        d = start_daemon(Path(root), daemon=daemon, reuse_dirs=reuse_dirs, profile_yaml=profile_yaml,
                         prompt=prompt, extra_env=extra_env, **kw)
        made.append(d)
        return d

    yield _make
    for d in reversed(made):
        try:
            d.stop()
        except Exception:
            pass


@pytest.fixture
def shell_daemon(slice21_daemon_factory, tmp_path: Path):
    """COM0 已 READY 的受測 daemon。"""
    d = slice21_daemon_factory(tmp_path / "shell")
    d.wait_state("COM0", "READY")
    return d
