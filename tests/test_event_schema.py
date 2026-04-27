from __future__ import annotations
import unittest

from sw_core.event_engine.schema import Rule, validate_rule_dict


class TestRuleSchema(unittest.TestCase):
    def _minimal(self) -> dict:
        return {
            "schema_version": 1,
            "owner": "tools-static",
            "name": "temp-overhold",
            "kind": "tool",
            "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "Temperature overhold 105C"},
            "handler": {"exec": ["/usr/local/bin/notice-tool"]},
        }

    def test_minimal_valid(self) -> None:
        rule = validate_rule_dict(self._minimal())
        self.assertIsInstance(rule, Rule)
        self.assertEqual(rule.rule_id, "tools-static.temp-overhold")
        self.assertEqual(rule.profile, "ALL")
        self.assertEqual(rule.level, "INFO")
        self.assertEqual(rule.scope, "spontaneous")
        self.assertIsNone(rule.max_fires)
        self.assertEqual(rule.cooldown_ms, 0)
        self.assertEqual(rule.timeout_ms, 10000)
        self.assertTrue(rule.auto_enable_com_on_load)
        self.assertFalse(rule.debug)

    def test_missing_schema_version(self) -> None:
        d = self._minimal(); d.pop("schema_version")
        with self.assertRaises(Exception):
            validate_rule_dict(d)

    def test_bad_owner(self) -> None:
        d = self._minimal(); d["owner"] = "Tools_Static"
        with self.assertRaises(Exception):
            validate_rule_dict(d)

    def test_bad_kind(self) -> None:
        d = self._minimal(); d["kind"] = "robot"
        with self.assertRaises(Exception):
            validate_rule_dict(d)

    def test_handler_must_xor(self) -> None:
        d = self._minimal()
        d["handler"] = {"exec": ["/x"], "shell": "echo x"}
        with self.assertRaises(Exception):
            validate_rule_dict(d)
        d["handler"] = {}
        with self.assertRaises(Exception):
            validate_rule_dict(d)

    def test_invalid_regex(self) -> None:
        d = self._minimal()
        d["pattern"] = {"kind": "regex", "value": "[broken"}
        with self.assertRaises(Exception):
            validate_rule_dict(d)

    def test_rule_id_mismatch(self) -> None:
        d = self._minimal()
        d["rule_id"] = "wrong.id"
        with self.assertRaises(Exception):
            validate_rule_dict(d)

    def test_explicit_rule_id_ok(self) -> None:
        d = self._minimal()
        d["rule_id"] = "tools-static.temp-overhold"
        rule = validate_rule_dict(d)
        self.assertEqual(rule.rule_id, "tools-static.temp-overhold")

    def test_selectors_all(self) -> None:
        d = self._minimal(); d["selectors"] = ["ALL"]
        rule = validate_rule_dict(d)
        self.assertEqual(rule.selectors, ("ALL",))


if __name__ == "__main__":
    unittest.main()
