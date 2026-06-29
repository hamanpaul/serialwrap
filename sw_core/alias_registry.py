from __future__ import annotations

import threading
from typing import Any

from .util import now_iso


class AliasRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, Any]] = {}

    def load(self, rows: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            self._rows = {k: dict(v) for k, v in rows.items()}

    def dump(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._rows.items()}

    def list_alias(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for alias, row in sorted(self._rows.items()):
                rec = dict(row)
                rec["alias"] = alias
                out.append(rec)
            return out

    def set_for_session(self, session_id: str, alias: str) -> None:
        with self._lock:
            self._rows[alias] = {
                "session_id": session_id,
                "updated_at": now_iso(),
            }

    def for_session(self, session_id: str) -> str | None:
        """回傳指向該 session_id 的 alias 字串；找不到回 None（#100 renumber 用）。"""
        with self._lock:
            for alias, row in self._rows.items():
                if row.get("session_id") == session_id:
                    return alias
            return None

    def reassign_session(self, old_session_id: str, new_session_id: str) -> None:
        """把所有指向 old_session_id 的 alias row 改指向 new_session_id（#100 renumber 用）。

        alias 字串綁 act_no、**不變**，只改 row 內 session_id 指向。
        """
        with self._lock:
            for row in self._rows.values():
                if row.get("session_id") == old_session_id:
                    row["session_id"] = new_session_id
                    row["updated_at"] = now_iso()

    def assign_by_id(self, by_id: str, alias: str, profile: str | None = None) -> None:
        with self._lock:
            row = {
                "device_by_id": by_id,
                "updated_at": now_iso(),
            }
            if profile:
                row["profile"] = profile
            self._rows[alias] = row

    def unassign(self, alias: str) -> bool:
        with self._lock:
            return self._rows.pop(alias, None) is not None
