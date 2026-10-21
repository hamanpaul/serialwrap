"""黑箱 E2E：CLI → Python daemon → FakeTarget 的絕對契約（P3-T18）。

依計畫 P3-T18（plans/04-stage1a-rust-cli.md §Task 18）：每個 test 以 function-scope
fixture 建立自己的 Python daemon 與 FakeTarget，單獨執行或任意順序執行皆成立。
daemon 一律為 Python 基線；本檔對 A（Python）與 B（Rust CLI）驗同一組絕對契約，
不做位元組差分（cmd_id、時間戳各輪不同）。
本檔為 stdlib-only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import select
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

    def __init__(self, workdir: Path) -> None:
        super().__init__(workdir)
        self.remote_files: dict[str, bytes] = {}
        self._holding_command = False

    def start(self, by_id_dir: Path, name: str = "usb-FAKE_TARGET_0-if00") -> Path:
        link = super().start(by_id_dir, name)
        if self.slave_fd is not None:
            os.close(self.slave_fd)
            self.slave_fd = None
        return link

    def _run_loop(self) -> None:
        assert self.master_fd is not None
        try:
            os.write(self.master_fd, b"login: ")
        except OSError:
            return

        state = "LOGIN"
        line_buf = bytearray()
        prev_was_cr = False
        try:
            while not self._stop.is_set():
                try:
                    ready, _, _ = select.select([self.master_fd], [], [], 0.05)
                except (OSError, ValueError):
                    if self._stop.wait(0.05):
                        break
                    continue
                if not ready:
                    continue
                try:
                    chunk = os.read(self.master_fd, 4096)
                except OSError:
                    # daemon 尚未開啟或正在重開 slave 時，PTY master 會短暫回 EIO。
                    if self._stop.wait(0.05):
                        break
                    continue
                if not chunk:
                    break

                with self._lock:
                    self._rx_log.extend(chunk)

                for value in chunk:
                    byte = bytes((value,))
                    if self._holding_command:
                        if byte == b"\x03":
                            self._holding_command = False
                            self._write_target(b"^C\r\n" + self.prompt.encode("utf-8"))
                        continue
                    if byte == b"\n" and prev_was_cr:
                        prev_was_cr = False
                        continue
                    prev_was_cr = byte == b"\r"

                    if state == "LOGIN":
                        if byte in (b"\r", b"\n"):
                            user = line_buf.decode("utf-8", errors="replace")
                            line_buf.clear()
                            if user == self.user:
                                self._write_target(b"\r\nPassword: ")
                                state = "PASSWORD"
                            else:
                                self._write_target(b"\r\nlogin: ")
                        else:
                            line_buf.extend(byte)
                    elif state == "PASSWORD":
                        if byte in (b"\r", b"\n"):
                            password = line_buf.decode("utf-8", errors="replace")
                            line_buf.clear()
                            if password == self.password:
                                self._write_target(b"\r\n" + self.prompt.encode("utf-8"))
                                state = "READY"
                            else:
                                self._write_target(b"\r\nLogin incorrect\r\nlogin: ")
                                state = "LOGIN"
                        else:
                            line_buf.extend(byte)
                    elif state == "READY":
                        if byte == b"\x03":
                            line_buf.clear()
                            self._write_target(b"^C\r\n" + self.prompt.encode("utf-8"))
                        elif byte in (b"\r", b"\n"):
                            command = line_buf.decode("utf-8", errors="replace")
                            line_buf.clear()
                            output = self._execute_target_command(command)
                            if output is None:
                                self._holding_command = True
                                continue
                            self._write_target(b"\r\n" + output + self.prompt.encode("utf-8"))
                        else:
                            line_buf.extend(byte)
                            # file.push 的 echo-ACK 路徑會在尚未送換行前等 target 回讀每段。
                            self._write_target(byte)
        except OSError:
            pass
        except BaseException as exc:
            self._thread_exc = exc

    def _write_target(self, data: bytes) -> None:
        assert self.master_fd is not None
        os.write(self.master_fd, data)

    def _execute_target_command(self, command: str) -> bytes | None:
        """模擬黑箱測試需要的 shell 命令與檔案傳輸協定。"""
        if command == "hold":
            return None
        if "SW_XFER_DECODER_B64_" in command:
            return b"SW_XFER_DECODER_B64_42\r\n"
        if "SW_XFER_ENCODER_B64_" in command:
            return b"SW_XFER_ENCODER_B64_42\r\n"

        chunk = re.search(
            r"printf '%s' (?P<payload>'[^']*'|[^ ]+) \| base64 -d "
            r"(?P<op>>>|>) (?P<path>'[^']+'|[^ ]+) &&",
            command,
        )
        if chunk:
            payload = base64.b64decode(chunk.group("payload").strip("'"))
            path = chunk.group("path").strip("'")
            self.remote_files[path] = (
                self.remote_files.get(path, b"") + payload
                if chunk.group("op") == ">>"
                else payload
            )
            return b"SW_XFER_CHUNK_42\r\n"

        digest = re.search(r"^md5sum ('[^']+'|[^ ]+) &&", command)
        if digest:
            path = digest.group(1).strip("'")
            data = self.remote_files.get(path, b"")
            md5 = hashlib.md5(data).hexdigest().encode("ascii")
            return md5 + b"  " + path.encode("utf-8") + b"\r\nSW_XFER_MD5_42\r\n"

        move = re.search(r"^mv ('[^']+'|[^ ]+) ('[^']+'|[^ ]+) &&", command)
        if move:
            source, destination = (value.strip("'") for value in move.groups())
            self.remote_files[destination] = self.remote_files.pop(source, b"")
            return b"SW_XFER_MV_42\r\n"

        cleanup = re.search(r"^rm -f ('[^']+'|[^ ]+) &&", command)
        if cleanup:
            self.remote_files.pop(cleanup.group(1).strip("'"), None)
            return b"SW_XFER_CLEANUP_42\r\n"

        pull = re.search(r"base64 < ('[^']+'|[^ ]+) &&", command)
        if pull:
            data = self.remote_files.get(pull.group(1).strip("'"), b"")
            encoded = base64.b64encode(data)
            return b"===SW_XFER_BEGIN===\r\n" + encoded + b"\r\n===SW_XFER_END===\r\n"

        if command.startswith("echo "):
            return command[5:].encode("utf-8") + b"\r\n"
        if command == "echo":
            return b"\r\n"
        return command.encode("utf-8") + b"\r\n"


class Rig:
    """單一測試用的 daemon＋FakeTarget＋CLI 端點。"""

    def __init__(self, daemon: Any, target: Any, sock: str, env: dict[str, str]) -> None:
        self.daemon = daemon
        self.target = target
        self.sock = sock
        self.env = env
        self.extra_targets: list[Any] = []

    def cli(self, args: list[str], *, timeout_s: float = 60.0, cwd: Path | None = None) -> CliResult:
        return run_cli(["--socket", self.sock, *args], env=self.env, timeout_s=timeout_s, cwd=cwd)

    def cli_json(self, args: list[str], *, cwd: Path | None = None) -> dict[str, Any]:
        r = self.cli(args, cwd=cwd)
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
        # 若 FakeTarget reader 已因模擬器錯誤退出，立即回報 thread 例外，不等滿整段狀態逾時。
        rig.target.rx_log()
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
    # 所有已建立（含 start 失敗者）的 daemon 先登錄，teardown 一律回收，不因 start 失敗而漏掉。
    daemons: list[Any] = []
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
        daemons.append(daemon)
        daemon.start()
        rig = Rig(daemon, target, sock, child_env)
        made.append(rig)
        return rig

    yield _make
    # stop() 失敗不吞例外：先以程序群組 SIGKILL 強制回收，最後統一回報，避免殘留 daemon 污染後續測試。
    cleanup_errors: list[str] = []
    for daemon in daemons:
        try:
            daemon.stop()
        except Exception as exc:  # noqa: BLE001 - 收集後統一回報
            cleanup_errors.append(f"daemon stop 失敗：{type(exc).__name__}: {exc}")
            proc = getattr(daemon, "proc", None)
            if proc is not None and proc.poll() is None and os.name == "posix":
                try:
                    os.killpg(proc.pid, 9)
                except OSError as kill_exc:
                    cleanup_errors.append(f"killpg 失敗：{kill_exc}")
    for rig in made:
        for target in rig.extra_targets:
            target.stop()
    for target in targets:
        target.stop()
    shutil.rmtree(sock_root, ignore_errors=True)
    if cleanup_errors:
        raise RuntimeError("rig teardown 回收失敗：\n" + "\n".join(cleanup_errors))


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


def _state_document(rig: Rig) -> dict[str, Any]:
    state_path = Path(rig.env["SERIALWRAP_STATE_DIR"]) / "state.json"
    return json.loads(state_path.read_text(encoding="utf-8"))


def _write_event_rule(rig: Rig, tmp_path: Path, name: str = "e2e-rule") -> tuple[str, Path]:
    rule_file = tmp_path / f"{name}.json"
    rule_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": "e2e",
                "name": name,
                "kind": "tool",
                "selectors": ["COM0"],
                "pattern": {"kind": "contains", "value": "x"},
                "scope": "any",
                "handler": {"exec": ["/bin/true"]},
            }
        ),
        encoding="utf-8",
    )
    added = rig.cli_json(["event", "add", "--file", str(rule_file)])
    rules = rig.cli_json(["event", "list"])["rules"]
    rule = next(row for row in rules if row.get("owner") == "e2e" and row.get("kind") == "tool")
    assert rule.get("rule_id")
    return str(rule["rule_id"]), rule_file


def _fired_rule(rig: Rig, rule_id: str) -> dict[str, Any] | None:
    listed = rig.cli_json(["event", "list", "--selector", "COM0"])
    for row in listed.get("rules", []):
        if row.get("rule_id") == rule_id and row.get("fires", 0) > 0:
            return row
    return None


def test_session_clear_detaches_and_reattaches(rig):
    before = rig.target.rx_log()
    cleared = rig.cli_json(["session", "clear", "--selector", "COM0"])
    assert cleared["session"]["com"] == "COM0"
    ready = _wait_state(rig, "READY")
    after = rig.target.rx_log()
    assert ready["session_id"] == cleared["session"]["session_id"]
    assert after != before
    assert after.count(b"echo __READY__") >= 2


def test_session_bind_switches_device(rig, tmp_path):
    by_id_dir = Path(rig.env["SERIALWRAP_BY_ID_DIR"])
    target = DaemonAttachableTarget(tmp_path / "second-target")
    link = target.start(by_id_dir, name="usb-FAKE_TARGET_1-if00")
    rig.extra_targets.append(target)

    bound = rig.cli_json(["session", "bind", "--selector", "COM0", "--device-by-id", str(link)])
    assert bound["session"]["device_by_id"] == str(link)
    ready = _wait_state(rig, "READY")
    assert ready["device_by_id"] == str(link)
    assert b"\nroot\n" in target.rx_log()
    assert b"echo __READY__" in target.rx_log()
    state = _state_document(rig)
    assert state["bindings"][ready["session_id"]] == str(link)


@pytest.mark.parametrize("operation", ["pin", "unpin"], ids=["pin", "unpin"])
def test_session_profile_pin_persists(rig, tmp_path, operation):
    target = DaemonAttachableTarget(tmp_path / "pin-target")
    link = target.start(Path(rig.env["SERIALWRAP_BY_ID_DIR"]), name="usb-FAKE_TARGET_PIN-if00")
    rig.extra_targets.append(target)
    by_id = str(link)
    _poll(lambda: True if by_id in json.dumps(rig.cli_json(["device", "list"])) else None)
    pins = ["session", "pin", "--selector", by_id, "--profile", "shell-e2e"]
    pin_result = rig.cli_json(pins)
    assert pin_result["profile"] == "shell-e2e"
    assert _state_document(rig)["profile_pins"][by_id] == "shell-e2e"

    if operation == "unpin":
        unpinned = rig.cli_json(["session", "unpin", "--selector", by_id])
        assert unpinned["device_key"] == by_id
        assert by_id not in _state_document(rig)["profile_pins"]


def test_session_attach_restarts_cleared_session(rig):
    session_id = _session_com0(rig)["session_id"]
    ready_probes = rig.target.rx_log().count(b"echo __READY__")

    # clear 會自動重試 attach；先讓 target 拒絕登入，確保後續 CLI attach
    # 必須重新介入尚未 READY 的 session，不能只驗到 clear 的副作用。
    rig.target.user = "temporarily-unavailable"
    cleared = rig.cli_json(["session", "clear", "--selector", "COM0"])
    assert cleared["session"]["session_id"] == session_id

    not_ready = _poll(
        lambda: (
            row
            if (row := _session_com0(rig)) is not None
            and row["state"] in {"ATTACHED", "DETACHED"}
            else None
        )
    )
    assert not_ready["session_id"] == session_id
    assert not_ready["state"] in {"ATTACHED", "DETACHED"}
    assert rig.target.rx_log().count(b"echo __READY__") == ready_probes

    rig.target.user = E2E_CREDENTIALS["SERIALWRAP_E2E_USER"]
    attached = rig.cli_json(["session", "attach", "--selector", "COM0"])
    assert attached["session"]["session_id"] == session_id
    ready = _wait_state(rig, "READY")
    assert ready["session_id"] == session_id
    assert rig.target.rx_log().count(b"echo __READY__") == ready_probes + 1


@pytest.mark.parametrize(
    "options",
    [([], "default"), (["--probe-timeout", "3", "--strict-human-lock"], "strict")],
    ids=lambda value: value[1] if isinstance(value, tuple) else None,
)
def test_session_self_test_reports_live_health(rig, options):
    args, _ = options
    result = rig.cli_json(["session", "self-test", "--selector", "COM0", *args])
    assert result["command_capable"] is True
    assert result["session"]["state"] == "READY"
    assert result["classification"]


def test_session_activity_reports_live_session(rig):
    submitted = rig.cli_json(["cmd", "submit", "--selector", "COM0", "--cmd", "echo activity-e2e"])
    _wait_cmd_terminal(rig, submitted["cmd_id"])
    activity = rig.cli_json(["session", "activity", "--selector", "COM0"])
    assert activity["session"]["session_id"] == _session_com0(rig)["session_id"]
    assert activity["session"]["state"] == "READY"
    assert activity["session"]["activity_classification"] in {"active", "idle"}


def test_session_log_capture_lifecycle(rig):
    started = rig.cli_json(["session", "log-start", "--selector", "COM0"])
    path = Path(started["log_path"])
    active = rig.cli_json(["session", "log-status", "--selector", "COM0"])
    assert active["active"] is True
    assert active["capture_id"] == started["capture_id"]

    opened = rig.cli_json(["session", "interactive-open", "--selector", "COM0", "--allow-attached"])
    interactive_id = opened["interactive_id"]
    rig.cli_json(["session", "interactive-send", "--interactive-id", interactive_id, "--data", "echo capture-e2e\r"])
    _poll(lambda: path.read_text(encoding="utf-8") if path.is_file() and "capture-e2e" in path.read_text(encoding="utf-8") else None)
    rig.cli_json(["session", "interactive-close", "--interactive-id", interactive_id])
    stopped = rig.cli_json(["session", "log-stop", "--selector", "COM0"])
    assert stopped["capture_id"] == started["capture_id"]
    assert stopped["byte_count"] > 0
    inactive = rig.cli_json(["session", "log-status", "--selector", "COM0"])
    assert inactive["active"] is False
    assert "capture-e2e" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("profile", [None, "shell-e2e"], ids=["no-profile", "with-profile"])
def test_alias_assign_persists_device_binding(rig, profile):
    by_id = str(rig.target.link_path)
    args = ["alias", "assign", "--by-id", by_id, "--alias", "lab-device"]
    if profile is not None:
        args.extend(["--profile", profile])
    result = rig.cli_json(args)
    assert result["device_by_id"] == by_id
    aliases = rig.cli_json(["alias", "list"])["aliases"]
    row = next(item for item in aliases if item["alias"] == "lab-device")
    assert row["device_by_id"] == by_id
    assert row.get("profile") == profile
    assert _state_document(rig)["aliases"]["lab-device"]["device_by_id"] == by_id


def test_alias_unassign_removes_device_binding(rig):
    by_id = str(rig.target.link_path)
    rig.cli_json(["alias", "assign", "--by-id", by_id, "--alias", "lab-device"])
    removed = rig.cli_json(["alias", "unassign", "--alias", "lab-device"])
    assert removed["ok"] is True
    assert all(row["alias"] != "lab-device" for row in rig.cli_json(["alias", "list"])["aliases"])
    assert "lab-device" not in _state_document(rig)["aliases"]


def test_cmd_cancel_marks_running_command_canceled(rig):
    submitted = rig.cli_json(["cmd", "submit", "--selector", "COM0", "--cmd", "hold"])
    cmd_id = submitted["cmd_id"]
    _poll(lambda: rig.target.rx_log() if b"hold" in rig.target.rx_log() else None)
    cancelled = rig.cli_json(["cmd", "cancel", "--cmd-id", cmd_id])
    assert cancelled["status"] == "canceled"
    result = _wait_cmd_terminal(rig, cmd_id)
    assert result["status"] == "canceled"
    assert _wait_state(rig, "READY")["state"] == "READY"


@pytest.mark.parametrize("from_seq", [None, 0], ids=["latest", "from-seq"])
def test_log_tail_raw_reads_daemon_wal(rig, from_seq):
    submitted = rig.cli_json(["cmd", "submit", "--selector", "COM0", "--cmd", "echo raw-e2e"])
    _wait_cmd_terminal(rig, submitted["cmd_id"])
    args = ["log", "tail-raw"]
    if from_seq is not None:
        args.extend(["--from-seq", str(from_seq)])
    result = rig.cli_json(args)
    payloads = [base64.b64decode(row["payload_b64"]) for row in result["records"]]
    assert any(b"raw-e2e" in payload for payload in payloads)
    assert result["returned"] == len(result["records"]) > 0


@pytest.mark.parametrize(
    "options",
    [([], "default"), (["--chunk-timeout", "3", "--ack-mode", "none", "--chunk-size", "64"], "chunk-options")],
    ids=lambda value: value[1] if isinstance(value, tuple) else None,
)
def test_file_push_transfers_bytes_to_target(rig, tmp_path, options):
    extra, name = options
    local_path = tmp_path / f"push-{name}.bin"
    payload = bytes(range(160)) if name == "chunk-options" else b"push-e2e-payload"
    local_path.write_bytes(payload)
    remote_path = f"/tmp/{name}.bin"
    result = rig.cli_json(
        ["file", "push", "--selector", "COM0", "--local", str(local_path), "--remote", remote_path, *extra]
    )
    assert result["bytes"] == len(payload)
    assert result["md5"] == hashlib.md5(payload).hexdigest()
    assert rig.target.remote_files[remote_path] == payload


@pytest.mark.parametrize("use_local", [False, True], ids=["default-local-name", "explicit-local-path"])
def test_file_pull_transfers_bytes_from_target(rig, tmp_path, use_local):
    remote_path = f"/tmp/{tmp_path.name}-pull-e2e.bin"
    payload = b"pull-e2e-payload\x00\xff"
    rig.target.remote_files[remote_path] = payload
    local_path = tmp_path / "download.bin"
    args = ["file", "pull", "--selector", "COM0", "--remote", remote_path]
    expected_path = local_path
    if use_local:
        args.extend(["--local", str(local_path), "--chunk-timeout", "2"])
    else:
        expected_path = ROOT / Path(remote_path).name
        assert not expected_path.exists()
    try:
        result = rig.cli_json(args)
        assert result["bytes"] == len(payload)
        assert result["md5"] == hashlib.md5(payload).hexdigest()
        assert result["local_path"] == (str(local_path) if use_local else Path(remote_path).name)
        assert expected_path.read_bytes() == payload
    finally:
        if not use_local and expected_path.exists():
            expected_path.unlink()


def test_wal_reset_rotates_and_resets_sequence(rig):
    submitted = rig.cli_json(["cmd", "submit", "--selector", "COM0", "--cmd", "echo before-reset"])
    _wait_cmd_terminal(rig, submitted["cmd_id"])
    before = rig.cli_json(["wal", "current-seq"])["seq"]
    assert before > 0

    reset = rig.cli_json(["wal", "reset"])
    assert reset["previous_seq"] == before
    assert rig.cli_json(["wal", "current-seq"])["seq"] == 0
    wal_dir = Path(rig.env["SERIALWRAP_WAL_DIR"])
    assert list(wal_dir.glob("raw.wal.ndjson.*"))


def test_event_show_returns_registered_rule(rig, tmp_path):
    rule_id, _ = _write_event_rule(rig, tmp_path)
    shown = rig.cli_json(["event", "show", rule_id])
    assert shown["rule"]["name"] == "e2e-rule"
    assert shown["rule"]["owner"] == "e2e"
    assert shown["counter"]["fires"] == 0


def test_event_enable_updates_matcher_state(rig):
    enabled = rig.cli_json(["event", "enable", "--selector", "COM0"])
    assert enabled["selector"] == "COM0"
    assert enabled["enabled"] is True
    status = rig.cli_json(["event", "status"])
    assert "COM0" in status["coms"]


def test_event_disable_updates_matcher_state(rig):
    rig.cli_json(["event", "enable", "--selector", "COM0"])
    disabled = rig.cli_json(["event", "disable", "--selector", "COM0"])
    assert disabled["selector"] == "COM0"
    assert disabled["enabled"] is False
    status = rig.cli_json(["event", "status"])
    assert "COM0" not in status["coms"]


def test_event_status_lists_enabled_daemon_selector(rig):
    rig.cli_json(["event", "enable", "--selector", "COM0"])
    status = rig.cli_json(["event", "status"])
    assert "COM0" in status["coms"]


def test_event_reset_clears_fired_rule_counter(rig, tmp_path):
    rule_id, _ = _write_event_rule(rig, tmp_path)
    rig.cli_json(["event", "enable", "--selector", "COM0"])
    submitted = rig.cli_json(["cmd", "submit", "--selector", "COM0", "--cmd", "echo x"])
    _wait_cmd_terminal(rig, submitted["cmd_id"])
    assert _poll(lambda: _fired_rule(rig, rule_id))

    reset = rig.cli_json(["event", "reset", "--rule-id", rule_id])
    assert reset["cleared"] == 1
    listed = rig.cli_json(["event", "list", "--selector", "COM0"])
    row = next(item for item in listed["rules"] if item["rule_id"] == rule_id)
    assert row["fires"] == 0


def test_event_reload_removes_deleted_registry_rule(rig, tmp_path):
    rule_id, _ = _write_event_rule(rig, tmp_path)
    registry_dir = Path(rig.env["SERIALWRAP_EVENTS_DIR"])
    registry_file = registry_dir / f"{rule_id}.json"
    assert registry_file.is_file()
    registry_file.unlink()

    reloaded = rig.cli_json(["event", "reload"])
    assert rule_id in reloaded["removed"]
    missing = rig.cli(["event", "show", rule_id])
    assert missing.rc != 0


def test_event_tail_reads_matcher_log(rig):
    rig.cli_json(["event", "enable", "--selector", "COM0"])
    tail = rig.cli_json(["event", "tail", "--selector", "COM0", "-n", "10"])
    assert any(
        row.get("type") == "com_enabled" and row.get("selector") == "COM0"
        for row in tail["entries"]
    )


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
