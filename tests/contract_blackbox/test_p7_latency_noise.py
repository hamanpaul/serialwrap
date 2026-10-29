"""P7 Task 18（08-slice23-login-events.md）：延遲與背景雜訊黑箱。

`test_login_with_delay_and_noise`：每個回應延遲 800 ms＋每 50 ms 一行 kernel noise 下仍能登入到 READY。
雜訊行以 `\r\n` 起頭（printk 獨立成行）：Python 基線實測（2026-10-29）中，不換行直接接在 `login: ` 之後的雜訊
會使 prompt 行永遠比對不到、無法登入（500 ms 間隔、無延遲亦然），故該寫法不是 Python 的可通過情境（案例設計
修正、不改契約語意，記於 p7-fix-responses 第 11 輪）。
`test_control_latency_under_flood_with_events`（計畫同列）待 `contracts/resource_limits.yaml` 凍結
`control_latency_flood_p99_ms`（本輪核對不存在、`event_latency.freeze_status: pending_review_record`），
屬數值門檻、需擁有者裁決，列 SPEC-GAPS／OWNER-BATCH，本檔不自訂門檻。
stdlib＋PyYAML＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import pytest

from bb_faketarget import FakeTarget
from bb_p7 import wait_state

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]


def test_login_with_delay_and_noise(p7, tmp_path):
    tgt = FakeTarget(
        tmp_path / "t0",
        prompt="# ",
        password="pw-noise",
        delay_ms=800,
        noise={"interval_ms": 50, "pattern": "\r\n[  12.345678] kernel: usb 1-1: noise"},
        login_steps=[
            {"expect": "", "respond": "\r\nLogin: "},
            {"expect": "root", "respond": "\r\nPassword: "},
            {"expect": "pw-noise", "respond": "\r\nCM> "},
        ],
        commands={"overrides": {"sh": {"response": "\r\n# "}}},
    )
    p7.start("bcm", [tgt])                                     # bcm：timeout_s 15
    # 雜訊下單次 attach 可能失敗而進入 DETACHED↔ATTACHING 重試（backoff 上限 15 s）；全套負載下 90 s 曾不足
    # （第 11 輪 g2 全套 1 次，同範圍連跑 2 次與 4 份並行皆綠），上限取 180 s，斷言仍為最終 READY。
    s = wait_state(p7.env, "COM0", "READY", timeout_s=180.0)
    assert s["last_error"] is None, s
    sent = tgt.rx_log()
    assert b"root\n" in sent and b"pw-noise\n" in sent and b"sh\n" in sent and b"echo __READY__" in sent, sent
