"""Docker DUT 執行模式的真跑失敗測試（Task 6 T6-1）。

四個測試都透過 bridge CLI（`--dut-mode docker`）啟動真實容器，只依 Task 6 Interfaces
與 Task 5 控制協定撰寫，不窺探實作。docker 不可用時以 `cap:docker` 能力式 SKIP
（reason 明示「非 PASS」），不得計為通過。posix-pty 端點與 PTY 操作僅 POSIX 可用，
故同時標 `posix_pty`，Windows L1 job 以 marker 排除。

被測符號或腳本尚未存在時以 pytest.fail 寫出「尚未實作」訊息，紅燈原因是缺少行為，
而非 collection 錯誤。
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import select
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.docker, pytest.mark.posix_pty]

TOOL_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = TOOL_ROOT / "src"
BUILD_SCRIPT = TOOL_ROOT / "scripts" / "build_dut_image.sh"
DUT_MODES = "uart_stdio_bridge.bridge.dut_modes"
TASK = "Task 6 I6-1"

# 與 Task 4 `TargetConfig` 預設值相同（plan 固定值；預設 noise 開啟）
DUT_BANNER = b"boot done\r\nroot@prplOS:/# "
LS_EXPECTED = b"EXEC:ls\r\nRESULT:ls:OK\r\nroot@prplOS:/# "

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _symbol(module: str, attr: str, task: str = TASK):
    """取得待實作符號；尚不存在時以明確訊息失敗。"""
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as exc:
        missing = f"模組 {module} 不存在（{task}；缺少 {exc.name}）"
    else:
        if hasattr(mod, attr):
            return getattr(mod, attr)
        missing = f"{module}.{attr} 不存在（{task}）"
    pytest.fail(f"尚未實作：{missing}", pytrace=False)


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _skip_without_docker() -> None:
    if not _docker_ok():
        pytest.skip("cap:docker 不可用（非 PASS）")


@pytest.fixture(scope="module")
def dut_image() -> str:
    """先以 build_dut_image.sh 建置固定 tag 的 DUT image，回傳 tag。"""
    _skip_without_docker()
    if not BUILD_SCRIPT.is_file():
        pytest.fail(f"build_dut_image.sh 尚未實作：{BUILD_SCRIPT}（{TASK}）", pytrace=False)
    proc = subprocess.run(
        ["bash", str(BUILD_SCRIPT)],
        cwd=str(TOOL_ROOT),
        capture_output=True,
        timeout=900,
        check=False,
    )
    stderr = proc.stderr.decode("utf-8", errors="replace")
    assert proc.returncode == 0, f"build_dut_image.sh 應 exit 0，實得 {proc.returncode}；stderr 尾段={stderr[-2000:]!r}"
    image_tag = _symbol(DUT_MODES, "image_tag")
    return image_tag()


def _new_run_id() -> str:
    return "t6" + uuid.uuid4().hex[:10]


def _readline_within(stream, timeout_s: float) -> bytes:
    box: list[bytes] = []
    t = threading.Thread(target=lambda: box.append(stream.readline()), daemon=True)
    t.start()
    t.join(timeout_s)
    return box[0] if box else b""


def _read_manifest(evidence: Path) -> dict[str, Any]:
    path = evidence / "bridge-manifest.json"
    assert path.is_file(), f"bridge-manifest.json 應於 shutdown／fail-stop 後寫出：{path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _meta_list(evidence: Path) -> list[dict[str, Any]]:
    wire_path = evidence / "wire.ndjson"
    assert wire_path.is_file(), f"wire.ndjson 不存在：{wire_path}"
    records = [json.loads(line) for line in wire_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [rec["meta"] for rec in records if rec.get("dir") == "META"]


def _docker_names(run_id: str, generation: int, *, running: bool) -> list[str]:
    cmd = ["docker", "ps"]
    if not running:
        cmd.append("-a")
    cmd += ["--filter", f"name=sw-dut-{run_id}-g{generation}", "--format", "{{.Names}}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, f"docker ps 失敗：{proc.stderr}"
    target = f"sw-dut-{run_id}-g{generation}"
    return [name for name in proc.stdout.split() if name == target]


def _wait_container_gone(run_id: str, generation: int, timeout_s: float = 20.0) -> list[str]:
    deadline = time.monotonic() + timeout_s
    names = _docker_names(run_id, generation, running=False)
    while names and time.monotonic() < deadline:
        time.sleep(0.5)
        names = _docker_names(run_id, generation, running=False)
    return names


def _remove_containers(run_id: str) -> None:
    proc = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name=sw-dut-{run_id}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    ids = proc.stdout.split()
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=120, check=False)


class _Peer:
    """模擬 serialwrap 端（PTY slave）：raw 模式，累積收到的全部 bytes。"""

    def __init__(self, slave_path: str) -> None:
        import tty  # POSIX only

        self.fd = os.open(slave_path, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.fd)
        self.buf = b""

    def read_until(self, needle: bytes, timeout_s: float = 60.0) -> None:
        deadline = time.monotonic() + timeout_s
        while needle not in self.buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"逾時 {timeout_s}s 未收到 {needle!r}；已收 {self.buf[-512:]!r}")
            ready, _, _ = select.select([self.fd], [], [], min(remaining, 0.5))
            if ready:
                chunk = os.read(self.fd, 65536)
                if not chunk:
                    raise AssertionError(f"peer EOF 於收到 {needle!r} 前；已收 {self.buf[-512:]!r}")
                self.buf += chunk

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def close(self) -> None:
        os.close(self.fd)


class _Bridge:
    """以 bridge CLI 啟動 `--dut-mode docker` 的 posix-pty 拓樸，並以控制通道操作。"""

    def __init__(self, tmp_path: Path, image: str) -> None:
        self.tmp = tmp_path
        self.image = image
        self.run_id = _new_run_id()
        self.evidence = tmp_path / "evidence"
        self.by_id = tmp_path / "by-id" / "fake-uart0"
        self.by_id.parent.mkdir(parents=True, exist_ok=True)
        self.target = tmp_path / "target.json"
        target_cls = _symbol("uart_stdio_bridge.dut.model", "TargetConfig", "Task 4 I4-1")
        self.target.write_text(json.dumps(target_cls().to_json(), ensure_ascii=False), encoding="utf-8")
        self.stderr_path = tmp_path / "bridge.stderr.log"
        self.proc: subprocess.Popen | None = None
        self.ready: dict[str, Any] = {}
        self.client: Any = None
        self.peer: _Peer | None = None
        self._stderr_file = None

    def start(self) -> None:
        control_cls = _symbol("uart_stdio_bridge.bridge.control", "ControlClient", "Task 5 I5-1")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_ROOT)
        argv = [
            sys.executable, "-m", "uart_stdio_bridge", "run",
            "--run-id", self.run_id,
            "--case-id", "T6-DOCKER",
            "--port-id", "COM0",
            "--evidence-dir", str(self.evidence),
            "--endpoint", "posix-pty",
            "--by-id-link", str(self.by_id),
            "--dut-mode", "docker",
            "--dut-image", self.image,
            "--dut-target-json", str(self.target),
        ]
        self._stderr_file = self.stderr_path.open("wb")
        self.proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=self._stderr_file,
            cwd=str(self.tmp),
            env=env,
        )
        line = _readline_within(self.proc.stdout, 180)
        assert line, f"bridge 未在限時內輸出 BRIDGE_READY（stderr：{self.stderr_path}）"
        self.ready = json.loads(line)
        assert self.ready.get("event") == "BRIDGE_READY", f"bridge 首行應為 BRIDGE_READY：{self.ready}"
        self.client = control_cls(Path(self.ready["control_info"]), timeout_s=120)

    def open_peer(self) -> _Peer:
        self.peer = _Peer(self.ready["slave_path"])
        return self.peer

    def call(self, action: str, **params: Any) -> dict[str, Any]:
        return self.client.call(action, params)

    def shutdown(self) -> int:
        self.call("shutdown")
        return self.proc.wait(timeout=180)

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:  # noqa: BLE001 - 收尾階段僅盡力清理
                pass
        if self.peer is not None:
            try:
                self.peer.close()
            except OSError:
                pass
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.kill()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
        if self._stderr_file is not None:
            self._stderr_file.close()
        _remove_containers(self.run_id)


def test_docker_roundtrip(tmp_path: Path, dut_image: str) -> None:
    docker_image_id = _symbol(DUT_MODES, "docker_image_id")
    bridge = _Bridge(tmp_path, dut_image)
    try:
        bridge.start()
        peer = bridge.open_peer()
        peer.read_until(DUT_BANNER)
        assert peer.buf.startswith(DUT_BANNER), f"peer 應先收到 banner，實得 {peer.buf[:64]!r}"

        peer.write(b"ls\n")
        peer.read_until(LS_EXPECTED)
        assert LS_EXPECTED in peer.buf, f"ls 應得 {LS_EXPECTED!r}，實得 {peer.buf[-256:]!r}"

        assert bridge.shutdown() == 0
        manifest = _read_manifest(bridge.evidence)
        expected_id = docker_image_id(dut_image)
        assert expected_id is not None, f"docker image inspect 失敗：{dut_image}"
        assert manifest["dut"]["image_id"] == expected_id, (
            f"bridge-manifest.dut.image_id 應為 {expected_id}，實得 {manifest['dut'].get('image_id')}"
        )
    finally:
        bridge.close()


def test_docker_dut_control_roundtrip(tmp_path: Path, dut_image: str) -> None:
    bridge = _Bridge(tmp_path, dut_image)
    try:
        bridge.start()
        messages = [
            {"action": "emit", "payload_b64": base64.b64encode(b"EMIT-OK").decode("ascii")},
            {"action": "pause_responding", "duration_ms": 100},
            {"action": "logical_reboot", "banner_delay_ms": 0, "pre_text": ""},
        ]
        for msg in messages:
            ack = bridge.call("dut_control", msg=msg)
            assert ack.get("ok") is True, (
                f"容器 DUT 應回 ack（/control/ack.ndjson 須可寫，無 PermissionError）：action={msg['action']} ack={ack}"
            )

        assert bridge.shutdown() == 0
        metas = _meta_list(bridge.evidence)
        assert any(
            m.get("event") == "dut_generation" and m.get("reason") == "logical_reboot" for m in metas
        ), f"logical_reboot 後 wire 應有 META dut_generation{{reason:logical_reboot}}：{metas}"
    finally:
        bridge.close()


def test_docker_restart_new_container(tmp_path: Path, dut_image: str) -> None:
    bridge = _Bridge(tmp_path, dut_image)
    try:
        bridge.start()
        result = bridge.call("dut_restart", pending="discard", grace_s=2.0)
        assert result.get("dut_generation") == 2, f"dut_restart 後 dut_generation 應為 2：{result}"

        leftover_g1 = _wait_container_gone(bridge.run_id, 1)
        assert not leftover_g1, f"舊容器 g1 應因 --rm 已移除，仍存在：{leftover_g1}"

        running_g2 = _docker_names(bridge.run_id, 2, running=True)
        assert running_g2 == [f"sw-dut-{bridge.run_id}-g2"], f"新容器 g2 應在執行中：{running_g2}"

        assert bridge.shutdown() == 0
        metas = _meta_list(bridge.evidence)
        assert any(
            m.get("event") == "dut_generation" and m.get("reason") == "restart" for m in metas
        ), f"dut_restart 應記 META dut_generation{{reason:restart}}：{metas}"
    finally:
        bridge.close()


def test_docker_kill_reported_as_transport_fault(tmp_path: Path, dut_image: str) -> None:
    bridge = _Bridge(tmp_path, dut_image)
    try:
        bridge.start()
        bridge.call("dut_restart", pending="discard", grace_s=2.0)
        bridge.call("dut_kill")

        # DUT 已被 kill，bridge 可能以 fail-stop 結束；本測試只要求 manifest 與 META 如實記錄
        try:
            bridge.shutdown()
        except Exception:  # noqa: BLE001 - 見上方說明
            pass
        bridge.proc.wait(timeout=180)

        manifest = _read_manifest(bridge.evidence)
        kinds = {fault.get("kind") for fault in manifest["dut"]["faults"]}
        assert "dut_reboot" in kinds, f"dut_restart 應記 {{kind:dut_reboot}}：{kinds}"
        assert "docker_transport_fault" in kinds, f"dut_kill 應記 {{kind:docker_transport_fault}}：{kinds}"

        metas = _meta_list(bridge.evidence)
        assert any(
            m.get("event") == "fault" and m.get("action") == "dut_kill" for m in metas
        ), f"wire 應有 META fault{{action:dut_kill}}：{metas}"
    finally:
        bridge.close()
