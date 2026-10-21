"""Slice 2.1 安全出口（L1 部分）黑箱案例（P5-T20）。

- `shell_daemon`：COM0 已 READY 的 daemon（Task 15 harness）。
- `cred_daemon`／`banner_target`／`profile_variant`：見 ./conftest.py。
- 拒絕路徑一律以 `t.written()` 與 WAL TX 行零增量判定（§11.3 不變量 1）。
"""
import errno
import os
import stat
import time

import pytest

pytestmark = pytest.mark.cap_posix_pty

UBOOT_BANNER = b"\r\nU-Boot 2024.01\r\nHit any key to stop autoboot:  3 \r\n"
PASSTHROUGH_QUIET_S = 30.0


# ---------------------------------------------------------------------------
# 共用輔助
# ---------------------------------------------------------------------------


def _session(d, com: str = "COM0") -> dict:
    return d.rpc("session.get_state", {"selector": com})["session"]


def _wait_last_error(d, want: str, com: str = "COM0", timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        s = _session(d, com)
        if s.get("last_error") == want:
            return s
        if time.monotonic() > deadline:
            raise AssertionError(f"逾時未見 last_error={want}：{s}")
        time.sleep(0.2)


def _tx_count(d) -> int:
    return sum(1 for r in d.wal_records() if r.get("dir") == "TX")


def _tgt(d):
    return d.targets["COM0"]


def _submit(d, cmd: str, *extra: str) -> dict:
    return d.sw("cmd", "submit", "--selector", "COM0", "--cmd", cmd, *extra)


# ---------------------------------------------------------------------------
# 規格原文案例
# ---------------------------------------------------------------------------


def test_zero_tx_while_autoboot_quiet(shell_daemon):
    t = shell_daemon.targets["COM0"]
    t.emit(b"\r\nU-Boot 2024.01\r\nHit any key to stop autoboot:  3 \r\n")
    time.sleep(0.5)
    before = len(t.written())
    r = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "echo should-not-send")
    assert r["_rc"] == 2 and r["error_code"] == "AUTOBOOT_QUIET" and 179.0 <= r["retry_after_s"] <= 180.0
    time.sleep(1.0)
    assert t.written()[before:] == b""


def test_external_open_blocked_then_release_attach_handoff(shell_daemon):
    if os.geteuid() == 0:
        pytest.skip("EVIDENCE_MISSING: TIOCEXCL bypassed for root (§6.2)")
    slave = shell_daemon.targets["COM0"].slave_path
    with pytest.raises(OSError) as e:
        os.open(slave, os.O_RDWR | os.O_NOCTTY)
    assert e.value.errno == errno.EBUSY                                     # BC-04 TIOCEXCL
    rel = shell_daemon.sw("device", "release", "--selector", "COM0", "--source", "agent:t", "--reason", "flash")
    assert rel["ok"] is True and rel["session"]["state"] == "RELEASED"
    fd = os.open(slave, os.O_RDWR | os.O_NOCTTY)                             # 外部工具接手；FakeTarget 持有 master，daemon 關閉前漏 TIOCNXCL 時此處 EBUSY（R0-F3）
    held = shell_daemon.sw("device", "attach", "--selector", "COM0")
    assert held["error_code"] == "DEVICE_STILL_HELD" and os.getpid() in held["pids"] and held["holder_unknown"] is False
    before = len(shell_daemon.targets["COM0"].written())
    forced = shell_daemon.sw("device", "attach", "--selector", "COM0", "--force")   # §6.2：force 不得略過已識別外部 holder
    assert forced["_rc"] == 2 and forced["error_code"] == "DEVICE_STILL_HELD"
    assert forced["pids"] == [os.getpid()] and forced["holder_unknown"] is False
    shell_daemon.wait_state("COM0", "RELEASED", timeout=1.0)              # 被拒後 released 保護不得被清除
    time.sleep(1.0)
    assert shell_daemon.targets["COM0"].written()[before:] == b"", "被拒的 force attach 不得對 UART 送出任何 byte"
    os.close(fd)
    ok = shell_daemon.sw("device", "attach", "--selector", "COM0")
    assert ok["ok"] is True
    shell_daemon.wait_state("COM0", "READY")


# ---------------------------------------------------------------------------
# 憑證、釋放與 socket 權限
# ---------------------------------------------------------------------------


def test_credentials_unresolved_no_empty_login(cred_daemon):
    s = _wait_last_error(cred_daemon, "CREDENTIALS_UNRESOLVED")
    assert s["state"] == "ATTACHED"
    assert s["last_error"] == "CREDENTIALS_UNRESOLVED"
    assert cred_daemon.targets["COM0"].written() == b""


