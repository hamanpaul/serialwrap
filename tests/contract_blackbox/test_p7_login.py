"""P7 Task 18（08-slice23-login-events.md）：Login FSM／帳密終態／passthrough（SC-35）黑箱。

外部 CLI＋FakeTarget 觀測；Python 基線與 Rust（`SERIALWRAP_AB_LABEL=rust-*`）皆須通過。
stdlib＋PyYAML＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import base64
import time

import pytest

from bb_faketarget import FakeTarget
from bb_p7 import wait_state

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]

SECRET = "S3cr3t-P7pw"
BANNER = "\r\nU-Boot 2024.04 (Jan 01 2024 - 00:00:00 +0000)\r\nHit any key to stop autoboot:  3"


def _row(p7, sel: str = "COM0") -> dict:
    rc, j = p7.cli("session", "list")
    assert rc == 0 and j, (rc, j)
    return [x for x in j["sessions"] if x["com"] == sel][0]


def _metas(p7, event: str, sel: str = "COM0") -> list[dict]:
    out = []
    for r in p7.wal_records(sel):
        if r.get("dir") != "META":
            continue
        meta = r.get("meta")
        if not isinstance(meta, dict):
            try:
                import json
                meta = json.loads(base64.b64decode(r.get("payload_b64") or "").decode("utf-8"))
            except ValueError:
                meta = {}
        if meta.get("event") == event:
            out.append(r)
    return out


def _tx_bytes(p7, sel: str = "COM0") -> bytes:
    return b"".join(base64.b64decode(r.get("payload_b64") or "") for r in p7.wal_records(sel) if r.get("dir") == "TX")


def _submit_wait(p7, cmd: str, sel: str = "COM0", timeout_s: float = 30.0) -> dict:
    rc, sub = p7.cli("cmd", "submit", "--selector", sel, "--cmd", cmd)
    assert sub and sub.get("ok") is True, (rc, sub)
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        _, st = p7.cli("cmd", "status", "--cmd-id", sub["cmd_id"])
        last = (st or {}).get("command")
        if isinstance(last, dict) and last.get("done_at"):
            return last
        time.sleep(0.2)
    raise AssertionError(f"命令 {cmd!r} 未於 {timeout_s}s 內完成：{last!r}")


def test_unresolved_credentials_terminal_zero_empty_secret_tx(p7, tmp_path):
    tgt = FakeTarget(tmp_path / "t0", prompt="root@op3:~# ", login_steps=[{"expect": "", "respond": "\r\nop3 login: "}])
    p7.start("shell_unresolved", [tgt])
    s = wait_state(p7.env, "COM0", "ATTACHED")
    deadline = time.monotonic() + 15.0
    while s.get("last_error") != "CREDENTIALS_UNRESOLVED" and time.monotonic() < deadline:
        time.sleep(0.2)
        s = _row(p7)
    assert s["last_error"] == "CREDENTIALS_UNRESOLVED", s
    assert tgt.rx_log() == b"", "宣告帳密但未解析時 attach 不得送任何 probe／login byte"
    mark = tgt.rx_mark()
    time.sleep(35.0)                                           # 跨過多個 reprobe 週期
    assert tgt.rx_log_since(mark) == b"", "終態期間不得自動重探或送任何 byte"
    assert _row(p7)["last_error"] == "CREDENTIALS_UNRESOLVED"
    assert len(_metas(p7, "credentials_unresolved")) == 1
    assert _tx_bytes(p7) == b"", "WAL 不得有任何 TX（含空帳密）"


def _wait_unresolved(p7) -> dict:
    wait_state(p7.env, "COM0", "ATTACHED")
    deadline = time.monotonic() + 15.0
    s = _row(p7)
    while s.get("last_error") != "CREDENTIALS_UNRESOLVED" and time.monotonic() < deadline:
        time.sleep(0.2)
        s = _row(p7)
    assert s["last_error"] == "CREDENTIALS_UNRESOLVED", s
    return s


def test_creds_file_fixed_does_not_auto_probe_until_attach(p7, tmp_path):
    tgt = FakeTarget(tmp_path / "t0", prompt="root@op3:~# ", password=SECRET)
    p7.start("shell_unresolved", [tgt])
    _wait_unresolved(p7)
    env_file = p7.tmp / "p7.env.unresolved"
    env_file.write_text(f"SW_P7_USER=root\nSW_P7_PASS={SECRET}\n", encoding="utf-8")   # §9.3：只改帳密檔不得自動解除
    mark = tgt.rx_mark()
    time.sleep(35.0)
    assert tgt.rx_log_since(mark) == b""
    assert _row(p7)["last_error"] == "CREDENTIALS_UNRESOLVED"
    p7.cli("session", "attach", "--selector", "COM0")
    s = wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    assert s["last_error"] is None, s


def test_creds_fixed_then_attach_ready(p7, tmp_path):
    tgt = FakeTarget(tmp_path / "t0", prompt="root@op3:~# ", password=SECRET)
    p7.start("shell_unresolved", [tgt])
    _wait_unresolved(p7)
    (p7.tmp / "p7.env.unresolved").write_text(f"SW_P7_USER=root\nSW_P7_PASS={SECRET}\n", encoding="utf-8")
    p7.cli("session", "attach", "--selector", "COM0")
    s = wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    assert s["last_error"] is None, s
    assert len(_metas(p7, "credentials_unresolved")) == 1, "META credentials_unresolved 只有修正前那一筆"
    assert SECRET.encode() not in _tx_bytes(p7), "WAL TX 不得含密碼 bytes"


def _bcm_target(tmp_path):
    return FakeTarget(
        tmp_path / "t0",
        prompt="# ",
        password=SECRET,
        login_steps=[
            {"expect": "", "respond": "\r\nLogin: "},
            {"expect": "root", "respond": "\r\nPassword: "},
            {"expect": SECRET, "respond": "\r\nCM> "},
        ],
        commands={"overrides": {"sh": {"response": "\r\n# "}}},
    )


@pytest.mark.parametrize("platform", ["shell", "prpl", "bcm"])
def test_platform_matrix(p7, tmp_path, platform):
    if platform == "shell":
        tgt = FakeTarget(tmp_path / "t0", password=SECRET)
    elif platform == "prpl":
        tgt = FakeTarget(tmp_path / "t0", prompt="root@prplOS:~# ", password=SECRET)
    else:
        tgt = _bcm_target(tmp_path)
    p7.start(platform, [tgt])
    s = wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    assert s["last_error"] is None, s
    assert SECRET.encode() in tgt.rx_log(), "密碼應已送到 target"
    assert SECRET.encode() not in _tx_bytes(p7), "WAL TX 不得含密碼 bytes"
    if platform == "bcm":
        done = _submit_wait(p7, "echo p7bcm")
        assert done["status"] == "done", done
        assert "p7bcm" in done["stdout"], done


def test_passthrough_uboot_quiet_ready_no_login(p7, tmp_path):
    """SC-35：passthrough 不跑登入、不送 ready_probe；ATTACHED 後安靜 30 s 轉 READY。"""
    tgt = FakeTarget(tmp_path / "t0", prompt="=> ", login=False)
    p7.start("passthrough_uboot", [tgt])
    wait_state(p7.env, "COM0", "ATTACHED", timeout_s=15.0)
    t_attached = time.monotonic()
    s = wait_state(p7.env, "COM0", "READY", timeout_s=40.0)
    elapsed = time.monotonic() - t_attached
    assert 28.0 <= elapsed <= 32.5, f"ATTACHED→READY 應為 30～32 s，實際 {elapsed:.2f}"
    assert s["last_error"] is None, s
    assert tgt.rx_log() == b"", "首個 cmd submit 之前 target 不得收到任何 byte"
    done = _submit_wait(p7, "version")
    assert done["status"] == "done", done
    assert done.get("partial") is False, done


def test_passthrough_others_quiet_ready(p7, tmp_path):
    """SC-35：others-template；第 10 s 的一行 RX 使安靜視窗重新起算。"""
    tgt = FakeTarget(tmp_path / "t0", login_steps=[])
    p7.start("passthrough_others", [tgt])
    wait_state(p7.env, "COM0", "ATTACHED", timeout_s=15.0)
    time.sleep(10.0)
    assert _row(p7)["state"] == "ATTACHED"
    tgt.emit("kernel: noise line\r\n")
    t_line = time.monotonic()
    time.sleep(1.0)
    s = _row(p7)
    assert s["state"] == "ATTACHED", s
    s = wait_state(p7.env, "COM0", "READY", timeout_s=40.0)
    elapsed = time.monotonic() - t_line
    assert 28.5 <= elapsed <= 32.5, f"自該行起 30～32 s 內轉 READY，實際 {elapsed:.2f}"
    assert s["last_error"] is None, s
    assert tgt.rx_log() == b""


def test_passthrough_banner_no_quiet_gate(p7, tmp_path):
    """SC-35：passthrough READY 後輸出 U-Boot banner 不觸發 quiet gate。"""
    tgt = FakeTarget(tmp_path / "t0", prompt="=> ", login=False)
    p7.start("passthrough_uboot", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=45.0)
    tgt.emit(BANNER)
    time.sleep(1.0)
    s = _row(p7)
    assert s["state"] == "READY", s
    assert s.get("boot_quiet_remaining_s") is None, s
    assert s.get("ready_reconfirm_pending") is False, s
    mark = tgt.rx_mark()
    rc, sub = p7.cli("cmd", "submit", "--selector", "COM0", "--cmd", "version")
    assert sub and sub.get("ok") is True and sub.get("error_code") != "AUTOBOOT_QUIET", (rc, sub)
    deadline = time.monotonic() + 10.0
    while b"version" not in tgt.rx_log_since(mark):
        assert time.monotonic() < deadline, "命令應進 Arbiter 並送出 bytes"
        time.sleep(0.1)


def test_console_unlock_smart4r_like(p7, tmp_path):
    """console unlock：開機先輸出 CFE banner 不中止 unlock（SC-30 例外）；secret 不進 WAL；INFO 不出現在 stderr。"""
    secret = "p7-unlock-Secret"
    tgt = FakeTarget(
        tmp_path / "t0",
        prompt="root@smart4r:~# ",
        console_lock={"secret": secret, "idle_s": 0},
        boot_banner=b"\r\nCFE version 1.0.38-163.231 for BCM96858\r\n",
    )
    p7.start("console_unlock", [tgt])
    s = wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    assert s["last_error"] is None, s
    assert secret.encode() in tgt.rx_log()
    assert secret.encode() not in _tx_bytes(p7), "secret 不得進 WAL"
    err = (p7.daemon.workdir / "daemon.stderr").read_text(encoding="utf-8", errors="replace")
    assert secret not in err, err
    assert "INFO" not in err, f"INFO log 不得出現在 stderr：{err!r}"
