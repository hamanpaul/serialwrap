"""#171：CLI RPC trace 最小診斷事件。

驗收重點：
1. 預設 stdout/stderr 契約不變；`-v` 或有效 `SERIALWRAP_LOG_LEVEL` 才輸出 trace。
2. trace 只含白名單欄位，且 method / endpoint source 反映當次實際 RPC。
3. errno 只取最後一次主請求 attempt；retry 後 success 與 TIMEOUT enrich 不得殘留 errno。
4. trace logger 與 root/serialwrap logger 隔離，重複呼叫 `main()` 不得重複掛 handler 或殘留 verbosity。
"""
from __future__ import annotations

import errno
import io
import json
import logging
import os
import re
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest import mock

import sw_core.client as client
from sw_core import cli, cli_trace


TRACE_KEYS = {
    "endpoint_transport",
    "endpoint_id",
    "endpoint_source",
    "method",
    "elapsed_ms",
    "error_code",
    "errno",
    "errno_name",
    "retry_count",
    "timeout_s",
}


class _TcpReplySocket:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._response = json.dumps(payload).encode("utf-8") + b"\n"
        self.timeout: float | None = None
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, timeout_s: float) -> None:
        self.timeout = timeout_s

    def sendall(self, payload: bytes) -> None:
        self.sent.append(payload)

    def recv(self, _size: int) -> bytes:
        if self._response is None:
            return b""
        response, self._response = self._response, None
        return response

    def close(self) -> None:
        self.closed = True


