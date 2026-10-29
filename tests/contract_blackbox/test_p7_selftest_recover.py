"""P7 Task 18（08-slice23-login-events.md）：self-test 分類、SC-34 last_error 欄位、recover／force recover 黑箱。

外部 CLI＋FakeTarget 觀測；Python 基線與 Rust（`SERIALWRAP_AB_LABEL=rust-*`）皆須通過。斷言值依 Python 基線實測
（2026-10-29，CPython 3.12）。情境調整（案例設計、不改契約語意，記於 p7-fix-responses 第 11 輪）：
- ATTACHED 只能經 READY 之後的失敗抵達（capable profile 初次 attach 失敗回 DETACHED），bootloader／flood
  分類以「READY＋banner → quiet 期間 recover 收斂 ATTACHED → 新 bridge 上再輸出 `=> `／洪水」構成。
- SC-34 的 READY_NONCE_TIMEOUT 子案：初次 attach 的 nonce 逾時會落 DETACHED（attach 回 ok:true），故改由
  ATTACHED＋`CREDENTIALS_UNRESOLVED` 補帳密後 `session recover`（既有 bridge 再探）取得 ATTACHED＋
  `READY_NONCE_TIMEOUT`（last_error_detail 非 null）。
- `test_selftest_classifications[released]`：BC-04（`ab_participation: diff`）——Rust 在 `/proc` 有不可讀 fd 目錄
  （非 root 執行）時 holder 不明、`reclaimable:false`、`wait_external_flash`；期望值以測試自行觀測的 /proc 可讀性決定。
- `test_recover_ready_ctrl_c`：Python 命令紀錄為 `status:"done"`、`error_code:null`、`partial:true`、
  `recovery_action:"CTRL_C"`（計畫寫的 `PROMPT_TIMEOUT_RECOVERED` 不是此路徑的外部可觀察值）。
stdlib＋PyYAML＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import os
import time

import pytest

from bb_faketarget import FakeTarget
from bb_harness import is_rust
from bb_p7 import wait_state

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]

BANNER = "\r\nU-Boot 2024.04 (Jan 01 2024 - 00:00:00 +0000)\r\nHit any key to stop autoboot:  3"


def _row(p7, sel: str = "COM0") -> dict:
    rc, j = p7.cli("session", "list")
    assert rc == 0 and j, (rc, j)
    return [x for x in j["sessions"] if x["com"] == sel][0]


def _ready_bootloader_target(p7, tmp_path) -> FakeTarget:
    tgt = FakeTarget(tmp_path / "t0", prompt="root@prplOS:~# ", commands={"overrides": {"": {"response": "\r\n=> "}}})
    p7.start("prpl", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    return tgt


def _to_attached(p7, tgt) -> None:
    """READY＋banner → quiet 期間 recover → ATTACHED＋PROMPT_UNAVAILABLE（新 bridge）。"""
    tgt.emit(BANNER + "\r\n=> ")
    time.sleep(1.0)
    rc, j = p7.cli("session", "recover", "--selector", "COM0")
    assert rc == 2 and j["error_code"] == "PROMPT_TIMEOUT", (rc, j)
    s = _row(p7)
    assert (s["state"], s["last_error"]) == ("ATTACHED", "PROMPT_UNAVAILABLE"), s
    time.sleep(1.0)


def _proc_fd_unreadable() -> bool:
    """外部觀測：本機是否有行程的 `/proc/<pid>/fd` 因權限不可讀（非 root 執行時常見）。"""
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            os.listdir(f"/proc/{name}/fd")
        except PermissionError:
            return True
        except OSError:
            continue
    return False


def _self_test(p7) -> dict:
    rc, j = p7.cli("session", "self-test", "--selector", "COM0")
    assert rc == 0 and j and j["ok"] is True, (rc, j)
    return j


@pytest.mark.parametrize("case", ["ready", "bootloader", "flood", "passthrough_attached", "passthrough_ready", "released"])
def test_selftest_classifications(p7, tmp_path, case):
    if case == "ready":
        tgt = _ready_bootloader_target(p7, tmp_path)
    elif case in ("bootloader", "flood"):
        tgt = _ready_bootloader_target(p7, tmp_path)
        _to_attached(p7, tgt)
        tgt.emit("\r\n=> " if case == "bootloader" else "x" * 25000)
        time.sleep(1.0)
    elif case == "passthrough_attached":
        tgt = FakeTarget(tmp_path / "t0", prompt="=> ", login=False)
        p7.start("passthrough_uboot", [tgt])
        wait_state(p7.env, "COM0", "ATTACHED", timeout_s=15.0)
        time.sleep(1.0)
    elif case == "passthrough_ready":
        tgt = FakeTarget(tmp_path / "t0", prompt="=> ", login=False)
        p7.start("passthrough_uboot", [tgt])
        wait_state(p7.env, "COM0", "READY", timeout_s=45.0)
    else:
        tgt = _ready_bootloader_target(p7, tmp_path)
        rc, j = p7.cli("device", "release", "--selector", "COM0", "--source", "agent:p7", "--reason", "flash")
        assert rc == 0 and j["ok"] is True, (rc, j)
        time.sleep(1.0)
    mark = tgt.rx_mark()
    j = _self_test(p7)
    released_action = "device_attach"
    if case == "released" and is_rust() and _proc_fd_unreadable():
        # BC-04（OD-04，ab_participation: diff）：/proc holder 掃描不完整 → holder_unknown，不視為可收回。
        # Python 0.3.2 基線略過不可讀的 fd 目錄；Rust 依 BC-04 回 wait_external_flash。
        released_action = "wait_external_flash"
    want = {
        "ready": ("OK", "none"),
        "bootloader": ("BOOTLOADER", "recover_interactive"),
        "flood": ("RX_FLOOD", "wait"),
        "passthrough_attached": ("PASSTHROUGH", "console_attach"),
        "passthrough_ready": ("PASSTHROUGH", "none"),
        "released": ("RELEASED", released_action),
    }[case]
    assert (j["classification"], j["recommended_action"]) == want, j
    if case == "ready":
        assert j["probe_ok"] is True, j
    elif case == "bootloader":
        assert (j["matched_prompt"], j["rx_tail"]) == ("^=> $", "\n=> "), j
    elif case == "flood":
        assert j["rx_bytes_last_10s"] == 25000, j
    elif case == "passthrough_attached":
        assert isinstance(j["passthrough_ready_in_s"], float) and 0.0 < j["passthrough_ready_in_s"] <= 30.0, j
    elif case == "passthrough_ready":
        assert "passthrough_ready_in_s" not in j, j
    elif case == "released":
        assert j["released_by"] == "agent:p7" and j["reason"] == "flash", j
        assert (j["external_holder"], j["reclaimable"]) == ("none", released_action == "device_attach"), j
    time.sleep(1.0)
    sent = tgt.rx_log_since(mark)
    if case == "ready":
        assert sent.startswith(b"echo __READY__") and sent.endswith(b"\n") and sent.count(b"\n") == 1, sent
    else:
        assert sent == b"", f"{case}：self-test 期間 target 不得收到任何 byte：{sent!r}"


def test_attach_recover_last_error_field(p7, tmp_path):
    """SC-34：attach／recover 的失敗回應帶 `last_error`／`last_error_detail`，與 `session` 內欄位一致。"""
    tgt = FakeTarget(
        tmp_path / "t0",
        prompt="root@op3:~# ",
        login_steps=[
            {"expect": "", "respond": "\r\nop3 login: "},
            {"expect": "root", "respond": "\r\nPassword: "},
            {"expect": "pw", "respond": "\r\nroot@op3:~# "},
            {"expect": "__never__", "respond": ""},   # 之後任何行（含 nonce probe）只重印 prompt、不回 nonce
        ],
    )
    p7.start("shell_unresolved", [tgt])
    wait_state(p7.env, "COM0", "ATTACHED")
    deadline = time.monotonic() + 15.0
    while _row(p7)["last_error"] != "CREDENTIALS_UNRESOLVED":
        assert time.monotonic() < deadline
        time.sleep(0.2)

    def check(expect_error: str, *, detail_nonnull: bool) -> None:
        rc, a = p7.cli("session", "attach", "--selector", "COM0")
        assert rc == 2, (rc, a)
        assert (a["ok"], a["error_code"], a["last_error"]) == (False, "SESSION_NOT_READY", expect_error), a
        assert "last_error_detail" in a and a["last_error_detail"] == a["session"]["last_error_detail"], a
        assert a["last_error"] == a["session"]["last_error"], a
        assert (a["last_error_detail"] is not None) == detail_nonnull, a

    check("CREDENTIALS_UNRESOLVED", detail_nonnull=False)
    rc, r = p7.cli("session", "recover", "--selector", "COM0")
    assert rc == 0, (rc, r)
    assert (r["ok"], r["action"], r["recovered"], r["error_code"], r["last_error"]) == (
        True, "REPROBE", False, "SESSION_NOT_READY", "CREDENTIALS_UNRESOLVED"), r
    assert "last_error_detail" in r and r["last_error_detail"] is None, r

    (p7.tmp / "p7.env.unresolved").write_text("SW_P7_USER=root\nSW_P7_PASS=pw\n", encoding="utf-8")
    rc, r = p7.cli("session", "recover", "--selector", "COM0")
    assert rc == 0, (rc, r)
    assert (r["ok"], r["action"], r["recovered"], r["error_code"], r["last_error"]) == (
        True, "REPROBE", False, "SESSION_NOT_READY", "READY_NONCE_TIMEOUT"), r
    assert r["last_error_detail"] == r["session"]["last_error_detail"] and r["last_error_detail"], r
    assert r["session"]["state"] == "ATTACHED", r
    check("READY_NONCE_TIMEOUT", detail_nonnull=True)


def test_recover_force_timeout_attached(p7, tmp_path):
    """ATTACHED、target 靜默：`recover --force` → `FORCE_RECOVER_TIMEOUT`、`FORCE_CLEAR_REATTACH`，10～13 s。"""
    tgt = FakeTarget(tmp_path / "t0", prompt="root@op3:~# ", login_steps=[{"expect": "", "respond": "\r\nop3 login: "}])
    p7.start("shell_unresolved", [tgt])
    wait_state(p7.env, "COM0", "ATTACHED")
    time.sleep(1.0)
    t0 = time.monotonic()
    rc, j = p7.cli("session", "recover", "--selector", "COM0", "--force", timeout_s=60.0)
    elapsed = time.monotonic() - t0
    assert rc == 2, (rc, j)
    assert (j["ok"], j["error_code"], j["action"], j["recovered"]) == (
        False, "FORCE_RECOVER_TIMEOUT", "FORCE_CLEAR_REATTACH", False), j
    assert 10.0 <= elapsed <= 13.0, f"耗時 {elapsed:.2f} s"
    assert tgt.rx_log() == b"", "帳密未解析：force reattach 不得送任何 byte"


def test_recover_force_on_ready_runs_keys_first(p7, tmp_path):
    """READY、target 靜默：`recover --force` 先跑救援按鍵（`\\x03`、`\\x04`），回 PROMPT_TIMEOUT＋partial，14～17 s。"""
    tgt = FakeTarget(tmp_path / "t0")
    p7.start("shell", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    tgt.freeze_rx()
    mark = tgt.rx_mark()
    t0 = time.monotonic()
    rc, j = p7.cli("session", "recover", "--selector", "COM0", "--force", timeout_s=60.0)
    elapsed = time.monotonic() - t0
    assert rc == 2, (rc, j)
    assert {k: j[k] for k in ("ok", "error_code", "partial", "recovery_action")} == {
        "ok": False, "error_code": "PROMPT_TIMEOUT", "partial": True, "recovery_action": "NONE"}, j
    assert 14.0 <= elapsed <= 17.0, f"耗時 {elapsed:.2f} s"
    sent = tgt.rx_log_since(mark)
    assert sent.startswith(b"\x03\x04"), sent


def test_recover_ready_ctrl_c(p7, tmp_path):
    """命令 hang 至逾時 → CTRL_C 救援後 prompt 回來：命令以 `recovery_action:"CTRL_C"`＋`partial:true` 完成。"""
    tgt = FakeTarget(tmp_path / "t0")
    p7.start("shell", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    mark = tgt.rx_mark()
    rc, sub = p7.cli("cmd", "submit", "--selector", "COM0", "--cmd", "sleep 60", "--cmd-timeout", "2")
    assert sub and sub.get("ok") is True, (rc, sub)
    deadline = time.monotonic() + 20.0
    cmd = None
    while time.monotonic() < deadline:
        _, st = p7.cli("cmd", "status", "--cmd-id", sub["cmd_id"])
        cmd = (st or {}).get("command")
        if isinstance(cmd, dict) and cmd.get("done_at"):
            break
        time.sleep(0.3)
    assert isinstance(cmd, dict) and cmd.get("done_at"), cmd
    assert {k: cmd[k] for k in ("status", "error_code", "partial", "recovery_action", "stdout")} == {
        "status": "done", "error_code": None, "partial": True, "recovery_action": "CTRL_C", "stdout": "^C"}, cmd
    assert tgt.rx_log_since(mark) == b"sleep 60\n\x03"
    assert _row(p7)["state"] == "READY"
