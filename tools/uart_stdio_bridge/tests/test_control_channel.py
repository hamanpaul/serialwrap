"""tools/uart_stdio_bridge/tests/test_control_channel.py

測試 bridge/control.py：控制通道權杖驗證、control.json 私有權限、以及 control.ndjson 的 requested/completed 稽核記錄。
"""
from __future__ import annotations

import json
import os
import socket
import stat
import sys
import time
from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent
if str(_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(_TEST_DIR))
_SRC_DIR = _TEST_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from helpers import bridge_proc


def test_control_bad_token():
    """錯 token 的請求得 {"ok":false,"error":"HARNESS_CONTROL_PROTOCOL_ERROR"} 且連線被關。"""
    from uart_stdio_bridge.bridge.control import ControlServer

    handler = lambda action, params, aid: {"ok": True, "result": {}}
    server = ControlServer(handler, token="good_token_123")
    host, port = server.start()
    try:
        s = socket.create_connection((host, port), timeout=3.0)
        req = (
            json.dumps(
                {
                    "action": "status",
                    "action_id": "a1",
                    "params": {},
                    "token": "bad_token_456",
                }
            )
            + "\n"
        )
        s.sendall(req.encode("utf-8"))
        resp_data = s.recv(4096)
        assert resp_data
        resp = json.loads(resp_data.decode("utf-8").strip())
        assert resp.get("ok") is False
        assert resp.get("error") == "HARNESS_CONTROL_PROTOCOL_ERROR"
        # 連線被關閉（EOF）
        time.sleep(0.05)
        assert s.recv(1024) == b""
        s.close()
    finally:
        server.stop()


def test_control_info_file_mode(tmp_path: Path):
    """POSIX 上 control.json 權限為 0o600；內容 endpoint 以 tcp://127.0.0.1: 開頭。"""
    from uart_stdio_bridge.bridge.control import write_control_info

    info_file = tmp_path / "control.json"
    write_control_info(info_file, host="127.0.0.1", port=48999, token="tok_xyz")
    assert info_file.is_file()
    if os.name != "nt":
        mode = stat.S_IMODE(info_file.stat().st_mode)
        assert mode == 0o600
    data = json.loads(info_file.read_text(encoding="utf-8"))
    assert data["endpoint"].startswith("tcp://127.0.0.1:")
    assert data["token"] == "tok_xyz"


def test_control_log_requested_and_completed(bridge_proc):
    """呼叫 mark 後 control.ndjson 有同 action_id 的 requested 與 completed 兩筆，wire_seq 單調。"""
    b = bridge_proc()
    res = b.control.call("mark", {"label": "m1", "op_id": "op-001"})
    assert "wire_seq" in res
    b.control.call("shutdown")
    assert b.wait(10) == 0

    ctrl_file = b.evidence / "control.ndjson"
    assert ctrl_file.is_file()
    records = [
        json.loads(line)
        for line in ctrl_file.read_text(encoding="utf-8").splitlines()
    ]
    mark_records = [r for r in records if r.get("action") == "mark"]
    assert len(mark_records) == 2
    req, comp = mark_records[0], mark_records[1]
    assert req["phase"] == "requested"
    assert comp["phase"] == "completed"
    assert req["action_id"] == comp["action_id"]
    assert req["wire_seq"] <= comp["wire_seq"]
