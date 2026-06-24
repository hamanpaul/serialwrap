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

    def _rotate_if_needed(self) -> bool:
        """檔案超過上限時輪替。回傳 True 表示輪替過程發生 OSError 已被 best-effort 含住（degrade）。

        輪替只是「限制檔案大小」的最佳化，不是資料平面必要步驟。原本此方法在 ``append`` 的 try
        之外執行，rotation path 的 ``getsize``/``replace``/``open(dir)``/``fsync(dir)`` 因 EIO/權限/
        fd 耗盡拋 OSError 會逃出 ``append`` → 殺死 RX reader thread（正是 #79 想避免的長壽 thread
        死亡，含 console fan-out 一併中止）。改為自身含住例外：失敗則告警並續用既有（可能超大的）
        檔案，遠優於殺 reader（#79 Codex 必修）。
        """
        rotation_failed = False
        for path in (self._wal_path, self._mirror_path):
            try:
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
            except OSError as exc:
                rotation_failed = True
                import logging
                logging.getLogger("serialwrap").warning(
                    "WAL 輪替失敗（best-effort 續用既有檔案，不殺 reader）：%s：%s", path, exc
                )
        return rotation_failed

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
            rotation_failed = self._rotate_if_needed()
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
            if rotation_failed:
                # 輪替失敗不丟資料（仍寫入既有檔），但標記供觀測；conditional key 維持常態 record 向後相容。
                record["rotation_failed"] = True
            try:
                with open(self._wal_path, "a", encoding="utf-8") as wal_fp:
                    wal_fp.write(dumps_stable(record))
                    wal_fp.write("\n")
                with open(self._mirror_path, "a", encoding="utf-8") as mirror_fp:
                    mirror_fp.write(to_printable(payload))
            except OSError as exc:
                # 稽核寫入失敗（ENOSPC/EROFS/權限/fd 耗盡）不得讓資料平面（RX reader thread）崩潰（#79 STA-1）。
                # best-effort：標記 loss、告警，仍回傳 record 讓上游照常 fan-out 給 console、續處理後續 RX。
                record["loss_flag"] = True
                import logging
                logging.getLogger("serialwrap").warning("WAL append 寫入失敗（best-effort 略過）：%s", exc)
        return record

    def reset(self) -> dict[str, Any]:
        """輪替現有 WAL 檔案並重設 seq 計數器。不需重啟 daemon。"""
        with self._lock:
            prev_seq = self._seq
            ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            for path in (self._wal_path, self._mirror_path):
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    dst = f"{path}.{ts}"
                    os.replace(path, dst)
            dir_fd = os.open(self._wal_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
            self._seq = 0
            return {"ok": True, "previous_seq": prev_seq, "rotated_suffix": ts}

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
