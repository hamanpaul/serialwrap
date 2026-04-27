from __future__ import annotations
import os
import shutil
import tempfile
import time
import unittest

from sw_core.event_engine.engine import EventEngine, EngineDeps
from sw_core.event_engine.counter import CounterStore


class _FakeBridgeQueries:
    def __init__(self) -> None:
        self.profiles: dict[str, str | None] = {}
        self.active: dict[str, str | None] = {}

    def active_cmd_id_for(self, com: str) -> str | None:
        return self.active.get(com)

    def profile_for(self, com: str) -> str | None:
        return self.profiles.get(com)

    def known_coms(self) -> list[str]:
        return sorted(set(self.profiles.keys()) | set(self.active.keys()))


class TestEventEngine(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sw-event-engine-")
        self.events_dir = os.path.join(self.tmp, "events.d")
        self.runtime_dir = os.path.join(self.tmp, "runtime")
        self.log_path = os.path.join(self.runtime_dir, "events.ndjson")
        os.makedirs(self.events_dir)
        os.makedirs(self.runtime_dir)
        self.bridge = _FakeBridgeQueries()
        self.engine = EventEngine(EngineDeps(
            events_dir=self.events_dir,
            runtime_dir=self.runtime_dir,
            log_path=self.log_path,
            bridge=self.bridge,
        ))

    def tearDown(self) -> None:
        self.engine.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_rule(self, owner: str, name: str, marker: str, **overrides) -> dict:
        rule = {
            "schema_version": 1,
            "owner": owner, "name": name, "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "panic"},
            "handler": {"shell": f"touch {marker}"},
            "timeout_ms": 2000,
            "auto_enable_com_on_load": True,
        }
        rule.update(overrides)
        self.engine.rule_set(rule)
        return rule

    def test_rule_set_then_fire(self) -> None:
        marker = os.path.join(self.tmp, "fired")
        self._add_rule("o", "n", marker)
        self.engine.start()
        # COM0 must be auto-enabled because rule.auto_enable_com_on_load=true
        status = self.engine.com_status("COM0")
        self.assertTrue(status["enabled"])
        self.engine.feed_line("COM0", "Kernel panic - not syncing", wal_seq=1)
        self._wait_for_file(marker, timeout=3.0)

    def test_disable_clears_counter(self) -> None:
        marker = os.path.join(self.tmp, "fired2")
        self._add_rule("o", "x", marker)
        self.engine.start()
        self.engine.feed_line("COM0", "panic", wal_seq=1)
        self._wait_for_file(marker, timeout=3.0)
        store = CounterStore(self.runtime_dir)
        self.assertGreater(store.load("o.x").fires, 0)
        self.engine.com_disable("COM0")
        self.assertEqual(store.load("o.x").fires, 0)

    def test_reload_diff_applies(self) -> None:
        marker = os.path.join(self.tmp, "fired3")
        self._add_rule("o", "y", marker)
        self.engine.start()
        # External edit: drop the rule file
        os.unlink(os.path.join(self.events_dir, "o.y.json"))
        self.engine.reload()
        self.engine.feed_line("COM0", "panic", wal_seq=1)
        time.sleep(0.4)
        self.assertFalse(os.path.exists(marker))

    def _wait_for_file(self, path: str, timeout: float) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if os.path.exists(path):
                return
            time.sleep(0.05)
        self.fail(f"file did not appear: {path}")


if __name__ == "__main__":
    unittest.main()
