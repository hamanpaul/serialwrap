from __future__ import annotations
import json
import os
import tempfile
import unittest

from sw_core.event_engine.registry import RuleRegistry, RuleLoadError


def _rule_dict(owner: str, name: str, **overrides) -> dict:
    base = {
        "schema_version": 1,
        "owner": owner,
        "name": name,
        "kind": "tool",
        "selectors": ["COM0"],
        "pattern": {"kind": "contains", "value": "x"},
        "handler": {"exec": ["/bin/true"]},
    }
    base.update(overrides)
    return base


class TestRuleRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="sw-events-d-")
        self.reg = RuleRegistry(self.tmp)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_empty_dir(self) -> None:
        result = self.reg.load_all()
        self.assertEqual(result.rules, [])
        self.assertEqual(result.failed, [])

    def test_save_then_load(self) -> None:
        d = _rule_dict("o", "n")
        rule = self.reg.upsert(d)
        self.assertEqual(rule.rule_id, "o.n")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "o.n.json")))
        loaded = self.reg.load_all().rules
        self.assertEqual([r.rule_id for r in loaded], ["o.n"])

    def test_delete(self) -> None:
        self.reg.upsert(_rule_dict("o", "n"))
        self.assertTrue(self.reg.delete("o.n"))
        self.assertFalse(self.reg.delete("o.n"))

    def test_invalid_file_collected_in_failed(self) -> None:
        with open(os.path.join(self.tmp, "bad.json"), "w") as f:
            f.write("{not json")
        with open(os.path.join(self.tmp, "bad-schema.json"), "w") as f:
            json.dump({"schema_version": 1}, f)
        result = self.reg.load_all()
        self.assertEqual(result.rules, [])
        self.assertEqual({entry.path for entry in result.failed},
                         {os.path.join(self.tmp, "bad.json"),
                          os.path.join(self.tmp, "bad-schema.json")})

    def test_reload_diff_classification(self) -> None:
        old_a = self.reg.upsert(_rule_dict("o", "a"))
        self.reg.upsert(_rule_dict("o", "b"))
        path_b = os.path.join(self.tmp, "o.b.json")
        with open(path_b, "r") as f:
            obj = json.load(f)
        obj["cooldown_ms"] = 5000
        with open(path_b, "w") as f:
            json.dump(obj, f)
        self.reg.upsert(_rule_dict("o", "c"))
        os.unlink(os.path.join(self.tmp, "o.a.json"))
        diff = self.reg.diff_against([old_a, self.reg.get("o.b")])
        self.assertEqual({r.rule_id for r in diff.added}, {"o.c"})
        self.assertEqual({r.rule_id for r in diff.changed}, {"o.b"})
        self.assertEqual({r.rule_id for r in diff.removed}, {"o.a"})


if __name__ == "__main__":
    unittest.main()