def test_released_survives_two_restarts_dynamic(shell_daemon, slice21_daemon_factory):
    t = shell_daemon.targets["COM0"]
    rel = shell_daemon.sw("device", "release", "--selector", "COM0", "--source", "agent:t", "--reason", "flash")
    assert rel["ok"] is True
    shell_daemon.wait_state("COM0", "RELEASED", timeout=10)
    d = shell_daemon
    for _ in range(2):
        assert d.stop() == 0
        d = slice21_daemon_factory(shell_daemon.root, reuse_dirs=True)
        d.wait_state("COM0", "RELEASED", timeout=10)
        fd = os.open(t.slave_path, os.O_RDWR | os.O_NOCTTY)                 # 重啟後 daemon 未持有 tty
        os.close(fd)


def test_socket_mode_0660(shell_daemon):
    assert "SERIALWRAP_SOCKET_GROUP" not in shell_daemon.env
    assert stat.S_IMODE(os.stat(shell_daemon.endpoint).st_mode) == 0o660


def test_attach_not_ready_last_error_field(cred_daemon):
    t = cred_daemon.targets["COM0"]
    _wait_last_error(cred_daemon, "CREDENTIALS_UNRESOLVED")
    r = cred_daemon.sw("session", "attach", "--selector", "COM0")
    assert r["ok"] is False
    assert r["error_code"] == "SESSION_NOT_READY"
    assert r["last_error"] == "CREDENTIALS_UNRESOLVED"
    assert "last_error_detail" in r
    rec = cred_daemon.sw("session", "recover", "--selector", "COM0")
    assert rec["ok"] is True
    assert rec["recovered"] is False
    assert rec["error_code"] == "SESSION_NOT_READY"
    assert rec["last_error"] == "CREDENTIALS_UNRESOLVED"
    assert t.written() == b""


# ---------------------------------------------------------------------------
# Passthrough 安靜升 READY（SC-35）
# ---------------------------------------------------------------------------


def test_passthrough_quiet_ready_and_cmd(slice21_daemon_factory, profile_variant, tmp_path):
    profile = profile_variant(platform="passthrough", prompt_regex="'.*'", ready_probe="''")
    d = slice21_daemon_factory(tmp_path / "passthrough", profile_yaml=profile)
    t = d.targets["COM0"]
    d.wait_state("COM0", "ATTACHED", timeout=15)
    t0 = time.monotonic()
    assert t.written() == b""
    time.sleep(max(0.0, t0 + 29.0 - time.monotonic()))
    early = _submit(d, "echo early")
    assert early["error_code"] == "SESSION_NOT_READY"
    assert 0 <= early["passthrough_ready_in_s"] <= 1
    time.sleep(max(0.0, t0 + PASSTHROUGH_QUIET_S + 1.0 - time.monotonic()))
    assert _session(d)["state"] == "READY"
    rc = d.sw(
        "cmd", "submit", "--selector", "COM0", "--cmd", "echo __RC__=0",
        "--completion-regex", r"(?m)^__RC__=0\r*$",
    )
    done = d.wait_command_done(rc["cmd_id"], timeout=30)
    assert done["partial"] is False
    to = _submit(d, "sleep 20", "--cmd-timeout", "2")
    done2 = d.wait_command_done(to["cmd_id"], timeout=30)
    assert done2["error_code"] == "PROMPT_TIMEOUT"
    assert done2["recovery_action"] == "NONE"
    written = t.written()
    assert b"\x03" not in written and b"\x04" not in written, "passthrough 逾時不得送 CTRL_C／CTRL_D"
    assert _session(d)["state"] == "READY"


# ---------------------------------------------------------------------------
# WAL 自癒、suspended 狀態與拒絕路徑
# ---------------------------------------------------------------------------


def test_wal_unlink_recreates_with_meta(shell_daemon):
    wal = shell_daemon.wal_dir / "raw.wal.ndjson"
    wal.unlink()
    sub = _submit(shell_daemon, "echo after-unlink")
    shell_daemon.wait_command_done(sub["cmd_id"], timeout=15)
    recs = shell_daemon.wal_records()
    assert recs[0]["dir"] == "META"
    m = recs[0]["meta"] if "meta" in recs[0] else recs[0]
    assert m["event"] == "WAL_RECREATED_AFTER_EXTERNAL_UNLINK"
    assert m.get("loss_flag", recs[0].get("loss_flag")) is True
    assert m["potentially_lost_seq_range"][1] < recs[1]["seq"]


def test_recovering_submit_session_recovering(shell_daemon):
    _tgt(shell_daemon).stop()                                              # FakeTarget I/O error 瞬間
    r = _submit(shell_daemon, "echo x")
    assert r.get("ok") is False, "I/O error 瞬間的 submit 絕不可 ok:true"
    assert r.get("error_code") in ("SESSION_RECOVERING", "SESSION_NOT_READY")


