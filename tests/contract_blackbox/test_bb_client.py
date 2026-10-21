"""黑箱 RPC client 契約測試（白名單重試、退避、TIMEOUT 探測）。

依計畫 P3-T13（lines 2438-2633）以 stubd 與 CLI 子程序驗證客戶端行為。
本檔為 stdlib-only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from bb_harness import is_rust, run_cli

pytestmark = [pytest.mark.process_rpc]

SIDE_EFFECT_COMMANDS = [
    ["cmd", "submit", "--selector", "A", "--cmd", "ls"],
    ["session", "interactive-send", "--interactive-id", "i", "--data", "d"],
    ["device", "release", "--selector", "A"],
    ["device", "attach", "--selector", "A"],
    ["file", "push", "--selector", "A", "--local", "l", "--remote", "r"],
    ["wal", "reset"],
    ["session", "recover", "--selector", "A"],
    ["cmd", "cancel", "--cmd-id", "c"],
    ["cmd", "result-tail", "--cmd-id", "c"],
]


@pytest.mark.core_portable
@pytest.mark.parametrize("method_args", SIDE_EFFECT_COMMANDS)
def test_side_effect_methods_single_send(env, stubd, method_args):
    """具有副作用或非唯讀白名單的方法即使帶 --retries 3 也只發送 1 次請求。"""
    s = stubd({"default": [{"close": True}]})
    r = run_cli(["--retries", "3", "--socket", s.endpoint] + method_args, env=env)
    assert r.rc == 2
    reqs = [
        req
        for req in s.requests()
        if b'"health.ping"' not in req and b'"health.status"' not in req
    ]
    assert len(reqs) == 1


@pytest.mark.core_portable
def test_whitelist_retry_success(env, stubd):
    """唯讀白名單方法在遇到暫態錯誤時進行指數退避重試，重試成功後 rc 為 0。"""
    s = stubd(
        {
            "methods": {
                "session.list": [
                    {"close": True},
                    {"close": True},
                    {"reply": {"ok": True, "sessions": []}},
                ]
            }
        }
    )
    t0 = time.monotonic()
    r = run_cli(["--retries", "2", "--socket", s.endpoint, "session", "list"], env=env)
    elapsed = time.monotonic() - t0
    assert r.rc == 0
    assert len(s.requests()) == 3
    assert elapsed >= 1.4


@pytest.mark.core_portable
def test_interactive_status_never_retries(env, stubd):
    """session.interactive_status 絕不重試，即使帶 --retries 2 也只連線 1 次。"""
    s = stubd({"default": [{"close": True}]})
    r = run_cli(
        [
            "--retries",
            "2",
            "--socket",
            s.endpoint,
            "session",
            "interactive-status",
            "--interactive-id",
            "i",
        ],
        env=env,
    )
    assert r.rc == 2
    reqs = [
        req
        for req in s.requests()
        if b'"health.ping"' not in req and b'"health.status"' not in req
    ]
    assert len(reqs) == 1


@pytest.mark.core_portable
def test_event_list_never_retries(env, stubd):
    """event 群組方法永不重試，即使帶 --retries 2 也只發送 1 次。"""
    s = stubd({"default": [{"close": True}]})
    r = run_cli(["--retries", "2", "--socket", s.endpoint, "event", "list"], env=env)
    assert r.rc == 2
    reqs = [
        req
        for req in s.requests()
        if b'"health.ping"' not in req and b'"health.status"' not in req
    ]
    assert len(reqs) == 1


@pytest.mark.core_portable
def test_timeout_enrich_daemon_reachable(env, stubd):
    """TIMEOUT 後對非探測方法進行輕量 health.ping 探測，附上 daemon_reachable。"""
    s = stubd(
        {
            "methods": {
                "device.list": [{"delay_s": 3, "reply": {"ok": True, "devices": []}}],
                "health.ping": [{"reply": {"ok": True, "pong": True}}],
            }
        }
    )
    r = run_cli(["--timeout", "1", "--socket", s.endpoint, "device", "list"], env=env)
    assert r.rc == 2
    data = json.loads(r.stdout.decode("utf-8"))
    assert data.get("ok") is False
    assert data.get("error_code") == "TIMEOUT"
    assert "daemon_reachable" in data
    assert data["daemon_reachable"] is True


@pytest.mark.posix_only
def test_timeout_zero_nonblocking(env):
    """--timeout 0 為非阻塞模式，TCP connect 回報 [Errno 115] Operation now in progress。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    r = run_cli(
        ["--timeout", "0", "--endpoint", f"tcp://127.0.0.1:{port}", "mcu", "status"],
        env=env,
    )
    assert r.rc == 2
    data = json.loads(r.stdout.decode("utf-8"))
    assert data.get("ok") is False
    assert data.get("error_code") == "SOCKET_ERROR"
    msg = data.get("message", "")
    assert "115" in msg or "Operation now in progress" in msg


@pytest.mark.core_portable
def test_timeout_negative_exit_1(env):
    """--timeout -1 為非法逾時值，在 socket 建立時拋出例外退出碼為 1，stdout 為空。

    用 TCP endpoint：Windows CPython 無 AF_UNIX，unix 路徑會先回 SOCKET_ERROR（rc 2）而走不到
    settimeout；TCP 在兩平台都於 `create_connection` 內 settimeout 拋 ValueError（不會連線）。
    unix 路徑的同一契約由 `test_unix_bad_timeout` 覆蓋。
    """
    r = run_cli(
        ["--timeout", "-1", "--socket", "tcp://127.0.0.1:9", "session", "list"],
        env=env,
    )
    assert r.rc == 1
    assert r.stdout == b""


@pytest.mark.core_portable
def test_not_json_is_invalid_response(env, stubd):
    """daemon 回傳非 JSON 內容時，client 應回報 INVALID_RESPONSE 且 rc 為 2（DF-06）。"""
    s = stubd({"default": [{"reply_raw": "not json\n"}]})
    r = run_cli(["--socket", s.endpoint, "session", "list"], env=env)
    assert r.rc == 2
    data = json.loads(r.stdout.decode("utf-8"))
    assert data.get("ok") is False
    assert data.get("error_code") == "INVALID_RESPONSE"