class CliDiagnosticsMixin:
    def _invoke_main(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with (
            mock.patch.dict(os.environ, env or {}, clear=False),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def _stderr_json_lines(self, stderr_text: str) -> list[dict[str, Any]]:
        traces: list[dict[str, Any]] = []
        for raw_line in stderr_text.splitlines():
            line = raw_line.strip()
            if not line.startswith("{"):
                continue
            traces.append(json.loads(line))
        return traces

    def _assert_trace_shape(self, trace: dict[str, Any]) -> None:
        self.assertEqual(set(trace), TRACE_KEYS)
        self.assertIsInstance(trace["elapsed_ms"], int)
        self.assertGreaterEqual(trace["elapsed_ms"], 0)

    def _assert_sha256_hex(self, value: str) -> None:
        self.assertRegex(value, r"^[0-9a-f]{64}$")


class TestCliTraceOutput(CliDiagnosticsMixin, unittest.TestCase):
    def test_default_stdout_stderr_bytes_remain_unchanged(self) -> None:
        with mock.patch(
            "sw_core.cli.rpc_call",
            return_value={"ok": True, "sessions": []},
        ):
            rc, out, err = self._invoke_main(["session", "list"])

        self.assertEqual(rc, 0)
        self.assertEqual(out, '{"ok":true,"sessions":[]}\n')
        self.assertEqual(err, "")

    def test_verbose_emits_single_trace_and_next_main_resets_to_quiet(self) -> None:
        with mock.patch(
            "sw_core.cli.rpc_call",
            return_value={"ok": True, "sessions": []},
        ):
            rc1, out1, err1 = self._invoke_main(["-v", "session", "list"])
            rc2, out2, err2 = self._invoke_main(["session", "list"])

        self.assertEqual((rc1, out1), (0, '{"ok":true,"sessions":[]}\n'))
        self.assertEqual((rc2, out2, err2), (0, '{"ok":true,"sessions":[]}\n', ""))

        traces = self._stderr_json_lines(err1)
        self.assertEqual(len(traces), 1)
        self._assert_trace_shape(traces[0])
        self.assertEqual(traces[0]["method"], "session.list")
        self.assertEqual(traces[0]["endpoint_source"], "default")
        logger = logging.getLogger("serialwrap.cli_trace")
        self.assertFalse(logger.propagate)
        self.assertEqual(
            sum(bool(getattr(handler, "_serialwrap_cli_trace_handler", False)) for handler in logger.handlers),
            1,
        )

    def test_env_log_level_controls_trace_and_verbose_overrides_env(self) -> None:
        with mock.patch(
            "sw_core.cli.rpc_call",
            return_value={"ok": True, "sessions": []},
        ):
            rc_info, _out_info, err_info = self._invoke_main(
                ["session", "list"],
                env={"SERIALWRAP_LOG_LEVEL": "INFO"},
            )
            rc_invalid, _out_invalid, err_invalid = self._invoke_main(
                ["session", "list"],
                env={"SERIALWRAP_LOG_LEVEL": "NOT_A_LEVEL"},
            )
            rc_override, _out_override, err_override = self._invoke_main(
                ["-v", "session", "list"],
                env={"SERIALWRAP_LOG_LEVEL": "ERROR"},
            )

        self.assertEqual(rc_info, 0)
        self.assertEqual(len(self._stderr_json_lines(err_info)), 1)
        self.assertEqual(rc_invalid, 0)
        self.assertEqual(self._stderr_json_lines(err_invalid), [])
        self.assertEqual(rc_override, 0)
        self.assertEqual(len(self._stderr_json_lines(err_override)), 1)

    def test_invalid_numeric_log_level_falls_back_quietly_and_still_sends_rpc(self) -> None:
        """未知數字環境值不得在一般 RPC CLI 送出前拋 ValueError。"""
        for raw in ("--1", "²", "9" * 5000):
            with self.subTest(raw=raw):
                with mock.patch(
                    "sw_core.cli.rpc_call",
                    return_value={"ok": True, "sessions": []},
                ) as rpc:
                    rc, out, err = self._invoke_main(
                        ["--socket", "/tmp/sw198-invalid-log-level.sock", "session", "list"],
                        env={"SERIALWRAP_LOG_LEVEL": raw},
                    )

                self.assertEqual(rc, 0)
                self.assertEqual(out, '{"ok":true,"sessions":[]}' + "\n")
                rpc.assert_called_once()
                self.assertEqual(self._stderr_json_lines(err), [])

    def test_numeric_log_level_and_whitespace_name_remain_supported(self) -> None:
        self.assertEqual(cli_trace._parse_log_level("  INFO  "), logging.INFO)
        self.assertEqual(cli_trace._parse_log_level("20"), logging.INFO)
        self.assertEqual(cli_trace._parse_log_level("5000"), 5000)
        self.assertEqual(cli_trace.resolve_cli_trace_level(0, {"SERIALWRAP_LOG_LEVEL": " 10 "}), logging.DEBUG)

    def test_numeric_log_level_has_a_fixed_4300_digit_boundary(self) -> None:
        get_digit_limit = getattr(sys, "get_int_max_str_digits", None)
        set_digit_limit = getattr(sys, "set_int_max_str_digits", None)
        previous_limit = (
            get_digit_limit()
            if callable(get_digit_limit) and callable(set_digit_limit)
            else None
        )
        try:
            if previous_limit is not None:
                set_digit_limit(0)

            boundary = "9" * 4300
            over_boundary = "9" * 4301
            self.assertIsNotNone(cli_trace._parse_log_level(boundary))
            self.assertIsNotNone(cli_trace._parse_log_level("-" + boundary))
            self.assertIsNone(cli_trace._parse_log_level(over_boundary))
            self.assertIsNone(cli_trace._parse_log_level("-" + over_boundary))
            self.assertEqual(
                cli_trace.resolve_cli_trace_level(
                    1, {"SERIALWRAP_LOG_LEVEL": "9" * 5000}
                ),
                logging.INFO,
            )
            self.assertEqual(
                cli_trace.resolve_cli_trace_level(
                    2, {"SERIALWRAP_LOG_LEVEL": "9" * 5000}
                ),
                logging.DEBUG,
            )
        finally:
            if previous_limit is not None:
                set_digit_limit(previous_limit)

    def test_5000_digit_log_level_warns_with_default_and_disabled_digit_limit(self) -> None:
        get_digit_limit = getattr(sys, "get_int_max_str_digits", None)
        set_digit_limit = getattr(sys, "set_int_max_str_digits", None)
        previous_limit = (
            get_digit_limit()
            if callable(get_digit_limit) and callable(set_digit_limit)
            else None
        )
        default_limit = getattr(sys.int_info, "default_max_str_digits", None)
        limits = (
            [default_limit, 0]
            if previous_limit is not None and isinstance(default_limit, int) and default_limit > 0
            else [None]
        )
        raw = "9" * 5000
        try:
            for digit_limit in limits:
                with self.subTest(python_digit_limit=digit_limit):
                    if digit_limit is not None:
                        set_digit_limit(digit_limit)

                    warning_stream = io.StringIO()
                    logger = cli_trace.configure_cli_trace_logger(
                        0,
                        stream=warning_stream,
                        env={"SERIALWRAP_LOG_LEVEL": raw},
                    )
                    self.assertEqual(logger.level, logging.WARNING)
                    self.assertTrue(logger.isEnabledFor(logging.WARNING))
                    logger.warning("R1 WARNING fallback is visible")
                    self.assertEqual(
                        warning_stream.getvalue(), "R1 WARNING fallback is visible\n"
                    )

                    with mock.patch(
                        "sw_core.cli.rpc_call",
                        return_value={"ok": True, "sessions": []},
                    ) as rpc:
                        rc, out, err = self._invoke_main(
                            ["--socket", "/tmp/sw198-digit-limit.sock", "session", "list"],
                            env={"SERIALWRAP_LOG_LEVEL": raw},
                        )

                    self.assertEqual(rc, 0)
                    self.assertEqual(out, '{"ok":true,"sessions":[]}\n')
                    self.assertEqual(err, "")
                    rpc.assert_called_once()
        finally:
            if previous_limit is not None:
                set_digit_limit(previous_limit)

    def test_trace_logger_recovers_when_previous_stream_was_closed(self) -> None:
        with tempfile.TemporaryFile(mode="w+") as previous_stream:
            cli_trace.configure_cli_trace_logger(1, stream=previous_stream)

        current_stream = io.StringIO()
        logger = cli_trace.configure_cli_trace_logger(1, stream=current_stream)
        logger.info("closed-stream recovery")

        self.assertEqual(current_stream.getvalue(), "closed-stream recovery\n")

    def test_config_fallback_trace_uses_single_resolution_without_extra_probe(self) -> None:
        fake_rc = mock.Mock()
        fake_rc.socket_path.return_value = "/tmp/sw171-config.sock"
        fake_rc.mode.return_value = "systemd-system"
        with (
            mock.patch("sw_core.cli._default_runtime_config", return_value=fake_rc),
            mock.patch(
                "sw_core.cli._endpoint_alive",
                side_effect=lambda endpoint: endpoint == cli.SYSTEM_SOCKET,
            ) as endpoint_alive,
            mock.patch(
                "sw_core.cli.rpc_call",
                return_value={"ok": True, "sessions": []},
            ),
        ):
            rc, _out, err = self._invoke_main(["-v", "session", "list"])

        self.assertEqual(rc, 0)
        self.assertEqual(
            [call.args[0] for call in endpoint_alive.call_args_list],
            ["/tmp/sw171-config.sock", cli.SYSTEM_SOCKET],
        )
        traces = self._stderr_json_lines(err)
        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self._assert_trace_shape(trace)
        self.assertEqual(trace["endpoint_transport"], "unix")
        self.assertEqual(trace["endpoint_source"], "config.yaml -> canonical fallback")
        self._assert_sha256_hex(trace["endpoint_id"])

    def test_help_includes_verbose_flag_and_precedence_text(self) -> None:
        out = io.StringIO()
        with self.assertRaises(SystemExit):
            with redirect_stdout(out):
                cli.main(["--help"])

        help_text = out.getvalue()
        self.assertIn("-v, --verbose", help_text)
        self.assertIn("-v=INFO", help_text)
        self.assertIn("-vv=DEBUG", help_text)
        self.assertIn("SERIALWRAP_LOG_LEVEL", help_text)

    def test_event_rule_set_trace_uses_actual_method_and_hides_params(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rule_path = os.path.join(td, "rule.json")
            with open(rule_path, "w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "rule_id": "sw171-rule",
                        "selector": "COM0",
                        "secret": "top-secret-token",
                        "command": "echo should-not-leak",
                    },
                    fp,
                )
            with mock.patch(
                "sw_core.cli.rpc_call",
                return_value={"ok": True, "saved": True},
            ):
                rc, _out, err = self._invoke_main(
                    ["-v", "--socket", "/tmp/sw171-event.sock", "event", "add", "--file", rule_path]
                )

        self.assertEqual(rc, 0)
        traces = self._stderr_json_lines(err)
        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self._assert_trace_shape(trace)
        self.assertEqual(trace["method"], "event.rule_set")
        self.assertEqual(trace["endpoint_source"], "--socket")
        self.assertEqual(trace["endpoint_transport"], "unix")
        self._assert_sha256_hex(trace["endpoint_id"])
        trace_text = json.dumps(trace, ensure_ascii=False, separators=(",", ":"))
        self.assertNotIn("top-secret-token", trace_text)
        self.assertNotIn("echo should-not-leak", trace_text)
        self.assertNotIn("sw171-rule", trace_text)
        self.assertNotIn("/tmp/sw171-event.sock", trace_text)


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "需 AF_UNIX（原生 Windows 無此屬性）")
class TestCliTraceErrno(CliDiagnosticsMixin, unittest.TestCase):
    def test_missing_unix_socket_records_enoent(self) -> None:
        missing = "/tmp/sw171-cli-trace-missing.sock"
        rc, out, err = self._invoke_main(["-v", "--socket", missing, "session", "list"])

        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error_code"], "SOCKET_ERROR")
        traces = self._stderr_json_lines(err)
        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self._assert_trace_shape(trace)
        self.assertEqual(trace["error_code"], "SOCKET_ERROR")
        self.assertEqual(trace["errno"], errno.ENOENT)
        self.assertEqual(trace["errno_name"], "ENOENT")
        self.assertEqual(trace["endpoint_transport"], "unix")
        self.assertEqual(trace["endpoint_source"], "--socket")
        self._assert_sha256_hex(trace["endpoint_id"])

    def test_unix_permission_error_records_eacces(self) -> None:
        fake_sock = mock.MagicMock()
        fake_sock.connect.side_effect = PermissionError(errno.EACCES, "Permission denied")
        with mock.patch("sw_core.client.socket.socket", return_value=fake_sock):
            rc, out, err = self._invoke_main(
                ["-v", "--socket", "/tmp/sw171-no-access.sock", "session", "list"]
            )

        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error_code"], "SOCKET_ERROR")
        traces = self._stderr_json_lines(err)
        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self.assertEqual(trace["errno"], errno.EACCES)
        self.assertEqual(trace["errno_name"], "EACCES")

    def test_timeout_enrich_socket_error_does_not_pollute_main_errno(self) -> None:
        def fake_once(
            _endpoint: str,
            method: str,
            _params: dict[str, Any],
            *,
            req_id: int = 1,
            timeout_s: float = 5.0,
        ) -> dict[str, Any]:
            del req_id, timeout_s
            if method == "session.recover":
                return {"ok": False, "error_code": "TIMEOUT"}
            if method == "health.ping":
                return {"ok": False, "error_code": "SOCKET_ERROR", "message": "[Errno 111] refused"}
            raise AssertionError(f"unexpected method {method}")

        with mock.patch.object(client, "_rpc_call_once", side_effect=fake_once):
            rc, out, err = self._invoke_main(
                ["-v", "session", "recover", "--selector", "COM0"]
            )

        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error_code"], "TIMEOUT")
        traces = self._stderr_json_lines(err)
        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self.assertEqual(trace["error_code"], "TIMEOUT")
        self.assertIsNone(trace["errno"])
        self.assertIsNone(trace["errno_name"])


class TestCliTraceTcpAndRetry(CliDiagnosticsMixin, unittest.TestCase):
    def test_loopback_tcp_connection_refused_keeps_visible_host_port(self) -> None:
        with mock.patch(
            "sw_core.client.socket.create_connection",
            side_effect=ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
        ):
            rc, out, err = self._invoke_main(
                ["-v", "--endpoint", "tcp://127.0.0.1:48700", "session", "list"]
            )

        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out)["error_code"], "SOCKET_ERROR")
        traces = self._stderr_json_lines(err)
        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self._assert_trace_shape(trace)
        self.assertEqual(trace["endpoint_transport"], "tcp")
        self.assertEqual(trace["endpoint_id"], "127.0.0.1:48700")
        self.assertEqual(trace["errno"], errno.ECONNREFUSED)
        self.assertEqual(trace["errno_name"], "ECONNREFUSED")

    def test_retry_success_clears_errno_and_reports_retry_count(self) -> None:
        reply_sock = _TcpReplySocket({"ok": True, "sessions": []})
        with (
            mock.patch(
                "sw_core.client.socket.create_connection",
                side_effect=[
                    ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"),
                    reply_sock,
                ],
            ),
            mock.patch("sw_core.client.time.sleep"),
        ):
            rc, out, err = self._invoke_main(
                [
                    "-v",
                    "--endpoint",
                    "tcp://127.0.0.1:48700",
                    "--retries",
                    "1",
                    "session",
                    "list",
                ]
            )

        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ok"])
        traces = self._stderr_json_lines(err)
        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self._assert_trace_shape(trace)
        self.assertIsNone(trace["error_code"])
        self.assertEqual(trace["retry_count"], 1)
        self.assertIsNone(trace["errno"])
        self.assertIsNone(trace["errno_name"])


