from __future__ import annotations

import json
import os
import threading
import time
from typing import Any


class EventLogger:
    """Newline-delimited JSON log with size-based rotation.

    Thread-safe via internal lock. Synchronous fsync on each write to keep
    forensic value across daemon failover.
    """

    def __init__(self, path: str, rotate_bytes: int = 10 * 1024 * 1024, backup_count: int = 3) -> None:
        self._path = path
        self._rotate_bytes = rotate_bytes
        self._backup_count = backup_count
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    @property
    def path(self) -> str:
        return self._path

    def write(self, event: dict[str, Any]) -> None:
        if "ts" not in event:
            event = dict(event)
            event["ts"] = int(time.time() * 1000)
        line = json.dumps(event, ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        with self._lock:
            self._rotate_if_needed(len(encoded))
            with open(self._path, "ab") as f:
                f.write(encoded)
                f.flush()
                os.fsync(f.fileno())

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = os.path.getsize(self._path)
        except FileNotFoundError:
            return
        if size + incoming <= self._rotate_bytes:
            return
        for i in range(self._backup_count - 1, 0, -1):
            src = f"{self._path}.{i}"
            dst = f"{self._path}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        if os.path.exists(self._path):
            os.replace(self._path, f"{self._path}.1")

    def tail(
        self,
        *,
        rule_id: str | None = None,
        selector: str | None = None,
        since_ts: int | None = None,
        n: int | None = None,
    ) -> list[dict[str, Any]]:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rule_id is not None and obj.get("rule_id") != rule_id:
                continue
            if selector is not None and obj.get("selector") != selector:
                continue
            if since_ts is not None and obj.get("ts", 0) < since_ts:
                continue
            out.append(obj)
        if n is not None:
            out = out[-n:]
        return out
