"""P7 Task 18（08-slice23-login-events.md）：Quiet gate／Ready Reconfirm 黑箱（外部 CLI＋FakeTarget 觀測）。

本輪承接 `test_rust_quiet_mid_login_aborts_sc30`（case_id `sc30-quiet-mid-login-aborts`，SC-30，OD-35）；
其餘計畫列（spontaneous reboot、attach during quiet、bootloader tail D1、attached bootloader stuck）待下一輪。
stdlib＋PyYAML＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import time

import pytest

from bb_faketarget import FakeTarget
from bb_harness import is_rust
from bb_p7 import wait_state

pytestmark = [pytest.mark.posix_only]

BANNER = "\r\nU-Boot 2024.04 (Jan 01 2024 - 00:00:00 +0000)\r\nHit any key to stop autoboot:  3"


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_rust_quiet_mid_login_aborts_sc30(p7, tmp_path):
    """attach 登入途中（送出帳號後）target 輸出 U-Boot banner：Rust 立即中止 run——banner 之後 target 收到 0 byte
    （secret 不得送出），session 收斂 ATTACHED＋`PROMPT_UNAVAILABLE`。SC-30 為 Rust-only（cases.yaml 平台限定），
    Python 基線不執行。"""
    if not is_rust():
        pytest.skip("SC-30 Rust-only")
    tgt = FakeTarget(
        tmp_path / "t0",
        login_steps=[
            {"expect": "", "respond": "\r\nfake login: "},
            {"expect": "root", "respond": BANNER},
        ],
    )
    p7.start("shell", [tgt])
    deadline = time.monotonic() + 20.0
    while b"root\n" not in tgt.rx_log() and b"root\r" not in tgt.rx_log():
        assert time.monotonic() < deadline, f"daemon 未送出帳號：{tgt.rx_log()!r}"
        time.sleep(0.05)
    s = wait_state(p7.env, "COM0", "ATTACHED", timeout_s=15.0)
    assert s["last_error"] == "PROMPT_UNAVAILABLE", s
    assert s.get("last_error_detail") is None, s
    time.sleep(3.0)   # 跨過原 login 步驟的等待期間
    log = tgt.rx_log()
    after_user = log.split(b"root", 1)[1]
    assert after_user in (b"\n", b"\r", b"\r\n"), f"banner 之後不得再送任何 byte（含 secret）：{log!r}"
    assert tgt.password.encode() not in log
    s = wait_state(p7.env, "COM0", "ATTACHED", timeout_s=2.0)
    assert s["last_error"] == "PROMPT_UNAVAILABLE", s
