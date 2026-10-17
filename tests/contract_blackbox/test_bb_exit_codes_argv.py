"""非 UTF-8 argv 與 `--option=value`／縮寫形式的 exit code、stdout 與 zero-TX 契約測試（Q-19、Q-22）。

以 Python 基線為 oracle：非 UTF-8 參數（以 surrogateescape 保留）在要進 RPC 請求時於 connect 後、送出前失敗，
daemon 收到 0 筆請求行；socket／endpoint 路徑以原始位元組運作；合法的 NUL 回應原樣輸出。
stdlib-only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from bb_harness import run_cli

pytestmark = [pytest.mark.process_rpc]

# 非 UTF-8 位元組 0xFF 的 surrogateescape 字串（子行程 argv 會原樣還原為該位元組）。
BAD = os.fsdecode(b"\xff")
BAD_ESC_STDERR = b"argument is not valid UTF-8"


class _RawUnixStub:
    """以原始位元組路徑綁定的 AF_UNIX stub：每行請求回同一 reply，並記錄收到的請求行。"""

    def __init__(self, path: bytes, reply: dict) -> None:
        self.path = path
        self.requests: list[bytes] = []
        self._reply = reply
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(path)
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                if b"\n" in buf:
                    self.requests.append(buf.split(b"\n", 1)[0] + b"\n")
                    payload = json.dumps(self._reply, ensure_ascii=False, separators=(",", ":"))
                    conn.sendall(payload.encode("utf-8") + b"\n")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)
        self._srv.close()


@pytest.fixture
def short_dir():
    """AF_UNIX 路徑長度上限 108 位元組，故以 /tmp 下的短目錄放 socket。"""
    d = Path(tempfile.mkdtemp(prefix="swargv"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def raw_stub(short_dir: Path):
    made: list[_RawUnixStub] = []

    def _make(name: bytes, reply: dict) -> _RawUnixStub:
        stub = _RawUnixStub(os.fsencode(str(short_dir)) + b"/" + name, reply)
        made.append(stub)
        return stub

    yield _make
    for stub in made:
        stub.close()


# ---------------------------------------------------------------------------
# 非 UTF-8 值進 RPC 請求：connect 後失敗、stdout 空、daemon 0 筆（Q-19 (b)）
# ---------------------------------------------------------------------------

_ZERO_TX_ARGV = [
    pytest.param(["session", "clear", "--selector", BAD], id="space-form"),
    pytest.param(["session", "clear", "--selector=" + BAD], id="equals-form"),
    pytest.param(["session", "clear", "--sel", BAD], id="abbreviated-space-form"),
    pytest.param(["session", "clear", "--sel=" + BAD], id="abbreviated-equals-form"),
    pytest.param(["cmd", "submit", "--selector", "A", "--cmd=echo " + os.fsdecode(b"\xe9")], id="cmd-equals"),
    pytest.param(
        ["session", "interactive-send", "--interactive-id", "i", "--data=" + BAD],
        id="interactive-send-equals",
    ),
    pytest.param(
        ["file", "push", "--selector", "A", "--local", "l", "--remote=" + BAD],
        id="file-push-remote-equals",
    ),
]


@pytest.mark.posix_only
@pytest.mark.parametrize("argv", _ZERO_TX_ARGV)
def test_non_utf8_request_value_zero_tx(argv, env, stubd):
    """`=` 與縮寫形式也必須在送出前失敗：rc 1、stdout 空、stub 收到 0 筆請求行。"""
    s = stubd({"default": [{"reply": {"ok": True}}]})
    r = run_cli(["--socket", s.endpoint, *argv], env=env)
    assert r.rc == 1
    assert r.stdout == b""
    assert r.stderr != b""
    assert s.requests() == []


@pytest.mark.posix_only
def test_non_utf8_completion_regex_is_local_invalid(env, stubd):
    """`--completion-regex` 非 UTF-8：在連 daemon 前回 INVALID_COMPLETION_REGEX（rc 1、stub 0 筆）。"""
    s = stubd({"default": [{"reply": {"ok": True}}]})
    r = run_cli(
        ["--socket", s.endpoint, "cmd", "submit", "--selector", "A", "--completion-regex", BAD],
        env=env,
    )
    assert r.rc == 1
    assert r.stdout == b'{"error_code":"INVALID_COMPLETION_REGEX","ok":false}\n'
    assert s.requests() == []


@pytest.mark.posix_only
def test_argparse_int_option_rejects_non_utf8_before_dispatch(env, stubd):
    """`event tail -n` 為 int 型別：非 UTF-8 在 argparse 階段即 rc 2，不送出任何請求。"""
    s = stubd({"default": [{"reply": {"ok": True}}]})
    r = run_cli(["--socket", s.endpoint, "event", "tail", "-n" + BAD], env=env)
    assert r.rc == 2
    assert r.stdout == b""
    assert s.requests() == []


@pytest.mark.posix_only
def test_non_utf8_unrecognized_argument_text(env):
    """未知的非 UTF-8 位置參數：stderr 以 `\\udcff` 文字呈現（backslashreplace），rc 2、stdout 空。"""
    r = run_cli(["session", "list", BAD], env=env)
    assert r.rc == 2
    assert r.stdout == b""
    assert b"serialwrap: error: unrecognized arguments: \\udcff" in r.stderr


# ---------------------------------------------------------------------------
# 合法 NUL 的 daemon 回應：照常輸出（不得誤判為非 UTF-8 參數）
# ---------------------------------------------------------------------------


@pytest.mark.core_portable
def test_nul_in_ok_response_is_printed(env, stubd):
    """回應含 U+0000（UART 文字可能含 NUL）：rc 0，stdout 為 JSON 跳脫後的原樣回應。"""
    s = stubd({"default": [{"reply": {"ok": True, "text": "a\x00b"}}]})
    r = run_cli(["--socket", s.endpoint, "log", "tail-text", "--com", "COM0"], env=env)
    assert r.rc == 0
    assert r.stdout == b'{"ok":true,"text":"a\\u0000b"}\n'


@pytest.mark.core_portable
def test_nul_in_failed_response_is_printed(env, stubd):
    """失敗回應含 NUL：rc 2，stdout 仍為 JSON 原樣輸出。"""
    s = stubd({"default": [{"reply": {"ok": False, "error_code": "X", "text": "a\x00b"}}]})
    r = run_cli(["--socket", s.endpoint, "log", "tail-text", "--com", "COM0"], env=env)
    assert r.rc == 2
    assert r.stdout == b'{"error_code":"X","ok":false,"text":"a\\u0000b"}\n'


# ---------------------------------------------------------------------------
# 非 UTF-8 socket／endpoint 路徑以原始位元組連線（Q-19 (a)）
# ---------------------------------------------------------------------------

_PATH_FORMS = [
    pytest.param(lambda p: ["--socket", p], id="socket-space"),
    pytest.param(lambda p: ["--socket=" + p], id="socket-equals"),
    pytest.param(lambda p: ["--endpoint", p], id="endpoint-bare-space"),
    pytest.param(lambda p: ["--endpoint=" + p], id="endpoint-bare-equals"),
    pytest.param(lambda p: ["--endpoint", "unix://" + p], id="endpoint-unix-scheme"),
]


@pytest.mark.posix_only
@pytest.mark.parametrize("make_args", _PATH_FORMS)
def test_non_utf8_socket_path_is_reachable(make_args, env, raw_stub):
    """既存的非 UTF-8 socket 必須可連線：rc 0、stdout 為 daemon 回應、stub 收到 1 筆請求。"""
    stub = raw_stub(b"s\xff.sock", {"ok": True})
    r = run_cli([*make_args(os.fsdecode(stub.path)), "session", "list"], env=env)
    assert r.rc == 0, r.stderr
    assert r.stdout == b'{"ok":true}\n'
    assert len(stub.requests) == 1


@pytest.mark.posix_only
def test_non_utf8_socket_trace_hashes_raw_bytes(env, raw_stub, short_dir):
    """`-vv` 的 endpoint_id 以原始位元組（surrogateescape）雜湊，與 Python 基線相同。"""
    stub = raw_stub(b"t\xff.sock", {"ok": True})
    r = run_cli(["-vv", "--socket", os.fsdecode(stub.path), "session", "list"], env=env)
    assert r.rc == 0
    trace = json.loads(r.stderr.decode("utf-8").splitlines()[0])
    assert trace["endpoint_id"] == hashlib.sha256(stub.path).hexdigest()


@pytest.mark.posix_only
def test_non_utf8_endpoint_repr_in_invalid_unix_endpoint(env):
    """`unix://` 相對路徑含非 UTF-8：INVALID_ENDPOINT 訊息以 `\\udcff` repr 呈現（ASCII），rc 2。"""
    r = run_cli(["--endpoint", "unix://rel" + BAD, "session", "list"], env=env)
    assert r.rc == 2
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["error_code"] == "INVALID_ENDPOINT"
    assert "'unix://rel\\udcff'" in data["message"]


@pytest.mark.posix_only
def test_non_utf8_tcp_host_is_crash_exit_1(env):
    """tcp 主機名含非 UTF-8：Python 於 getaddrinfo 拋未攔截 UnicodeError（rc 1、stdout 空）。"""
    r = run_cli(["--endpoint", "tcp://" + BAD + ":7777", "session", "list"], env=env)
    assert r.rc == 1
    assert r.stdout == b""
