"""P3-T12 黑箱：endpoint 解析（#108 dangling fallback、SC-09 config 探索）與 `--bench` state 讀取。

期望值依 Python 基線（`sw_core/cli.py:658-835`、`1115-1188`）實測；Rust 輪以 `SERIALWRAP_CLI_EXE` 指向 Rust 產物時執行同一組斷言。
SC-09（`SERIALWRAP_CONFIG`）相關案例原以 strict xfail 標示 P1 前置（plan §0.2）；P1 SC-09 已併入（017fbf1），標記已移除。
stdlib-only；不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bb_harness import NL, run_cli

pytestmark = [pytest.mark.process_rpc]

_CORRUPT_MSG = 'bench "lab" 的 endpoint 記憶損壞；請重新執行 serialwrap connect lab'
_NOT_REMEMBERED_MSG = 'bench "lab" 尚未記住 endpoint；請先執行 serialwrap connect lab'
_SESSION_LIST_REPLY = {"ok": True, "sessions": []}
_SESSION_LIST_SCRIPT = {"methods": {"session.list": [{"reply": _SESSION_LIST_REPLY}]}}
def _line(obj: dict) -> bytes:
    """CLI JSON 輸出：`sort_keys`、緊湊分隔、`ensure_ascii=False`（Q-01）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + NL


def _err_line(context: str, error_code: str, message: str) -> bytes:
    """stderr 錯誤行：`serialwrap: {ctx} failed: {code}: {message}`（Q-02）。"""
    return f"serialwrap: {context} failed: {error_code}: {message}".encode("utf-8") + NL


def _bench_state(env: dict[str, str]) -> Path:
    return Path(env["SERIALWRAP_STATE_DIR"]) / "benches.state.json"


def _user_config(env: dict[str, str]) -> Path:
    return Path(env["SERIALWRAP_CONFIG_DIR"]) / "config.yaml"


def _yaml_str(text: str) -> str:
    """JSON 字串為合法 YAML double-quoted scalar，用於產生 config.yaml 的路徑值。"""
    return json.dumps(text)


# ---------------------------------------------------------------------------
# --bench（cli.py:1115-1188）
# ---------------------------------------------------------------------------


@pytest.mark.core_portable
def test_bench_remembered_tcp_endpoint_routes_request(env, stubd):
    s = stubd(_SESSION_LIST_SCRIPT, transport="tcp")
    _bench_state(env).write_text(json.dumps({"lab": s.endpoint}), encoding="utf-8")
    r = run_cli(["--bench", "lab", "session", "list"], env=env)
    assert r.rc == 0
    assert r.stdout == _line(_SESSION_LIST_REPLY)
    assert r.stderr == b""
    assert s.requests() == [b'{"id":1,"method":"session.list","params":{}}\n']


@pytest.mark.core_portable
def test_bench_missing_state_is_not_remembered(env):
    r = run_cli(["--bench", "lab", "session", "list"], env=env)
    assert r.rc == 1
    assert r.stdout == _line(
        {"error_code": "BENCH_ENDPOINT_NOT_REMEMBERED", "message": _NOT_REMEMBERED_MSG, "ok": False}
    )
    assert r.stderr == _err_line("session.list", "BENCH_ENDPOINT_NOT_REMEMBERED", _NOT_REMEMBERED_MSG)


_INVALID_STATE_CASES = [
    pytest.param(b"[1]", "benches.state.json 必須是 object", id="not-object"),
    pytest.param(
        b'{"benches":[1]}',
        "benches.state.json 的 benches 欄位必須是 object",
        id="benches-not-object",
    ),
    pytest.param(
        b"{",
        "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
        id="bad-json",
    ),
    pytest.param(b'{"lab":"tcp://10.0.0.1:7001"}', _CORRUPT_MSG, id="non-loopback"),
    pytest.param(b'{"lab":"/x.sock"}', _CORRUPT_MSG, id="unix-value"),
    pytest.param(
        b"\xff",
        "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
        id="bad-utf8",
    ),
]


@pytest.mark.core_portable
@pytest.mark.parametrize("content, message", _INVALID_STATE_CASES)
def test_bench_invalid_state_is_rejected(env, content, message):
    _bench_state(env).write_bytes(content)
    r = run_cli(["--bench", "lab", "session", "list"], env=env)
    assert r.rc == 1
    assert r.stdout == _line({"error_code": "INVALID_BENCH_STATE", "message": message, "ok": False})
    assert r.stderr == _err_line("session.list", "INVALID_BENCH_STATE", message)


@pytest.mark.posix_only
def test_bench_state_directory_is_io_error(env):
    # Windows 的 open(目錄) 回 [Errno 13]，訊息不同，故只在 POSIX 比對。
    _bench_state(env).mkdir()
    path = str(_bench_state(env))
    message = f"[Errno 21] Is a directory: '{path}'"
    r = run_cli(["--bench", "lab", "session", "list"], env=env)
    assert r.rc == 1
    assert r.stdout == _line({"error_code": "BENCH_STATE_IO_ERROR", "message": message, "ok": False})
    assert r.stderr == _err_line("session.list", "BENCH_STATE_IO_ERROR", message)


# ---------------------------------------------------------------------------
# --endpoint／--socket 明確值（Q-07、Q-08 相關）
# ---------------------------------------------------------------------------

