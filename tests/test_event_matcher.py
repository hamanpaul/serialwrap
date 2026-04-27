from __future__ import annotations
import time
import unittest

from sw_core.event_engine.matcher import (
    MatcherFire,
    MatcherWorker,
    PatternMatcher,
    apply_cooldown,
    apply_max_fires,
    apply_profile,
    apply_scope,
)
from sw_core.event_engine.counter import Counter
from sw_core.event_engine.schema import validate_rule_dict


def _rule(**overrides) -> object:
    base = {
        "schema_version": 1,
        "owner": "o",
        "name": "n",
        "kind": "tool",
        "selectors": ["COM0"],
        "pattern": {"kind": "contains", "value": "panic"},
        "handler": {"exec": ["/bin/true"]},
    }
    base.update(overrides)
    return validate_rule_dict(base)


class TestPatternMatcher(unittest.TestCase):
    def test_contains_hit(self) -> None:
        rule = _rule()
        pm = PatternMatcher(rule.pattern)
        m = pm.eval("Kernel panic - not syncing")
        self.assertIsNotNone(m)
        self.assertEqual(m.matched_text, "panic")
        self.assertEqual(m.groups, [])

    def test_contains_miss(self) -> None:
        rule = _rule()
        pm = PatternMatcher(rule.pattern)
        self.assertIsNone(pm.eval("nothing here"))

    def test_contains_case_insensitive(self) -> None:
        rule = _rule(pattern={"kind": "contains", "value": "PANIC", "flags": "i"})
        pm = PatternMatcher(rule.pattern)
        self.assertIsNotNone(pm.eval("kernel panic"))

    def test_regex_groups(self) -> None:
        rule = _rule(pattern={"kind": "regex", "value": r"temp=(\d+)C"})
        pm = PatternMatcher(rule.pattern)
        m = pm.eval("sensor temp=105C ok")
        self.assertIsNotNone(m)
        self.assertEqual(m.groups, ["105"])
        self.assertEqual(m.matched_text, "temp=105C")


class TestGates(unittest.TestCase):
    def test_cooldown_blocks_within_window(self) -> None:
        rule = _rule(cooldown_ms=1000)
        c = Counter(fires=1, last_fire_ts=1_000_000, exhausted=False)
        self.assertFalse(apply_cooldown(rule, c, now_ms=1_000_500))
        self.assertTrue(apply_cooldown(rule, c, now_ms=1_001_500))

    def test_cooldown_zero_always_passes(self) -> None:
        rule = _rule(cooldown_ms=0)
        c = Counter(fires=1, last_fire_ts=1_000_000)
        self.assertTrue(apply_cooldown(rule, c, now_ms=1_000_001))

    def test_max_fires_exhaustion(self) -> None:
        rule = _rule(max_fires=2)
        self.assertTrue(apply_max_fires(rule, Counter(fires=0)))
        self.assertTrue(apply_max_fires(rule, Counter(fires=1)))
        self.assertFalse(apply_max_fires(rule, Counter(fires=2, exhausted=True)))

    def test_max_fires_null_unlimited(self) -> None:
        rule = _rule(max_fires=None)
        self.assertTrue(apply_max_fires(rule, Counter(fires=10000)))

    def test_scope_spontaneous(self) -> None:
        rule = _rule(scope="spontaneous")
        self.assertTrue(apply_scope(rule, active_cmd_id=None))
        self.assertFalse(apply_scope(rule, active_cmd_id="cmd-7"))

    def test_scope_command_output(self) -> None:
        rule = _rule(scope="command_output")
        self.assertFalse(apply_scope(rule, active_cmd_id=None))
        self.assertTrue(apply_scope(rule, active_cmd_id="cmd-7"))

    def test_scope_any(self) -> None:
        rule = _rule(scope="any")
        self.assertTrue(apply_scope(rule, active_cmd_id=None))
        self.assertTrue(apply_scope(rule, active_cmd_id="cmd-7"))

    def test_profile_all(self) -> None:
        rule = _rule(profile="ALL")
        self.assertTrue(apply_profile(rule, com_profile=None))
        self.assertTrue(apply_profile(rule, com_profile="brcm"))

    def test_profile_match(self) -> None:
        rule = _rule(profile="brcm")
        self.assertTrue(apply_profile(rule, com_profile="brcm"))
        self.assertFalse(apply_profile(rule, com_profile="opi"))
        self.assertFalse(apply_profile(rule, com_profile=None))


class _FakeContext:
    def __init__(self) -> None:
        self.active_cmd_id_value: str | None = None
        self.profile_value: str | None = None
        self.fires: list[MatcherFire] = []
        self.dropped: list[dict] = []
        self.skipped: list[dict] = []

    def active_cmd_id(self, com: str) -> str | None:
        return self.active_cmd_id_value

    def com_profile(self, com: str) -> str | None:
        return self.profile_value

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def emit_fire(self, fire: MatcherFire) -> None:
        self.fires.append(fire)

    def emit_dropped(self, event: dict) -> None:
        self.dropped.append(event)

    def emit_skipped(self, event: dict) -> None:
        self.skipped.append(event)

    def counter_for(self, rule_id: str) -> Counter:
        return Counter()


class TestMatcherWorker(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = _FakeContext()
        self.rule = _rule(pattern={"kind": "contains", "value": "panic"})
        self.worker = MatcherWorker(rules=[self.rule], context=self.ctx, queue_max=4)
        self.worker.start()

    def tearDown(self) -> None:
        self.worker.stop()

    def test_match_emits_fire(self) -> None:
        self.worker.feed_line("COM0", "Kernel panic - not syncing", wal_seq=42)
        self.worker.flush_for_test(timeout=1.0)
        self.assertEqual(len(self.ctx.fires), 1)
        f = self.ctx.fires[0]
        self.assertEqual(f.rule.rule_id, "o.n")
        self.assertEqual(f.selector, "COM0")
        self.assertEqual(f.match.matched_text, "panic")
        self.assertEqual(f.wal_seq, 42)

    def test_no_match_does_not_emit(self) -> None:
        self.worker.feed_line("COM0", "all good", wal_seq=1)
        self.worker.flush_for_test(timeout=1.0)
        self.assertEqual(self.ctx.fires, [])

    def test_drop_oldest_when_queue_full(self) -> None:
        self.worker._paused_for_test = True
        for i in range(10):
            self.worker.feed_line("COM0", f"panic {i}", wal_seq=i)
        self.worker._paused_for_test = False
        self.worker.flush_for_test(timeout=1.0)
        self.assertGreaterEqual(len(self.ctx.dropped), 1)

    def test_selector_all_applies_to_any_com(self) -> None:
        rule_all = _rule(name="m", selectors=["ALL"])
        self.worker.replace_rules([self.rule, rule_all])
        self.worker.feed_line("COMX", "panic now", wal_seq=99)
        self.worker.flush_for_test(timeout=1.0)
        self.assertEqual([f.rule.rule_id for f in self.ctx.fires], ["o.m"])


if __name__ == "__main__":
    unittest.main()
