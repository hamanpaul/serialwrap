"""tools/uart_stdio_bridge/tests/test_dut_process.py

測試 bridge/dut_process.py：DUT 行程生命週期、二進位 pipe 透傳、世代累加與 stdin 關閉異常。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_TEST_DIR = Path(__file__).resolve().parent
if str(_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(_TEST_DIR))
_SRC_DIR = _TEST_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


def test_dut_process_binary_pipes():
    """echo DUT 寫入 b"\\x00\\r\\n\\xff"，stdout_read 得完全相同 bytes。"""
    from uart_stdio_bridge.bridge.dut_process import DutProcess

    argv_factory = lambda gen: [
        sys.executable,
        "-c",
        "import sys; data = sys.stdin.buffer.read(4); sys.stdout.buffer.write(data); sys.stdout.buffer.flush()",
    ]
    dut = DutProcess(argv_factory, mode="local-process")
    dut.spawn()
    try:
        payload = b"\x00\r\n\xff"
        n = dut.stdin_write_some(payload)
        assert n == len(payload)
        dut.close_stdin()
        recvd = dut.stdout_read(1024)
        assert recvd == payload
    finally:
        dut.terminate()


def test_dut_process_respawn_generation():
    """spawn() 後 generation 1；respawn() 回 2，新 pid ≠ 舊 pid。"""
    from uart_stdio_bridge.bridge.dut_process import DutProcess

    argv_factory = lambda gen: [sys.executable, "-c", "import time; time.sleep(10)"]
    dut = DutProcess(argv_factory, mode="local-process")
    dut.spawn()
    try:
        assert dut.generation == 1
        desc1 = dut.describe()
        assert desc1["generation"] == 1
        pid1 = desc1["pid"]
        assert pid1 is not None

        gen2 = dut.respawn(grace_s=0.5)
        assert gen2 == 2
        assert dut.generation == 2
        desc2 = dut.describe()
        assert desc2["generation"] == 2
        pid2 = desc2["pid"]
        assert pid2 is not None
        assert pid1 != pid2
    finally:
        dut.terminate()


def test_dut_stdin_closed_raises():
    """DUT 結束後 stdin_write_some raise DutStdinClosed。"""
    from uart_stdio_bridge.bridge.dut_process import DutProcess, DutStdinClosed

    argv_factory = lambda gen: [sys.executable, "-c", "import sys; sys.exit(0)"]
    dut = DutProcess(argv_factory, mode="local-process")
    dut.spawn()
    try:
        for _ in range(50):
            if dut.poll() is not None:
                break
            time.sleep(0.05)
        assert dut.poll() is not None

        with pytest.raises(DutStdinClosed):
            for _ in range(100):
                dut.stdin_write_some(b"x" * 4096)
    finally:
        dut.terminate()