def _fill_queue(d) -> None:
    first = _submit(d, "sleep 30", "--cmd-timeout", "60")
    assert "cmd_id" in first, first
    for i in range(400):
        r = _submit(d, f"echo fill{i}")
        if r.get("error_code") == "SESSION_QUEUE_FULL":
            return
    raise AssertionError("佇列 400 筆仍未出現 SESSION_QUEUE_FULL")


def _prepare_reject(row: str, request):
    """回傳 (daemon, trigger, expected_error_codes)；trigger(d) 回傳被拒的回應 dict。"""
    if row == "CMD_TOO_LONG":
        d = request.getfixturevalue("shell_daemon")
        return d, lambda d: _submit(d, "echo " + "A" * 16380), {"CMD_TOO_LONG"}
    if row == "CMD_CONTAINS_NEWLINE":
        d = request.getfixturevalue("shell_daemon")
        return d, lambda d: _submit(d, "a\nb"), {"CMD_CONTAINS_NEWLINE"}
    if row == "SESSION_QUEUE_FULL":
        d = request.getfixturevalue("shell_daemon")
        _fill_queue(d)
        return d, lambda d: _submit(d, "echo one-more"), {"SESSION_QUEUE_FULL"}
    if row == "SESSION_NOT_READY-DETACHED":
        d = request.getfixturevalue("shell_daemon")
        _tgt(d).link_path.unlink()                                          # 熱拔除：by-id 連結消失
        d.wait_state("COM0", "DETACHED", timeout=15)
        return d, lambda d: _submit(d, "echo x"), {"SESSION_NOT_READY"}
    if row == "SESSION_NOT_READY-ATTACHING":
        d = request.getfixturevalue("shell_daemon")
        d.sw("device", "release", "--selector", "COM0", "--source", "agent:t", "--reason", "flash")
        d.wait_state("COM0", "RELEASED", timeout=10)
        _tgt(d).pause_responding(15)                                        # 目標不回應 → 停在 attach 過程
        d.sw("device", "attach", "--selector", "COM0")
        return d, lambda d: _submit(d, "echo x"), {"SESSION_NOT_READY"}
    if row == "SESSION_NOT_READY-RELEASED":
        d = request.getfixturevalue("shell_daemon")
        d.sw("device", "release", "--selector", "COM0", "--source", "agent:t", "--reason", "flash")
        d.wait_state("COM0", "RELEASED", timeout=10)
        return d, lambda d: _submit(d, "echo x"), {"SESSION_NOT_READY"}
    if row == "SESSION_RECOVERING":
        d = request.getfixturevalue("shell_daemon")
        _tgt(d).stop()
        return d, lambda d: _submit(d, "echo x"), {"SESSION_RECOVERING", "SESSION_NOT_READY"}
    if row == "SESSION_NOT_READY-PASSTHROUGH":
        factory = request.getfixturevalue("slice21_daemon_factory")
        profile = request.getfixturevalue("profile_variant")(
            platform="passthrough", prompt_regex="'.*'", ready_probe="''"
        )
        d = factory(request.getfixturevalue("tmp_path") / "pt-reject", profile_yaml=profile)
        d.wait_state("COM0", "ATTACHED", timeout=15)                        # ATTACHED 未滿 30 s（SC-35）
        return d, lambda d: _submit(d, "echo x"), {"SESSION_NOT_READY"}
    if row == "PROFILE_NOT_COMMAND_CAPABLE":
        factory = request.getfixturevalue("slice21_daemon_factory")
        profile = request.getfixturevalue("profile_variant")(ready_probe="''")
        d = factory(request.getfixturevalue("tmp_path") / "not-capable", profile_yaml=profile)
        d.wait_state("COM0", "ATTACHED", timeout=15)
        return d, lambda d: _submit(d, "echo x"), {"PROFILE_NOT_COMMAND_CAPABLE"}
    if row == "READY_UNCONFIRMED":
        d = request.getfixturevalue("shell_daemon")
        _tgt(d).pause_responding(60)                                        # ready probe 無回應
        d.sw("session", "recover", "--selector", "COM0")
        return d, lambda d: _submit(d, "echo x"), {"READY_UNCONFIRMED"}
    if row == "AUTOBOOT_QUIET":
        d = request.getfixturevalue("banner_target")
        time.sleep(0.5)
        return d, lambda d: _submit(d, "echo should-not-send"), {"AUTOBOOT_QUIET"}
    if row == "SESSION_INTERACTIVE_BUSY":
        d = request.getfixturevalue("shell_daemon")
        lease = d.sw("session", "interactive-open", "--selector", "COM0", "--owner", "agent")
        assert "interactive_id" in lease, lease
        return d, lambda d: _submit(d, "echo x"), {"SESSION_INTERACTIVE_BUSY"}
    if row == "INVALID_COMPLETION_REGEX":
        d = request.getfixturevalue("shell_daemon")
        return d, lambda d: _submit(d, "echo x", "--completion-regex", "("), {"INVALID_COMPLETION_REGEX"}
    if row == "METHOD_NOT_FOUND":
        d = request.getfixturevalue("shell_daemon")
        return d, lambda d: d.rpc("file.push", {"selector": "COM0"}), {"METHOD_NOT_FOUND"}
    raise AssertionError(f"未知拒絕路徑列：{row}")


