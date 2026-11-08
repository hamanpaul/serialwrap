"""以位元組數量限制的 FIFO 佇列，滿載時以阻塞將背壓傳回讀取端。"""

from __future__ import annotations

import collections
import threading
import time


class ByteQueue:
    """保留 chunk 邊界的有界位元組佇列。"""

    def __init__(self, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes 必須大於 0")
        self.max_bytes = int(max_bytes)
        self._chunks: collections.deque[bytes] = collections.deque()
        self._pending = 0
        self._closed = False
        self._condition = threading.Condition()

    def put(
        self,
        data: bytes,
        *,
        stop: threading.Event,
        timeout_s: float | None = None,
    ) -> bool:
        """加入一個 chunk；佇列非空時容量不足會等候並持續檢查 stop。"""
        chunk = bytes(data)
        if not chunk:
            return True
        deadline = None if timeout_s is None else time.monotonic() + max(0.0, timeout_s)
        with self._condition:
            while True:
                if self._closed or stop.is_set():
                    return False
                fits = self._pending + len(chunk) <= self.max_bytes
                oversize_empty = self._pending == 0
                if fits or oversize_empty:
                    self._chunks.append(chunk)
                    self._pending += len(chunk)
                    self._condition.notify_all()
                    return True
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._condition.wait(min(remaining, 0.05))
                else:
                    self._condition.wait(0.05)

    def get(self, timeout_s: float) -> bytes | None:
        """取出最早加入的 chunk；逾時或已關閉且為空時回傳 None。"""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._condition:
            while not self._chunks:
                if self._closed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            chunk = self._chunks.popleft()
            self._pending -= len(chunk)
            self._condition.notify_all()
            return chunk

    def pending_bytes(self) -> int:
        with self._condition:
            return self._pending

    def discard_all(self) -> int:
        """丟棄佇列中尚未取出的資料，回傳丟棄位元組數。"""
        with self._condition:
            discarded = self._pending
            self._chunks.clear()
            self._pending = 0
            self._condition.notify_all()
            return discarded

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
