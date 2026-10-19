# tests/test_telnet_client.py
"""Windows Telnet console client 與 TelnetClientCodec 協商、過濾與編解碼測試。"""
from __future__ import annotations

import pathlib
import socket
import sys
import threading
import time
from typing import Any

import pytest

pytestmark = [pytest.mark.core_portable]

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FUNC_TEST_DIR = REPO_ROOT / "func-test"
if str(FUNC_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(FUNC_TEST_DIR))


@pytest.mark.memory_only
def test_codec_negotiation():
    """驗證 TelnetClientCodec 對 server greeting 的協商回應與不重複送出相同選項。"""
    from realhw.telnet_client import TelnetClientCodec

    codec = TelnetClientCodec()
    # Server greeting: FF FB 01 (WILL ECHO), FF FB 03 (WILL SGA), FF FD 03 (DO SGA), FF FB 00 (WILL BINARY)
    greeting = bytes.fromhex("fffb01fffb03fffd03fffb00")
    app_data, reply = codec.feed(greeting)

    assert app_data == b""
    # Client expected response: FF FD 01 (DO ECHO), FF FD 03 (DO SGA), FF FB 03 (WILL SGA), FF FD 00 (DO BINARY)
    expected_reply = bytes.fromhex("fffd01fffd03fffb03fffd00")
    assert reply == expected_reply

    # 再次餵入相同 greeting，不應重複回應
    app_data2, reply2 = codec.feed(greeting)
    assert app_data2 == b""
    assert reply2 == b""


@pytest.mark.memory_only
def test_codec_strips_sb_and_unescapes():
    """驗證 TelnetClientCodec 移除 subnegotiation (SB...SE) 並將 IAC IAC (FF FF) 還原為 0xFF。"""
    from realhw.telnet_client import TelnetClientCodec

    codec = TelnetClientCodec()
    # b"a\xff\xfa\x18\x01\xff\xf0b\xff\xffc"
    # IAC SB 18 01 IAC SE 被丟棄，IAC IAC (FF FF) -> FF
    data = b"a\xff\xfa\x18\x01\xff\xf0b\xff\xffc"
    app_data, reply = codec.feed(data)
    assert app_data == b"ab\xffc"


@pytest.mark.memory_only
def test_codec_split_across_feeds():
    """驗證序列跨 feed 逐 byte 餵入時解碼結果一致。"""
    from realhw.telnet_client import TelnetClientCodec

    codec = TelnetClientCodec()
    data = b"a\xff\xfa\x18\x01\xff\xf0b\xff\xffc"
    chunks: list[bytes] = []
    for byte in data:
        app_data, _ = codec.feed(bytes([byte]))
        if app_data:
            chunks.append(app_data)
    assert b"".join(chunks) == b"ab\xffc"


@pytest.mark.memory_only
def test_encode_escapes_iac():
    """驗證 TelnetClientCodec.encode 將 0xFF 跳脫為 0xFF 0xFF。"""
    from realhw.telnet_client import TelnetClientCodec

    assert TelnetClientCodec.encode(b"\xff\x00") == b"\xff\xff\x00"
    assert TelnetClientCodec.encode(b"hello\xffworld") == b"hello\xff\xffworld"


@pytest.mark.process_rpc
def test_telnet_console_against_fake_server():
    """驗證 TelnetConsole 與本機 fake server 連線、協商及指令/資料過濾。"""
    from lib.console_client import TelnetConsole

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    received_by_server = bytearray()
    server_done = threading.Event()

    def server_thread():
        try:
            conn, _ = server_sock.accept()
            # 送 greeting: WILL ECHO (FF FB 01), WILL SGA (FF FB 03)
            conn.sendall(bytes.fromhex("fffb01fffb03"))
            conn.settimeout(3.0)
            while not server_done.is_set():
                try:
                    data = conn.recv(1024)
                    if not data:
                        break
                    received_by_server.extend(data)
                    if b"ls\r\n" in received_by_server:
                        # 回送包含 IAC 命令與資料
                        conn.sendall(b"output1\r\n\xff\xfb\x01output2\r\n")
                        break
                except socket.timeout:
                    break
            conn.close()
        finally:
            server_sock.close()

    th = threading.Thread(target=server_thread, daemon=True)
    th.start()

    try:
        console = TelnetConsole(f"127.0.0.1:{port}", timeout_s=5.0)
        console.write(b"ls\r\n")
        # server 收到協商回覆 (DO ECHO, DO SGA: fffd01fffd03) + b"ls\r\n"
        time_limit = time.monotonic() + 3.0
        while time.monotonic() < time_limit and b"ls\r\n" not in received_by_server:
            time.sleep(0.05)

        assert bytes.fromhex("fffd01fffd03") in received_by_server
        assert b"ls\r\n" in received_by_server

        # read 得去除指令後的資料
        read_data = console.read(timeout_s=3.0)
        assert read_data == b"output1\r\noutput2\r\n"
        console.close()
    finally:
        server_done.set()
        th.join(timeout=2.0)
