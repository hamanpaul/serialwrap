"""P6 Task 11：黑箱 FakeTarget 再擴充（`scripts`、`ready`、`hold_reply`）的紅燈測試（測試資產變更，規格 §10.4）。

對應 docs/rust-refactor/plans/07-slice22-console.md「Task 11」Files 的 `bb_faketarget.py` 修改項。
全部 keyword-only、預設值維持舊行為；本檔只用 stdlib 與 PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import os
import time

import pytest

from test_bb_faketarget_s21 import PROMPT_B, _login_default, _open_reader, make_target  # noqa: F401

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty]


def _drain(rd, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            rd.wait_for(b"\x00never\x00", start=len(rd.buf), timeout=max(0.01, deadline - time.monotonic()))
        except AssertionError:
            return


def test_script_delays_reply_then_prompt(make_target, tmp_path):
    ft = make_target(scripts={b"sleep 2": (0.5, b"sleep 2: ok\r\n")})
    rd = _open_reader(ft, tmp_path)
    try:
        mark = _login_default(rd)
        t0 = time.monotonic()
        os.write(rd.fd, b"sleep 2\r")
        idx, arrived = rd.wait_for(b"sleep 2: ok\r\n", start=mark, timeout=5.0)
        assert 0.4 <= arrived - t0 <= 1.2, f"腳本回覆應於延遲後送出，實測 {arrived - t0:.3f} s"
        assert bytes(rd.buf[mark:idx]) == b"sleep 2\r\n", "回覆之前只有回顯"
        rd.wait_for(PROMPT_B, start=idx, timeout=2.0)
    finally:
        rd.close()


def test_script_without_prompt(make_target, tmp_path):
    ft = make_target(scripts={b"bootcmd": (0.2, b"U-Boot 2023.01\r\n", False)})
    rd = _open_reader(ft, tmp_path)
    try:
        mark = _login_default(rd)
        os.write(rd.fd, b"bootcmd\r")
        idx, _ = rd.wait_for(b"U-Boot 2023.01\r\n", start=mark, timeout=3.0)
        _drain(rd, 0.6)
        assert PROMPT_B not in bytes(rd.buf[idx:]), "第三欄 False 時不回 prompt"
        os.write(rd.fd, b"\x03")
        rd.wait_for(b"^C\r\n" + PROMPT_B, start=idx, timeout=2.0)
    finally:
        rd.close()


def test_ready_false_ignores_ready_probe(make_target, tmp_path):
    ft = make_target(ready=False)
    rd = _open_reader(ft, tmp_path)
    try:
        mark = _login_default(rd)
        os.write(rd.fd, b"echo __READY__abcd1234\r")
        _drain(rd, 0.6)
        assert bytes(rd.buf[mark:]) == b"", "ready=False 時 ready probe 不回顯、不回 prompt"
        os.write(rd.fd, b"echo hi\r")
        rd.wait_for(b"echo hi\r\nhi\r\n" + PROMPT_B, start=mark, timeout=2.0)
        assert b"echo __READY__abcd1234\r" in ft.written(0.2), "輸入照常記錄"
    finally:
        rd.close()


def test_ready_default_answers_probe(make_target, tmp_path):
    ft = make_target()
    rd = _open_reader(ft, tmp_path)
    try:
        mark = _login_default(rd)
        os.write(rd.fd, b"echo __READY__abcd1234\r")
        rd.wait_for(b"__READY__abcd1234\r\n" + PROMPT_B, start=mark, timeout=2.0)
    finally:
        rd.close()


def test_hold_reply_pauses_until_released(make_target, tmp_path):
    ft = make_target()
    rd = _open_reader(ft, tmp_path)
    try:
        mark = _login_default(rd)
        ft.hold_reply(True)
        os.write(rd.fd, b"echo H\r")
        _drain(rd, 1.0)
        assert bytes(rd.buf[mark:]) == b"", "hold 期間不回應"
        assert b"echo H\r" in ft.written(0.2), "hold 期間輸入照常記錄"
        ft.hold_reply(False)
        rd.wait_for(b"echo H\r\nH\r\n" + PROMPT_B, start=mark, timeout=2.0)
    finally:
        rd.close()
