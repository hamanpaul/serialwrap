import unittest
from unittest import mock

from sw_core.service import SerialwrapService


class TestServiceHumanConsole(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
