from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterable


@dataclass
class Counter:
    fires: int = 0
    last_fire_ts: int | None = None
    exhausted: bool = False

    def to_json(self) -> dict:
        return {"fires": self.fires, "last_fire_ts": self.last_fire_ts, "exhausted": self.exhausted}

    @classmethod
    def from_json(cls, obj: dict) -> "Counter":
        return cls(
            fires=int(obj.get("fires", 0)),
            last_fire_ts=int(obj["last_fire_ts"]) if obj.get("last_fire_ts") is not None else None,
            exhausted=bool(obj.get("exhausted", False)),
        )


class CounterStore:
    """Per-rule counter persisted under EVENTS_RUNTIME_DIR (tmpfs).

    Files are atomic via tmp+rename. Missing file ⇒ zero counter.
    """

    def __init__(self, root: str) -> None:
        self._root = root
        os.makedirs(self._root, exist_ok=True)

    def path_for(self, rule_id: str) -> str:
        return os.path.join(self._root, f"{rule_id}.counter.json")

    def load(self, rule_id: str) -> Counter:
        path = self.path_for(rule_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return Counter.from_json(json.load(f))
        except FileNotFoundError:
            return Counter()
        except (json.JSONDecodeError, OSError):
            return Counter()

    def save(self, rule_id: str, counter: Counter) -> None:
        path = self.path_for(rule_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(counter.to_json(), f)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)

    def clear(self, rule_id: str) -> None:
        try:
            os.unlink(self.path_for(rule_id))
        except FileNotFoundError:
            pass

    def known_rule_ids(self) -> Iterable[str]:
        try:
            entries = os.listdir(self._root)
        except FileNotFoundError:
            return []
        out: list[str] = []
        suffix = ".counter.json"
        for entry in entries:
            if entry.endswith(suffix):
                out.append(entry[: -len(suffix)])
        return out
