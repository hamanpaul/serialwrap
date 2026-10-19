"""CLI RPC trace 契約測試（P3-T11）。

驗證 -v／-vv／SERIALWRAP_LOG_LEVEL 與 --retries 產生的 trace JSON 欄位與順序。
stdlib-only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import json
import os

import pytest

from bb_harness import run_cli

pytestmark = [pytest.mark.process_rpc]


@pytest.mark.core_portable
def test_trace_stderr_json_format(env):
    """-v --socket /nonexistent/x.sock session list 的 stderr 第 1 行為 trace JSON，第 2 行為錯誤訊息。"""
    r = run_cli(["-v", "--socket", "/nonexistent/x.sock", "session", "list"], env=env)
    assert r.rc == 2
    err_lines = r.stderr.decode("utf-8").splitlines()
    assert len(err_lines) >= 2

    if os.name == "nt":
        # Windows CPython 無 AF_UNIX：Python 基線（client.py `_rpc_call_once`）不嘗試連線、不記 errno。
        expected_errno, expected_errno_name = None, None
        expected_line = (
            "serialwrap: session.list failed: SOCKET_ERROR: 本平台無 AF_UNIX，無法連接 unix endpoint "
            "'/nonexistent/x.sock'（請改用 tcp://127.0.0.1:<port>，#131）"
        )
    else:
        expected_errno, expected_errno_name = 2, "ENOENT"
        expected_line = "serialwrap: session.list failed: SOCKET_ERROR: [Errno 2] No such file or directory"

    trace_obj = json.loads(err_lines[0])
    assert isinstance(trace_obj.get("elapsed_ms"), int)
    trace_obj["elapsed_ms"] = 0
    assert trace_obj == {
        "endpoint_transport": "unix",
        "endpoint_id": "534a413975f6a6393e9cc37ef545b4492fedd7cab94f4359d7949e0b54f82ec2",
        "endpoint_source": "--socket",
        "method": "session.list",
        "elapsed_ms": 0,
        "error_code": "SOCKET_ERROR",
        "errno": expected_errno,
        "errno_name": expected_errno_name,
        "retry_count": 0,
        "timeout_s": 5.0,
    }
    assert err_lines[1] == expected_line


@pytest.mark.core_portable
def test_trace_vv_enables_trace(env):
    """-vv 旗標亦啟用 trace 輸出（DEBUG 層級 <= INFO 門檻）。"""
    r = run_cli(["-vv", "--socket", "/nonexistent/x.sock", "session", "list"], env=env)
    assert r.rc == 2
    err_lines = r.stderr.decode("utf-8").splitlines()
    assert len(err_lines) >= 2
    trace_obj = json.loads(err_lines[0])
    assert trace_obj.get("method") == "session.list"
    assert trace_obj.get("endpoint_source") == "--socket"


@pytest.mark.core_portable
@pytest.mark.parametrize(
    "level,expected_trace",
    [
        ("INFO", True),
        ("0", False),
        ("-5", True),
        ("NOTSET", False),
        ("garbage", False),
    ],
)
def test_trace_log_level_env(env, level, expected_trace):
    """SERIALWRAP_LOG_LEVEL 各值下 trace 輸出啟用情況與 Python 一致。"""
    custom_env = dict(env)
    custom_env["SERIALWRAP_LOG_LEVEL"] = level
    r = run_cli(["--socket", "/nonexistent/x.sock", "session", "list"], env=custom_env)
    assert r.rc == 2
    err_lines = r.stderr.decode("utf-8").splitlines()
    has_trace = any(l.startswith('{"endpoint_transport":') for l in err_lines)
    assert has_trace == expected_trace


@pytest.mark.core_portable
def test_trace_retries_count(env):
    """--retries 1 時，失敗 trace 的 retry_count 欄位值為 1。"""
    r = run_cli(
        ["-v", "--retries", "1", "--socket", "/nonexistent/x.sock", "session", "list"],
        env=env,
    )
    assert r.rc == 2
    err_lines = r.stderr.decode("utf-8").splitlines()
    assert len(err_lines) >= 2
    trace_obj = json.loads(err_lines[0])
    assert trace_obj.get("retry_count") == 1
