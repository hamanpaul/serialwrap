from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol

from .counter import Counter, CounterStore
from .dispatcher import Dispatcher
from .event_log import EventLogger
from .matcher import MatcherFire, MatcherWorker
from .registry import RuleRegistry
from .schema import Rule


class BridgeQueries(Protocol):
    def active_cmd_id_for(self, com: str) -> str | None: ...
    def profile_for(self, com: str) -> str | None: ...
    def known_coms(self) -> list[str]: ...


@dataclass
class EngineDeps:
    events_dir: str
    runtime_dir: str
    log_path: str
    bridge: BridgeQueries
    queue_max: int = 1024
    per_daemon_max: int = 8


class EventEngine:
    def __init__(self, deps: EngineDeps) -> None:
        self._deps = deps
        self._registry = RuleRegistry(deps.events_dir)
        self._counters = CounterStore(deps.runtime_dir)
        self._log = EventLogger(deps.log_path)
        self._lock = threading.RLock()
        self._enabled_coms: set[str] = set()
        self._rules: list[Rule] = []
        self._matcher: MatcherWorker | None = None
        self._dispatcher: Dispatcher | None = None
        self._started = False

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._started:
            return
        self._dispatcher = Dispatcher(
            event_emit=self._log.write,
            counter_store=self._counters,
            per_daemon_max=self._deps.per_daemon_max,
        )
        self._dispatcher.start()

        load = self._registry.load_all()
        for fail in load.failed:
            self._log.write({"type": "rule_load_failed", "path": fail.path, "reason": fail.reason})
        with self._lock:
            self._rules = load.rules
            for rule in load.rules:
                self._log.write({"type": "rule_loaded", "rule_id": rule.rule_id})
                if rule.auto_enable_com_on_load:
                    for sel in rule.selectors:
                        if sel == "ALL":
                            for com in self._deps.bridge.known_coms():
                                self._mark_com_enabled(com, "auto")
                        else:
                            self._mark_com_enabled(sel, "auto")

        self._matcher = MatcherWorker(
            rules=self._rules,
            context=self._matcher_ctx(),
            queue_max=self._deps.queue_max,
        )
        self._matcher.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        if self._matcher is not None:
            self._matcher.stop()
        if self._dispatcher is not None:
            self._dispatcher.stop()
        self._matcher = None
        self._dispatcher = None
        self._started = False

    # ── feeds ────────────────────────────────────────────────────────────
    def feed_line(self, com: str, line: str, wal_seq: int) -> None:
        with self._lock:
            if com not in self._enabled_coms:
                return
            matcher = self._matcher
        if matcher is None:
            return
        matcher.feed_line(com, line, wal_seq)

    # ── CRUD ─────────────────────────────────────────────────────────────
    def rule_set(self, obj: dict) -> Rule:
        rule = self._registry.upsert(obj)
        with self._lock:
            self._rules = [r for r in self._rules if r.rule_id != rule.rule_id] + [rule]
            if self._matcher is not None:
                self._matcher.replace_rules(self._rules)
            if rule.auto_enable_com_on_load:
                for sel in rule.selectors:
                    if sel == "ALL":
                        for com in self._deps.bridge.known_coms():
                            self._mark_com_enabled(com, "rule_set")
                    else:
                        self._mark_com_enabled(sel, "rule_set")
        self._log.write({"type": "rule_loaded", "rule_id": rule.rule_id})
        return rule

    def rule_delete(self, rule_id: str) -> bool:
        ok = self._registry.delete(rule_id)
        if ok:
            self._counters.clear(rule_id)
            with self._lock:
                self._rules = [r for r in self._rules if r.rule_id != rule_id]
                if self._matcher is not None:
                    self._matcher.replace_rules(self._rules)
            self._log.write({"type": "rule_unloaded", "rule_id": rule_id})
        return ok

    def rule_list(self, *, selector: str | None = None, owner: str | None = None) -> list[dict]:
        with self._lock:
            rules = list(self._rules)
        out: list[dict] = []
        for r in rules:
            if selector is not None and selector not in r.selectors and "ALL" not in r.selectors:
                continue
            if owner is not None and r.owner != owner:
                continue
            counter = self._counters.load(r.rule_id)
            out.append({
                "rule_id": r.rule_id,
                "owner": r.owner,
                "kind": r.kind,
                "selectors": list(r.selectors),
                "fires": counter.fires,
                "exhausted": counter.exhausted,
                "last_fire_ts": counter.last_fire_ts,
            })
        return out

    def rule_get(self, rule_id: str) -> dict | None:
        rule = self._registry.get(rule_id)
        if rule is None:
            return None
        counter = self._counters.load(rule_id)
        return {"rule": rule.raw, "counter": counter.to_json()}

    def reload(self) -> dict:
        with self._lock:
            previous = list(self._rules)
        diff = self._registry.diff_against(previous)
        with self._lock:
            current = self._registry.load_all().rules
            self._rules = current
            if self._matcher is not None:
                self._matcher.replace_rules(self._rules)
            for r in diff.removed:
                self._counters.clear(r.rule_id)
            for r in diff.added:
                if r.auto_enable_com_on_load:
                    for sel in r.selectors:
                        if sel == "ALL":
                            for com in self._deps.bridge.known_coms():
                                self._mark_com_enabled(com, "reload")
                        else:
                            self._mark_com_enabled(sel, "reload")
        for r in diff.added:
            self._log.write({"type": "rule_loaded", "rule_id": r.rule_id})
        for r in diff.removed:
            self._log.write({"type": "rule_unloaded", "rule_id": r.rule_id})
        for r in diff.changed:
            self._log.write({"type": "rule_loaded", "rule_id": r.rule_id})
        return {
            "added": [r.rule_id for r in diff.added],
            "removed": [r.rule_id for r in diff.removed],
            "changed": [r.rule_id for r in diff.changed],
        }

    # ── COM toggle ───────────────────────────────────────────────────────
    def com_enable(self, com: str) -> dict:
        self._mark_com_enabled(com, "manual")
        return self.com_status(com)

    def com_disable(self, com: str) -> dict:
        with self._lock:
            self._enabled_coms.discard(com)
            affected = [r.rule_id for r in self._rules if com in r.selectors or "ALL" in r.selectors]
        for rid in affected:
            previous = self._counters.load(rid)
            self._counters.clear(rid)
            self._log.write({
                "type": "counter_reset",
                "rule_id": rid,
                "previous_fires": previous.fires,
                "triggered_by": "com_disable",
            })
        self._log.write({"type": "com_disabled", "selector": com, "triggered_by": "manual"})
        return self.com_status(com)

    def com_status(self, com: str | None = None) -> dict:
        with self._lock:
            if com is None:
                return {"coms": sorted(self._enabled_coms)}
            return {
                "selector": com,
                "enabled": com in self._enabled_coms,
                "active_rules": [
                    r.rule_id for r in self._rules
                    if com in r.selectors or "ALL" in r.selectors
                ],
            }

    def reset(self, *, rule_id: str | None = None, selector: str | None = None) -> int:
        cleared = 0
        with self._lock:
            target_ids: list[str] = []
            if rule_id is not None:
                target_ids = [rule_id]
            elif selector is not None:
                target_ids = [r.rule_id for r in self._rules
                              if selector in r.selectors or "ALL" in r.selectors]
        for rid in target_ids:
            previous = self._counters.load(rid)
            self._counters.clear(rid)
            cleared += 1
            self._log.write({
                "type": "counter_reset",
                "rule_id": rid,
                "previous_fires": previous.fires,
                "triggered_by": "reset",
            })
        return cleared

    def tail(self, **filters) -> list[dict]:
        return self._log.tail(**filters)

    # ── internals ────────────────────────────────────────────────────────
    def _mark_com_enabled(self, com: str, triggered_by: str) -> None:
        with self._lock:
            if com in self._enabled_coms:
                return
            self._enabled_coms.add(com)
        self._log.write({"type": "com_enabled", "selector": com, "triggered_by": triggered_by})

    def _matcher_ctx(self) -> "MatcherContextImpl":
        return MatcherContextImpl(self)


