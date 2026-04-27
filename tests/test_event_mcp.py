from __future__ import annotations
import unittest

from sw_mcp.server import _TOOL_DEFS, _TOOL_MAP


_REQUIRED_TOOLS = {
    "serialwrap_event_rule_set": "event.rule_set",
    "serialwrap_event_rule_delete": "event.rule_delete",
    "serialwrap_event_rule_list": "event.rule_list",
    "serialwrap_event_rule_get": "event.rule_get",
    "serialwrap_event_enable": "event.com_enable",
    "serialwrap_event_disable": "event.com_disable",
    "serialwrap_event_status": "event.com_status",
    "serialwrap_event_reset": "event.reset",
    "serialwrap_event_reload": "event.reload",
    "serialwrap_event_tail": "event.tail",
}


class TestEventMcp(unittest.TestCase):
    def test_all_tools_in_map(self) -> None:
        for tool, method in _REQUIRED_TOOLS.items():
            self.assertIn(tool, _TOOL_MAP)
            self.assertEqual(_TOOL_MAP[tool], method)

    def test_descriptions_mention_status_first(self) -> None:
        for td in _TOOL_DEFS:
            if td["name"].startswith("serialwrap_event_"):
                self.assertIn("status", td["description"])

    def test_descriptions_warn_about_auto_enable(self) -> None:
        warned = False
        for td in _TOOL_DEFS:
            if td["name"] == "serialwrap_event_enable":
                self.assertIn("auto_enable", td["description"])
                warned = True
        self.assertTrue(warned)


if __name__ == "__main__":
    unittest.main()
