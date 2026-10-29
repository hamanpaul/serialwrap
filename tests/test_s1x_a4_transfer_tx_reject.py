"""S1X-a-4：檔案傳輸中 TX 被拒（boot quiet／drain）時 `TxIncompleteError` 不得穿越 RPC 邊界。

Stage 1 出口審查 A 指出：傳輸中目標重開機觸發 `arm_boot_quiet`，之後的 chunk 被
`_tx_reject_reason` 以 `quiet` 拒絕並拋 `TxIncompleteError`；`push_file`／`file_push`／
`file_pull` 都沒有捕捉，例外落到 RPC 通用 handler 回 `EXCEPTION`。

修正後：
- `quiet` → `AUTOBOOT_QUIET`＋`retry_after_s`（SC-03 已核准此碼用於 file.*）。
- 其餘拒絕（drain、世代過期、取消、部分寫出）→ 傳輸自身的 `TRANSFER_TIMEOUT`
  （0.3.2 結束碼，`contracts/rpc_schema.json` file.* 既有錯誤碼），push 附 `chunks_sent`／
  `chunks_total`。

本檔以真 `UARTBridge`＋`FakePort`＋最小 shell 模擬器重現「第 3 個 chunk 後翻 quiet」。
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any, Callable

import pytest

from stage0_fakes import FakePort, make_bridge, make_manager, make_profile

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

BY_ID = "/dev/serial/by-id/X"
GENERATION = 4
PROMPT = "root@t:~# "
PROMPT_REGEX = r"(?m)^root@t:~# "


class _ShellTarget:
    """以 FakePort.on_write 模擬板端 shell：逐 byte 回顯，整行時執行傳輸命令並回 prompt。"""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.lines: list[str] = []
        self._buf = b""
        self.inject: Callable[[bytes], None] = lambda _data: None
        self.after_line: Callable[[str], None] = lambda _line: None

    def on_write(self, data: bytes) -> None:
        self.inject(data.replace(b"\n", b"\r\n"))
        self._buf += data
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace")
            self.lines.append(line)
            out = self._run(line)
            self.inject((out + PROMPT).encode())
            self.after_line(line)

    def _run(self, line: str) -> str:
        if "SW_XFER_DECODER_B64_" in line:
            return "SW_XFER_DECODER_B64_42\r\n"
        if "SW_XFER_ENCODER_B64_" in line:
            return "SW_XFER_ENCODER_B64_42\r\n"
        m = re.match(r"printf '%s' (\S*) \| base64 -d (>>?) (\S+) && ", line)
        if m:
            payload = base64.b64decode(m.group(1))
            path = m.group(3)
            self.files[path] = (self.files.get(path, b"") if m.group(2) == ">>" else b"") + payload
            return "SW_XFER_CHUNK_42\r\n"
        m = re.match(r"md5sum (\S+) && ", line)
        if m:
            digest = hashlib.md5(self.files.get(m.group(1), b"")).hexdigest()
            return f"{digest}  {m.group(1)}\r\nSW_XFER_MD5_42\r\n"
        m = re.match(r"mv (\S+) (\S+) && ", line)
        if m:
            self.files[m.group(2)] = self.files.pop(m.group(1), b"")
            return "SW_XFER_MV_42\r\n"
        m = re.match(r"rm -f (\S+) && ", line)
        if m:
            self.files.pop(m.group(1), None)
            return "SW_XFER_CLEANUP_42\r\n"
        m = re.search(r"&& base64 < (\S+) &&", line)
        if m:
            b64 = base64.b64encode(self.files.get(m.group(1), b"")).decode()
            return f"===SW_XFER_BEGIN===\r\n{b64}\r\n===SW_XFER_END===\r\n"
        return ""


def _ready(tmp_path: Path) -> tuple[Any, Any, Any, FakePort, _ShellTarget]:
    from sw_core.device_watcher import DeviceInfo

    target = _ShellTarget()
    port = FakePort(on_write=target.on_write)
    mgr = make_manager(tmp_path, [make_profile(by_id=BY_ID, prompt_regex=PROMPT_REGEX)])
    s = mgr.get_session("COM0")
    assert s is not None
    bridge, _wal = make_bridge(
        tmp_path,
        port=port,
        wal=mgr._wal,
        on_rx_data=lambda data, wal_seq=None, sid=s.session_id: mgr._on_bridge_rx(
            sid, data, wal_seq
        ),
    )
    mgr._devices[BY_ID] = DeviceInfo(by_id=BY_ID, real_path="fake-uart")
    manager_rx = bridge._on_rx_data

    def inject_rx(data: bytes) -> None:
        bridge._append_rx_text(data)
        if manager_rx is not None:
            manager_rx(data, wal_seq=None)

    target.inject = inject_rx
    with mgr._lock:
        s.bridge = bridge
        s.state = "READY"
        s.bridge_generation = GENERATION
    bridge.set_tx_gate(generation=GENERATION, quiet=False, lease_owner=None)
    return mgr, s, bridge, port, target


def _chunk_lines(target: _ShellTarget) -> list[str]:
    return [line for line in target.lines if "SW_XFER_CHUNK_" in line]


@pytest.mark.parametrize("ack_mode", ["none", "echo"])
def test_push_roundtrip_simulator_sanity(tmp_path: Path, ack_mode: str) -> None:
    """模擬器本身可完成整段 push（確保下列拒絕案例不是模擬器缺陷）。"""
    mgr, _s, _bridge, _port, target = _ready(tmp_path)
    src = tmp_path / "src.bin"
    src.write_bytes(bytes(range(100)) * 6)
    result = mgr.file_push(
        "COM0", local_path=str(src), remote_path="/tmp/dst", chunk_size=100,
        ack_mode=ack_mode, chunk_timeout_s=2.0, echo_timeout_s=2.0,
    )
    assert result["ok"] is True, result
    assert result["chunks"] == 6
    assert target.files["/tmp/dst"] == src.read_bytes()


@pytest.mark.parametrize("ack_mode", ["none", "echo"])
def test_push_quiet_mid_transfer_returns_autoboot_quiet(tmp_path: Path, ack_mode: str) -> None:
    """第 3 個 chunk 執行後目標重開機（arm_boot_quiet）：回 AUTOBOOT_QUIET＋retry_after_s，不拋例外。"""
    mgr, s, _bridge, _port, target = _ready(tmp_path)

    def flip_quiet(line: str) -> None:
        if "SW_XFER_CHUNK_" in line and len(_chunk_lines(target)) == 3:
            s.arm_boot_quiet()

    target.after_line = flip_quiet
    src = tmp_path / "src.bin"
    src.write_bytes(bytes(range(100)) * 6)

    result = mgr.file_push(
        "COM0", local_path=str(src), remote_path="/tmp/dst", chunk_size=100,
        ack_mode=ack_mode, chunk_timeout_s=2.0, echo_timeout_s=2.0,
    )

    assert result["ok"] is False, result
    assert result["error_code"] == "AUTOBOOT_QUIET", result
    assert result["retry_after_s"] > 0
    assert result["chunks_sent"] == 3
    assert result["chunks_total"] == 6
    assert len(_chunk_lines(target)) == 3, "quiet 後不得再送 chunk 進 autoboot 倒數"
    assert s.foreground_busy is False


@pytest.mark.parametrize("ack_mode", ["none", "echo"])
def test_push_drain_mid_transfer_returns_transfer_timeout(tmp_path: Path, ack_mode: str) -> None:
    """傳輸中 bridge 進入 drain：回傳輸自身的 TRANSFER_TIMEOUT（附 chunks_sent），不拋例外。"""
    mgr, s, bridge, _port, target = _ready(tmp_path)

    def freeze(line: str) -> None:
        if "SW_XFER_CHUNK_" in line and len(_chunk_lines(target)) == 2:
            bridge._tx_frozen = True

    target.after_line = freeze
    src = tmp_path / "src.bin"
    src.write_bytes(bytes(range(100)) * 6)

    result = mgr.file_push(
        "COM0", local_path=str(src), remote_path="/tmp/dst", chunk_size=100,
        ack_mode=ack_mode, chunk_timeout_s=2.0, echo_timeout_s=2.0,
    )

    assert result["ok"] is False, result
    assert result["error_code"] == "TRANSFER_TIMEOUT", result
    assert result["chunks_sent"] == 2
    assert result["chunks_total"] == 6
    assert s.foreground_busy is False


def test_pull_quiet_before_read_returns_autoboot_quiet(tmp_path: Path) -> None:
    """pull 在 encoder probe 後目標重開機：回 AUTOBOOT_QUIET＋retry_after_s，不拋例外。"""
    mgr, s, _bridge, _port, target = _ready(tmp_path)
    target.files["/tmp/y"] = b"hello"

    def flip_quiet(line: str) -> None:
        if "SW_XFER_ENCODER_B64_" in line:
            s.arm_boot_quiet()

    target.after_line = flip_quiet
    result = mgr.file_pull(
        "COM0", remote_path="/tmp/y", local_path=str(tmp_path / "out.bin"), chunk_timeout_s=2.0,
    )

    assert result["ok"] is False, result
    assert result["error_code"] == "AUTOBOOT_QUIET", result
    assert result["retry_after_s"] > 0
    assert not any("base64 < " in line for line in target.lines)
    assert s.foreground_busy is False


def test_pull_drain_before_read_returns_transfer_timeout(tmp_path: Path) -> None:
    """pull 中 bridge 進入 drain：回 TRANSFER_TIMEOUT，不拋例外。"""
    mgr, s, bridge, _port, target = _ready(tmp_path)
    target.files["/tmp/y"] = b"hello"

    def freeze(line: str) -> None:
        if "SW_XFER_ENCODER_B64_" in line:
            bridge._tx_frozen = True

    target.after_line = freeze
    result = mgr.file_pull(
        "COM0", remote_path="/tmp/y", local_path=str(tmp_path / "out.bin"), chunk_timeout_s=2.0,
    )

    assert result["ok"] is False, result
    assert result["error_code"] == "TRANSFER_TIMEOUT", result
    assert s.foreground_busy is False
