"""MCP _TOOL_MAP 完整性測試。"""
from __future__ import annotations

import re
import unittest


class TestMcpCompleteness(unittest.TestCase):
    """驗證 _TOOL_MAP 的所有 RPC method 都在 service.py 中有對應。"""

    def _rpc_methods(self) -> set[str]:
        """從 service.py rpc() 原始碼擷取所有 method == "xxx" 的 method name。"""
        import inspect

        import sw_core.service

        source = inspect.getsource(sw_core.service.SerialwrapService.rpc)
        return set(re.findall(r'method == "([^"]+)"', source))

    def test_all_tool_map_values_exist_in_rpc(self) -> None:
        """_TOOL_MAP 中的每個 value 都應該是 service.py rpc() 中的有效 method。"""
        from sw_mcp.server import _TOOL_MAP

        methods = self._rpc_methods()

        for tool_name, rpc_method in _TOOL_MAP.items():
            with self.subTest(tool=tool_name, method=rpc_method):
                self.assertIn(
                    rpc_method,
                    methods,
                    f"MCP tool {tool_name} maps to {rpc_method} "
                    f"but that method is not in rpc()",
                )

    def test_all_rpc_methods_have_mcp_mapping(self) -> None:
        """service.py rpc() 中的每個 method 都應該在 _TOOL_MAP 中有對應。"""
        from sw_mcp.server import _TOOL_MAP

        methods = self._rpc_methods()
        mapped_methods = set(_TOOL_MAP.values())
        unmapped = methods - mapped_methods
        self.assertEqual(
            unmapped,
            set(),
            f"以下 RPC methods 缺少 MCP mapping: {unmapped}",
        )

    def test_tool_map_no_duplicates(self) -> None:
        """_TOOL_MAP 中不應有重複的 RPC method。"""
        from sw_mcp.server import _TOOL_MAP

        values = list(_TOOL_MAP.values())
        seen: set[str] = set()
        dupes: set[str] = set()
        for v in values:
            if v in seen:
                dupes.add(v)
            seen.add(v)
        self.assertEqual(
            dupes,
            set(),
            f"重複的 RPC method mapping: {dupes}",
        )

    def test_list_tools_matches_tool_map(self) -> None:
        """list_tools() 回傳的工具名稱集合應與 _TOOL_MAP 的 key 一致。"""
        from sw_mcp.server import _TOOL_MAP, list_tools

        tool_names = {t["name"] for t in list_tools()}
        map_keys = set(_TOOL_MAP.keys())
        self.assertEqual(
            tool_names,
            map_keys,
            f"list_tools() 與 _TOOL_MAP 不一致 — "
            f"僅在 list_tools: {tool_names - map_keys}, "
            f"僅在 _TOOL_MAP: {map_keys - tool_names}",
        )

    def test_list_tools_no_duplicate_names(self) -> None:
        """list_tools() 中不應有重複的工具名稱。"""
        from sw_mcp.server import list_tools

        names = [t["name"] for t in list_tools()]
        self.assertEqual(
            len(names),
            len(set(names)),
            f"list_tools() 有重複工具名稱",
        )

    def test_self_test_tool_schema_describes_strict_human_lock(self) -> None:
        """serialwrap_self_test 應公開 strict_human_lock schema 與模式說明。"""
        from sw_mcp.server import list_tools

        tool = next(t for t in list_tools() if t["name"] == "serialwrap_self_test")
        props = tool["inputSchema"]["properties"]
        required = tool["inputSchema"].get("required", [])
        strict_desc = props["strict_human_lock"]["description"]

        self.assertEqual(props["strict_human_lock"]["type"], "boolean")
        self.assertIn("嚴格", tool["description"])
        self.assertIn("預設", tool["description"])
        self.assertIn("interactive lease", tool["description"])
        self.assertNotIn("raw interactive", tool["description"])
        self.assertIn("嚴格", strict_desc)
        self.assertIn("預設", strict_desc)
        self.assertIn("interactive lease", strict_desc)
        self.assertNotIn("strict_human_lock", required)


if __name__ == "__main__":
    unittest.main()
