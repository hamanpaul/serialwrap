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

from pathlib import Path

import pytest

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
