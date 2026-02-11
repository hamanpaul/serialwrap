import unittest

from sw_core.alias_registry import AliasRegistry


class TestAliasRegistry(unittest.TestCase):
    def test_assign_unassign(self) -> None:
        reg = AliasRegistry()
        reg.assign_by_id("/dev/serial/by-id/a", "demo+1", "demo")
        rows = reg.list_alias()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["alias"], "demo+1")
        self.assertTrue(reg.unassign("demo+1"))
        self.assertFalse(reg.unassign("demo+1"))


if __name__ == "__main__":
    unittest.main()
