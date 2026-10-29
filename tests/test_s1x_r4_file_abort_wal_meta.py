"""file.push／file.pull 回 COMMAND_ABORTED_IN_FLIGHT 時寫 WAL META（S1X-a 第 2 輪 SPEC-GAP 1）。

編排者裁決（S1X-a 第 4 輪）：比照 BC-03 一般命令——與 OD-62「file.* 與一般命令用同一套
錯誤碼」一致，一般命令在 BC-03 中止時寫什麼 WAL META，file.* 就寫同樣的（欄位與時機相同）。

一般命令（`SessionManager._map_aborted_result`）：已確認寫出 ≥1 byte 而回
`COMMAND_ABORTED_IN_FLIGHT` 時，於映射當下（回應送出、foreground 操作釋放之前）寫一筆
`dir=META`、`source=system`、`cmd_id=<該命令>`、`payload` 為空的紀錄，meta 為
`{"event":"command_aborted_in_flight","confirmed_tx_bytes":N,"tx_outcome":…}`；零寫出
（`FLUSHED_BY_RECOVERY`）不寫。

file.* 的「命令」是整段傳輸（OD-62：已寫出以整段傳輸計），WAL 上的識別為傳輸的 cmd_id
前綴 `ft-<8hex>`（各子命令 TX 紀錄的 cmd_id 為 `ft-<8hex>-<子命令>`）；
`confirmed_tx_bytes` 為整段傳輸累計，等於這些 TX 紀錄的 payload 長度總和。

修正前：file.* 回 `COMMAND_ABORTED_IN_FLIGHT` 時 WAL 沒有任何 `command_aborted_in_flight` META。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

import pytest

from stage0_fakes import FakePort, wal_records
from test_contracts_wal_state_schema import _wal_validator
from test_s1x_a4_transfer_tx_reject import GENERATION, _chunk_lines, _ready

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

EVENT = "command_aborted_in_flight"
TRANSFER_ID = re.compile(r"ft-[0-9a-f]{8}")


def _src(tmp_path: Path) -> Path:
    src = tmp_path / "src.bin"
    src.write_bytes(bytes(range(100)) * 6)
    return src


def _push(mgr: Any, src: Path, ack_mode: str = "none") -> dict:
    return mgr.file_push(
        "COM0", local_path=str(src), remote_path="/tmp/dst", chunk_size=100,
        ack_mode=ack_mode, chunk_timeout_s=2.0, echo_timeout_s=2.0,
    )


def _pull(mgr: Any, tmp_path: Path) -> dict:
    return mgr.file_pull(
        "COM0", remote_path="/tmp/y", local_path=str(tmp_path / "out.bin"), chunk_timeout_s=2.0,
    )


def _abort_metas(mgr: Any) -> list[dict[str, Any]]:
    return [
        r for r in wal_records(mgr._wal)
        if r["dir"] == "META" and (r.get("meta") or {}).get("event") == EVENT
    ]


def _tx_bytes(mgr: Any, transfer_id: str) -> int:
    return sum(
        len(base64.b64decode(r["payload_b64"]))
        for r in wal_records(mgr._wal)
        if r["dir"] == "TX" and str(r.get("cmd_id") or "").startswith(f"{transfer_id}-")
    )


def _assert_single_abort_meta(mgr: Any, port: FakePort) -> dict[str, Any]:
    metas = _abort_metas(mgr)
    assert len(metas) == 1, metas
    record = metas[0]
    _wal_validator().validate(record)
    assert record["source"] == "system", record
    assert record["payload_b64"] == "" and record["len"] == 0, record
    transfer_id = record["cmd_id"]
    assert isinstance(transfer_id, str) and TRANSFER_ID.fullmatch(transfer_id), record
    meta = record["meta"]
    assert set(meta) == {"event", "confirmed_tx_bytes", "tx_outcome"}, meta
    written = sum(len(chunk) for chunk in port.writes)
    assert meta["confirmed_tx_bytes"] == written == _tx_bytes(mgr, transfer_id), (meta, written)
    assert meta["tx_outcome"] == "partial", meta
    tx_seqs = [
        r["seq"] for r in wal_records(mgr._wal)
        if r["dir"] == "TX" and str(r.get("cmd_id") or "").startswith(f"{transfer_id}-")
    ]
    assert tx_seqs and record["seq"] > max(tx_seqs), "META 須在該傳輸的 TX 紀錄之後"
    return record


@pytest.mark.parametrize("ack_mode", ["none", "echo"])
def test_push_stale_generation_mid_transfer_writes_abort_meta(tmp_path: Path, ack_mode: str) -> None:
    """第 3 個 chunk 後世代前進：回 COMMAND_ABORTED_IN_FLIGHT，WAL 有一筆 command_aborted_in_flight META。"""
    mgr, s, bridge, port, target = _ready(tmp_path)

    def bump(line: str) -> None:
        if "SW_XFER_CHUNK_" in line and len(_chunk_lines(target)) == 3:
            bridge._gate_generation = GENERATION + 1

    target.after_line = bump
    result = _push(mgr, _src(tmp_path), ack_mode)

    assert result["error_code"] == "COMMAND_ABORTED_IN_FLIGHT", result
    _assert_single_abort_meta(mgr, port)
    assert s.foreground_busy is False


def test_push_cancelled_mid_transfer_writes_abort_meta(tmp_path: Path) -> None:
    """傳輸操作 permit 於第 1 個 chunk 後被撤銷：同樣寫一筆 META。"""
    mgr, _s, bridge, port, target = _ready(tmp_path)

    def cancel(line: str) -> None:
        if "SW_XFER_CHUNK_" in line and len(_chunk_lines(target)) == 1:
            permit = bridge._tx_permit_context.operation_permit
            bridge._revoke_ops_locked([permit.op_id])

    target.after_line = cancel
    result = _push(mgr, _src(tmp_path))

    assert result["error_code"] == "COMMAND_ABORTED_IN_FLIGHT", result
    _assert_single_abort_meta(mgr, port)


def test_pull_first_write_partially_confirmed_then_drained_writes_abort_meta(tmp_path: Path) -> None:
    """pull 第一筆寫入只確認 1 byte 即因 drain 中止：META 的 confirmed_tx_bytes 為 1。"""
    from sw_core.serial_port import TxResult

    mgr, _s, _bridge, port, target = _ready(tmp_path)
    target.files["/tmp/y"] = b"hello"
    original_write = port.write

    def first_write_partial(data: bytes, **kwargs):
        if not port.writes:
            port.writes.append(data[:1])
            return TxResult("completed", 1, len(data), "draining")
        return original_write(data, **kwargs)

    port.write = first_write_partial
    result = _pull(mgr, tmp_path)

    assert result["error_code"] == "COMMAND_ABORTED_IN_FLIGHT", result
    record = _assert_single_abort_meta(mgr, port)
    assert record["meta"]["confirmed_tx_bytes"] == 1


def test_file_abort_meta_written_before_foreground_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """時機比照一般命令：META 在映射當下寫入，早於 foreground 操作釋放（回應送出前）。"""
    mgr, _s, bridge, _port, target = _ready(tmp_path)
    seen_at_release: list[int] = []
    original_finish = mgr._finish_foreground_operation

    def finish(session: Any, operation: Any) -> None:
        seen_at_release.append(len(_abort_metas(mgr)))
        original_finish(session, operation)

    monkeypatch.setattr(mgr, "_finish_foreground_operation", finish)

    def bump(line: str) -> None:
        if "SW_XFER_CHUNK_" in line and len(_chunk_lines(target)) == 2:
            bridge._gate_generation = GENERATION + 1

    target.after_line = bump
    result = _push(mgr, _src(tmp_path))

    assert result["error_code"] == "COMMAND_ABORTED_IN_FLIGHT", result
    assert seen_at_release == [1], seen_at_release


def test_file_zero_write_flushed_writes_no_abort_meta(tmp_path: Path) -> None:
    """零寫出回 FLUSHED_BY_RECOVERY 時不寫 META（與一般命令相同）。"""
    mgr, _s, bridge, port, _target = _ready(tmp_path)
    bridge._tx_frozen = True

    result = _push(mgr, _src(tmp_path))

    assert result["error_code"] == "FLUSHED_BY_RECOVERY", result
    assert port.writes == []
    assert _abort_metas(mgr) == []


def test_file_abort_meta_shape_matches_general_command(tmp_path: Path) -> None:
    """欄位比照一般命令：外層鍵、dir、source、meta 鍵集合與一般命令 BC-03 的 META 相同。"""
    mgr, _s, bridge, port, target = _ready(tmp_path)

    def bump(line: str) -> None:
        if "SW_XFER_CHUNK_" in line and len(_chunk_lines(target)) == 1:
            bridge._gate_generation = GENERATION + 1

    target.after_line = bump
    assert _push(mgr, _src(tmp_path))["error_code"] == "COMMAND_ABORTED_IN_FLIGHT"
    file_meta = _assert_single_abort_meta(mgr, port)

    if not hasattr(bridge, "_tx_accounts"):
        bridge._tx_accounts = {}
    bridge._tx_accounts["c1"] = (2, False)
    general = mgr._map_aborted_result("c1", {"ok": False}, bridge=bridge, aborted_by_drain=True)
    assert general["error_code"] == "COMMAND_ABORTED_IN_FLIGHT"
    general_meta = _abort_metas(mgr)[-1]
    assert general_meta["cmd_id"] == "c1"

    assert sorted(file_meta) == sorted(general_meta)
    for key in ("dir", "source", "com", "len", "payload_b64", "loss_flag"):
        assert file_meta[key] == general_meta[key], key
    assert sorted(file_meta["meta"]) == sorted(general_meta["meta"])


def test_contracts_note_file_abort_meta() -> None:
    """契約同步：BC-03 compatibility_impact 與 wal_meta_events 分支皆寫明 file.* 亦寫此 META。"""
    import json

    import yaml

    repo = Path(__file__).resolve().parents[1]
    changes = yaml.safe_load(
        (repo / "contracts" / "behavior_changes.yaml").read_text(encoding="utf-8")
    )["changes"]
    bc03 = next(entry for entry in changes if entry["id"] == "BC-03")
    assert EVENT in bc03["compatibility_impact"], bc03["compatibility_impact"]
    schema = json.loads(
        (repo / "contracts" / "wal_meta_events.schema.json").read_text(encoding="utf-8")
    )
    branch = next(b for b in schema["oneOf"] if b.get("title") == EVENT)
    assert "file.push" in branch["description"] and "file.pull" in branch["description"]
    assert "ft-<8hex>" in branch["x-outer-record"]["cmd_id"]
