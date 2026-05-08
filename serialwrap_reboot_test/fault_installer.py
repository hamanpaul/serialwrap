"""Fault installer module for serialwrap-fault-install."""

import sys
import argparse
import base64
import re
import shlex
from typing import List, Tuple

from .constants import SERIALWRAP_CMD


def build_fault_injector_script() -> str:
    """Build the target fault injector script content.

    Returns a shell script that runs at boot with 10% probability,
    choosing equally among four fault types: thermal notification,
    5G ethernet AN rerun, process coredump, and system crash coredump.
    """
    return '''#!/bin/sh
# Serialwrap fault injector - runs once at boot
# Silent execution - redirects to /dev/null except for intentional fault output

# POSIX-compatible random number helper using /dev/urandom
# Reads 2 bytes as unsigned 16-bit integer (0-65535)
# Returns 0 if /dev/urandom or od fails
get_random() {
    value=$(od -An -N2 -tu2 /dev/urandom 2>/dev/null | tr -d ' ')
    case "$value" in
        ''|*[!0-9]*) echo 0 ;;
        *) echo "$value" ;;
    esac
}

# 10% probability gate: trigger fault if random % 10 == 0
GATE_RAND=$(get_random)
if [ $(( $GATE_RAND % 10 )) -ne 0 ]; then
    exit 0
fi

# Choose fault type: 0-3 for equal 25% probability among four faults
FAULT_TYPE_RAND=$(get_random)
FAULT_TYPE=$(( $FAULT_TYPE_RAND % 4 ))

case $FAULT_TYPE in
    0)
        # Thermal notification - write to /dev/console
        echo "bcm_thermal_drv brcm-therm: Trip 0: threshold=105000 mC hysteresis=2000 mC" > /dev/console
        ;;
    1)
        # 5G ethernet AN rerun - PHY reset
        ethctl eth0 phy-reset > /dev/null 2>&1
        ;;
    2)
        # Process coredump - collect all PIDs > 4000 and randomly select one
        ALL_PIDS=$(ps aux | awk '$2 > 4000 {print $2}')
        if [ -z "$ALL_PIDS" ]; then
            # No eligible process, exit silently
            exit 0
        fi
        # Count PIDs and randomly select one
        PID_COUNT=$(echo "$ALL_PIDS" | wc -l)
        PID_RAND=$(get_random)
        RANDOM_LINE=$(( ($PID_RAND % $PID_COUNT) + 1 ))
        TARGET_PID=$(echo "$ALL_PIDS" | sed -n "${RANDOM_LINE}p")
        kill -SIGABRT "$TARGET_PID" > /dev/null 2>&1
        ;;
    3)
        # System crash coredump - kernel panic via sysrq
        echo c > /proc/sysrq-trigger
        ;;
esac

exit 0
'''


def build_init_script() -> str:
    """Build the init script content for /etc/init.d.

    Returns a shell script that calls the fault injector on start.
    """
    return '''#!/bin/sh
# Init script for serialwrap-fault-injector
# Run at boot via S50 symlink

case "$1" in
    start)
        /usr/sbin/serialwrap-fault-injector > /dev/null 2>&1
        ;;
    stop|restart|reload)
        # No-op - injector runs once at boot
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|reload}"
        exit 1
        ;;
esac

exit 0
'''


def short_write_commands(path: str, content: str, mode: str) -> List[str]:
    """Generate fallback commands for writing files without heredoc.

    Uses base64 encoding and chunked writes to avoid large UART transfers.

    Args:
        path: Target file path
        content: File content to write
        mode: File permissions (e.g., "0755")

    Returns:
        List of shell commands to execute via serialwrap
    """
    # Validate mode parameter (must be 3-4 octal digits)
    if not re.match(r'^0?[0-7]{3}$', mode):
        raise ValueError(f"Invalid mode '{mode}': must be 3-4 octal digits (e.g., '0755', '644')")

    commands = []

    # Quote paths to prevent command injection
    quoted_path = shlex.quote(path)

    # Remove any existing file
    commands.append(f"rm -f {quoted_path}")

    # Encode content as base64 to handle special characters safely
    encoded = base64.b64encode(content.encode()).decode()

    # Create temp file for base64 content
    temp_b64 = f"{path}.b64"
    quoted_temp_b64 = shlex.quote(temp_b64)
    commands.append(f"rm -f {quoted_temp_b64}")

    # Write base64 content in chunks (max 200 chars per command for UART safety)
    chunk_size = 200
    for i in range(0, len(encoded), chunk_size):
        chunk = encoded[i:i+chunk_size]
        commands.append(f"echo '{chunk}' >> {quoted_temp_b64}")

    # Decode base64 to final file
    commands.append(f"base64 -d {quoted_temp_b64} > {quoted_path}")
    commands.append(f"rm -f {quoted_temp_b64}")

    # Set permissions
    commands.append(f"chmod {mode} {quoted_path}")

    return commands


