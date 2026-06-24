from __future__ import annotations

import queue
import re
import threading
from dataclasses import dataclass
from typing import Iterable, Protocol

from .counter import Counter
from .schema import REGEX_MATCH_INPUT_MAX, Pattern, Rule, _flags_from_string


@dataclass
class MatchResult:
    matched_text: str
    groups: list[str]


class PatternMatcher:
    def __init__(self, pattern: Pattern) -> None:
        self._pattern = pattern
        self._is_regex = pattern.kind == "regex"
        if pattern.kind == "regex":
            self._re = re.compile(pattern.value, _flags_from_string(pattern.flags))
        elif pattern.kind == "contains":
            flags = _flags_from_string(pattern.flags)
            self._re = re.compile(re.escape(pattern.value), flags)
        else:
            raise ValueError(f"unknown pattern kind: {pattern.kind}")

    def eval(self, line: str) -> MatchResult | None:
        # ReDoS runtime 防護（#83 Codex 必修）：對 user-controlled regex 把求值輸入長度封頂，使殘餘
        # 多項式回溯成本有界，即便 schema AST 靜態分析漏放亦不致凍結單執行緒 matcher。contains（已
        # re.escape 的字面量）無回溯風險，維持原樣不截斷以保語意。
        target = line[:REGEX_MATCH_INPUT_MAX] if self._is_regex else line
        m = self._re.search(target)
        if m is None:
            return None
        return MatchResult(matched_text=m.group(0), groups=list(m.groups()))


def apply_cooldown(rule: Rule, counter: Counter, *, now_ms: int) -> bool:
    if rule.cooldown_ms <= 0 or counter.last_fire_ts is None:
        return True
    return (now_ms - counter.last_fire_ts) >= rule.cooldown_ms


def apply_max_fires(rule: Rule, counter: Counter) -> bool:
    if rule.max_fires is None:
        return True
    if counter.exhausted:
        return False
    return counter.fires < rule.max_fires


def apply_scope(rule: Rule, *, active_cmd_id: str | None) -> bool:
    if rule.scope == "any":
        return True
    if rule.scope == "spontaneous":
        return active_cmd_id is None
    if rule.scope == "command_output":
        return active_cmd_id is not None
    return False


def apply_profile(rule: Rule, *, com_profile: str | None) -> bool:
    if rule.profile == "ALL":
        return True
    return com_profile == rule.profile


class MatcherContext(Protocol):
    def active_cmd_id(self, com: str) -> str | None: ...
    def com_profile(self, com: str) -> str | None: ...
    def now_ms(self) -> int: ...
    def emit_fire(self, fire: "MatcherFire") -> None: ...
    def emit_dropped(self, event: dict) -> None: ...
    def emit_skipped(self, event: dict) -> None: ...
    def counter_for(self, rule_id: str) -> Counter: ...


@dataclass
class MatcherFire:
    rule: Rule
    selector: str
    match: MatchResult
    wal_seq: int
    matched_at: int
    active_cmd_id: str | None


@dataclass
class _Item:
    selector: str
    line: str
    wal_seq: int


class MatcherWorker:
    def __init__(
        self,
        rules: Iterable[Rule],
        context: MatcherContext,
        queue_max: int = 1024,
    ) -> None:
        self._rules: list[Rule] = list(rules)
        self._ctx = context
        self._queue: queue.Queue[_Item] = queue.Queue(maxsize=queue_max)
        self._queue_max = queue_max
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._compiled: dict[str, PatternMatcher] = {}
        self._paused_for_test = False
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._rebuild_compiled()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="event-matcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            try:
                self._queue.put_nowait(_Item("__stop__", "", 0))
            except queue.Full:
                pass
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                import logging
                logging.getLogger(__name__).warning(
                    "event-matcher thread did not exit within 2s; context may receive late callbacks"
                )
            self._thread = None

    def replace_rules(self, rules: Iterable[Rule]) -> None:
        with self._lock:
            self._rules = list(rules)
            self._rebuild_compiled()

    def _rebuild_compiled(self) -> None:
        self._compiled = {r.rule_id: PatternMatcher(r.pattern) for r in self._rules}

    def feed_line(self, selector: str, line: str, wal_seq: int) -> None:
        item = _Item(selector=selector, line=line, wal_seq=wal_seq)
        self._idle_event.clear()
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                dropped = self._queue.get_nowait()
                self._ctx.emit_dropped({
                    "type": "event_dropped",
                    "reason": "matcher_queue_overflow",
                    "selector": dropped.selector,
                    "wal_seq": dropped.wal_seq,
                })
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._ctx.emit_dropped({
                    "type": "event_dropped",
                    "reason": "matcher_queue_overflow",
                    "selector": selector,
                    "wal_seq": wal_seq,
                })

    def flush_for_test(self, timeout: float = 1.0) -> None:
        end = self._ctx.now_ms() + int(timeout * 1000)
        while True:
            if self._queue.empty() and self._idle_event.is_set():
                return
            if self._ctx.now_ms() > end:
                return
            self._idle_event.wait(0.01)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                self._idle_event.set()
                continue
            if item.selector == "__stop__":
                return
            if self._paused_for_test:
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    self._ctx.emit_dropped({
                        "type": "event_dropped",
                        "reason": "matcher_queue_overflow",
                        "selector": item.selector,
                        "wal_seq": item.wal_seq,
                    })
                continue
            try:
                self._evaluate(item)
            finally:
                if self._queue.empty():
                    self._idle_event.set()

    def _evaluate(self, item: _Item) -> None:
        with self._lock:
            rules_snapshot = list(self._rules)
            compiled_snapshot = dict(self._compiled)
        for rule in rules_snapshot:
            if "ALL" not in rule.selectors and item.selector not in rule.selectors:
                continue
            counter = self._ctx.counter_for(rule.rule_id)
            now = self._ctx.now_ms()
            active = self._ctx.active_cmd_id(item.selector)
            profile = self._ctx.com_profile(item.selector)
            pm = compiled_snapshot.get(rule.rule_id)
            if pm is None:
                continue
            match = pm.eval(item.line)
            if match is None:
                continue
            if not apply_scope(rule, active_cmd_id=active):
                if rule.debug:
                    self._ctx.emit_skipped({
                        "type": "match_skipped",
                        "rule_id": rule.rule_id,
                        "selector": item.selector,
                        "reason": "scope_mismatch",
                    })
                continue
            if not apply_profile(rule, com_profile=profile):
                if rule.debug:
                    self._ctx.emit_skipped({
                        "type": "match_skipped",
                        "rule_id": rule.rule_id,
                        "selector": item.selector,
                        "reason": "profile_mismatch",
                    })
                continue
            if not apply_max_fires(rule, counter):
                if rule.debug:
                    self._ctx.emit_skipped({
                        "type": "match_skipped",
                        "rule_id": rule.rule_id,
                        "selector": item.selector,
                        "reason": "exhausted",
                    })
                continue
            if not apply_cooldown(rule, counter, now_ms=now):
                if rule.debug:
                    self._ctx.emit_skipped({
                        "type": "match_skipped",
                        "rule_id": rule.rule_id,
                        "selector": item.selector,
                        "reason": "cooldown",
                    })
                continue
            self._ctx.emit_fire(MatcherFire(
                rule=rule,
                selector=item.selector,
                match=match,
                wal_seq=item.wal_seq,
                matched_at=now,
                active_cmd_id=active,
            ))
