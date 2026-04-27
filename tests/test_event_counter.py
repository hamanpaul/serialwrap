from __future__ import annotations
import os
import tempfile
import unittest

from sw_core.event_engine.counter import Counter, CounterStore


class TestCounter(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sw-counter-")
        self.store = CounterStore(self.tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_missing_returns_zero(self) -> None:
        c = self.store.load("a.b")
        self.assertEqual(c.fires, 0)
        self.assertIsNone(c.last_fire_ts)
        self.assertFalse(c.exhausted)

    def test_save_then_load(self) -> None:
        self.store.save("a.b", Counter(fires=3, last_fire_ts=1234, exhausted=False))
        c = self.store.load("a.b")
        self.assertEqual(c.fires, 3)
        self.assertEqual(c.last_fire_ts, 1234)
        self.assertFalse(c.exhausted)

    def test_clear_removes_file(self) -> None:
        self.store.save("a.b", Counter(fires=1, last_fire_ts=10, exhausted=False))
        self.assertTrue(os.path.exists(self.store.path_for("a.b")))
        self.store.clear("a.b")
        self.assertFalse(os.path.exists(self.store.path_for("a.b")))
        c = self.store.load("a.b")
        self.assertEqual(c.fires, 0)

    def test_atomic_save_does_not_leave_tmp(self) -> None:
        self.store.save("a.b", Counter(fires=1, last_fire_ts=1, exhausted=False))
        leftover = [f for f in os.listdir(self.tmp) if f.endswith(".tmp")]
        self.assertEqual(leftover, [])

    def test_list_known_rule_ids(self) -> None:
        self.store.save("a.b", Counter(fires=1, last_fire_ts=1, exhausted=False))
        self.store.save("c.d", Counter(fires=2, last_fire_ts=2, exhausted=True))
        self.assertEqual(set(self.store.known_rule_ids()), {"a.b", "c.d"})


if __name__ == "__main__":
    unittest.main()