def parse_args(args: List[str]):
    """Parse command-line arguments for fault installer.

    Args:
        args: Command-line arguments (typically sys.argv[1:])

    Returns:
        Parsed arguments namespace
    """
    parser = argparse.ArgumentParser(
        description='Install serialwrap fault injector to target device'
    )
    parser.add_argument(
        '--selector',
        required=True,
        help='COM selector (e.g., COM0, COM1)',
        type=str
    )

    parsed = parser.parse_args(args)

    # Validate selector format: COMx where x is a digit
    if not re.match(r'^COM\d+$', parsed.selector):
        parser.error(f"Invalid selector format: {parsed.selector}. Expected COM<digit>")

    return parsed


class CommandRunner:
    """Interface for running commands on target via serialwrap."""

    def __init__(self, selector: str):
        """Initialize runner with COM selector.

        Args:
            selector: COM port selector (e.g., COM1)
        """
        self.selector = selector
        self.serialwrap_cmd = SERIALWRAP_CMD

    def run_host(self, args: List[str]) -> Tuple[int, str, str]:
        """Run a host-side serialwrap command (e.g., file operations).

        Args:
            args: Full command arguments including serialwrap subcommand

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        import subprocess
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=30
            )
            return (result.returncode, result.stdout, result.stderr)
        except subprocess.TimeoutExpired as e:
            return (1, "", f"Command timed out: {e}")
        except FileNotFoundError as e:
            return (1, "", f"Serialwrap command not found: {e}")
        except OSError as e:
            return (1, "", f"OS error running command: {e}")

    def run_target(self, cmd: str) -> Tuple[int, str, str]:
        """Run a command on target via serialwrap cmd submit.

        Args:
            cmd: Shell command to execute on target

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        import subprocess
        try:
            result = subprocess.run(
                [self.serialwrap_cmd, 'cmd', 'submit',
                 '--selector', self.selector,
                 '--mode', 'line',
                 '--cmd', cmd],
                capture_output=True,
                text=True,
                timeout=30
            )
            return (result.returncode, result.stdout, result.stderr)
        except subprocess.TimeoutExpired as e:
            return (1, "", f"Command timed out: {e}")
        except FileNotFoundError as e:
            return (1, "", f"Serialwrap command not found: {e}")
        except OSError as e:
            return (1, "", f"OS error running command: {e}")

    def run(self, cmd: str) -> Tuple[int, str, str]:
        """Backward compatibility: run command on target.

        Args:
            cmd: Shell command to execute on target

        Returns:
            Tuple of (exit_code, stdout, stderr)
        """
        return self.run_target(cmd)


