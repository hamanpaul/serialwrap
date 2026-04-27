from __future__ import annotations
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from sw_core.event_engine.dispatcher import (
    Dispatcher,
    DispatcherContext,
    build_payload,
    build_env,
)
from sw_core.event_engine.counter import Counter, CounterStore
from sw_core.event_engine.schema import validate_rule_dict
from sw_core.event_engine.matcher import MatcherFire, MatchResult


def _rule_exec(exec_argv: list[str], **overrides) -> object:
    base = {
        "schema_version": 1,
        "owner": "o", "name": "n", "kind": "tool",
        "selectors": ["COM0"],
        "pattern": {"kind": "contains", "value": "panic"},
        "handler": {"exec": exec_argv},
        "timeout_ms": 2000,
    }
    base.update(overrides)
    return validate_rule_dict(base)


class _Ctx:
    def __init__(self, tmp: str) -> None:
        self.tmp = tmp
        self.events: list[dict] = []
        self.counters = CounterStore(os.path.join(tmp, "counters"))
        self.lock = threading.Lock()

    def emit(self, event: dict) -> None:
        with self.lock:
            self.events.append(event)

    def counter_store(self) -> CounterStore:
        return self.counters


class TestPayload(unittest.TestCase):
    def test_payload_shape(self) -> None:
        rule = _rule_exec(["/bin/true"])
        fire = MatcherFire(
            rule=rule, selector="COM0",
            match=MatchResult(matched_text="panic", groups=[]),
            wal_seq=12345, matched_at=1000, active_cmd_id=None,
        )
        payload = build_payload(fire, fire_count=3, bridge_generation=7,
                                matched_line="Kernel panic - not syncing")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["rule_id"], "o.n")
        self.assertEqual(payload["selector"], "COM0")
        self.assertEqual(payload["wal_seq"], 12345)
        self.assertEqual(payload["fire_count"], 3)
        self.assertEqual(payload["matched_text"], "panic")
        self.assertEqual(payload["matched_line"], "Kernel panic - not syncing")

    def test_env_subset_only_strings(self) -> None:
        rule = _rule_exec(["/bin/true"])
        fire = MatcherFire(
            rule=rule, selector="COM0",
            match=MatchResult(matched_text="panic", groups=[]),
            wal_seq=12345, matched_at=1000, active_cmd_id=None,
        )
        env = build_env(fire, fire_count=3)
        self.assertIn("PATH", env)
        self.assertEqual(env["SERIALWRAP_EVENT_RULE_ID"], "o.n")
        self.assertEqual(env["SERIALWRAP_EVENT_FIRE_COUNT"], "3")
        for v in env.values():
            self.assertIsInstance(v, str)


class TestDispatcher(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sw-dispatcher-")
        self.ctx = _Ctx(self.tmp)
        self.dispatcher = Dispatcher(
            event_emit=self.ctx.emit,
            counter_store=self.ctx.counters,
            per_daemon_max=2,
        )
        self.dispatcher.start()

    def tearDown(self) -> None:
        self.dispatcher.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_fire(self, rule_dict: dict, matched_line: str = "panic now") -> MatcherFire:
        rule = validate_rule_dict(rule_dict)
        return MatcherFire(
            rule=rule, selector="COM0",
            match=MatchResult(matched_text="panic", groups=[]),
            wal_seq=1, matched_at=1, active_cmd_id=None,
        )

    def test_exec_handler_writes_completed(self) -> None:
        marker = os.path.join(self.tmp, "fired")
        rule = {
            "schema_version": 1,
            "owner": "o", "name": "fire", "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "panic"},
            "handler": {"shell": f"echo done > {marker}"},
            "timeout_ms": 3000,
        }
        self.dispatcher.dispatch(self._make_fire(rule, "panic"), matched_line="panic line")
        self.dispatcher.flush_for_test(timeout=3.0)
        self.assertTrue(os.path.exists(marker))
        types = [e["type"] for e in self.ctx.events]
        self.assertIn("fire_completed", types)

    def test_timeout_kills_handler(self) -> None:
        rule = {
            "schema_version": 1,
            "owner": "o", "name": "slow", "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "panic"},
            "handler": {"shell": "sleep 5"},
            "timeout_ms": 200,
        }
        self.dispatcher.dispatch(self._make_fire(rule, "panic"), matched_line="x")
        self.dispatcher.flush_for_test(timeout=3.0)
        types = [e["type"] for e in self.ctx.events]
        self.assertIn("fire_timeout", types)

    def test_per_rule_concurrency_drops_oldest(self) -> None:
        rule = {
            "schema_version": 1,
            "owner": "o", "name": "busy", "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "panic"},
            "handler": {"shell": "sleep 0.5"},
            "timeout_ms": 2000,
        }
        f1 = self._make_fire(rule, "p1")
        f2 = self._make_fire(rule, "p2")
        self.dispatcher.dispatch(f1, matched_line="p1")
        # back-to-back; second one should be dropped while first is running
        self.dispatcher.dispatch(f2, matched_line="p2")
        self.dispatcher.flush_for_test(timeout=3.0)
        reasons = [e.get("reason") for e in self.ctx.events if e["type"] == "event_dropped"]
        self.assertIn("per_rule_busy", reasons)


if __name__ == "__main__":
    unittest.main()
