"""Root 驗證：#166 的預算與工具探測仍受 #198 operation admission 保護。"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from sw_core.config import SessionProfile
from sw_core.file_transfer import pull_file, push_file
from sw_core.session_manager import SessionManager
from sw_core.wal import WalWriter
from test_refine_ownership import RecordingBridge
from test_transfer_portability import _LocalShellBridge


def _ready_manager(tmp_path: Path, budget: int) -> tuple[SessionManager, RecordingBridge]:
    profile = SessionProfile(
        profile_name="transfer", com="COM0", act_no=1, alias="transfer+1",
        device_by_id="/dev/serial/by-id/fake", platform="prpl",
        max_console_line_chars=budget,
    )
    manager = SessionManager(
        [profile], WalWriter(wal_dir=str(tmp_path / "wal")),
        on_ready=lambda _sid: None, on_detached=lambda _sid: None,
        state_path=str(tmp_path / "state.json"),
    )
    session = manager.get_session("COM0")
    assert session is not None
    session.state = "READY"
    bridge = RecordingBridge("probe")
    session.bridge = bridge  # type: ignore[assignment]
    session.bridge_generation = 1
    return manager, bridge


def test_profile_budget_rejects_before_tx_and_releases_operation(tmp_path: Path) -> None:
    manager, bridge = _ready_manager(tmp_path, 1)
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    result = manager.file_push("COM0", local_path=str(source), remote_path="/tmp/dest")
    assert result["error_code"] == "CONSOLE_LINE_LIMIT_TOO_SMALL"
    assert bridge.tx == []
    assert manager.get_session("COM0").foreground_busy is False


def test_utf8_path_budget_is_checked_before_probe(tmp_path: Path) -> None:
    manager, bridge = _ready_manager(tmp_path, 505)
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    remote = "/tmp/" + "/".join(["界" * 70] * 3)
    # 各個檔名元件小於 255 bytes，但整條路徑的 UTF-8 bytes 已超出預算；
    # 若錯用字元數，會繼續送出工具探測，而不是在 TX 前拒絕。
    assert len(remote) < 505 < len(remote.encode("utf-8"))
    result = manager.file_push("COM0", local_path=str(source), remote_path=remote)
    assert result["error_code"] == "CONSOLE_LINE_LIMIT_TOO_SMALL"
    assert bridge.tx == []
    assert manager.get_session("COM0").foreground_busy is False


def test_probe_keeps_competing_transfer_out_until_cleanup(tmp_path: Path) -> None:
    manager, bridge = _ready_manager(tmp_path, 505)
    bridge.block_tx = True
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def transfer() -> None:
        try:
            results.append(manager.file_push(
                "COM0", local_path=str(source), remote_path="/tmp/dest", ack_mode="none",
            ))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=transfer)
    worker.start()
    try:
        assert bridge.tx_started.wait(2), "工具探測尚未到達 TX 邊界"
        writes_before = list(bridge.tx)
        result = manager.file_pull("COM0", remote_path="/tmp/other")
        assert result["error_code"] == "SESSION_BUSY"
        assert bridge.tx == writes_before
        assert manager.get_session("COM0").foreground_busy is True
    finally:
        bridge.release_tx.set()
        worker.join(5)
    assert not worker.is_alive()
    assert errors == []
    assert len(results) == 1
    assert results[0]["error_code"] == "TARGET_DECODER_MISSING"
    assert manager.get_session("COM0").foreground_busy is False


@pytest.mark.parametrize("payload", [bytes(range(256)) * 16, b""], ids=["multi-chunk", "empty"])
def test_openssl_roundtrip_with_505_byte_budget(tmp_path: Path, payload: bytes) -> None:
    """把真 shell fallback 與行長限制合測，不靠 fake 解碼器證明分段正確。"""
    bridge = _LocalShellBridge()
    try:
        source = tmp_path / "source.bin"
        destination = tmp_path / "result.bin"
        source.write_bytes(payload)
        remote = str(Path(bridge._tmp.name) / "remote '空白檔'")
        pushed = push_file(
            bridge, str(source), remote, chunk_size=4096,
            max_console_line_chars=505, prompt_regex=r"(?m)^root@target:~#\s",
        )
        assert pushed["ok"], pushed
        if payload:
            assert pushed["chunks"] > 1
        else:
            assert pushed["chunks"] == 1
        pulled = pull_file(
            bridge, remote, str(destination), max_console_line_chars=505,
            prompt_regex=r"(?m)^root@target:~#\s",
        )
        assert pulled["ok"], pulled
        assert destination.read_bytes() == payload
        assert pulled["md5"] == pushed["md5"]
        assert all(len(command.encode("utf-8")) <= 505 for command in bridge.commands)
    finally:
        bridge.close()