class FaultInstaller:
    """Fault installer for target device."""

    INJECTOR_PATH = "/usr/sbin/serialwrap-fault-injector"
    INIT_PATH = "/etc/init.d/serialwrap-fault-injector"
    SYMLINK_PATH = "/etc/rc.d/S50serialwrap-fault-injector"

    def __init__(self, selector: str, runner: CommandRunner = None):
        """Initialize installer.

        Args:
            selector: COM selector
            runner: Command runner (defaults to CommandRunner(selector))
        """
        self.selector = selector
        self.runner = runner or CommandRunner(selector)

    def check_target_directories(self) -> bool:
        """Check that required target directories exist.

        Returns:
            True if directories exist, False otherwise
        """
        # Check /etc/init.d
        rc, _, _ = self.runner.run("test -d /etc/init.d")
        if rc != 0:
            print("ERROR: /etc/init.d does not exist on target", file=sys.stderr)
            return False

        # Check /etc/rc.d
        rc, _, _ = self.runner.run("test -d /etc/rc.d")
        if rc != 0:
            print("ERROR: /etc/rc.d does not exist on target", file=sys.stderr)
            return False

        return True

    def transfer_file(self, local_content: str, remote_path: str, mode: str) -> bool:
        """Transfer file to target, trying file transfer first, then fallback.

        Args:
            local_content: Content to write
            remote_path: Target file path
            mode: File permissions

        Returns:
            True if successful, False otherwise
        """
        import tempfile
        import os

        # Try preferred file transfer first via serialwrap file send
        temp_file = None
        try:
            # Create temporary file with content
            fd, temp_file = tempfile.mkstemp(prefix='fault_installer_', suffix='.tmp')
            try:
                os.write(fd, local_content.encode())
            finally:
                os.close(fd)

            # Try serialwrap file send using host-side runner
            serialwrap_cmd = getattr(self.runner, 'serialwrap_cmd', SERIALWRAP_CMD)
            rc, _, _ = self.runner.run_host([
                serialwrap_cmd, 'file', 'send',
                '--selector', self.selector,
                temp_file, remote_path
            ])

            if rc == 0:
                # File transfer succeeded, set permissions using target runner
                rc_chmod, _, _ = self.runner.run_target(f"chmod {mode} {shlex.quote(remote_path)}")
                return rc_chmod == 0

            # File transfer failed, try fallback

        finally:
            # Clean up temp file
            if temp_file and os.path.exists(temp_file):
                try:
                    os.unlink(temp_file)
                except OSError:
                    pass

        # Use fallback short-write commands (all run on target)
        commands = short_write_commands(remote_path, local_content, mode)
        for cmd in commands:
            rc, _, _ = self.runner.run_target(cmd)
            if rc != 0:
                return False
        return True

    def install(self) -> bool:
        """Install fault injector to target.

        Returns:
            True if successful, False otherwise
        """
        # Check directories
        if not self.check_target_directories():
            return False

        # Transfer fault injector script
        injector_content = build_fault_injector_script()
        if not self.transfer_file(injector_content, self.INJECTOR_PATH, "0755"):
            print(f"ERROR: Failed to transfer {self.INJECTOR_PATH}", file=sys.stderr)
            return False

        # Transfer init script
        init_content = build_init_script()
        if not self.transfer_file(init_content, self.INIT_PATH, "0755"):
            print(f"ERROR: Failed to transfer {self.INIT_PATH}", file=sys.stderr)
            return False

        # Create symlink
        rc, _, _ = self.runner.run(f"ln -sf {shlex.quote(self.INIT_PATH)} {shlex.quote(self.SYMLINK_PATH)}")
        if rc != 0:
            print(f"ERROR: Failed to create symlink {self.SYMLINK_PATH}", file=sys.stderr)
            return False

        # Verify installation
        if not self.verify():
            return False

        return True

    def verify(self) -> bool:
        """Verify installation.

        Returns:
            True if verification passes, False otherwise
        """
        # Check injector exists and is executable
        rc, _, _ = self.runner.run(f"test -x {shlex.quote(self.INJECTOR_PATH)}")
        if rc != 0:
            print(f"ERROR: {self.INJECTOR_PATH} not executable", file=sys.stderr)
            return False

        # Check init script exists and is executable
        rc, _, _ = self.runner.run(f"test -x {shlex.quote(self.INIT_PATH)}")
        if rc != 0:
            print(f"ERROR: {self.INIT_PATH} not executable", file=sys.stderr)
            return False

        # Check symlink exists
        rc, _, _ = self.runner.run(f"test -L {shlex.quote(self.SYMLINK_PATH)}")
        if rc != 0:
            print(f"ERROR: {self.SYMLINK_PATH} symlink missing", file=sys.stderr)
            return False

        # Check symlink target
        rc, stdout, _ = self.runner.run(f"readlink {shlex.quote(self.SYMLINK_PATH)}")
        if rc != 0:
            print(f"ERROR: Cannot read {self.SYMLINK_PATH} symlink target", file=sys.stderr)
            return False

        target = stdout.strip()
        if target != self.INIT_PATH:
            print(f"ERROR: {self.SYMLINK_PATH} points to {target}, expected {self.INIT_PATH}", file=sys.stderr)
            return False

        return True


def main():
    """Main entry point for the fault installer."""
    try:
        args = parse_args(sys.argv[1:])
    except SystemExit as e:
        return e.code if e.code is not None else 1

    installer = FaultInstaller(args.selector)

    print(f"Installing fault injector to {args.selector}...", file=sys.stderr)

    if installer.install():
        print(f"Installation successful on {args.selector}", file=sys.stderr)
        return 0
    else:
        print(f"Installation failed on {args.selector}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
