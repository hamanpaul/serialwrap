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
