from __future__ import annotations
import os
import shutil
import tempfile
import unittest

from sw_core.event_engine import EventEngine, EngineDeps


class _NullBridge:
    def active_cmd_id_for(self, com): return None
    def profile_for(self, com): return None
    def known_coms(self): return ["COM0"]


class TestEventRpcDispatch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sw-event-rpc-")
        events_dir = os.path.join(self.tmp, "events.d")
        runtime_dir = os.path.join(self.tmp, "runtime")
        os.makedirs(runtime_dir)
        self.engine = EventEngine(EngineDeps(
            events_dir=events_dir,
            runtime_dir=runtime_dir,
            log_path=os.path.join(runtime_dir, "events.ndjson"),
            bridge=_NullBridge(),
        ))

    def tearDown(self) -> None:
        self.engine.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rule_set_then_list(self) -> None:
        rule = {
            "schema_version": 1,
            "owner": "o", "name": "n", "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "x"},
            "handler": {"exec": ["/bin/true"]},
        }
        self.engine.rule_set(rule)
        rows = self.engine.rule_list()
        self.assertEqual([r["rule_id"] for r in rows], ["o.n"])

    def test_status_reflects_auto_enable(self) -> None:
        rule = {
            "schema_version": 1,
            "owner": "o", "name": "n", "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "x"},
            "handler": {"exec": ["/bin/true"]},
            "auto_enable_com_on_load": True,
        }
        self.engine.rule_set(rule)
        self.assertTrue(self.engine.com_status("COM0")["enabled"])

    def test_reset_clears_counter(self) -> None:
        rule = {
            "schema_version": 1,
            "owner": "o", "name": "n", "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "x"},
            "handler": {"exec": ["/bin/true"]},
        }
        self.engine.rule_set(rule)
        counter_cls = self.engine._counters.load("o.n").__class__
        self.engine._counters.save("o.n", counter_cls(fires=5, last_fire_ts=1, exhausted=False))
        cleared = self.engine.reset(rule_id="o.n")
        self.assertEqual(cleared, 1)
        self.assertEqual(self.engine._counters.load("o.n").fires, 0)


if __name__ == "__main__":
    unittest.main()