class TestCliTraceDaemonStart(CliDiagnosticsMixin, unittest.TestCase):
    def test_daemon_start_already_running_success_emits_single_probe_trace(self) -> None:
        with (
            mock.patch("sw_core.cli._safe_runtime_config", return_value=None),
            mock.patch("sw_core.cli.rpc_call", return_value={"ok": True}) as rpc,
        ):
            rc, out, err = self._invoke_main(
                ["-v", "--socket", "/tmp/sw171-already-running.sock", "daemon", "start"]
            )

        self.assertEqual(rc, 0)
        self.assertEqual(rpc.call_count, 1)
        self.assertEqual(rpc.call_args.args[1], "health.ping")
        self.assertEqual(json.loads(out), {"ok": True, "already_running": True, "socket": "/tmp/sw171-already-running.sock"})
        traces = self._stderr_json_lines(err)
        self.assertEqual(len(traces), 1)
        trace = traces[0]
        self._assert_trace_shape(trace)
        self.assertEqual(trace["method"], "health.ping")
        self.assertEqual(trace["endpoint_source"], "--socket")
        self.assertIsNone(trace["error_code"])

    def test_daemon_start_probe_failure_is_traced_before_spawn_wait_loop(self) -> None:
        proc = mock.Mock(pid=4321, returncode=None)
        proc.poll.return_value = None
        rpc_responses = [
            {"ok": False, "error_code": "SOCKET_ERROR", "message": "[Errno 2] missing"},
            {"ok": True},
            {"ok": True, "warnings": ["no_profiles_loaded"]},
        ]
        with (
            mock.patch("sw_core.cli._safe_runtime_config", return_value=None),
            mock.patch("sw_core.cli._find_conflicting_daemon", return_value=None),
            mock.patch("sw_core.cli._resolve_daemon_start_env_files", return_value=[]),
            mock.patch("sw_core.cli._load_daemon_start_env_files", return_value=({}, [])),
            mock.patch("sw_core.cli.subprocess.Popen", return_value=proc),
            mock.patch("sw_core.cli.time.sleep"),
            mock.patch("sw_core.cli.rpc_call", side_effect=rpc_responses) as rpc,
        ):
            rc, out, err = self._invoke_main(
                ["-v", "--socket", "/tmp/sw171-probe-fail.sock", "daemon", "start"]
            )

        self.assertEqual(rc, 0)
        self.assertEqual(rpc.call_count, 3)
        methods = [call.args[1] for call in rpc.call_args_list]
        self.assertEqual(methods, ["health.ping", "health.ping", "health.status"])
        self.assertEqual(json.loads(out), {"ok": True, "pid": 4321, "socket": "/tmp/sw171-probe-fail.sock", "warnings": ["no_profiles_loaded"]})
        traces = self._stderr_json_lines(err)
        self.assertEqual([trace["method"] for trace in traces], ["health.ping", "health.ping", "health.status"])
        first = traces[0]
        self._assert_trace_shape(first)
        self.assertEqual(first["endpoint_source"], "--socket")
        self.assertEqual(first["error_code"], "SOCKET_ERROR")
        self.assertIsNone(first["errno"])