_ENDPOINT_CASES = [
    pytest.param("tcp://h:1/", "invalid tcp endpoint: 'tcp://h:1/'", id="tcp-with-path"),
    pytest.param("foo://x", "unsupported endpoint scheme: 'foo'", id="unknown-scheme"),
    pytest.param("tcp://[::1", "Invalid IPv6 URL", id="broken-ipv6"),
]


@pytest.mark.core_portable
@pytest.mark.parametrize("endpoint, message", _ENDPOINT_CASES)
def test_invalid_endpoint_flag_is_rejected(env, endpoint, message):
    r = run_cli(["--endpoint", endpoint, "session", "list"], env=env)
    assert r.rc == 2
    assert r.stdout == _line({"error_code": "INVALID_ENDPOINT", "message": message, "ok": False})
    assert r.stderr == _err_line("session.list", "INVALID_ENDPOINT", message)


@pytest.mark.posix_only
def test_explicit_empty_socket_does_not_fall_back_to_config(env, stubd):
    # `--socket ""` 為明確指定（Q-07）：即使 config.yaml 指向可連的 stub，也不得改連。
    s = stubd(_SESSION_LIST_SCRIPT)
    _user_config(env).write_text(f"socket_path: {_yaml_str(str(s.socket_path))}\n", encoding="utf-8")
    r = run_cli(["--socket", "", "session", "list"], env=env)
    assert r.rc == 2
    assert r.stdout == _line({"error_code": "SOCKET_ERROR", "message": "[Errno 22] Invalid argument", "ok": False})
    assert s.requests() == []


@pytest.mark.core_portable
@pytest.mark.parametrize(
    "port, argv",
    [
        pytest.param("", ["skill"], id="empty-port-skill"),
        pytest.param("abc", ["skill"], id="abc-port-skill"),
        pytest.param("abc", ["--help"], id="abc-port-help"),
    ],
)
def test_unparsable_tcp_port_env_exits_1(env, port, argv):
    # GAP-X1（Q-15）：SERIALWRAP_TCP_PORT 非 int() 可解析時，任何命令（含 --help）都 exit 1 且 stdout 為空。
    e = dict(env)
    e["SERIALWRAP_TCP_PORT"] = port
    r = run_cli(argv, env=e)
    assert r.rc == 1
    assert r.stdout == b""


# ---------------------------------------------------------------------------
# config.yaml 與 #108 dangling fallback
# ---------------------------------------------------------------------------


@pytest.mark.posix_only
def test_dangling_config_socket_falls_back_to_canonical(env, stubd, tmp_path):
    dead = tmp_path / "gone" / "cfg.sock"  # 父目錄不存在，connect 必定失敗
    _user_config(env).write_text(f"socket_path: {_yaml_str(str(dead))}\n", encoding="utf-8")
    s = stubd(_SESSION_LIST_SCRIPT)
    canonical = Path(env["SERIALWRAP_RUN_DIR"]) / "serialwrapd.sock"
    os.symlink(s.socket_path, canonical)  # canonical 路徑指向可連的 stub socket
    r = run_cli(["session", "list"], env=env)
    assert r.rc == 0
    assert r.stdout == _line(_SESSION_LIST_REPLY)
    assert r.stderr == (
        f"serialwrap: config.yaml socket_path '{dead}' 不可連，依 supervision_mode=on-demand "
        f"改用 '{canonical}'；如需修正請更新 config.yaml 或重跑 serialwrap setup。\n"
    ).encode("utf-8")
    assert s.requests() == [b'{"id":1,"method":"session.list","params":{}}\n']


# ---------------------------------------------------------------------------
# SC-09：SERIALWRAP_CONFIG（P1 前置已併入）
# ---------------------------------------------------------------------------


@pytest.mark.core_portable
def test_serialwrap_config_env_selects_config_file(env, stubd, tmp_path):
    s = stubd(_SESSION_LIST_SCRIPT, transport="tcp")
    cfg = tmp_path / "x.yaml"
    cfg.write_text(f"socket_path: {_yaml_str(s.endpoint)}\n", encoding="utf-8")
    e = dict(env)
    e["SERIALWRAP_CONFIG"] = str(cfg)
    r = run_cli(["session", "list"], env=e)
    assert r.rc == 0
    assert r.stdout == _line(_SESSION_LIST_REPLY)
    assert r.stderr == b""


@pytest.mark.posix_only
def test_missing_serialwrap_config_does_not_read_user_config(env, stubd, tmp_path):
    # 指定但不存在的 SERIALWRAP_CONFIG 為空設定，不得回退讀取使用者 config.yaml（SC-09）。
    # 預設 endpoint 只在 POSIX 比對，避免 Windows 預設 TCP 埠與實機 daemon 衝突。
    s = stubd(_SESSION_LIST_SCRIPT)
    _user_config(env).write_text(f"socket_path: {_yaml_str(str(s.socket_path))}\n", encoding="utf-8")
    e = dict(env)
    e["SERIALWRAP_CONFIG"] = str(tmp_path / "missing.yaml")
    r = run_cli(["session", "list"], env=e)
    assert r.rc == 2
    assert r.stdout == _line({"error_code": "SOCKET_ERROR", "message": "[Errno 2] No such file or directory", "ok": False})
    assert s.requests() == []
