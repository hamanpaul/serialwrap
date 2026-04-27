from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterable

from .schema import Rule, RuleSchemaError, validate_rule_dict


class RuleLoadError(Exception):
    pass


@dataclass
class FailedLoad:
    path: str
    reason: str


@dataclass
class LoadResult:
    rules: list[Rule]
    failed: list[FailedLoad]


@dataclass
class DiffResult:
    added: list[Rule]
    removed: list[Rule]
    changed: list[Rule]


class RuleRegistry:
    """File-backed registry under EVENTS_DIR (events.d/<rule_id>.json).

    An in-memory write cache shadows upserted rules so that `get` returns the
    last-written version, enabling `diff_against` to detect external disk edits.
    """

    def __init__(self, root: str) -> None:
        self._root = root
        self._cache: dict[str, Rule] = {}
        os.makedirs(self._root, exist_ok=True)

    @property
    def root(self) -> str:
        return self._root

    def path_for(self, rule_id: str) -> str:
        return os.path.join(self._root, f"{rule_id}.json")

    def load_all(self) -> LoadResult:
        rules: list[Rule] = []
        failed: list[FailedLoad] = []
        try:
            entries = sorted(os.listdir(self._root))
        except FileNotFoundError:
            return LoadResult(rules=[], failed=[])
        for entry in entries:
            if not entry.endswith(".json"):
                continue
            full = os.path.join(self._root, entry)
            try:
                with open(full, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                failed.append(FailedLoad(full, f"read/parse failed: {exc}"))
                continue
            try:
                rule = validate_rule_dict(data)
            except RuleSchemaError as exc:
                failed.append(FailedLoad(full, str(exc)))
                continue
            rules.append(rule)
        return LoadResult(rules=rules, failed=failed)

    def get(self, rule_id: str) -> Rule | None:
        if rule_id in self._cache:
            return self._cache[rule_id]
        path = self.path_for(rule_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return validate_rule_dict(json.load(f))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, RuleSchemaError) as exc:
            raise RuleLoadError(f"cannot load {rule_id}: {exc}") from exc

    def upsert(self, obj: dict) -> Rule:
        rule = validate_rule_dict(obj)
        path = self.path_for(rule.rule_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(rule.raw), f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
        self._cache[rule.rule_id] = rule
        return rule

    def delete(self, rule_id: str) -> bool:
        try:
            os.unlink(self.path_for(rule_id))
            self._cache.pop(rule_id, None)
            return True
        except FileNotFoundError:
            return False

    def diff_against(self, previous: Iterable[Rule]) -> DiffResult:
        prev_map = {r.rule_id: r for r in previous}
        current = {r.rule_id: r for r in self.load_all().rules}
        added = [current[k] for k in current.keys() - prev_map.keys()]
        removed = [prev_map[k] for k in prev_map.keys() - current.keys()]
        changed: list[Rule] = []
        for k in current.keys() & prev_map.keys():
            if current[k].raw != prev_map[k].raw:
                changed.append(current[k])
        return DiffResult(added=added, removed=removed, changed=changed)