class TestCliTraceSetup(CliDiagnosticsMixin, unittest.TestCase):
    def _run_isolated_setup(
        self,
        argv: list[str],
        *,
        flashing: bool = False,
        health_ok: bool = True,
    ) -> dict[str, Any]:
        calls: list[dict[str, Any]] = []
        runtime = mock.Mock()
        runtime.mode.return_value = "on-demand"
        effects = mock.Mock()
        effects.has_systemd.return_value = False

        def fake_rpc(
            endpoint: str,
            method: str,
            params: dict[str, Any],
            *,
            timeout_s: float = 5.0,
            retries: int = 0,
            trace_sink: Any = None,
        ) -> dict[str, Any]:
            calls.append({
                "endpoint": endpoint,
                "method": method,
                "params": dict(params),
                "timeout_s": timeout_s,
                "retries": retries,
                "trace_sink": trace_sink,
            })
            if trace_sink is not None:
                trace_sink({"elapsed_ms": 1, "retry_count": 0, "errno": None, "errno_name": None})
            if method == "health.ping":
                return {"ok": health_ok, **({} if health_ok else {"error_code": "SOCKET_ERROR"})}
            if method == "mcu.status":
                return {"ok": True, "flashing": flashing}
            raise AssertionError(f"unexpected method: {method}")

        with (
            mock.patch("sw_core.sysenv.SystemEffects", return_value=effects),
            mock.patch("sw_core.cli._default_runtime_config", return_value=runtime),
            mock.patch(
                "sw_core.cli._resolve_endpoint_info",
                return_value=cli._ResolvedEndpoint("/tmp/sw198-setup.sock", "--socket"),
            ) as resolve_endpoint,
            mock.patch("sw_core.cli.detect_legacy_install", return_value=[]),
            mock.patch("sw_core.cli.materialize_assets") as materialize,
            mock.patch("sw_core.cli.ensure_wsl_systemd", return_value={"needs_restart": False}),
            mock.patch("sw_core.cli.reconcile", return_value={"mode": "on-demand"}) as reconcile,
            mock.patch("sw_core.cli.rpc_call", side_effect=fake_rpc),
        ):
            rc, out, err = self._invoke_main(argv, env={"SERIALWRAP_LOG_LEVEL": "WARNING"})

        return {
            "rc": rc,
            "out": out,
            "err": err,
            "calls": calls,
            "resolve_count": resolve_endpoint.call_count,
            "materialize": materialize,
            "reconcile": reconcile,
        }

    def test_setup_success_trace_covers_existing_probe_rpcs_without_changing_sequence(self) -> None:
        quiet = self._run_isolated_setup(["setup", "--on-demand"])
        verbose = self._run_isolated_setup(["-v", "setup", "--on-demand"])

        quiet_sequence = [(item["method"], item["timeout_s"]) for item in quiet["calls"]]
        verbose_sequence = [(item["method"], item["timeout_s"]) for item in verbose["calls"]]
        self.assertEqual(quiet_sequence, [("health.ping", 0.5), ("mcu.status", 0.5)])
        self.assertEqual(verbose_sequence, quiet_sequence)
        self.assertEqual(quiet["resolve_count"], 2)
        self.assertEqual(verbose["resolve_count"], 2)
        self.assertEqual(quiet["rc"], 0)
        self.assertEqual(verbose["rc"], 0)
        self.assertEqual(self._stderr_json_lines(quiet["err"]), [])
        self.assertEqual(
            [trace["method"] for trace in self._stderr_json_lines(verbose["err"])],
            ["health.ping", "mcu.status"],
        )
        self.assertEqual(json.loads(quiet["out"])["ok"], True)
        self.assertEqual(json.loads(verbose["out"])["ok"], True)

    def test_setup_flashing_early_return_keeps_traced_probe_and_skips_mutations(self) -> None:
        result = self._run_isolated_setup(["-v", "setup", "--on-demand"], flashing=True)

        self.assertEqual(result["rc"], 2)
        self.assertEqual(
            [(item["method"], item["timeout_s"]) for item in result["calls"]],
            [("health.ping", 0.5), ("mcu.status", 0.5)],
        )
        self.assertEqual(
            [trace["method"] for trace in self._stderr_json_lines(result["err"])],
            ["health.ping", "mcu.status"],
        )
        self.assertEqual(json.loads(result["out"])["error_code"], "FLASHING_BUSY")
        result["materialize"].assert_not_called()
        result["reconcile"].assert_not_called()

    def test_setup_health_ping_failure_still_sends_mcu_status_probe(self) -> None:
        result = self._run_isolated_setup(
            ["-v", "setup", "--on-demand"],
            health_ok=False,
        )

        self.assertEqual(result["rc"], 0)
        self.assertEqual(
            [item["method"] for item in result["calls"]],
            ["health.ping", "mcu.status"],
        )
        self.assertEqual(
            [trace["method"] for trace in self._stderr_json_lines(result["err"])],
            ["health.ping", "mcu.status"],
        )


