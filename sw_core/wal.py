from __future__ import annotations

import base64
import json
import os
import threading
import time
import zlib
from typing import Any

from .constants import DEFAULT_WAL_ROTATE_BYTES, WAL_DIR
from .util import dumps_stable, monotonic_ns, now_iso, to_printable


class WalWriter:
    def __init__(self, wal_dir: str = WAL_DIR, rotate_bytes: int = DEFAULT_WAL_ROTATE_BYTES) -> None:
        self._wal_dir = wal_dir
        self._rotate_bytes = rotate_bytes
        self._wal_path = os.path.join(self._wal_dir, "raw.wal.ndjson")
        self._mirror_path = os.path.join(self._wal_dir, "raw.mirror.log")
        self._lock = threading.Lock()
        self._seq = 0
        os.makedirs(self._wal_dir, exist_ok=True)
        self._load_last_seq()

    @property
    def wal_path(self) -> str:
        return self._wal_path

    @property
    def mirror_path(self) -> str:
        return self._mirror_path

    @property
    def current_seq(self) -> int:
        with self._lock:
            return self._seq

    def _load_last_seq(self) -> None:
        if not os.path.exists(self._wal_path):
            self._seq = 0
            return
        last = 0
        with open(self._wal_path, "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    seq = obj.get("seq")
                    if isinstance(seq, int) and seq > last:
                        last = seq
        self._seq = last

    def _rotate_if_needed(self) -> None:
        for path in (self._wal_path, self._mirror_path):
            if not os.path.exists(path):
                continue
            if os.path.getsize(path) < self._rotate_bytes:
                continue
            ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            dst = f"{path}.{ts}"
            os.replace(path, dst)
            dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def append(
        self,
        *,
        com: str,
        direction: str,
        source: str,
        payload: bytes,
        cmd_id: str | None = None,
        loss_flag: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_b64 = base64.b64encode(payload).decode("ascii")
        crc32 = zlib.crc32(payload) & 0xFFFFFFFF
        with self._lock:
            self._rotate_if_needed()
            self._seq += 1
            record = {
                "seq": self._seq,
                "mono_ts_ns": monotonic_ns(),
                "wall_ts": now_iso(),
                "com": com,
                "dir": direction,
                "source": source,
                "cmd_id": cmd_id,
                "len": len(payload),
                "crc32": f"{crc32:08x}",
                "payload_b64": payload_b64,
                "loss_flag": bool(loss_flag),
                "meta": meta or {},
            }
            with open(self._wal_path, "a", encoding="utf-8") as wal_fp:
                wal_fp.write(dumps_stable(record))
                wal_fp.write("\n")
            with open(self._mirror_path, "a", encoding="utf-8") as mirror_fp:
                mirror_fp.write(to_printable(payload))
        return record

    def tail_raw(self, *, from_seq: int = 0, com: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not os.path.exists(self._wal_path):
            return out
        with open(self._wal_path, "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                seq = obj.get("seq")
                if not isinstance(seq, int) or seq <= from_seq:
                    continue
                if com and obj.get("com") != com:
                    continue
                out.append(obj)
                if len(out) >= limit:
                    break
        return out

    def tail_text(self, *, from_seq: int = 0, com: str | None = None, limit: int = 200) -> list[str]:
        rows = self.tail_raw(from_seq=from_seq, com=com, limit=limit)
        chunks: list[str] = []
        for row in rows:
            payload = base64.b64decode(row.get("payload_b64", ""), validate=False)
            chunks.append(to_printable(payload))
        text = "".join(chunks)
        if not text:
            return []
        lines = text.splitlines()
        if text.endswith("\n"):
            return lines
        if not lines:
            return [text]
        consumed = sum(len(line) for line in lines) + max(len(lines) - 1, 0)
        if consumed < len(text):
            lines.append(text[consumed:])
        return lines
