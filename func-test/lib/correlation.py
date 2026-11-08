"""以穩定、可稽核的 NDJSON 記錄 func-test 操作邊界。"""
from __future__ import annotations

import json
import os
import pathlib
import hashlib
import threading
import time
from datetime import datetime, timezone
from typing import Any


class CorrelationLog:
    """追加 correlation.v1 記錄；每筆寫入後 flush，checkpoint 前可再 fsync。"""

    def __init__(self, path: pathlib.Path, *, run_id: str, case_id: str, clock_id: str) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("ab", buffering=0)
        self.run_id = run_id
        self.case_id = case_id
        self.clock_id = clock_id
        self._seq = 0
        self._open_ops: set[str] = set()
        self._lock = threading.RLock()
        self._closed = False

    def _write(self, *, op_id: str, step_index: int, kind: str, action: str,
               settled: bool, **fields: Any) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("correlation log 已關閉")
            self._seq += 1
            record: dict[str, Any] = {
                "schema_version": "correlation.v1",
                "run_id": self.run_id,
                "case_id": self.case_id,
                "seq": self._seq,
                "mono_ts_ns": time.monotonic_ns(),
                "wall_ts": datetime.now(timezone.utc).isoformat(),
                "clock_id": self.clock_id,
                "op_id": op_id,
                "step_index": int(step_index),
                "kind": kind,
                "action": action,
                "settled": bool(settled),
                "argv": None,
                "rc": None,
                "response_sha256": None,
                "cmd_id": None,
                "wal_seq": None,
                "wire": None,
                "marker": None,
                "marker_dir": None,
            }
            record.update(fields)
            payload = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
            self._stream.write(payload)

    def op_start(self, op_id: str, *, step_index: int, action: str,
                 argv: list[str] | None, snapshot: dict[str, Any] | None) -> None:
        with self._lock:
            if op_id in self._open_ops:
                raise ValueError(f"op_id 重複或尚未結束：{op_id}")
            self._open_ops.add(op_id)
            snap = snapshot or {}
            self._write(
                op_id=op_id,
                step_index=step_index,
                kind="op_start",
                action=action,
                settled=snap.get("settled") is True,
                argv=argv,
                rc=None,
                response_sha256=None,
                cmd_id=None,
                wal_seq=snap.get("wal_seq", 0),
                wire=snap.get("wire"),
            )

    def op_end(self, op_id: str, *, step_index: int, action: str, rc: int | None,
               response: dict[str, Any] | None, snapshot: dict[str, Any] | None) -> None:
        with self._lock:
            if op_id not in self._open_ops:
                raise ValueError(f"op_id 尚未開始：{op_id}")
            self._open_ops.remove(op_id)
            snap = snapshot or {}
            response_sha = None
            cmd_id = None
            if response is not None:
                normalized = json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                response_sha = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                value = response.get("cmd_id")
                cmd_id = str(value) if value is not None else None
            self._write(
                op_id=op_id,
                step_index=step_index,
                kind="op_end",
                action=action,
                settled=snap.get("settled") is True,
                rc=rc,
                response_sha256=response_sha,
                cmd_id=cmd_id,
                wal_seq=snap.get("wal_seq", 0),
                wire=snap.get("wire"),
            )

    def marker(self, op_id: str, *, marker: str, marker_dir: str) -> None:
        self._write(op_id=op_id, step_index=0, kind="marker", action="mark", settled=False,
                    marker=marker, marker_dir=marker_dir, wal_seq=None, wire=None)

    def flush(self) -> None:
        with self._lock:
            if not self._closed:
                self._stream.flush()
                os.fsync(self._stream.fileno())

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._stream.flush()
                os.fsync(self._stream.fileno())
                self._stream.close()
                self._closed = True


__all__ = ["CorrelationLog"]
