"""tools/uart_stdio_bridge/tests/test_bytequeue.py

測試 bridge/bytequeue.py：有界位元組佇列的背壓、單 chunk 允許超限、以及 stop 事件解除阻塞。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent
if str(_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(_TEST_DIR))
_SRC_DIR = _TEST_DIR.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


def test_bytequeue_backpressure_blocks_not_drops():
    """max_bytes=10：put 8 B 後第二個 put 8 B 在另一 thread 阻塞；get 後才完成；pending_bytes 從未 >18；總取出等於總放入。"""
    from uart_stdio_bridge.bridge.bytequeue import ByteQueue

    q = ByteQueue(max_bytes=10)
    stop = threading.Event()
    assert q.put(b"12345678", stop=stop) is True
    assert q.pending_bytes() == 8

    started = threading.Event()
    finished = threading.Event()
    peak_pending = [q.pending_bytes()]

    def worker():
        started.set()
        res = q.put(b"ABCDEFGH", stop=stop)
        peak_pending.append(q.pending_bytes())
        finished.set()
        assert res is True

    t = threading.Thread(target=worker)
    t.start()
    assert started.wait(timeout=1.0)
    time.sleep(0.1)

    # 第二個 put 必須處於阻塞狀態（因為 8 + 8 = 16 > 10）
    assert not finished.is_set()
    assert q.pending_bytes() <= 18

    # 取出第一個 chunk，佇列騰出空間
    got1 = q.get(timeout_s=1.0)
    assert got1 == b"12345678"

    # worker 應解除阻塞並成功完成
    t.join(timeout=1.0)
    assert finished.is_set()
    assert max(peak_pending) <= 18

    got2 = q.get(timeout_s=1.0)
    assert got2 == b"ABCDEFGH"
    assert q.pending_bytes() == 0
    assert got1 + got2 == b"12345678ABCDEFGH"


def test_bytequeue_oversize_chunk_when_empty():
    """空佇列 put 20 B 成功。"""
    from uart_stdio_bridge.bridge.bytequeue import ByteQueue

    q = ByteQueue(max_bytes=10)
    stop = threading.Event()
    # 佇列為空時允許單一 chunk 超過 max_bytes
    data = b"01234567890123456789"
    assert len(data) == 20
    assert q.put(data, stop=stop) is True
    assert q.pending_bytes() == 20
    got = q.get(timeout_s=1.0)
    assert got == data
    assert q.pending_bytes() == 0


def test_bytequeue_stop_unblocks():
    """阻塞中的 put 在 stop.set() 後 ≤0.2 s 回 False。"""
    from uart_stdio_bridge.bridge.bytequeue import ByteQueue

    q = ByteQueue(max_bytes=10)
    stop = threading.Event()
    assert q.put(b"12345678", stop=stop) is True

    started = threading.Event()
    result: list[tuple[bool, float]] = []

    def worker():
        started.set()
        t0 = time.monotonic()
        res = q.put(b"ABCDEFGH", stop=stop)
        t1 = time.monotonic()
        result.append((res, t1 - t0))

    t = threading.Thread(target=worker)
    t.start()
    assert started.wait(timeout=1.0)
    time.sleep(0.05)

    t_before_stop = time.monotonic()
    stop.set()
    t.join(timeout=1.0)
    elapsed = time.monotonic() - t_before_stop

    assert len(result) == 1
    res, _ = result[0]
    assert res is False
    assert elapsed <= 0.2
