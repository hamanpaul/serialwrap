"""Slice 2.2 console 黑箱案例的 pytest 設定（07 Task 11）。

以 06 `slice21/harness.py` 的 `start_daemon` 與 P3 `bb_faketarget.FakeTarget` 為基礎；同一批案例可對 Python 基線
與 Rust artifact（`SERIALWRAP_AB_LABEL=rust*`、`SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE`）執行。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SLICE21 = str(Path(__file__).resolve().parents[1] / "slice21")
if _SLICE21 not in sys.path:
    sys.path.insert(0, _SLICE21)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "cap_posix_pty: 需要 POSIX PTY pair")


# 07 Task 11：console_daemon 的 FakeTarget 腳本（`sleep 2` 延遲回覆、`bootcmd` 回 U-Boot banner 後不回 prompt）
CONSOLE_SCRIPTS: dict[bytes, tuple] = {
    b"sleep 2": (2.0, b"sleep 2: ok\r\n"),
    b"bootcmd": (0.5, b"U-Boot 2023.01\r\n", False),
}


class _WindowsDutAdapter:
    """透過 COM20:COM21 bridge.v1 紀錄與 DUT 控制通道觀察 Windows 假 DUT。"""

    def __init__(self, wire_path: Path) -> None:
        self.wire_path = wire_path
        bridge_source = str(Path(__file__).resolve().parents[3] / "tools" / "uart_stdio_bridge" / "src")
        if bridge_source not in sys.path:
            sys.path.insert(0, bridge_source)
        from uart_stdio_bridge.bridge.control import ControlClient

        self.control = ControlClient(wire_path.with_name("control.json"), timeout_s=10.0)

    def _written(self) -> bytes:
        if not self.wire_path.is_file():
            return b""
        out = bytearray()
        for line in self.wire_path.read_bytes().splitlines():
            try:
                row = json.loads(line)
                if row.get("observation") == "dut_stdin_write":
                    out.extend(base64.b64decode(row.get("payload_b64", ""), validate=True))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
        return bytes(out)

    def written(self, timeout_s: float = 1.0) -> bytes:
        deadline = time.monotonic() + max(0.0, timeout_s)
        previous_size = -1
        quiet_since = time.monotonic()
        while time.monotonic() < deadline:
            try:
                size = self.wire_path.stat().st_size
            except OSError:
                size = 0
            if size != previous_size:
                previous_size = size
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= min(0.05, max(0.01, timeout_s)):
                break
            time.sleep(0.01)
        return self._written()

    def emit(self, data: bytes) -> None:
        result = self.control.call(
            "dut_control",
            {"msg": {"action": "emit", "payload_b64": base64.b64encode(data).decode("ascii")}},
        )
        assert result.get("ok") is True, result

    def hold_reply(self, on: bool) -> None:
        action = "stdin_pause" if on else "stdin_resume"
        result = self.control.call("dut_control", {"msg": {"action": action}})
        assert result.get("ok") is True, result

    def stop(self) -> None:
        self.control.close()


def _start_windows(root: Path, *, profile_yaml: str, extra_env: dict[str, str] | None = None):
    """以 com0com DUT pair 啟動隔離 daemon，不建立 POSIX FakeTarget。"""
    pair = os.environ.get("SW_TEST_COM_PAIR")
    wire = os.environ.get("SW_TEST_DUT_WIRE")
    if pair != "COM20:COM21" or not wire:
        pytest.skip("EVIDENCE_MISSING：Windows console A/B 需要 COM20:COM21 與 bridge.v1 wire")

    import harness
    from harness import CRED_PASS_ENV, CRED_USER_ENV, DEVICE_PLACEHOLDER, SHELL_TARGET_YAML

    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    port = harness._free_tcp_port()
    overrides = {
        "SERIALWRAP_PROFILE_DIR": str(root / "profiles"),
        "SERIALWRAP_TCP_PORT": str(port),
        "SERIALWRAP_TEST_DEVICE_ALLOWLIST": "COM20",
        "SERIALWRAP_SERIAL_BACKEND": "pyserial",
        "SERIALWRAP_CONSOLE_BACKEND": "tcp",
        CRED_USER_ENV: "root",
        CRED_PASS_ENV: "pw",
    }
    overrides.update(extra_env or {})
    env = harness.isolated_env(root, **overrides)
    selected_profile = "".join(
        line for line in profile_yaml.splitlines(keepends=True)
        if CRED_USER_ENV not in line and CRED_PASS_ENV not in line
    )
    selected_profile = selected_profile.replace(DEVICE_PLACEHOLDER, "COM20")
    profile_dir = Path(env["SERIALWRAP_PROFILE_DIR"])
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "slice22.yaml").write_text(selected_profile, encoding="utf-8")

    # test_device_allowlist 在 Python 與 Rust daemon 都只放行 COM20；COM21 由 bridge 持有，
    # COM22:COM23 僅由獨立的 windows_com 能力列使用。
    target = _WindowsDutAdapter(Path(wire))
    daemon_cmd = harness.daemon_prefix()
    cli_cmd = harness.cli_prefix()
    endpoint = f"tcp://127.0.0.1:{port}"
    stderr_path = root / "daemon.stderr"
    with stderr_path.open("ab") as stderr_file:
        proc = subprocess.Popen(
            daemon_cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
        )
    d = harness.Slice21Daemon(
        root=root,
        env=env,
        endpoint=endpoint,
        state_dir=Path(env["SERIALWRAP_STATE_DIR"]),
        wal_dir=Path(env["SERIALWRAP_WAL_DIR"]),
        targets={"COM0": target},
        proc=proc,
        cli=" ".join(cli_cmd),
        daemon_cmd=daemon_cmd,
        cli_cmd=cli_cmd,
        owns_targets=False,
    )
    deadline = time.monotonic() + 30.0
    try:
        while True:
            if proc.poll() is not None:
                raise harness.DaemonStartError(proc.returncode or 0, d._stderr_tail(1 << 20))
            try:
                if d.rpc("health.ping", {}, timeout=0.5).get("ok"):
                    break
            except (OSError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Windows daemon 未就緒；stderr={d._stderr_tail()!r}")
            time.sleep(0.1)
        harness.check_artifact_identity(proc.pid, daemon_cmd)
        return d
    except BaseException:
        d.stop()
        target.stop()
        raise


def _capture_windows_daemon_stderr(d, request: pytest.FixtureRequest) -> None:
    destination = os.environ.get("SW_TEST_DAEMON_STDERR_DIR")
    if os.name != "nt" or not destination:
        return
    out = Path(destination)
    out.mkdir(parents=True, exist_ok=True)
    name = "".join(c if c.isalnum() or c in "-_." else "_" for c in request.node.nodeid)
    shutil.copyfile(d.root / "daemon.stderr", out / f"{name}.daemon.stderr")
    wal = d.wal_dir / "raw.wal.ndjson"
    if wal.is_file():
        wal_out = out / "wal"
        wal_out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(wal, wal_out / f"{name}.raw.wal.ndjson")


def _start(
    root: Path,
    *,
    profile_yaml: str | None = None,
    second_board: bool = False,
    extra_env: dict[str, str] | None = None,
    **target_attrs,
):
    """以 06 `start_daemon` 啟動，並在 FakeTarget start 前套用 P6 追加能力（`scripts`／`ready`）。

    不改 06 harness：暫時包裝 `harness._make_target`，於回傳前設定屬性（FakeTarget 於執行期讀取）。
    """
    if os.name == "nt":
        if second_board:
            raise RuntimeError("Windows slice22 DUT adapter 僅支援本項的單一 COM20 target")
        import harness

        from harness import SHELL_TARGET_YAML

        return _start_windows(root, profile_yaml=profile_yaml or SHELL_TARGET_YAML, extra_env=extra_env)

    import harness
    from harness import start_daemon

    orig = harness._make_target
    extra: list = []

    def _make(workdir, *args, **kwargs):
        target = orig(workdir, *args, **kwargs)
        for key, value in target_attrs.items():
            setattr(target, key, value)
        if second_board:
            # 第二塊板：同一 by-id 目錄、連結名為第一塊加 `-2`（profile 以 `<placeholder>-2` 指向它）
            board2 = orig(Path(str(workdir) + "-2"), *args, **kwargs)
            first_start = target.start

            def _start_both(by_id_dir, name=harness.BY_ID_NAME):
                link = first_start(by_id_dir, name)
                board2.start(by_id_dir, name + "-2")
                return link

            target.start = _start_both
            extra.append(board2)
        return target

    kwargs: dict = {} if profile_yaml is None else {"profile_yaml": profile_yaml}
    if extra_env:
        kwargs["extra_env"] = extra_env
    harness._make_target = _make
    try:
        d = start_daemon(root, **kwargs)
    except BaseException:
        for t in extra:
            t.stop()
        raise
    finally:
        harness._make_target = orig
    if extra:
        d.targets["COM1"] = extra[0]
    return d


def _stop(d) -> None:
    try:
        d.stop()
    except Exception:
        pass
    if os.name == "nt":
        for target in d.targets.values():
            target.stop()
    else:
        for com, target in d.targets.items():
            if com != "COM0":
                target.stop()


@pytest.fixture
def console_daemon(tmp_path: Path, request: pytest.FixtureRequest):
    """COM0 已 READY 的受測 daemon；yield `(daemon, FakeTarget)`，teardown 一律 stop。"""
    d = _start(tmp_path / "console", scripts=dict(CONSOLE_SCRIPTS))
    try:
        d.wait_state("COM0", "READY")
        yield d, d.targets["COM0"]
    finally:
        _stop(d)
        _capture_windows_daemon_stderr(d, request)


@pytest.fixture
def unready_daemon(tmp_path: Path, request: pytest.FixtureRequest):
    """未就緒 target 停在 ATTACHED；yield `(daemon, target adapter)`。"""
    from harness import CRED_PASS_ENV, CRED_USER_ENV, DEVICE_PLACEHOLDER, SHELL_TARGET_YAML

    # POSIX 不宣告帳密時 probe_ready 失敗並停在 ATTACHED；Windows 用 passthrough 保持 ATTACHED，
    # 並設 login/password regex 供 recovery prompt 分類（不執行 login probe）。
    if os.name == "nt":
        # passthrough 保持 ATTACHED；login/password regex 僅供 interactive-open recovery 分類。
        yaml = (
            "profiles:\n"
            "  t:\n"
            "    platform: passthrough\n"
            "    login_regex: '(?mi)^.*login:\\s*$'\n"
            "    password_regex: '(?mi)^password:\\s*$'\n"
            "targets:\n"
            "  - profile: t\n"
            "    com: COM0\n"
            f"    device_by_id: {DEVICE_PLACEHOLDER}\n"
        )
    else:
        yaml = "".join(
            line
            for line in SHELL_TARGET_YAML.splitlines(keepends=True)
            if CRED_USER_ENV not in line and CRED_PASS_ENV not in line
        )
    d = _start(tmp_path / "unready", profile_yaml=yaml, ready=False)
    try:
        d.wait_state("COM0", "ATTACHED", timeout=30.0)
        yield d, d.targets["COM0"]
    finally:
        _stop(d)
        _capture_windows_daemon_stderr(d, request)


@pytest.fixture
def two_board_daemon(tmp_path: Path):
    """COM0、COM1 兩塊 FakeTarget 皆 READY 的受測 daemon；yield daemon（`d.targets` 含兩者）。"""
    from harness import DEVICE_PLACEHOLDER, SHELL_TARGET_YAML

    yaml = SHELL_TARGET_YAML + f"  - profile: t\n    com: COM1\n    device_by_id: {DEVICE_PLACEHOLDER}-2\n"
    d = _start(tmp_path / "two", profile_yaml=yaml, second_board=True)
    try:
        d.wait_state("COM0", "READY")
        d.wait_state("COM1", "READY")
        yield d
    finally:
        _stop(d)


@pytest.fixture
def telnet_daemon(tmp_path: Path, request: pytest.FixtureRequest):
    """SC-28 `SERIALWRAP_CONSOLE_BACKEND=tcp`：human console 走 127.0.0.1 Telnet listener；yield `(daemon, FakeTarget)`。"""
    d = _start(tmp_path / "telnet", extra_env={"SERIALWRAP_CONSOLE_BACKEND": "tcp"})
    try:
        d.wait_state("COM0", "READY")
        yield d, d.targets["COM0"]
    finally:
        _stop(d)
        _capture_windows_daemon_stderr(d, request)
