from __future__ import annotations
import json
import os
import tempfile
import unittest

from sw_core.event_engine.event_log import EventLogger


class TestEventLogger(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sw-event-log-")
        self.path = os.path.join(self.tmp, "events.ndjson")
        self.log = EventLogger(self.path, rotate_bytes=1024, backup_count=2)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_one(self) -> None:
        self.log.write({"type": "rule_loaded", "rule_id": "o.n"})
        with open(self.path, "r", encoding="utf-8") as f:
            line = f.readline().strip()
        obj = json.loads(line)
        self.assertEqual(obj["type"], "rule_loaded")
        self.assertEqual(obj["rule_id"], "o.n")
        self.assertIn("ts", obj)

    def test_rotation(self) -> None:
        big = "x" * 200
        for _ in range(10):
            self.log.write({"type": "match_recorded", "blob": big})
        files = sorted(os.listdir(self.tmp))
        self.assertIn("events.ndjson", files)
        self.assertTrue(any(f.startswith("events.ndjson.") for f in files))

    def test_tail_filter(self) -> None:
        self.log.write({"type": "match_recorded", "rule_id": "o.a", "selector": "COM0"})
        self.log.write({"type": "match_recorded", "rule_id": "o.b", "selector": "COM1"})
        self.log.write({"type": "fire_completed", "rule_id": "o.a", "selector": "COM0"})
        rows = self.log.tail(rule_id="o.a")
        self.assertEqual([r["type"] for r in rows], ["match_recorded", "fire_completed"])
        rows2 = self.log.tail(selector="COM1")
        self.assertEqual([r["rule_id"] for r in rows2], ["o.b"])
        rows3 = self.log.tail(n=1)
        self.assertEqual(len(rows3), 1)
        self.assertEqual(rows3[0]["type"], "fire_completed")


if __name__ == "__main__":
    unittest.main()
