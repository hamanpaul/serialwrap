"""Slice 2.2 console 黑箱案例的 pytest 設定（07 Task 11）。

以 06 `slice21/harness.py` 的 `start_daemon` 與 P3 `bb_faketarget.FakeTarget` 為基礎；同一批案例可對 Python 基線
與 Rust artifact（`SERIALWRAP_AB_LABEL=rust*`、`SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE`）執行。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SLICE21 = str(Path(__file__).resolve().parents[1] / "slice21")
if _SLICE21 not in sys.path:
    sys.path.insert(0, _SLICE21)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "cap_posix_pty: 需要 POSIX PTY pair")


# 07 Task 11：console_daemon 的 FakeTarget 腳本（`sleep 2` 延遲回覆、`bootcmd` 回 U-Boot banner 後不回 prompt）
CONSOLE_SCRIPTS: dict[bytes, tuple] = {
    b"sleep 2": (2.0, b"sleep 2: ok\r\n"),
    b"bootcmd": (0.5, b"U-Boot 2023.01\r\n", False),
}


def _start(
    root: Path,
    *,
    profile_yaml: str | None = None,
    second_board: bool = False,
    extra_env: dict[str, str] | None = None,
    **target_attrs,
):
    """以 06 `start_daemon` 啟動，並在 FakeTarget start 前套用 P6 追加能力（`scripts`／`ready`）。

    不改 06 harness：暫時包裝 `harness._make_target`，於回傳前設定屬性（FakeTarget 於執行期讀取）。
    """
    import harness
    from harness import start_daemon

    orig = harness._make_target
    extra: list = []

    def _make(workdir, *args, **kwargs):
        target = orig(workdir, *args, **kwargs)
        for key, value in target_attrs.items():
            setattr(target, key, value)
        if second_board:
            # 第二塊板：同一 by-id 目錄、連結名為第一塊加 `-2`（profile 以 `<placeholder>-2` 指向它）
            board2 = orig(Path(str(workdir) + "-2"), *args, **kwargs)
            first_start = target.start

            def _start_both(by_id_dir, name=harness.BY_ID_NAME):
                link = first_start(by_id_dir, name)
                board2.start(by_id_dir, name + "-2")
                return link

            target.start = _start_both
            extra.append(board2)
        return target

    kwargs: dict = {} if profile_yaml is None else {"profile_yaml": profile_yaml}
    if extra_env:
        kwargs["extra_env"] = extra_env
    harness._make_target = _make
    try:
        d = start_daemon(root, **kwargs)
    except BaseException:
        for t in extra:
            t.stop()
        raise
    finally:
        harness._make_target = orig
    if extra:
        d.targets["COM1"] = extra[0]
    return d


def _stop(d) -> None:
    try:
        d.stop()
    except Exception:
        pass
    for com, target in d.targets.items():
        if com != "COM0":
            target.stop()


@pytest.fixture
def console_daemon(tmp_path: Path):
    """COM0 已 READY 的受測 daemon；yield `(daemon, FakeTarget)`，teardown 一律 stop。"""
    d = _start(tmp_path / "console", scripts=dict(CONSOLE_SCRIPTS))
    try:
        d.wait_state("COM0", "READY")
        yield d, d.targets["COM0"]
    finally:
        _stop(d)


@pytest.fixture
def unready_daemon(tmp_path: Path):
    """FakeTarget(ready=False)：ready nonce 永遠等不到，COM0 停在 ATTACHED；yield `(daemon, FakeTarget)`。"""
    from harness import CRED_PASS_ENV, CRED_USER_ENV, SHELL_TARGET_YAML

    # 不宣告帳密：Python 走 probe_ready，失敗停 ATTACHED（宣告帳密走 ensure_ready，失敗轉 DETACHED，
    # `session_manager.py:4748-4781`）
    yaml = "".join(
        line
        for line in SHELL_TARGET_YAML.splitlines(keepends=True)
        if CRED_USER_ENV not in line and CRED_PASS_ENV not in line
    )
    d = _start(tmp_path / "unready", profile_yaml=yaml, ready=False)
    try:
        d.wait_state("COM0", "ATTACHED", timeout=30.0)
        yield d, d.targets["COM0"]
    finally:
        _stop(d)


@pytest.fixture
def two_board_daemon(tmp_path: Path):
    """COM0、COM1 兩塊 FakeTarget 皆 READY 的受測 daemon；yield daemon（`d.targets` 含兩者）。"""
    from harness import DEVICE_PLACEHOLDER, SHELL_TARGET_YAML

    yaml = SHELL_TARGET_YAML + f"  - profile: t\n    com: COM1\n    device_by_id: {DEVICE_PLACEHOLDER}-2\n"
    d = _start(tmp_path / "two", profile_yaml=yaml, second_board=True)
    try:
        d.wait_state("COM0", "READY")
        d.wait_state("COM1", "READY")
        yield d
    finally:
        _stop(d)


@pytest.fixture
def telnet_daemon(tmp_path: Path):
    """SC-28 `SERIALWRAP_CONSOLE_BACKEND=tcp`：human console 走 127.0.0.1 Telnet listener；yield `(daemon, FakeTarget)`。"""
    d = _start(tmp_path / "telnet", extra_env={"SERIALWRAP_CONSOLE_BACKEND": "tcp"})
    try:
        d.wait_state("COM0", "READY")
        yield d, d.targets["COM0"]
    finally:
        _stop(d)
