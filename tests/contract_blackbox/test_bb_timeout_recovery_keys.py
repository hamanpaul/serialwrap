"""profile `timeout_recovery_keys` 黑箱：命令逾時與 READY 時 `session recover` 依設定送救援鍵。

外部 CLI＋FakeTarget 觀測；Python 基線與 Rust（`SERIALWRAP_AB_LABEL=rust-*`）皆須通過（同一組斷言即 A/B）。
- `[CTRL_C]`：CTRL_C 救不回時不得再送 CTRL_D（CTRL_D 打在已登入 shell 的空提示符上＝登出）。
- `[]`：一個救援鍵都不送，直接 PROMPT_TIMEOUT。
預設（未設定）仍是先 CTRL_C 再 CTRL_D，見 `test_p7_selftest_recover.py::test_recover_force_on_ready_runs_keys_first`。
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


def _timed_out_command(p7, cmd: str = "sleep 60") -> dict:
    rc, sub = p7.cli("cmd", "submit", "--selector", "COM0", "--cmd", cmd, "--cmd-timeout", "2")
    assert sub and sub.get("ok") is True, (rc, sub)
    deadline = time.monotonic() + 20.0
    rec = None
    while time.monotonic() < deadline:
        _, st = p7.cli("cmd", "status", "--cmd-id", sub["cmd_id"])
        rec = (st or {}).get("command")
        if isinstance(rec, dict) and rec.get("done_at"):
            return rec
        time.sleep(0.3)
    raise AssertionError(f"命令未在期限內結案：{rec}")


def test_ctrl_c_only_timeout_never_sends_ctrl_d(p7, tmp_path):
    """`[CTRL_C]`、DUT 不回任何東西：只送 `\\x03`，救不回即 PROMPT_TIMEOUT＋ATTACHED，零 `\\x04`。"""
    tgt = FakeTarget(tmp_path / "t0")
    p7.start("shell_ctrl_c_only", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    mark = tgt.rx_mark()
    tgt.hold_reply(True)
    try:
        rec = _timed_out_command(p7)
        sent = tgt.rx_log_since(mark)
    finally:
        tgt.hold_reply(False)
    assert {k: rec[k] for k in ("status", "error_code", "partial", "recovery_action")} == {
        "status": "error", "error_code": "PROMPT_TIMEOUT", "partial": True, "recovery_action": "NONE"}, rec
    assert sent.startswith(b"sleep 60\n\x03"), sent
    assert b"\x04" not in sent, sent


def test_ctrl_c_only_timeout_recovers_with_ctrl_c(p7, tmp_path):
    """`[CTRL_C]`、DUT 對 CTRL_C 回 prompt：與預設相同，以 `recovery_action:"CTRL_C"` 完成、維持 READY。"""
    tgt = FakeTarget(tmp_path / "t0")
    p7.start("shell_ctrl_c_only", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    mark = tgt.rx_mark()
    rec = _timed_out_command(p7)
    assert {k: rec[k] for k in ("status", "error_code", "partial", "recovery_action", "stdout")} == {
        "status": "done", "error_code": None, "partial": True, "recovery_action": "CTRL_C", "stdout": "^C"}, rec
    assert tgt.rx_log_since(mark) == b"sleep 60\n\x03"
    assert _row(p7)["state"] == "READY"


def test_empty_keys_timeout_sends_no_keys(p7, tmp_path):
    """`[]`：命令逾時不送 `\\x03`／`\\x04`，直接 PROMPT_TIMEOUT（recovery_action NONE）。"""
    tgt = FakeTarget(tmp_path / "t0")
    p7.start("shell_no_recovery_keys", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    mark = tgt.rx_mark()
    rec = _timed_out_command(p7)
    sent = tgt.rx_log_since(mark)
    assert {k: rec[k] for k in ("status", "error_code", "partial", "recovery_action")} == {
        "status": "error", "error_code": "PROMPT_TIMEOUT", "partial": True, "recovery_action": "NONE"}, rec
    assert sent.startswith(b"sleep 60\n"), sent
    assert b"\x03" not in sent and b"\x04" not in sent, sent


def test_ctrl_c_only_recover_on_ready_sends_single_key(p7, tmp_path):
    """READY、target 靜默：`recover --force` 只送 `\\x03`（無 `\\x04`），回 PROMPT_TIMEOUT＋partial。

    預設兩鍵版本耗時 14～17 s（`test_recover_force_on_ready_runs_keys_first`）；少一鍵的等待，約 12～15 s。
    """
    tgt = FakeTarget(tmp_path / "t0")
    p7.start("shell_ctrl_c_only", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    tgt.freeze_rx()
    mark = tgt.rx_mark()
    t0 = time.monotonic()
    rc, j = p7.cli("session", "recover", "--selector", "COM0", "--force", timeout_s=60.0)
    elapsed = time.monotonic() - t0
    assert rc == 2, (rc, j)
    assert {k: j[k] for k in ("ok", "error_code", "partial", "recovery_action")} == {
        "ok": False, "error_code": "PROMPT_TIMEOUT", "partial": True, "recovery_action": "NONE"}, j
    assert 12.0 <= elapsed <= 15.0, f"耗時 {elapsed:.2f} s"
    sent = tgt.rx_log_since(mark)
    assert sent.startswith(b"\x03"), sent
    assert b"\x04" not in sent, sent
