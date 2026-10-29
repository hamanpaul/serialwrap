"""P7 Task 18（08-slice23-login-events.md）：transport stall（#150）黑箱。

READY 後 FakeTarget `freeze_rx()`（模擬 read-endpoint stall：TX 照收、0 byte 輸出）逾 30 s，`session recover`
之後的 probe 全程零 RX → `last_error:"TRANSPORT_STALL"`、`last_error_detail` 以 `零 RX ` 開頭、WAL META
`transport_stall_suspected` 一筆（一次性告警）；RX 恢復重臂後再 stall → 共兩筆。

Python 基線實測（2026-10-29）：READY 的 recover 先跑救援按鍵回 `PROMPT_TIMEOUT`，隨後 reattach 的 probe
失敗落 DETACHED＋`TRANSPORT_STALL` 並持續 DETACHED↔ATTACHING 重試，故以輪詢 `session list` 觀察 last_error。
stdlib＋PyYAML＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import time

import pytest

from bb_faketarget import FakeTarget
from bb_p7 import wait_state

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]


def _row(p7, sel: str = "COM0") -> dict:
    rc, j = p7.cli("session", "list")
    assert rc == 0 and j, (rc, j)
    return [x for x in j["sessions"] if x["com"] == sel][0]


def _stall_metas(p7) -> int:
    return sum(
        1 for r in p7.wal_records("COM0", limit=2000)
        if r.get("dir") == "META" and (r.get("meta") or {}).get("event") == "transport_stall_suspected"
    )


def _stall_and_observe(p7, tgt, want_metas: int) -> None:
    tgt.freeze_rx()
    time.sleep(35.0)
    rc, j = p7.cli("session", "recover", "--selector", "COM0", timeout_s=90.0)
    assert rc == 2 and (j["error_code"], j["partial"], j["recovery_action"]) == ("PROMPT_TIMEOUT", True, "NONE"), (rc, j)
    deadline = time.monotonic() + 40.0
    s = _row(p7)
    while s["last_error"] != "TRANSPORT_STALL":
        assert time.monotonic() < deadline, f"stall 未於 40 s 內翻轉為 TRANSPORT_STALL：{s!r}"
        time.sleep(0.5)
        s = _row(p7)
    assert isinstance(s["last_error_detail"], str) and s["last_error_detail"].startswith("零 RX "), s
    deadline = time.monotonic() + 10.0
    while _stall_metas(p7) < want_metas:
        assert time.monotonic() < deadline, "WAL META transport_stall_suspected 未出現"
        time.sleep(0.5)
    time.sleep(15.0)   # 持續 stall 的重試期間不得重複告警
    assert _stall_metas(p7) == want_metas


def test_transport_stall_flip(p7, tmp_path):
    tgt = FakeTarget(tmp_path / "t0")
    p7.start("shell", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    _stall_and_observe(p7, tgt, want_metas=1)
    tgt.thaw_rx()
    tgt.emit("hello\r\n")
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    _stall_and_observe(p7, tgt, want_metas=2)
