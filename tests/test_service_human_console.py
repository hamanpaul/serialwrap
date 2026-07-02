import unittest
from unittest import mock

from sw_core.service import SerialwrapService

try:
    import state_iso  # pytest／unittest discover：tests/ 在 sys.path
except ImportError:  # python3 -m unittest tests.test_x（repo root 跑法，#120）
    from tests import state_iso


class TestServiceHumanConsole(unittest.TestCase):
    def setUp(self) -> None:
        state_iso.isolate_testcase(self)  # #120 per-file 隔離（unittest 不載 conftest）

    def test_human_console_interactive_command_uses_interactive_mode(self) -> None:
        svc = SerialwrapService([])
        with mock.patch.object(svc._arbiter, "submit") as submit:
            svc._on_console_line("p:COM0", "c1", "vim notes.txt")

        submit.assert_called_once_with(
            session_id="p:COM0",
            command="vim notes.txt",
            source="human:c1",
            mode="interactive",
            timeout_s=30.0,
            priority=100,
        )

    def test_human_console_regular_command_uses_line_mode(self) -> None:
        svc = SerialwrapService([])
        with mock.patch.object(svc._arbiter, "submit") as submit:
            svc._on_console_line("p:COM0", "c1", "echo hello")

        submit.assert_called_once_with(
            session_id="p:COM0",
            command="echo hello",
            source="human:c1",
            mode="line",
            timeout_s=30.0,
            priority=100,
        )

    def test_human_console_sudo_vim_is_interactive(self) -> None:
        svc = SerialwrapService([])
        with mock.patch.object(svc._arbiter, "submit") as submit:
            svc._on_console_line("p:COM0", "c1", "sudo vim /etc/config")

        self.assertEqual(submit.call_args.kwargs["mode"], "interactive")

    def test_session_self_test_rpc_passes_strict_human_lock(self) -> None:
        svc = SerialwrapService([])
        expected = {"ok": True, "selector": "COM0"}
        with mock.patch.object(svc._sessions, "self_test", return_value=expected) as self_test:
            resp = svc.rpc(
                "session.self_test",
                {
                    "selector": "COM0",
                    "timeout_s": 4.5,
                    "strict_human_lock": True,
                },
            )

        self_test.assert_called_once_with("COM0", timeout_s=4.5, strict_human_lock=True)
        self.assertEqual(resp, expected)

    def test_session_self_test_rpc_defaults_strict_human_lock_false(self) -> None:
        svc = SerialwrapService([])
        expected = {"ok": True, "selector": "COM0"}
        with mock.patch.object(svc._sessions, "self_test", return_value=expected) as self_test:
            resp = svc.rpc(
                "session.self_test",
                {
                    "selector": "COM0",
                    "timeout_s": 2.5,
                },
            )

        self_test.assert_called_once_with("COM0", timeout_s=2.5, strict_human_lock=False)
        self.assertEqual(resp, expected)

    def test_session_self_test_rpc_coerces_strict_human_lock_values(self) -> None:
        cases = [
            ("bool-true", True, True),
            ("bool-false", False, False),
            ("str-true", "true", True),
            ("str-false", "false", False),
            ("str-one", "1", True),
            ("str-zero", "0", False),
            ("int-one", 1, True),
            ("int-zero", 0, False),
            ("none", None, False),
        ]

        for name, raw_value, expected_value in cases:
            with self.subTest(case=name):
                svc = SerialwrapService([])
                with mock.patch.object(svc._sessions, "self_test", return_value={"ok": True}) as self_test:
                    resp = svc.rpc(
                        "session.self_test",
                        {
                            "selector": "COM0",
                            "strict_human_lock": raw_value,
                        },
                    )

                self_test.assert_called_once_with("COM0", timeout_s=2.0, strict_human_lock=expected_value)
                self.assertTrue(resp["ok"])


if __name__ == "__main__":
    unittest.main()