class MatcherContextImpl:
    def __init__(self, engine: EventEngine) -> None:
        self._engine = engine

    def active_cmd_id(self, com: str) -> str | None:
        return self._engine._deps.bridge.active_cmd_id_for(com)

    def com_profile(self, com: str) -> str | None:
        return self._engine._deps.bridge.profile_for(com)

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def emit_fire(self, fire: MatcherFire) -> None:
        engine = self._engine
        engine._log.write({
            "type": "match_recorded",
            "rule_id": fire.rule.rule_id,
            "selector": fire.selector,
            "wal_seq": fire.wal_seq,
            "matched_text": fire.match.matched_text,
        })
        counter = engine._counters.load(fire.rule.rule_id)
        if fire.rule.max_fires is not None and counter.fires + 1 >= fire.rule.max_fires:
            counter.exhausted = True
            engine._counters.save(fire.rule.rule_id, counter)
        if engine._dispatcher is not None:
            engine._dispatcher.dispatch(fire, matched_line=fire.match.matched_text)

    def emit_dropped(self, event: dict) -> None:
        self._engine._log.write(event)

    def emit_skipped(self, event: dict) -> None:
        self._engine._log.write(event)

    def counter_for(self, rule_id: str) -> Counter:
        return self._engine._counters.load(rule_id)