class TestCliTraceNoExtraRpcOnTraceSinkFailure(CliDiagnosticsMixin, unittest.TestCase):
    def test_mutating_rpc_typeerror_after_send_is_not_retried(self) -> None:
        cases = [
            (
                ["-v", "session", "recover", "--selector", "COM0"],
                "session.recover",
            ),
            (
                ["-v", "cmd", "submit", "--selector", "COM0", "--cmd", "echo hi"],
                "command.submit",
            ),
        ]

        for argv, expected_method in cases:
            calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

            def fake_rpc_call(
                endpoint: str,
                method: str,
                params: dict[str, Any],
                *,
                timeout_s: float = 5.0,
                retries: int = 0,
                trace_sink: Any = None,
            ) -> dict[str, Any]:
                calls.append((endpoint, dict(params), {"timeout_s": timeout_s, "retries": retries, "trace_sink": trace_sink}))
                raise TypeError("trace_sink error after request was sent")

            with self.subTest(method=expected_method):
                with mock.patch("sw_core.cli.rpc_call", side_effect=fake_rpc_call):
                    with self.assertRaises(TypeError):
                        self._invoke_main(argv)

                self.assertEqual(len(calls), 1, "主請求送出後不得因 trace_sink 相關 TypeError 再呼叫第二次 RPC")
                self.assertEqual(calls[0][0], cli.SOCKET_PATH)
                self.assertEqual(calls[0][2]["retries"], 0)
                self.assertIsNotNone(calls[0][2]["trace_sink"])


