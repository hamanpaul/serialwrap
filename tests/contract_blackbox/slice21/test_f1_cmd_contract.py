"""F1 命令契約（Slice 2.1 黑箱，承接 f1-* 案例）。

前提：`shell_daemon` 為 COM0 已 READY 的 daemon；FakeTarget 行為見 Task 15a
（`echo X` 回 `X`、`sleep N` 於 N 秒後回 prompt、`\\x03` 回 `^C` 與 prompt）。
"""
import time

import pytest

pytestmark = pytest.mark.cap_posix_pty


def _cmd_fields(st: dict) -> dict:
    """`cmd status` 的命令欄位：巢狀 `command` 或頂層皆接受。"""
    return st.get("command", st)


def test_limits_queryable(shell_daemon):
    st = shell_daemon.sw("daemon", "status")
    assert st["limits"]["max_submit_cmd_bytes"] == 16384
    assert st["limits"]["warn_submit_cmd_bytes"] == 4096


def test_too_long_rejected(shell_daemon):
    t = shell_daemon.targets["COM0"]
    cmd = "echo " + "A" * (16385 - len("echo "))
    assert len(cmd.encode("utf-8")) == 16385
    before = len(t.written())
    r = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", cmd)
    assert r["_rc"] == 2
    assert r["error_code"] == "CMD_TOO_LONG"
    assert r["cmd_length"] == 16385
    assert r["limit"] == 16384
    time.sleep(0.5)
    assert t.written()[before:] == b"", "被拒的超長命令不得送出任何 byte"


def test_newline_rejected(shell_daemon):
    r = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "a\nb")
    assert r["error_code"] == "CMD_CONTAINS_NEWLINE"


def test_near_limit_no_logout(shell_daemon):
    cmd = "echo " + "x" * (4000 - len("echo "))
    sub = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", cmd)
    done = shell_daemon.wait_command_done(sub["cmd_id"], timeout=15)
    assert done["status"] == "done"
    s = shell_daemon.rpc("session.get_state", {"selector": "COM0"})["session"]
    assert s["state"] == "READY"


def test_cmdid_survives_timeout(shell_daemon):
    sub = shell_daemon.sw("cmd", "submit", "--selector", "COM0", "--cmd", "sleep 20", "--cmd-timeout", "2")
    cmd_id = sub["cmd_id"]
    # 逾時前後持續輪詢 cmd status：任何時刻都不得回 CMD_NOT_FOUND（cmd_id 不因逾時遺失）
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        st = shell_daemon.sw("cmd", "status", "--cmd-id", cmd_id)
        assert st.get("error_code") != "CMD_NOT_FOUND", f"cmd_id 於逾時期間遺失：{st}"
        if _cmd_fields(st).get("done_at"):
            break
        time.sleep(0.2)
    done = shell_daemon.wait_command_done(cmd_id, timeout=10)
    assert done["status"] == "done"
    assert done["recovery_action"] == "CTRL_C"
