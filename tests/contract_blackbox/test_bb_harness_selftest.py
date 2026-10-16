"""黑箱 harness 自測（P3-T2 紅燈）。

模組層只放資源 marker；平台 marker 由各函式自帶（同檔混合 POSIX-only 與可攜測試，P0 T14 規則 2）。
紅燈階段 harness 函式本體為 NotImplementedError("P3-T2")，自測以 xfail(strict) 標示。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bb_harness import NL, run_cli

pytestmark = [pytest.mark.process_rpc]

XFAIL_RED = pytest.mark.xfail(
    strict=True, raises=(NotImplementedError, AssertionError), reason="P3-T2 紅燈"
)


@pytest.mark.core_portable
@XFAIL_RED
def test_stubd_records_wire_and_cli_prints_sorted(env, stubd):
    s = stubd({"methods": {"session.list": [{"reply": {"version": "0.0.0-test", "sessions": [], "ok": True, "b": {"z": 1, "a": 2.0}}}]}})
    r = run_cli(["--socket", s.endpoint, "session", "list"], env=env)
    assert r.rc == 0
    assert r.stdout == b'{"b":{"a":2.0,"z":1},"ok":true,"sessions":[],"version":"0.0.0-test"}' + NL
    assert s.requests() == [b'{"id":1,"method":"session.list","params":{}}\n']


@pytest.mark.core_portable
@XFAIL_RED
def test_stubd_close_gives_empty_response(env, stubd):
    s = stubd({"default": [{"close": True}]})
    r = run_cli(["--socket", s.endpoint, "wal", "current-seq"], env=env)
    assert r.rc == 2
    assert json.loads(r.stdout) == {"ok": False, "error_code": "EMPTY_RESPONSE"}


@pytest.mark.posix_only
@pytest.mark.posix_pty
@XFAIL_RED
def test_faketarget_login_flow(tmp_path, faketarget):
    import os, time
    link = faketarget.start(tmp_path / "by-id")
    assert link.is_symlink()
    fd = os.open(os.path.realpath(link), os.O_RDWR | os.O_NOCTTY)
    def read_until(token: bytes, timeout: float = 3.0) -> bytes:
        buf, end = b"", time.monotonic() + timeout
        while token not in buf and time.monotonic() < end:
            try:
                buf += os.read(fd, 4096)
            except BlockingIOError:
                time.sleep(0.01)
        assert token in buf, buf
        return buf
    os.set_blocking(fd, False)
    read_until(b"login: ")
    os.write(fd, b"root\r"); read_until(b"Password: ")
    os.write(fd, b"pw\r"); read_until(b"root@fake:~# ")
    os.write(fd, b"echo hi\r"); out = read_until(b"hi\r\nroot@fake:~# ")
    assert b"echo hi\r\n" in out
    os.write(fd, b"\x03"); read_until(b"^C\r\nroot@fake:~# ")
    os.close(fd)
    assert faketarget.rx_log() == b"root\rpw\recho hi\r\x03"


@pytest.mark.core_portable
@XFAIL_RED
def test_isolated_env_strips_all_serialwrap_vars(env, monkeypatch, tmp_path):
    from bb_harness import isolated_env
    monkeypatch.setenv("SERIALWRAP_RELEASE_MANIFEST", "/x")
    monkeypatch.setenv("SERIALWRAP_SOMETHING_NEW", "1")
    e = isolated_env(tmp_path)
    assert not [k for k in e if k.startswith("SERIALWRAP_") and not k.endswith("_DIR")]
    for k in ("SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE", "SERIALWRAP_AB_LABEL", "SERIALWRAP_DELEGATE_DEPTH"):
        assert k not in env
    assert env["COLUMNS"] == "80"


# P2 Task 13 的 L1 報告 plugin 與獨立執行時的 sessionfinish 斷言；見 plan Task 2 conftest 段落。
@pytest.mark.core_portable
@pytest.mark.process_rpc
@XFAIL_RED
def test_bb_standalone_writes_l1_report(tmp_path):
    import os
    import subprocess
    import sys

    here = Path(__file__).resolve().parent  # tests/contract_blackbox
    rpt = tmp_path / "r.json"
    env = dict(os.environ, SW_RESOURCE_REPORT=str(rpt))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(here), f"--confcutdir={here}",
         "-p", "no:cacheprovider", "-k", "bb_no_product_import"],
        env=env, cwd=str(here.parents[1]), capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rpt.exists(), "L1 報告未寫出（sw_resource_report plugin 未載入）"
    data = json.loads(rpt.read_text(encoding="utf-8"))
    assert data["format"] == "l1-results.v1"
    assert data["finished"] is True
