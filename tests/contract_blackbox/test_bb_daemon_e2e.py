"""黑箱 E2E：CLI → Python daemon → FakeTarget 的絕對契約（P3-T18）。

依計畫 P3-T18（plans/04-stage1a-rust-cli.md §Task 18）：每個 test 以 function-scope
fixture 建立自己的 Python daemon 與 FakeTarget，單獨執行或任意順序執行皆成立。
daemon 一律為 Python 基線；本檔對 A（Python）與 B（Rust CLI）驗同一組絕對契約，
不做位元組差分（cmd_id、時間戳各輪不同）。
本檔為 stdlib-only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from bb_faketarget import FakeTarget
from bb_harness import CliResult, isolated_env, run_cli

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc, pytest.mark.needs_py_daemon]

ROOT = Path(__file__).resolve().parents[2]
PROFILE_TEMPLATE = ROOT / "tests" / "contract_blackbox" / "data" / "e2e_profiles" / "shell.yaml"
E2E_CREDENTIALS = {"SERIALWRAP_E2E_USER": "root", "SERIALWRAP_E2E_PASS": "pw"}
TERMINAL_STATES = {"done", "failed", "cancelled", "canceled", "timeout", "error"}


class DaemonAttachableTarget(FakeTarget):
    """bb_harness.FakeTarget 的 daemon 相容變體（只供本檔使用，不改動 harness）。

    harness 版本在 start() 後仍持有 slave fd。Python daemon 的 BC-04 外部 holder 偵測
    （sw_core/serial_port.py probe_holders）會把同 tty 上任何其他行程（含 pytest 本身）
    視為外部 holder，開埠即 DeviceHeldError（ATTACH_FAILED）。本變體依 tests/test_multiagent_e2e.py
    的既有做法：連結建立後立即關閉 slave；reader 在 daemon 關閉或重開 slave 造成的 EIO 後
    持續存活，等待下一次開啟（release／attach 循環需要）。
    """

    def start(self, by_id_dir: Path, name: str = "usb-FAKE_TARGET_0-if00") -> Path:
        link = super().start(by_id_dir, name)
        if self.slave_fd is not None:
            os.close(self.slave_fd)
            self.slave_fd = None
        return link

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            super()._run_loop()
            if self._stop.wait(0.05):
                break


class Rig:
    """單一測試用的 daemon＋FakeTarget＋CLI 端點。"""

    def __init__(self, daemon: Any, target: Any, sock: str, env: dict[str, str]) -> None:
        self.daemon = daemon
        self.target = target
        self.sock = sock
        self.env = env

    def cli(self, args: list[str], *, timeout_s: float = 60.0) -> CliResult:
        return run_cli(["--socket", self.sock, *args], env=self.env, timeout_s=timeout_s)

    def cli_json(self, args: list[str]) -> dict[str, Any]:
        r = self.cli(args)
        assert r.rc == 0, (args, r.rc, r.stderr)
        obj = json.loads(r.stdout)
        assert obj.get("ok") is True, (args, obj)
        return obj


def _poll(fn: Callable[[], Any], *, timeout_s: float = 20.0, interval_s: float = 0.2) -> Any:
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval_s)
    raise AssertionError(f"逾時 {timeout_s}s；最後觀察值：{last!r}")


def _session_com0(rig: Rig) -> dict[str, Any] | None:
    obj = rig.cli_json(["session", "list"])
    for row in obj.get("sessions", []):
        if row.get("com") == "COM0":
            return row
    return None


def _wait_state(rig: Rig, state: str, *, timeout_s: float = 20.0) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        row = _session_com0(rig)
        return row if row is not None and row.get("state") == state else None

    return _poll(probe, timeout_s=timeout_s)


def _wait_cmd_terminal(rig: Rig, cmd_id: str) -> dict[str, Any]:
    def probe() -> dict[str, Any] | None:
        cmd = rig.cli_json(["cmd", "status", "--cmd-id", cmd_id])["command"]
        return cmd if cmd.get("status") in TERMINAL_STATES else None

    return _poll(probe)


@pytest.fixture
def rig_factory(tmp_path: Path, env: dict[str, str], py_daemon):
    """回傳建立 Rig 的函式；建立的 daemon 與 DUT 於 teardown 時停止。

    AF_UNIX 路徑上限約 108 bytes，socket 放在短路徑的暫存目錄（不用 tmp_path）。
    """
    made: list[Rig] = []
    targets: list[DaemonAttachableTarget] = []
    sock_root = Path(tempfile.mkdtemp(prefix="swe2e-"))

    def _make() -> Rig:
        n = len(made)
        base = tmp_path / f"rig{n}"
        base.mkdir(parents=True, exist_ok=True)
        by_id_dir = base / "by-id"
        target = DaemonAttachableTarget(base / "fw")
        targets.append(target)
        link = target.start(by_id_dir)
        profile_dir = base / "profiles"
        profile_dir.mkdir(parents=True, exist_ok=True)
        text = PROFILE_TEMPLATE.read_text(encoding="utf-8").replace("__DEVICE_BY_ID__", str(link))
        (profile_dir / "shell.yaml").write_text(text, encoding="utf-8")
        sock = str(sock_root / f"d{n}.sock")
        child_env = isolated_env(base, **E2E_CREDENTIALS)
        daemon = py_daemon(profile_dir=profile_dir, socket=sock, lock=base / "e2e.lock", custom_env=child_env)
        daemon.start()
        rig = Rig(daemon, target, sock, child_env)
        made.append(rig)
        return rig

    yield _make
    for target in targets:
        target.stop()
    shutil.rmtree(sock_root, ignore_errors=True)


@pytest.fixture
def rig(rig_factory) -> Rig:
    r = rig_factory()
    _wait_state(r, "READY")
    return r


def test_session_reaches_ready(rig_factory):
    """session list 輪詢至 COM0 進入 READY。"""
    r = rig_factory()
    res = r.cli(["session", "list"])
    assert res.rc == 0
    row = _wait_state(r, "READY")
    assert row["com"] == "COM0"


def test_cmd_submit_status(rig):
    sub = rig.cli_json(["cmd", "submit", "--selector", "COM0", "--cmd", "echo hi"])
    cmd_id = sub["cmd_id"]
    cmd = _wait_cmd_terminal(rig, cmd_id)
    assert cmd["status"] == "done"
    assert "hi" in cmd["stdout"]


def test_cmd_background_result_tail(rig):
    sub = rig.cli_json(["cmd", "submit", "--selector", "COM0", "--cmd", "echo bg", "--mode", "background"])
    cmd_id = sub["cmd_id"]
    _wait_cmd_terminal(rig, cmd_id)
    tail = rig.cli_json(["cmd", "result-tail", "--cmd-id", cmd_id])
    assert "bg" in json.dumps(tail.get("chunks", []), ensure_ascii=False)


def test_log_and_wal(rig):
    sub = rig.cli_json(["cmd", "submit", "--selector", "COM0", "--cmd", "echo hi"])
    _wait_cmd_terminal(rig, sub["cmd_id"])
    text = rig.cli_json(["log", "tail-text", "--selector", "COM0"])
    assert any("hi" in line for line in text["lines"])
    seq = rig.cli_json(["wal", "current-seq"])
    assert seq["seq"] > 0
    export = rig.cli_json(["wal", "export", "--limit", "5"])
    assert isinstance(export["records"], list)
    stream = rig.cli_json(["stream", "tail", "--selector", "COM0"])
    assert stream["ok"] is True


def test_console_attach_detach(rig):
    att = rig.cli_json(["session", "console-attach", "--selector", "COM0", "--label", "t"])
    client_id = att["client_id"]
    assert att["vtty"]
    listing = rig.cli_json(["session", "console-list", "--selector", "COM0"])
    assert client_id in json.dumps(listing, ensure_ascii=False)
    det = rig.cli_json(["session", "console-detach", "--selector", "COM0", "--client-id", client_id])
    assert det["ok"] is True


def test_interactive_lease_roundtrip(rig):
    opened = rig.cli_json(["session", "interactive-open", "--selector", "COM0", "--allow-attached"])
    iid = opened.get("interactive_id") or opened.get("interactive_session_id")
    assert iid
    rig.cli_json(["session", "interactive-send", "--interactive-id", iid, "--data", "echo il\r"])
    def screen_has_echo() -> dict[str, Any] | None:
        status = rig.cli_json(["session", "interactive-status", "--interactive-id", iid])
        return status if "il" in json.dumps(status, ensure_ascii=False) else None

    status = _poll(screen_has_echo)
    assert "il" in json.dumps(status, ensure_ascii=False)
    rig.cli_json(["session", "interactive-close", "--interactive-id", iid])
    sub = rig.cli_json(["cmd", "submit", "--selector", "COM0", "--cmd", "echo after"])
    assert _wait_cmd_terminal(rig, sub["cmd_id"])["status"] == "done"


def test_recover_release_attach_no_replay(rig):
    sub = rig.cli_json(["cmd", "submit", "--selector", "COM0", "--cmd", "echo before"])
    _wait_cmd_terminal(rig, sub["cmd_id"])
    rig.cli_json(["session", "recover", "--selector", "COM0"])
    _wait_state(rig, "READY")
    rig.cli_json(["device", "release", "--selector", "COM0"])
    assert _session_com0(rig)["state"] == "RELEASED"
    rig.cli_json(["device", "attach", "--selector", "COM0"])
    _wait_state(rig, "READY")
    assert rig.target.rx_log().count(b"echo before") == 1


def test_alias_roundtrip(rig):
    sid = _session_com0(rig)["session_id"]
    rig.cli_json(["alias", "set", "--session-id", sid, "--alias", "lab0"])
    listing = rig.cli_json(["alias", "list"])
    assert "lab0" in json.dumps(listing, ensure_ascii=False)


def test_event_rule_roundtrip(rig, tmp_path):
    rule = {
        "schema_version": 1,
        "owner": "e2e",
        "name": "e2e-rule",
        "kind": "tool",
        "selectors": ["COM0"],
        "pattern": {"kind": "contains", "value": "x"},
        "handler": {"exec": ["/bin/true"]},
    }
    rule_file = tmp_path / "rule.json"
    rule_file.write_text(json.dumps(rule), encoding="utf-8")
    added = rig.cli_json(["event", "add", "--file", str(rule_file)])
    rule_id = added.get("rule_id") or added.get("id")
    assert rule_id
    rig.cli_json(["event", "list"])
    rig.cli_json(["event", "rm", str(rule_id)])


def test_device_mcu_health(rig):
    rig.cli_json(["device", "list"])
    rig.cli_json(["mcu", "patterns"])
    status = rig.cli_json(["mcu", "status"])
    assert status["flashing"] is False


def test_local_rejections_zero_tx(rig):
    rx_before = rig.target.rx_log()
    seq_before = rig.cli_json(["wal", "current-seq"])["seq"]

    r1 = rig.cli(["cmd", "submit", "--selector", "COM0", "--completion-regex", "^"])
    assert r1.rc == 1
    assert json.loads(r1.stdout)["error_code"] == "INVALID_COMPLETION_REGEX"

    r2 = rig.cli(["cmd", "submit", "--selector", "COM0", "--completion-regex", "a" * 1025])
    assert r2.rc == 1

    r3 = run_cli(["--bench", "nope", "session", "list"], env=rig.env)
    assert r3.rc == 1

    # Python 基線實測為 rc 2＋INVALID_ENDPOINT（經 RPC 錯誤路徑）；計畫 Q-04 寫「endpoint 解析錯誤 1」，
    # 與實測不符，本案以基線實測為準，差異另於 PR 說明登記。
    r4 = run_cli(["--endpoint", "foo://x", "session", "list"], env=rig.env)
    assert r4.rc == 2
    assert json.loads(r4.stdout)["error_code"] == "INVALID_ENDPOINT"

    r5 = run_cli(
        ["--socket", rig.sock, "--timeout", "-1", "cmd", "submit", "--selector", "COM0", "--cmd", "x"],
        env=rig.env,
    )
    assert r5.rc == 1
    assert r5.stdout == b""

    assert rig.target.rx_log() == rx_before
    assert rig.cli_json(["wal", "current-seq"])["seq"] == seq_before


def test_no_version_warning(rig):
    cases = [
        ["session", "list"],
        ["device", "list"],
        ["wal", "current-seq"],
    ]
    for args in cases:
        r = rig.cli(args)
        assert r.rc == 0, args
        assert "不一致".encode("utf-8") not in r.stderr, args
    r = run_cli(["--bench", "nope", "session", "list"], env=rig.env)
    assert "不一致".encode("utf-8") not in r.stderr
