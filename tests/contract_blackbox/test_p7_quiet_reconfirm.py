"""P7 Task 18（08-slice23-login-events.md）：Quiet gate／Ready Reconfirm 黑箱（外部 CLI＋FakeTarget 觀測）。

承接 `test_rust_quiet_mid_login_aborts_sc30`（case_id `sc30-quiet-mid-login-aborts`，SC-30，OD-35）與
spontaneous reboot、attach during quiet、bootloader tail D1、attached bootloader stuck 四列。

情境設計依 Python 基線實測（2026-10-29，CPython 3.12）：capable profile 的**初次** attach 失敗一律回
DETACHED 重試（`LOGIN_PROMPT_TIMEOUT`），黑箱無法在「無 READY 歷史」下抵達 ATTACHED；ATTACHED 只能經
READY 之後的失敗（READY＋banner 期間 `session recover` → `PROMPT_TIMEOUT` 收斂 ATTACHED＋`PROMPT_UNAVAILABLE`）
進入。故 attach-during-quiet 的 ATTACHED 子案與 attached-bootloader-stuck 皆以 READY 起手（計畫原寫「無 READY
歷史」為案例設計不可達，見 p7-fix-responses 第 11 輪）。bootloader 停留以 FakeTarget READY 狀態的空行 override
（`\r\n=> `）模擬：banner 以 `emit()` 送出（`boot()` 會重設登入狀態使空行重新走 login）。
慢案（> 180 s）：`test_ready_bootloader_tail_terminal_d1`、`test_attached_bootloader_probe_sets_stuck`，皆為必要案例、不得 SKIP。
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


def _row(p7, sel: str = "COM0") -> dict:
    rc, j = p7.cli("session", "list")
    assert rc == 0 and j, (rc, j)
    return [x for x in j["sessions"] if x["com"] == sel][0]


def _ready_bootloader_target(p7, tmp_path) -> FakeTarget:
    """prpl READY；之後對空行一律回 `=> `（停在 bootloader 的 DUT）。"""
    tgt = FakeTarget(tmp_path / "t0", prompt="root@prplOS:~# ", commands={"overrides": {"": {"response": "\r\n=> "}}})
    p7.start("prpl", [tgt])
    s = wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    assert s["last_error"] is None, s
    return tgt


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_spontaneous_reboot_gates_agent(p7, tmp_path):
    """READY 後 DUT 自發重開機（U-Boot 倒數）：submit 回 `AUTOBOOT_QUIET`＋浮點 `retry_after_s`；倒數期間 0 byte。"""
    tgt = _ready_bootloader_target(p7, tmp_path)
    mark = tgt.rx_mark()
    tgt.boot([(0.0, BANNER), (1.0, "\b\b\b 2"), (2.0, "\b\b\b 1")])
    time.sleep(0.5)
    rc, j = p7.cli("cmd", "submit", "--selector", "COM0", "--cmd", "echo gated")
    assert rc == 2, (rc, j)
    assert j["ok"] is False and j["error_code"] == "AUTOBOOT_QUIET", j
    assert isinstance(j["retry_after_s"], float) and 0.0 < j["retry_after_s"] <= 180.0, j
    time.sleep(3.0)
    assert tgt.rx_log_since(mark) == b"", "倒數期間 target 不得收到任何 byte"


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_attach_during_quiet_zero_tx(p7, tmp_path):
    """quiet 期間 attach／recover 皆 0 byte；READY 時 last_error 維持 null，ATTACHED 時為 `PROMPT_UNAVAILABLE`。"""
    tgt = _ready_bootloader_target(p7, tmp_path)
    mark = tgt.rx_mark()
    tgt.emit(BANNER + "\r\n=> ")
    time.sleep(1.0)
    # READY：attach 不探測、不改 last_error
    rc, j = p7.cli("session", "attach", "--selector", "COM0")
    assert rc == 0 and j["ok"] is True, (rc, j)
    assert j["session"]["state"] == "READY" and j["session"]["last_error"] is None, j
    # READY 的 recover 於 quiet 期間略過按鍵、收斂 ATTACHED
    rc, j = p7.cli("session", "recover", "--selector", "COM0")
    assert rc == 2, (rc, j)
    assert (j["ok"], j["error_code"], j["partial"], j["recovery_action"]) == (False, "PROMPT_TIMEOUT", True, "NONE"), j
    s = _row(p7)
    assert (s["state"], s["last_error"]) == ("ATTACHED", "PROMPT_UNAVAILABLE"), s
    # ATTACHED：attach 與 recover（REPROBE）皆不送 byte
    rc, j = p7.cli("session", "attach", "--selector", "COM0")
    assert rc == 2, (rc, j)
    assert (j["ok"], j["error_code"], j["last_error"]) == (False, "SESSION_NOT_READY", "PROMPT_UNAVAILABLE"), j
    assert "last_error_detail" in j and j["last_error_detail"] is None, j
    rc, j = p7.cli("session", "recover", "--selector", "COM0")
    assert rc == 0, (rc, j)
    assert (j["ok"], j["action"], j["recovered"], j["error_code"], j["last_error"]) == (
        True, "REPROBE", False, "SESSION_NOT_READY", "PROMPT_UNAVAILABLE"), j
    time.sleep(2.0)
    assert tgt.rx_log_since(mark) == b"", "quiet 期間 attach／recover 不得送任何 byte"
    s = _row(p7)
    assert (s["state"], s["last_error"]) == ("ATTACHED", "PROMPT_UNAVAILABLE"), s


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_ready_bootloader_tail_terminal_d1(p7, tmp_path):
    """慢案：READY 後 banner 停在 `=> `；quiet（180 s，常數不可調）結束後 D1 判定 READY_UNCONFIRMED，全程 0 byte。"""
    tgt = _ready_bootloader_target(p7, tmp_path)
    mark = tgt.rx_mark()
    tgt.emit(BANNER + "\r\n=> ")
    t0 = time.monotonic()
    s = _row(p7)
    while not s["ready_reconfirm_failed"]:
        assert time.monotonic() - t0 <= 182.0, f"180 s＋2 s 內應判定 ready_reconfirm_failed：{s!r}"
        time.sleep(1.0)
        s = _row(p7)
    assert (s["state"], s["last_error"], s["ready_reconfirm_pending"]) == ("READY", None, False), s
    rc, j = p7.cli("cmd", "submit", "--selector", "COM0", "--cmd", "echo x")
    assert rc == 2, (rc, j)
    assert (j["ok"], j["error_code"], j["recommended_action"]) == (False, "READY_UNCONFIRMED", "self_test"), j
    assert "retry_after_s" not in j, j
    time.sleep(1.0)
    assert tgt.rx_log_since(mark) == b"", "D1 不對 U-Boot 送 probe"


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_attached_bootloader_probe_sets_stuck(p7, tmp_path):
    """慢案：ATTACHED（經 READY＋quiet 收斂）、tail 停在 `=> `、quiet 過期後自動 reprobe 失敗一次 →
    `BOOTLOADER_STUCK`＋`reprobe_exhausted`；之後手動 recover → `BOOTLOADER` 分類與 `matched_prompt`。

    次序說明：quiet 過期瞬間自動 reprobe 與手動 recover 會競爭 probe（Python per-session probe lock、Rust
    login run 等待者），後到者的 `_probe_existing_bridge` 覆寫 `last_error`；兩實作同序時結果相同，故本案先等
    自動 reprobe 落終態、再手動 recover，避免以時序決定判定。"""
    tgt = _ready_bootloader_target(p7, tmp_path)
    tgt.emit(BANNER + "\r\n=> ")
    time.sleep(1.0)
    rc, j = p7.cli("session", "recover", "--selector", "COM0")
    assert rc == 2 and j["error_code"] == "PROMPT_TIMEOUT", (rc, j)
    s = _row(p7)
    assert (s["state"], s["last_error"]) == ("ATTACHED", "PROMPT_UNAVAILABLE"), s
    t0 = time.monotonic()
    while s["last_error"] != "BOOTLOADER_STUCK":
        assert time.monotonic() - t0 <= 182.0 + 60.0, f"quiet 過期後自動 reprobe 應判定 BOOTLOADER_STUCK：{s!r}"
        time.sleep(1.0)
        s = _row(p7)
    assert s["boot_quiet_remaining_s"] is None, s
    assert (s["state"], s["last_error_detail"], s["reprobe_exhausted"]) == ("ATTACHED", "matched_prompt=^=> $", True), s
    rc, j = p7.cli("session", "recover", "--selector", "COM0")
    assert rc == 0, (rc, j)
    assert (j["ok"], j["action"], j["recovered"], j["error_code"]) == (True, "REPROBE", False, "SESSION_NOT_READY"), j
    assert (j["classification"], j["matched_prompt"], j["recommended_action"]) == (
        "BOOTLOADER", "^=> $", "recover_interactive"), j
