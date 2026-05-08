"""Controller module for serialwrap-reboot-controller."""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List

from .constants import SERIALWRAP_CMD


class ControllerError(Exception):
    """Controller operation error."""
    def __init__(
        self,
        message: str,
        *,
        error_code: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None
    ):
        super().__init__(message)
        self.error_code = error_code
        self.payload = payload or {}


class CommandRunner:
    """Run commands and return results."""

    def run(self, cmd: List[str], **kwargs) -> tuple[int, str, str]:
        """Run a command and return (returncode, stdout, stderr)."""
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            **kwargs
        )
        return result.returncode, result.stdout, result.stderr


def positive_int(value: str) -> int:
    """Parse a positive integer argument."""
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer")
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be greater than zero")
    return parsed


class RebootController:
    """Main controller for reboot testing."""

    def __init__(
        self,
        selector: str,
        runner: Optional[Any] = None,
        log_dir: Optional[Path] = None,
        hours_limit: Optional[int] = None,
        count_limit: Optional[int] = None,
        active_log: Optional[Path] = None
    ):
        """Initialize the controller.

        Args:
            selector: COM selector (e.g., COM0, COM1).
            runner: Command runner (defaults to real CommandRunner).
            log_dir: Directory for minicom logs (defaults to ~/b-log).
            hours_limit: Optional hours limit for stop condition.
            count_limit: Optional count limit for stop condition.
            active_log: Optional active minicom log path for testing.

        Raises:
            ValueError: If selector is invalid or contains path traversal.
        """
        # Validate selector format (must match ^COM[0-9]+$)
        if not re.match(r'^COM[0-9]+$', selector):
            raise ValueError(f"Invalid selector: {selector}. Must match pattern COM[0-9]+")

        self.selector = selector
        self.runner = runner or CommandRunner()
        # Ensure log_dir is a Path object
        if log_dir is None:
            self.log_dir = Path.home() / "b-log"
        elif isinstance(log_dir, str):
            self.log_dir = Path(log_dir)
        else:
            self.log_dir = log_dir
        self.hours_limit = hours_limit
        self.count_limit = count_limit
        self.active_log_path = active_log

        self.state_dir: Optional[Path] = None
        self.start_time = time.time()
        self.reboot_count = 0
        self._stop_requested = False
        self.last_action_time: Optional[float] = None
        self.sleep_fn = time.sleep  # Injectable for testing
        self.loop_delay = 10  # Configurable loop delay in seconds
        self.marker_wait_seconds = 45
        self.marker_poll_interval = 1
        self.rpc_timeout_seconds = 30
        self.recover_timeout_seconds = 15
        self.command_timeout_seconds = 15

    def serialwrap_cmd(self, *args: str) -> List[str]:
        """Build a serialwrap command with an extended RPC timeout."""
        return [SERIALWRAP_CMD, "--timeout", str(self.rpc_timeout_seconds), *args]

    def format_command_error(self, stdout: str, stderr: str) -> str:
        """Format CLI failures, preferring JSON stdout error payloads."""
        stderr_text = stderr.strip()
        if stderr_text:
            return stderr_text

        stdout_text = stdout.strip()
        if not stdout_text:
            return ""

        try:
            data = json.loads(stdout_text)
        except json.JSONDecodeError:
            return stdout_text

        if not isinstance(data, dict):
            return stdout_text

        parts: List[str] = []
        if data.get("error_code"):
            parts.append(str(data["error_code"]))
        if data.get("message"):
            parts.append(str(data["message"]))
        if data.get("classification"):
            parts.append(f"classification={data['classification']}")
        if data.get("partial") is True:
            parts.append("partial=true")
        session = data.get("session")
        if isinstance(session, dict) and session.get("state"):
            parts.append(f"state={session['state']}")

        return "; ".join(parts) if parts else stdout_text

    def parse_json_object(self, text: str) -> Optional[Dict[str, Any]]:
        """Parse a JSON object response when available."""
        text = text.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def check_serialwrap_event_support(self) -> bool:
        """Check if serialwrap supports event subcommand."""
        returncode, stdout, stderr = self.runner.run(
            self.serialwrap_cmd("event", "--help")
        )
        return returncode == 0

    def check_daemon_status(self) -> bool:
        """Check if serialwrap daemon is running."""
        returncode, stdout, stderr = self.runner.run(
            self.serialwrap_cmd("daemon", "status")
        )
        return returncode == 0

    def send_marker_command(self) -> str:
        """Send marker command through serialwrap and return marker string.

        Raises:
            ControllerError: If marker submission fails.
        """
        # Generate unique marker
        run_id = str(uuid.uuid4())[:8]
        marker = f"__SW_REBOOT_TEST_{self.selector}_{run_id}__"

        # Submit echo command with marker
        returncode, stdout, stderr = self.runner.run(self.serialwrap_cmd(
            "cmd", "submit",
            "--selector", self.selector,
            "--source", "agent:reboot-controller",
            "--mode", "line",
            "--cmd", f"echo {marker}"
        ))

        payload = None
        stdout_text = stdout.strip()
        if stdout_text:
            try:
                data = json.loads(stdout_text)
                if isinstance(data, dict):
                    payload = data
            except json.JSONDecodeError:
                payload = None

        if returncode != 0:
            details = self.format_command_error(stdout, stderr)
            raise ControllerError(
                f"Failed to submit marker command: {details}",
                error_code=payload.get("error_code") if payload else None,
                payload=payload
            )

        return marker

    def find_active_minicom_log(
        self,
        marker: str,
        max_age_seconds: int = 600
    ) -> Optional[Path]:
        """Find active minicom log containing marker.

        Args:
            marker: Marker string to search for.
            max_age_seconds: Maximum age for log file in seconds.

        Returns:
            Path to active log or None if not found.
        """
        pattern = f"mini_{self.selector}_*.log"
        deadline = time.time() + max(0, self.marker_wait_seconds)

        while True:
            current_time = time.time()

            for log_file in self.log_dir.glob(pattern):
                # Check file age
                try:
                    mtime = log_file.stat().st_mtime
                    if current_time - mtime > max_age_seconds:
                        continue
                except OSError as e:
                    print(f"WARNING: Cannot stat {log_file}: {e}", file=sys.stderr)
                    continue

                # Check for marker using streaming read
                try:
                    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                        for line in f:
                            if marker in line:
                                return log_file
                except OSError as e:
                    print(f"WARNING: Cannot read {log_file}: {e}", file=sys.stderr)
                    continue

            if current_time >= deadline:
                return None

            sleep_seconds = min(self.marker_poll_interval, deadline - current_time)
            if sleep_seconds > 0:
                self.sleep_fn(sleep_seconds)

    def derive_report_path(self, minicom_log: Path) -> Path:
        """Derive report path from minicom log name.

        Args:
            minicom_log: Path to minicom log file.

        Returns:
            Path to report file.
        """
        # Extract timestamp from minicom log name
        # mini_COM1_260506-152744.log -> event-triggered_COM1_260506-152744.md
        name = minicom_log.name
        prefix = f"mini_{self.selector}_"
        if name.startswith(prefix):
            timestamp = name[len(prefix):].replace('.log', '')
            report_name = f"event-triggered_{self.selector}_{timestamp}.md"
            return minicom_log.parent / report_name

        # Fallback
        return minicom_log.parent / f"event-triggered_{self.selector}.md"

    def create_run_state_directory(self) -> Path:
        """Create /tmp run-state directory.

        Returns:
            Path to created state directory.

        Raises:
            ValueError: If resolved path is not under /tmp.
        """
        pid = os.getpid()
        state_dir = Path(f"/tmp/serialwrap-reboot-test.{self.selector}.{pid}")

        # Verify resolved path is under /tmp (protect against symlink attacks)
        resolved = state_dir.resolve()
        if not str(resolved).startswith("/tmp/"):
            raise ValueError(f"State directory must be under /tmp, got: {resolved}")

        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir

    def store_run_state(
        self,
        state_dir: Path,
        minicom_log: Path,
        report_path: Path
    ) -> None:
        """Store run state files.

        Args:
            state_dir: State directory path.
            minicom_log: Active minicom log path.
            report_path: Report file path.
        """
        (state_dir / "active_minicom_log.txt").write_text(str(minicom_log))
        (state_dir / "report_path.txt").write_text(str(report_path))

    def generate_event_rules(self) -> List[Dict[str, Any]]:
        """Generate event rule definitions.

        Returns:
            List of rule dictionaries.
        """
        owner = "agent-reboot-controller"
        rules = [
            {
                "schema_version": 1,
                "owner": owner,
                "name": "brcm-therm",
                "rule_id": f"{owner}.brcm-therm",
                "kind": "tool",
                "selectors": ["COM0", "COM1"],
                "pattern": {"kind": "contains", "value": "brcm-therm"},
                "handler": {"exec": ["serialwrap-event-handler"]},
                "auto_enable_com_on_load": False
            },
            {
                "schema_version": 1,
                "owner": owner,
                "name": "link-down",
                "rule_id": f"{owner}.link-down",
                "kind": "tool",
                "selectors": ["COM0", "COM1"],
                "pattern": {"kind": "contains", "value": "Link is Down"},
                "handler": {"exec": ["serialwrap-event-handler"]},
                "auto_enable_com_on_load": False
            },
            {
                "schema_version": 1,
                "owner": owner,
                "name": "pstate",
                "rule_id": f"{owner}.pstate",
                "kind": "tool",
                "selectors": ["COM0", "COM1"],
                "pattern": {"kind": "contains", "value": "pstate"},
                "handler": {"exec": ["serialwrap-event-handler"]},
                "auto_enable_com_on_load": False
            },
            {
                "schema_version": 1,
                "owner": owner,
                "name": "kernel-panic",
                "rule_id": f"{owner}.kernel-panic",
                "kind": "tool",
                "selectors": ["COM0", "COM1"],
                "pattern": {"kind": "contains", "value": "Kernel panic"},
                "handler": {"exec": ["serialwrap-event-handler"]},
                "auto_enable_com_on_load": False
            },
            {
                "schema_version": 1,
                "owner": owner,
                "name": "smc-bootloader",
                "rule_id": f"{owner}.smc-bootloader",
                "kind": "tool",
                "selectors": ["COM0", "COM1"],
                "pattern": {"kind": "contains", "value": "SMC bootloader"},
                "handler": {"exec": ["serialwrap-event-handler"]},
                "auto_enable_com_on_load": False
            }
        ]
        return rules

    def register_event_rules(self) -> None:
        """Register event rules with serialwrap.

        Raises:
            ControllerError: If rule registration fails.
        """
        rules = self.generate_event_rules()
        for rule in rules:
            temp_path = None
            try:
                fd, temp_path = tempfile.mkstemp(
                    prefix=f"serialwrap-event-rule-{rule['name']}-",
                    suffix=".json"
                )
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(rule, f)
                returncode, stdout, stderr = self.runner.run(self.serialwrap_cmd(
                    "event", "add",
                    "--file", temp_path
                ))
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)

            if returncode != 0:
                details = self.format_command_error(stdout, stderr)
                raise ControllerError(f"Failed to add event rule {rule['name']}: {details}")

    def enable_selector(self) -> None:
        """Enable event matcher for this selector.

        Raises:
            ControllerError: If enable command fails.
        """
        returncode, stdout, stderr = self.runner.run(self.serialwrap_cmd(
            "event", "enable",
            "--selector", self.selector
        ))

        if returncode != 0:
            details = self.format_command_error(stdout, stderr)
            raise ControllerError(f"Failed to enable selector {self.selector}: {details}")

    def disable_selector(self) -> None:
        """Disable event matcher for this selector.

        Raises:
            ControllerError: If disable command fails.
        """
        returncode, stdout, stderr = self.runner.run(self.serialwrap_cmd(
            "event", "disable",
            "--selector", self.selector
        ))

        if returncode != 0:
            details = self.format_command_error(stdout, stderr)
            raise ControllerError(f"Failed to disable selector {self.selector}: {details}")

    def reset_selector(self) -> None:
        """Reset event state for this selector.

        Raises:
            ControllerError: If reset command fails.
        """
        returncode, stdout, stderr = self.runner.run(self.serialwrap_cmd(
            "event", "reset",
            "--selector", self.selector
        ))

        if returncode != 0:
            details = self.format_command_error(stdout, stderr)
            raise ControllerError(f"Failed to reset selector {self.selector}: {details}")

    def check_other_selectors_enabled(self) -> Optional[bool]:
        """Check if other COM selectors are still enabled.

        Returns:
            True if any other selector is enabled, False if none are enabled,
            None if status could not be determined safely.
        """
        returncode, stdout, stderr = self.runner.run(self.serialwrap_cmd("event", "status"))

        if returncode != 0:
            details = self.format_command_error(stdout, stderr)
            print(f"WARNING: Failed to query event status: {details}", file=sys.stderr)
            return None

        try:
            status = json.loads(stdout)
            if not isinstance(status, dict):
                print("WARNING: Unexpected event status format: top-level JSON is not an object", file=sys.stderr)
                return None

            coms = status.get("coms")
            if isinstance(coms, list):
                for selector in coms:
                    if not isinstance(selector, str):
                        print("WARNING: Unexpected event status format: coms contains non-string entries", file=sys.stderr)
                        return None
                    if selector != self.selector:
                        return True
                return False

            selectors = status.get("selectors", {})
            if not isinstance(selectors, dict):
                print("WARNING: Unexpected event status format: selectors is not an object", file=sys.stderr)
                return None

            for selector, info in selectors.items():
                if not isinstance(info, dict):
                    print(f"WARNING: Unexpected event status format for {selector}", file=sys.stderr)
                    return None
                if selector != self.selector and info.get("enabled"):
                    return True

            return False
        except json.JSONDecodeError as e:
            print(f"WARNING: Failed to parse event status JSON: {e}", file=sys.stderr)
            return None
        except (KeyError, TypeError) as e:
            print(f"WARNING: Unexpected event status format: {e}", file=sys.stderr)
            return None

    def remove_event_rules(self) -> None:
        """Remove shared event rules.

        Raises:
            ControllerError: If rule removal fails.
        """
        rules = self.generate_event_rules()
        for rule in rules:
            returncode, stdout, stderr = self.runner.run(self.serialwrap_cmd(
                "event", "rm",
                rule["rule_id"]
            ))

            if returncode != 0:
                details = self.format_command_error(stdout, stderr)
                raise ControllerError(f"Failed to remove event rule {rule['name']}: {details}")

    def check_ready_state(self) -> bool:
        """Check if session is in READY state."""
        returncode, stdout, stderr = self.runner.run(self.serialwrap_cmd("session", "list"))

        if returncode != 0:
            return False

        try:
            data = json.loads(stdout)
            sessions = data.get("sessions", [])
            for session in sessions:
                session_selector = session.get("selector", session.get("com"))
                if session_selector == self.selector:
                    return session.get("state") == "READY"
            return False
        except json.JSONDecodeError as e:
            print(f"WARNING: Failed to parse session list JSON: {e}", file=sys.stderr)
            return False
        except (KeyError, TypeError) as e:
            print(f"WARNING: Unexpected session list format: {e}", file=sys.stderr)
            return False

    def check_self_test(self) -> bool:
        """Run self-test and check if OK."""
        returncode, stdout, stderr = self.runner.run(self.serialwrap_cmd(
            "session", "self-test",
            "--selector", self.selector,
            "--probe-timeout", "10"
        ))

        if returncode != 0:
            return False

        try:
            data = json.loads(stdout)
            return (
                data.get("classification") == "OK" and
                data.get("probe_ok") is True
            )
        except json.JSONDecodeError as e:
            print(f"WARNING: Failed to parse self-test JSON: {e}", file=sys.stderr)
            return False
        except (KeyError, TypeError) as e:
            print(f"WARNING: Unexpected self-test format: {e}", file=sys.stderr)
            return False

    def submit_normal_reboot(self) -> float:
        """Submit normal reboot command.

        Returns:
            Timestamp of reboot submission.

        Raises:
            ControllerError: If reboot submission fails.
        """
        returncode, stdout, stderr = self.runner.run(self.serialwrap_cmd(
            "cmd", "submit",
            "--selector", self.selector,
            "--source", "agent:reboot-controller",
            "--mode", "line",
            "--cmd", "reboot",
            "--cmd-timeout", str(self.command_timeout_seconds),
        ))

        if returncode != 0:
            details = self.format_command_error(stdout, stderr)
            raise ControllerError(f"Failed to submit reboot command: {details}")

        self.reboot_count += 1
        return time.time()

    def should_throttle_recovery(
        self,
        last_action: Optional[float],
        throttle_seconds: int = 300
    ) -> bool:
        """Check if recovery should be throttled.

        Args:
            last_action: Timestamp of last reboot or fallback action.
            throttle_seconds: Throttle period in seconds.

        Returns:
            True if should wait, False if can proceed.
        """
        if last_action is None:
            return False

        elapsed = time.time() - last_action
        return elapsed < throttle_seconds

    def run_session_recover(self) -> None:
        """Run session recover command.

        Raises:
            ControllerError: If recover command fails.
        """
        returncode, stdout, stderr = self.runner.run(self.serialwrap_cmd(
            "session", "recover",
            "--selector", self.selector,
            "--timeout", str(self.recover_timeout_seconds),
        ))

        if returncode != 0:
            details = self.format_command_error(stdout, stderr)
            raise ControllerError(f"Failed to run session recover for {self.selector}: {details}")

    def check_log_tail_for_prompt(
        self,
        log_file: Path,
        prompt: str,
        tail_lines: int = 50
    ) -> bool:
        """Check log tail for specific prompt.

        Args:
            log_file: Path to log file.
            prompt: Prompt string to search for.
            tail_lines: Number of lines to check from end.

        Returns:
            True if prompt found in tail.
        """
        try:
            from collections import deque

            # Use bounded deque to keep only last N lines in memory
            tail = deque(maxlen=tail_lines)

            with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    tail.append(line)

            tail_content = ''.join(tail)
            return prompt in tail_content
        except OSError as e:
            print(f"WARNING: Cannot read {log_file}: {e}", file=sys.stderr)
            return False

    def find_prompted_minicom_log(self, max_age_seconds: int = 600) -> Optional[Path]:
        """Find a recent minicom log whose tail already shows a usable prompt."""
        log_file = self.find_latest_selector_log(max_age_seconds=max_age_seconds)
        if not log_file:
            return None

        prompt_checks = ("=>", "root@prplOS:/#")
        if any(self.check_log_tail_for_prompt(log_file, prompt) for prompt in prompt_checks):
            return log_file

        return None

    def find_latest_selector_log(self, max_age_seconds: Optional[int] = 600) -> Optional[Path]:
        """Find the newest minicom log for this selector, optionally bounded by age."""
        pattern = f"mini_{self.selector}_*.log"
        current_time = time.time()
        latest_candidate: Optional[tuple[float, Path]] = None

        for log_file in self.log_dir.glob(pattern):
            try:
                mtime = log_file.stat().st_mtime
            except OSError as e:
                print(f"WARNING: Cannot stat {log_file}: {e}", file=sys.stderr)
                continue
            if max_age_seconds is not None and current_time - mtime > max_age_seconds:
                continue
            if latest_candidate is None or mtime > latest_candidate[0]:
                latest_candidate = (mtime, log_file)

        if latest_candidate is None:
            return None

        return latest_candidate[1]

    def iter_previous_run_state_dirs(self):
        """Iterate previously created run-state directories for this selector."""
        return Path("/tmp").glob(f"serialwrap-reboot-test.{self.selector}.*")

    def find_trusted_previous_active_log(self, max_age_seconds: int = 600) -> Optional[Path]:
        """Reuse a prior active log only when it matches the newest current selector log."""
        latest_log = self.find_latest_selector_log(max_age_seconds=None)
        if not latest_log or not latest_log.exists():
            return None

        log_dir_resolved = self.log_dir.resolve()
        latest_resolved = latest_log.resolve()
        try:
            latest_resolved.relative_to(log_dir_resolved)
        except ValueError:
            return None

        for state_dir in self.iter_previous_run_state_dirs():
            state_file = state_dir / "active_minicom_log.txt"
            try:
                stored_path_text = state_file.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not stored_path_text:
                continue

            stored_path = Path(stored_path_text)
            if not stored_path.exists():
                continue

            stored_resolved = stored_path.resolve()
            try:
                stored_resolved.relative_to(log_dir_resolved)
            except ValueError:
                continue

            if stored_resolved == latest_resolved:
                return latest_log

        return None

    def send_raw_broker_command(self, command: str) -> float:
        """Send raw console command through interactive session APIs.

        Args:
            command: Command string to send.

        Returns:
            Timestamp of command submission.

        Raises:
            ControllerError: If broker command fails.
        """
        returncode, stdout, stderr = self.runner.run(self.serialwrap_cmd(
            "session", "interactive-open",
            "--selector", self.selector,
            "--owner", "agent:reboot-controller",
            "--timeout", "30"
        ))
        data = self.parse_json_object(stdout)
        if returncode != 0:
            details = self.format_command_error(stdout, stderr)
            error = ControllerError(
                f"Failed to open interactive session for '{command}' on {self.selector}: {details}",
                error_code=data.get("error_code") if data else None,
                payload=data,
            )
            if error.error_code != "SESSION_NOT_READY":
                raise error

            attach_rc, attach_stdout, attach_stderr = self.runner.run(self.serialwrap_cmd(
                "session", "console-attach",
                "--selector", self.selector,
                "--label", "human:agent-reboot-controller",
            ))
            attach_data = self.parse_json_object(attach_stdout)
            if attach_rc != 0:
                details = self.format_command_error(attach_stdout, attach_stderr)
                raise ControllerError(
                    f"Failed to attach console for '{command}' on {self.selector}: {details}",
                    error_code=attach_data.get("error_code") if attach_data else None,
                    payload=attach_data,
                )

            if not attach_data:
                raise ControllerError(
                    f"Failed to parse console-attach response for {self.selector}: expected JSON object"
                )

            client_id = attach_data.get("client_id")
            fd = None
            result_time = None
            pending_error = None
            detach_error = None
            try:
                vtty = attach_data.get("vtty")
                interactive_owner = attach_data.get("interactive_owner") is True
                if not interactive_owner:
                    raise ControllerError(
                        f"Console attach for {self.selector} did not grant interactive ownership for '{command}'",
                        payload=attach_data,
                    )
                if not client_id or not vtty:
                    raise ControllerError(
                        f"Failed to parse console-attach response for {self.selector}: missing client_id or vtty",
                        payload=attach_data,
                    )

                payload = f"{command}\n".encode("utf-8")
                fd = os.open(vtty, os.O_WRONLY)
                written = os.write(fd, payload)
                if written != len(payload):
                    raise ControllerError(
                        f"Failed to write raw broker command '{command}' to {self.selector}: short write"
                    )
                result_time = time.time()
            except OSError as e:
                pending_error = ControllerError(
                    f"Failed to write raw broker command '{command}' to {self.selector}: {e}"
                )
            except ControllerError as e:
                pending_error = e
            finally:
                if fd is not None:
                    os.close(fd)

                if client_id:
                    detach_rc, detach_stdout, detach_stderr = self.runner.run(self.serialwrap_cmd(
                        "session", "console-detach",
                        "--selector", self.selector,
                        "--client-id", str(client_id),
                    ))
                    detach_data = self.parse_json_object(detach_stdout)
                    if detach_rc != 0:
                        if detach_data and detach_data.get("error_code") == "CONSOLE_NOT_FOUND":
                            detach_error = None
                        else:
                            details = self.format_command_error(detach_stdout, detach_stderr)
                            detach_error = ControllerError(
                                f"Failed to detach console for {self.selector}: {details}",
                                error_code=detach_data.get("error_code") if detach_data else None,
                                payload=detach_data,
                            )

            if pending_error is not None:
                raise pending_error
            if detach_error is not None:
                raise detach_error

            return result_time

        try:
            data = json.loads(stdout)
            interactive_id = data["interactive_id"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise ControllerError(f"Failed to parse interactive-open response for {self.selector}: {e}")

        pending_error = None
        close_error = None
        result_time = None
        try:
            returncode, stdout, stderr = self.runner.run(self.serialwrap_cmd(
                "session", "interactive-send",
                "--interactive-id", interactive_id,
                "--data", f"{command}\n",
                "--encoding", "plain"
            ))
            if returncode != 0:
                details = self.format_command_error(stdout, stderr)
                pending_error = ControllerError(
                    f"Failed to send raw broker command '{command}' to {self.selector}: {details}"
                )
            else:
                result_time = time.time()
        finally:
            close_rc, close_stdout, close_stderr = self.runner.run(self.serialwrap_cmd(
                "session", "interactive-close",
                "--interactive-id", interactive_id
            ))
            if close_rc != 0:
                details = self.format_command_error(close_stdout, close_stderr)
                close_error = ControllerError(f"Failed to close interactive session for {self.selector}: {details}")

        if pending_error is not None:
            raise pending_error
        if close_error is not None:
            raise close_error

        return result_time

    def decide_reboot_action(
        self,
        last_action: Optional[float]
    ) -> Dict[str, Any]:
        """Decide next reboot action.

        Args:
            last_action: Timestamp of last reboot or fallback action.

        Returns:
            Dictionary with action type and details.
        """
        # Check if READY and self-test OK
        if self.check_ready_state() and self.check_self_test():
            return {"type": "normal_reboot"}

        # Not READY - check throttle
        if self.should_throttle_recovery(last_action):
            return {"type": "wait"}

        # Run recover
        try:
            self.run_session_recover()
        except ControllerError as e:
            print(f"ERROR: {e}", file=sys.stderr)

        # Check if now ready
        if self.check_ready_state():
            return {"type": "wait"}  # Will reboot on next cycle

        # Check log tail for prompts
        if self.active_log_path:
            if self.check_log_tail_for_prompt(self.active_log_path, "=>"):
                return {"type": "raw_reset"}
            elif self.check_log_tail_for_prompt(self.active_log_path, "root@prplOS:/#"):
                return {"type": "raw_reboot"}

        return {"type": "wait"}

    def startup(self) -> bool:
        """Run startup sequence.

        Returns:
            True if startup succeeded, False otherwise.
        """
        # 1. Check serialwrap event support
        if not self.check_serialwrap_event_support():
            print("ERROR: serialwrap event subcommand not available", file=sys.stderr)
            return False

        # 2. Check daemon status
        if not self.check_daemon_status():
            print("ERROR: serialwrap daemon not running", file=sys.stderr)
            return False

        active_log: Optional[Path] = None

        # 3. Send marker command
        try:
            marker = self.send_marker_command()
        except ControllerError as e:
            if getattr(e, "error_code", None) != "SESSION_NOT_READY":
                print(f"ERROR: {e}", file=sys.stderr)
                return False
            active_log = self.find_prompted_minicom_log()
            if not active_log:
                active_log = self.find_trusted_previous_active_log()
            if not active_log:
                print(f"ERROR: {e}", file=sys.stderr)
                return False
        else:
            # 4. Find active minicom log
            active_log = self.find_active_minicom_log(marker)
            if not active_log:
                active_log = self.find_prompted_minicom_log()
            if not active_log:
                active_log = self.find_trusted_previous_active_log()

        if not active_log:
            print(f"ERROR: No active minicom log found for {self.selector}", file=sys.stderr)
            return False

        # Set active_log_path for use in reboot loop
        self.active_log_path = active_log

        # 5. Derive report path
        report_path = self.derive_report_path(active_log)

        # 6. Create run-state directory
        self.state_dir = self.create_run_state_directory()

        # Wrap everything after state creation to ensure cleanup on failure
        try:
            # 7. Store run state
            self.store_run_state(self.state_dir, active_log, report_path)

            # 8. Register event rules
            self.register_event_rules()

            # 9. Enable this selector
            self.enable_selector()

            # 10. Register signal handlers
            self.register_signal_handlers()

            return True
        except ControllerError as e:
            # Cleanup on any failure after state dir creation
            print(f"ERROR: {e}", file=sys.stderr)
            self._cleanup_on_startup_failure()
            return False

    def run_loop(self) -> None:
        """Run one iteration of the reboot loop."""
        # Decide next action
        action = self.decide_reboot_action(self.last_action_time)

        # Execute action
        if action['type'] == 'normal_reboot':
            try:
                self.last_action_time = self.submit_normal_reboot()
                self.sleep_fn(self.loop_delay)
            except ControllerError as e:
                print(f"ERROR: {e}", file=sys.stderr)
                self.sleep_fn(self.loop_delay)
        elif action['type'] == 'raw_reset':
            try:
                self.last_action_time = self.send_raw_broker_command("reset")
            except ControllerError as e:
                print(f"ERROR: {e}", file=sys.stderr)
            self.sleep_fn(self.loop_delay)
        elif action['type'] == 'raw_reboot':
            try:
                self.last_action_time = self.send_raw_broker_command("reboot -f")
            except ControllerError as e:
                print(f"ERROR: {e}", file=sys.stderr)
            self.sleep_fn(self.loop_delay)
        elif action['type'] == 'wait':
            self.sleep_fn(self.loop_delay)

    def _cleanup_on_startup_failure(self) -> None:
        """Best-effort cleanup when startup fails after state creation.

        Attempts to clean up state directory and event rules. Errors are logged
        as warnings but cleanup continues through all steps.
        """
        # Try to disable and reset selector (may not be enabled yet)
        try:
            self.disable_selector()
        except ControllerError as e:
            print(f"WARNING: Failed to disable selector during startup cleanup: {e}", file=sys.stderr)

        try:
            self.reset_selector()
        except ControllerError as e:
            print(f"WARNING: Failed to reset selector during startup cleanup: {e}", file=sys.stderr)

        # Try to remove event rules if no other selectors are enabled
        try:
            other_selectors_enabled = self.check_other_selectors_enabled()
            if other_selectors_enabled is False:
                self.remove_event_rules()
        except ControllerError as e:
            print(f"WARNING: Failed to remove event rules during startup cleanup: {e}", file=sys.stderr)

        # Always try to remove state directory
        if self.state_dir and self.state_dir.exists():
            try:
                import shutil
                shutil.rmtree(self.state_dir)
            except OSError as e:
                print(f"WARNING: Failed to remove state directory during startup cleanup: {e}", file=sys.stderr)

    def cleanup(self) -> None:
        """Cleanup on exit.

        Attempts all cleanup steps. Errors are logged but don't prevent
        state directory removal.
        """
        # Disable and reset this selector
        try:
            self.disable_selector()
        except ControllerError as e:
            print(f"WARNING: Failed to disable selector during cleanup: {e}", file=sys.stderr)

        try:
            self.reset_selector()
        except ControllerError as e:
            print(f"WARNING: Failed to reset selector during cleanup: {e}", file=sys.stderr)

        # Check if other selectors are still enabled
        try:
            other_selectors_enabled = self.check_other_selectors_enabled()
            if other_selectors_enabled is False:
                # Remove shared rules
                self.remove_event_rules()
        except ControllerError as e:
            print(f"WARNING: Failed to remove event rules during cleanup: {e}", file=sys.stderr)

        # Remove state directory (always attempt this)
        if self.state_dir and self.state_dir.exists():
            import shutil
            try:
                shutil.rmtree(self.state_dir)
            except OSError as e:
                print(f"WARNING: Failed to remove state directory: {e}", file=sys.stderr)

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """Handle signals."""
        self._stop_requested = True
        self.cleanup()
        sys.exit(0)

    def register_signal_handlers(self) -> None:
        """Register signal handlers for SIGINT and SIGTERM."""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def exit_gracefully(self) -> None:
        """Exit gracefully with cleanup."""
        self.cleanup()

    def should_stop(self) -> bool:
        """Check if controller should stop.

        Returns:
            True if stop condition met.
        """
        if self._stop_requested:
            return True

        # Check hours limit
        if self.hours_limit is not None:
            elapsed_hours = (time.time() - self.start_time) / 3600
            if elapsed_hours >= self.hours_limit:
                return True

        # Check count limit
        if self.count_limit is not None:
            if self.reboot_count >= self.count_limit:
                return True

        return False


def parse_args(argv):
    """Parse command-line arguments for the reboot controller.

    Args:
        argv: List of command-line arguments (excluding program name).

    Returns:
        Namespace object with parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description="Control reboot testing for a single COM selector."
    )
    parser.add_argument(
        "--selector",
        required=True,
        help="COM selector (e.g., COM0, COM1)"
    )
    parser.add_argument(
        "--hours",
        type=positive_int,
        help="Stop after N hours"
    )
    parser.add_argument(
        "--count",
        type=positive_int,
        help="Stop after N completed reboot attempts"
    )

    args = parser.parse_args(argv)

    # Validate selector format
    if not re.match(r'^COM[0-9]+$', args.selector):
        parser.error(f"Invalid selector: {args.selector}. Must match pattern COM[0-9]+")

    return args


def main_with_runner(
    argv: List[str],
    runner: Optional[Any] = None,
    log_dir: Optional[Path] = None
) -> int:
    """Main entry point with injectable runner for testing.

    Args:
        argv: Command-line arguments.
        runner: Optional command runner.
        log_dir: Optional log directory.

    Returns:
        Exit code.
    """
    args = parse_args(argv)

    # Create controller
    controller = RebootController(
        selector=args.selector,
        runner=runner,
        log_dir=log_dir,
        hours_limit=args.hours,
        count_limit=args.count
    )

    # Run startup
    if not controller.startup():
        return 1

    # Run loop until stop condition
    try:
        try:
            while not controller.should_stop():
                controller.run_loop()
        except KeyboardInterrupt:
            print("\nInterrupted by user", file=sys.stderr)
    finally:
        controller.cleanup()
    return 0


def main():
    """Main entry point for the reboot controller."""
    return main_with_runner(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
