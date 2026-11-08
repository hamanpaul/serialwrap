"""黑箱 exit code 與錯誤處理契約測試。

依據 P3-T14 規範，驗證各種正常與異常情境下的 return code、stdout 與 stderr 輸出。
stdlib-only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bb_harness import isolated_env, run_cli

pytestmark = [pytest.mark.process_rpc]


@pytest.mark.core_portable
def test_rpc_ok(env, stubd):
    """RPC 成功時回傳 exit code 0，stdout 為 JSON，stderr 為空。"""
    s = stubd({"default": [{"reply": {"ok": True}}]})
    r = run_cli(["--socket", s.endpoint, "session", "list"], env=env)
    assert r.rc == 0
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["ok"] is True
    assert r.stderr == b""


@pytest.mark.core_portable
def test_rpc_fail(env, stubd):
    """RPC 業務失敗時回傳 exit code 2，stdout 為 JSON，stderr 印出具體錯誤行。"""
    s = stubd({"default": [{"reply": {"ok": False, "error_code": "X"}}]})
    r = run_cli(["--socket", s.endpoint, "session", "list"], env=env)
    assert r.rc == 2
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["ok"] is False
    assert data["error_code"] == "X"
    assert b"serialwrap: session.list failed: X" in r.stderr


@pytest.mark.core_portable
def test_resolution_error(env):
    """Endpoint 解析失敗（未記住的 bench）回傳 exit code 1，stdout 為 JSON，stderr 鏡像錯誤。"""
    r = run_cli(["--bench", "nope", "session", "list"], env=env)
    assert r.rc == 1
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["ok"] is False
    assert data["error_code"] == "BENCH_ENDPOINT_NOT_REMEMBERED"
    assert b"BENCH_ENDPOINT_NOT_REMEMBERED" in r.stderr


@pytest.mark.core_portable
def test_completion_regex_local(env, stubd):
    """本地正規表示式無效檢查失敗回傳 exit code 1，stdout 為 JSON，不連 daemon。"""
    s = stubd({"default": [{"reply": {"ok": True}}]})
    r = run_cli(
        ["--socket", s.endpoint, "cmd", "submit", "--selector", "A", "--completion-regex", "^"],
        env=env,
    )
    assert r.rc == 1
    data = json.loads(r.stdout.decode("utf-8"))
    assert data == {"ok": False, "error_code": "INVALID_COMPLETION_REGEX"}
    assert r.stderr == b""
    assert len(s.requests()) == 0


@pytest.mark.core_portable
def test_completion_regex_too_long(env, stubd):
    """超過長度上限的 completion-regex 回傳 exit code 1，stdout 為 JSON，不連 daemon。"""
    s = stubd({"default": [{"reply": {"ok": True}}]})
    r = run_cli(
        ["--socket", s.endpoint, "cmd", "submit", "--selector", "A", "--completion-regex", "a" * 1025],
        env=env,
    )
    assert r.rc == 1
    data = json.loads(r.stdout.decode("utf-8"))
    assert data == {"ok": False, "error_code": "INVALID_COMPLETION_REGEX"}
    assert r.stderr == b""
    assert len(s.requests()) == 0


@pytest.mark.core_portable
def test_argparse_error(env):
    """參數解析錯誤（缺少必要子命令）回傳 exit code 2，stdout 為空，stderr 輸出 usage 與 error。"""
    r = run_cli(["session"], env=env)
    assert r.rc == 2
    assert r.stdout == b""
    assert b"usage: serialwrap" in r.stderr
    assert b"error:" in r.stderr


@pytest.mark.core_portable
def test_socket_connect_boundary(env):
    """邊界情境（--socket connect 漏參數）回傳 exit code 1，stdout 為 INVALID_ARGS，stderr 輸出 usage、error 與失敗行。"""
    r = run_cli(["--socket", "connect"], env=env)
    assert r.rc == 1
    data = json.loads(r.stdout.decode("utf-8"))
    assert data == {"error_code": "INVALID_ARGS", "ok": False}
    assert b"usage: serialwrap" in r.stderr
    assert b"serialwrap: connect failed: INVALID_ARGS" in r.stderr


@pytest.mark.core_portable
def test_help_version(env):
    """--help 與 --version 回傳 exit code 0，stdout 有文字，stderr 為空。"""
    r_h = run_cli(["--help"], env=env)
    assert r_h.rc == 0
    assert b"usage: serialwrap" in r_h.stdout
    assert r_h.stderr == b""

    r_v = run_cli(["--version"], env=env)
    assert r_v.rc == 0
    assert b"serialwrap" in r_v.stdout
    assert r_v.stderr == b""


@pytest.mark.core_portable
def test_tcp_port_invalid(tmp_path):
    """SERIALWRAP_TCP_PORT 為無效字串時在初期環境解析崩潰，回傳 exit code 1，stderr 非空。"""
    env_bad = isolated_env(tmp_path, SERIALWRAP_TCP_PORT="")
    r = run_cli(["skill"], env=env_bad)
    assert r.rc == 1
    assert r.stdout == b""
    assert len(r.stderr) > 0


@pytest.mark.core_portable
def test_completion_regex_overflow(env, stubd):
    """正規表示式重複次數過大溢位時回傳 exit code 1，stderr 非空，不連 daemon。"""
    s = stubd({"default": [{"reply": {"ok": True}}]})
    r = run_cli(
        ["--socket", s.endpoint, "cmd", "submit", "--selector", "A", "--completion-regex", "a{99999999999}"],
        env=env,
    )
    assert r.rc == 1
    assert r.stdout == b""
    assert len(r.stderr) > 0
    assert len(s.requests()) == 0


@pytest.mark.core_portable
def test_tcp_gai_before_timeout(env):
    """TCP getaddrinfo 失敗早於 timeout 驗證，回傳 exit code 2，stdout 為 SOCKET_ERROR，stderr 鏡像。"""
    r = run_cli(
        ["--timeout", "-1", "--endpoint", "tcp://no-such-host.invalid:1", "device", "list"],
        env=env,
    )
    assert r.rc == 2
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["ok"] is False
    assert data["error_code"] == "SOCKET_ERROR"
    assert b"SOCKET_ERROR" in r.stderr


@pytest.mark.core_portable
def test_unix_bad_timeout(env, stubd):
    """Unix socket 連線前 timeout 驗證失敗回傳 exit code 1，stdout 為空，stderr 非空，不發送請求。"""
    s = stubd({"default": [{"reply": {"ok": True}}]})
    r = run_cli(["--timeout", "-1", "--socket", s.endpoint, "device", "list"], env=env)
    assert r.rc == 1
    assert r.stdout == b""
    assert len(r.stderr) > 0
    assert len(s.requests()) == 0


@pytest.mark.posix_only
def test_non_utf8_socket_path(tmp_path, env):
    """非 UTF-8 socket 路徑不存在時回傳 exit code 2，SOCKET_ERROR message 不含非 UTF-8 路徑。"""
    bad_sock = os.fsdecode(os.fsencode(str(tmp_path)) + b"/\xff.sock")
    r = run_cli(["--socket", bad_sock, "session", "list"], env=env)
    assert r.rc == 2
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["ok"] is False
    assert data["error_code"] == "SOCKET_ERROR"
    assert bad_sock not in data.get("message", "")
    assert b"SOCKET_ERROR" in r.stderr


@pytest.mark.posix_only
def test_non_utf8_endpoint_path(tmp_path, env):
    """非 UTF-8 endpoint 裸路徑回傳 exit code 2，stdout 為 SOCKET_ERROR，stderr 鏡像。"""
    bad_sock = os.fsdecode(os.fsencode(str(tmp_path)) + b"/\xff.sock")
    r = run_cli(["--endpoint", bad_sock, "session", "list"], env=env)
    assert r.rc == 2
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["ok"] is False
    assert data["error_code"] == "SOCKET_ERROR"
    assert b"SOCKET_ERROR" in r.stderr


@pytest.mark.posix_only
def test_non_utf8_selector_request(env, stubd):
    """非路徑 dest（selector）包含非 UTF-8 字元在組包或連線時失敗回傳 exit code 1，stdout 為空，stub 收到 0 筆請求行。"""
    s = stubd({"default": [{"reply": {"ok": True}}]})
    bad_sel = os.fsdecode(b"\xff")
    r = run_cli(["--socket", s.endpoint, "session", "clear", "--selector", bad_sel], env=env)
    assert r.rc == 1
    assert r.stdout == b""
    assert len(r.stderr) > 0
    assert len(s.requests()) == 0


@pytest.mark.posix_only
def test_non_utf8_selector_dead_socket(tmp_path, env):
    """socket 不可達早於 selector 非 UTF-8 編碼失敗，回傳 exit code 2，stdout 為 SOCKET_ERROR。"""
    bad_sel = os.fsdecode(b"\xff")
    dead_sock = str(tmp_path / "nope.sock")
    r = run_cli(["--socket", dead_sock, "session", "clear", "--selector", bad_sel], env=env)
    assert r.rc == 2
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["ok"] is False
    assert data["error_code"] == "SOCKET_ERROR"
    assert b"SOCKET_ERROR" in r.stderr


@pytest.mark.posix_only
@pytest.mark.rust_only
def test_non_utf8_bench_echo(env):
    """Rust strict 規則下非 UTF-8 bench 代號輸出時失敗回傳 exit code 1，stderr 為 internal error。"""
    bad_bench = os.fsdecode(b"\xff")
    r = run_cli(["--bench", bad_bench, "session", "list"], env=env)
    assert r.rc == 1
    assert r.stdout == b""
    assert b"serialwrap: internal error: argument is not valid UTF-8" in r.stderr


@pytest.mark.posix_only
def test_non_utf8_bench_echo_strict_locale(tmp_path):
    """Python strict locale 下非 UTF-8 bench 代號輸出失敗回傳 exit code 1，stdout 為空，stderr 非空。"""
    env_strict = isolated_env(tmp_path, PYTHONIOENCODING="utf-8:strict")
    bad_bench = os.fsdecode(b"\xff")
    r = run_cli(["--bench", bad_bench, "session", "list"], env=env_strict)
    assert r.rc == 1
    assert r.stdout == b""
    assert len(r.stderr) > 0