class TestCliTraceLoggerIsolation(CliDiagnosticsMixin, unittest.TestCase):
    def test_trace_logger_does_not_propagate_to_root_or_serialwrap_logger(self) -> None:
        root_stream = io.StringIO()
        serialwrap_stream = io.StringIO()
        root_handler = logging.StreamHandler(root_stream)
        serialwrap_handler = logging.StreamHandler(serialwrap_stream)
        root_logger = logging.getLogger()
        serialwrap_logger = logging.getLogger("serialwrap")
        old_root_level = root_logger.level
        old_serialwrap_level = serialwrap_logger.level
        root_logger.addHandler(root_handler)
        serialwrap_logger.addHandler(serialwrap_handler)
        root_logger.setLevel(logging.INFO)
        serialwrap_logger.setLevel(logging.INFO)
        try:
            with mock.patch(
                "sw_core.cli.rpc_call",
                return_value={"ok": True, "sessions": []},
            ):
                rc, _out, err = self._invoke_main(["-v", "session", "list"])
        finally:
            root_logger.removeHandler(root_handler)
            serialwrap_logger.removeHandler(serialwrap_handler)
            root_logger.setLevel(old_root_level)
            serialwrap_logger.setLevel(old_serialwrap_level)

        self.assertEqual(rc, 0)
        self.assertEqual(len(self._stderr_json_lines(err)), 1)
        self.assertEqual(root_stream.getvalue(), "")
        self.assertEqual(serialwrap_stream.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