REJECT_ROWS = [
    "CMD_TOO_LONG",
    "CMD_CONTAINS_NEWLINE",
    "SESSION_QUEUE_FULL",
    "SESSION_NOT_READY-DETACHED",
    "SESSION_NOT_READY-ATTACHING",
    "SESSION_NOT_READY-RELEASED",
    "SESSION_RECOVERING",
    "SESSION_NOT_READY-PASSTHROUGH",
    "PROFILE_NOT_COMMAND_CAPABLE",
    "READY_UNCONFIRMED",
    "AUTOBOOT_QUIET",
    "SESSION_INTERACTIVE_BUSY",
    "INVALID_COMPLETION_REGEX",
    "METHOD_NOT_FOUND",
]


@pytest.mark.parametrize("row", REJECT_ROWS, ids=REJECT_ROWS)
def test_reject_paths_zero_tx(row, request):
    d, trigger, expected = _prepare_reject(row, request)
    before = len(_tgt(d).written())
    tx0 = _tx_count(d)
    resp = trigger(d)
    assert resp.get("error_code") in expected, f"{row}：回應 {resp}"
    time.sleep(1.0)
    assert _tgt(d).written()[before:] == b"", f"{row}：拒絕路徑不得送出任何 byte"
    assert _tx_count(d) == tx0, f"{row}：拒絕路徑不得寫入新的 TX WAL 行"


# ---------------------------------------------------------------------------
# 失效上下文、雙 daemon 競爭
# ---------------------------------------------------------------------------


def test_stale_context_not_replayed(shell_daemon):
    t = shell_daemon.targets["COM0"]

    def _lease() -> str:
        r = shell_daemon.sw("session", "interactive-open", "--selector", "COM0", "--owner", "agent")
        assert "interactive_id" in r, r
        return r["interactive_id"]

    def _assert_stale(old_id: str) -> None:
        r = shell_daemon.sw("session", "interactive-send", "--interactive-id", old_id, "--data", "echo STALE")
        assert r.get("error_code") == "INTERACTIVE_NOT_FOUND", r
        time.sleep(0.5)
        assert b"STALE" not in t.written(), "失效上下文不得被回放到 UART"

    # 1) release → attach（交接後舊 lease 失效）
    old = _lease()
    shell_daemon.sw("device", "release", "--selector", "COM0", "--source", "agent:t", "--reason", "flash")
    shell_daemon.wait_state("COM0", "RELEASED", timeout=10)
    shell_daemon.sw("device", "attach", "--selector", "COM0")
    shell_daemon.wait_state("COM0", "READY")
    _assert_stale(old)

    # 2) session recover（重建 bridge 後舊 lease 失效）
    old = _lease()
    shell_daemon.sw("session", "recover", "--selector", "COM0")
    shell_daemon.wait_state("COM0", "READY")
    _assert_stale(old)

    # 3) reboot：U-Boot banner 重新開機後舊 lease 失效
    old = _lease()
    t.emit(UBOOT_BANNER)
    time.sleep(1.0)
    _assert_stale(old)


def test_two_daemons_same_tty(shell_daemon, slice21_daemon_factory, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("EVIDENCE_MISSING: root 下 TIOCEXCL 不生效（§6.2），證據不足")
    t = shell_daemon.targets["COM0"]
    snap = len(t.written())
    # 第二個 daemon：不同 RUN／STATE／socket／lock，BY_ID 目錄指向第一個 daemon 的 FakeTarget
    second = slice21_daemon_factory(
        tmp_path / "second-daemon",
        extra_env={"SERIALWRAP_BY_ID_DIR": shell_daemon.env["SERIALWRAP_BY_ID_DIR"]},
    )
    deadline = time.monotonic() + 20.0
    while True:
        s = _session(second)
        if (s.get("last_error") or "").startswith("ATTACH_FAILED:DeviceHeldError"):
            break
        if time.monotonic() > deadline:
            raise AssertionError(f"第二個 daemon 未回報 ATTACH_FAILED:DeviceHeldError：{s}")
        time.sleep(0.2)
    assert s["state"] != "READY"
    time.sleep(1.0)
    assert t.written()[snap:] == b"", "第二個 daemon 不得對同一 tty 送出任何 TX"
