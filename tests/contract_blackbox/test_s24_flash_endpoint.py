"""P8 Task 9（09-slice24-transfer-flash.md）：`/dev/ttyMCU` 端點黑箱（Python 基線與 Rust artifact 同檔各跑一次）。

案例（tests/acceptance/cases.yaml，Slice 2.4 凍結時已登記的節點）：
- `s24-flash-silent-unbridged`：未 bridge 時端點零輸出、MCU 線零 TX（CLAUDE.md MCU 坑 1）；
- `s24-flash-sync-replay`：只轉送 flasher 自己的 sync、回放 MCU ACK 一次（坑 2；no_auto_resend）；
- `s24-flash-holder-gone`：flasher 關閉後以 holder-probe 結束 pump 回 ATTACHED，同一 daemon 可連續燒錄（坑 3、R2-F5）；
- `s24-flash-baud-mirror`：flash 開始時鏡射 flasher 的 baud 一次。

外部 oracle：「送了什麼給 MCU」以 `fake_mcu.rx_log()` 判定，「送了什麼給 flasher」以 flasher 實際讀到的 bytes 判定；
WAL 只供關聯（source=flash）。profile 為 `platform: passthrough`（非 command_capable）。throwaway daemon 的 by-id 目錄只放
fake MCU 線（`SwEnv` 的隔離 `SERIALWRAP_BY_ID_DIR`／`SERIALWRAP_BY_PATH_DIR`）。
stdlib＋pytest＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import os
import select
import time

import pytest

from p8_fixtures import FakeFlasher

pytestmark = [pytest.mark.posix_only]

SYNC = b"\x55\x55"
ACK = b"\x00\xcc"
PACKET = b"\x03\x10\x20"


def _link(sw_env):
    return sw_env.run_dir / "dev" / "ttyMCU"


def _start_attached(sw_env, fake_mcu, **kw) -> None:
    sw_env.start(targets={"COM0": fake_mcu}, profile="passthrough", **kw)
    sw_env.wait_state("COM0", "ATTACHED", timeout=10)
    link = _link(sw_env)
    sw_env.wait_until(link.is_symlink, timeout=10)


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_ttymcu_silent_when_unbridged(sw_env, fake_mcu):
    import tty

    _start_attached(sw_env, fake_mcu)
    link = _link(sw_env)
    fd = os.open(str(link), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        tty.setraw(fd)
        r, _, _ = select.select([fd], [], [], 2.5)
        assert r == [], "未 bridge：端點不得主動寫任何 bytes"
    finally:
        os.close(fd)
    assert fake_mcu.rx_log() == b"", "未 bridge：MCU 線零 TX"
    assert sw_env.session("COM0")["state"] == "ATTACHED"


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_sync_replay_pump_and_holder_gone(sw_env, fake_mcu):
    _start_attached(sw_env, fake_mcu)
    fl = FakeFlasher(_link(sw_env), baud=115200)
    try:
        fl.write(SYNC)
        assert fl.read_exact(2, timeout=3.0) == ACK
        assert fake_mcu.rx_log().count(SYNC) == 1, "double-sync 會吃掉 ACK"
        assert sw_env.session("COM0")["state"] == "FLASHING"
        st = sw_env.rpc("mcu.status", {})
        assert st["flashing"] is True and st["flashing_com"] == "COM0"
        fl.write(PACKET)
        sw_env.wait_until(lambda: fake_mcu.rx_log().endswith(PACKET), timeout=3)
        assert fl.read_exact(2, timeout=3.0) == ACK, "封包 ACK 經 RX observer 轉回 flasher"
        time.sleep(1.2)                    # 讓 holder-probe 至少 armed 一次（poll 0.5 s）
    finally:
        fl.close()
    sw_env.wait_until(lambda: sw_env.session("COM0")["state"] == "ATTACHED", timeout=5)
    st = sw_env.rpc("mcu.status", {})
    assert (st["flashing"], st["flashing_com"]) == (False, None)
    assert st["last_detect"] == {"status": "matched", "com": "COM0", "family": "ti-cc26xx", "hits": []}
    assert fake_mcu.rx_log() == SYNC + PACKET, "MCU 線只收到 flasher 的 sync 與封包（不重送 first）"
    tx = sw_env.wal_tx("COM0")
    assert tx and all(r["source"] == "flash" for r in tx)


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_baud_mirrored_once_from_flasher(sw_env, fake_mcu):
    import termios

    _start_attached(sw_env, fake_mcu, uart_baud=115200)
    assert fake_mcu.current_speed() == termios.B115200
    fl = FakeFlasher(_link(sw_env), baud=57600)
    try:
        fl.write(SYNC)
        assert fl.read_exact(2, timeout=3.0) == ACK
        sw_env.wait_until(lambda: fake_mcu.current_speed() == termios.B57600, timeout=3)
        fl.set_baud(9600)
        fl.write(b"\x00")
        sw_env.wait_until(lambda: fake_mcu.rx_log().endswith(b"\x00"), timeout=3)
        time.sleep(0.5)
        assert fake_mcu.current_speed() == termios.B57600, "只在 flash 開始時鏡射一次"
    finally:
        fl.close()


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_two_flashes_same_daemon(sw_env, fake_mcu):
    """R2-F5：第一次 flash 正常結束（holder gone）後，同一 daemon 的常駐端點仍能認線並回放 ACK。"""
    _start_attached(sw_env, fake_mcu)
    link = _link(sw_env)
    for round_no in (1, 2):
        fl = FakeFlasher(link, baud=115200)
        try:
            fl.write(SYNC)
            assert fl.read_exact(2, timeout=3.0) == ACK, f"第 {round_no} 次 ACK 回放"
            assert sw_env.session("COM0")["state"] == "FLASHING"
            fl.write(PACKET)
            sw_env.wait_until(lambda: fake_mcu.rx_log().endswith(PACKET), timeout=3)
            # 讀走封包 ACK：daemon 持 slave keepalive，未讀的 bytes 會留在 tty 輸入佇列，下一個 flasher 開啟後先讀到舊 ACK。
            assert fl.read_exact(2, timeout=3.0) == ACK, f"第 {round_no} 次封包 ACK 轉回 flasher"
            time.sleep(1.2)                # holder-probe armed
        finally:
            fl.close()
        sw_env.wait_until(lambda: sw_env.session("COM0")["state"] == "ATTACHED", timeout=5)
        assert link.is_symlink(), f"第 {round_no} 次結束後端點 link 仍在"
        assert sw_env.rpc("mcu.status", {})["flashing"] is False
    assert fake_mcu.rx_log().count(SYNC) == 2, "兩次 sync 各轉送一次"
    assert fake_mcu.rx_log().count(PACKET) == 2
