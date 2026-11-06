"""OD-62：file.push／file.pull 傳輸中 TX 被拒（drain、世代過期、取消）比照 BC-03 映射。

擁有者裁決（OD-62，2026-10-29；審查 A 修正回報 OWNER-BATCH #1）：
- 本次傳輸尚未寫出任何 byte → `FLUSHED_BY_RECOVERY`（未送出，可在 READY 後重送）。
- 本次傳輸已確認寫出 ≥1 byte → `COMMAND_ABORTED_IN_FLIGHT`（不得自動重送）。

「已寫出」以整段傳輸計（probe、chunk、md5、mv 等子命令共用同一個操作 permit），
不是只看被拒的那一筆 send。boot quiet（SC-03 `AUTOBOOT_QUIET`）的案例見
`tests/test_s1x_a4_transfer_tx_reject.py`。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from test_s1x_a4_transfer_tx_reject import GENERATION, _chunk_lines, _ready

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO = Path(__file__).resolve().parents[1]
FILE_METHODS = ("file.push", "file.pull")
OD62_CODES = ("FLUSHED_BY_RECOVERY", "COMMAND_ABORTED_IN_FLIGHT")


def _src(tmp_path: Path) -> Path:
    src = tmp_path / "src.bin"
    src.write_bytes(bytes(range(100)) * 6)
    return src


def _push(mgr, src: Path, ack_mode: str = "none") -> dict:
    return mgr.file_push(
        "COM0", local_path=str(src), remote_path="/tmp/dst", chunk_size=100,
        ack_mode=ack_mode, chunk_timeout_s=2.0, echo_timeout_s=2.0,
    )


def _pull(mgr, tmp_path: Path) -> dict:
    return mgr.file_pull(
        "COM0", remote_path="/tmp/y", local_path=str(tmp_path / "out.bin"), chunk_timeout_s=2.0,
    )


@pytest.mark.parametrize("ack_mode", ["none", "echo"])
def test_push_drain_before_first_tx_returns_flushed(tmp_path: Path, ack_mode: str) -> None:
    """drain 早於第一筆 TX：零寫出 → FLUSHED_BY_RECOVERY，UART 無任何 bytes。"""
    mgr, s, bridge, port, target = _ready(tmp_path)
    bridge._tx_frozen = True

    result = _push(mgr, _src(tmp_path), ack_mode)

    assert result["ok"] is False, result
    assert result["error_code"] == "FLUSHED_BY_RECOVERY", result
    assert port.writes == []
    assert target.lines == []
    assert s.foreground_busy is False


def test_pull_drain_before_first_tx_returns_flushed(tmp_path: Path) -> None:
    """pull 在第一筆 TX 前 drain：零寫出 → FLUSHED_BY_RECOVERY。"""
    mgr, s, bridge, port, target = _ready(tmp_path)
    target.files["/tmp/y"] = b"hello"
    bridge._tx_frozen = True

    result = _pull(mgr, tmp_path)

    assert result["ok"] is False, result
    assert result["error_code"] == "FLUSHED_BY_RECOVERY", result
    assert port.writes == []
    assert not (tmp_path / "out.bin").exists()
    assert s.foreground_busy is False


def test_push_stale_generation_before_first_tx_returns_flushed(tmp_path: Path) -> None:
    """世代已過期（permit 發放後 gate 世代前進）且尚未寫出：FLUSHED_BY_RECOVERY。"""
    mgr, s, bridge, port, _target = _ready(tmp_path)
    bridge._gate_generation = GENERATION + 1

    result = _push(mgr, _src(tmp_path))

    assert result["ok"] is False, result
    assert result["error_code"] == "FLUSHED_BY_RECOVERY", result
    assert port.writes == []
    assert s.foreground_busy is False


@pytest.mark.parametrize("ack_mode", ["none", "echo"])
def test_push_stale_generation_mid_transfer_returns_aborted(tmp_path: Path, ack_mode: str) -> None:
    """第 3 個 chunk 後世代前進：已寫出 → COMMAND_ABORTED_IN_FLIGHT，附進度，不再送 chunk。"""
    mgr, s, bridge, _port, target = _ready(tmp_path)

    def bump(line: str) -> None:
        if "SW_XFER_CHUNK_" in line and len(_chunk_lines(target)) == 3:
            bridge._gate_generation = GENERATION + 1

    target.after_line = bump
    result = _push(mgr, _src(tmp_path), ack_mode)

    assert result["ok"] is False, result
    assert result["error_code"] == "COMMAND_ABORTED_IN_FLIGHT", result
    assert result["chunks_sent"] == 3
    assert result["chunks_total"] == 6
    assert len(_chunk_lines(target)) == 3
    assert s.foreground_busy is False


def test_push_cancelled_mid_transfer_returns_aborted(tmp_path: Path) -> None:
    """傳輸操作 permit 於第 1 個 chunk 後被撤銷（cancelled）：已寫出 → COMMAND_ABORTED_IN_FLIGHT。"""
    mgr, s, bridge, _port, target = _ready(tmp_path)

    def cancel(line: str) -> None:
        if "SW_XFER_CHUNK_" in line and len(_chunk_lines(target)) == 1:
            permit = bridge._tx_permit_context.operation_permit
            bridge._revoke_ops_locked([permit.op_id])

    target.after_line = cancel
    result = _push(mgr, _src(tmp_path))

    assert result["ok"] is False, result
    assert result["error_code"] == "COMMAND_ABORTED_IN_FLIGHT", result
    assert result["chunks_sent"] == 1
    assert len(_chunk_lines(target)) == 1
    assert s.foreground_busy is False


def test_pull_cancelled_before_first_tx_returns_flushed(tmp_path: Path) -> None:
    """pull 的操作 permit 在第一筆 TX 前就被撤銷：零寫出 → FLUSHED_BY_RECOVERY。"""
    mgr, s, bridge, port, target = _ready(tmp_path)
    target.files["/tmp/y"] = b"hello"
    original = bridge.operation_permit

    def revoked_permit(permit):
        bridge._revoke_ops_locked([permit.op_id])
        return original(permit)

    bridge.operation_permit = revoked_permit
    result = _pull(mgr, tmp_path)

    assert result["ok"] is False, result
    assert result["error_code"] == "FLUSHED_BY_RECOVERY", result
    assert port.writes == []
    assert s.foreground_busy is False


def test_od62_codes_registered_for_file_methods() -> None:
    """契約同步：rpc_schema 兩方法的 errors／error_order 與 error_codes.yaml scope 皆含兩碼。"""
    schema = json.loads((REPO / "contracts/rpc_schema.json").read_text(encoding="utf-8"))
    catalog = yaml.safe_load((REPO / "contracts/error_codes.yaml").read_text(encoding="utf-8"))
    by_code = {entry["code"]: entry for entry in catalog["codes"]}
    for method in FILE_METHODS:
        spec = schema["x-methods"][method]
        for code in OD62_CODES:
            assert code in spec["errors"], (method, code)
            assert code in spec["error_order"], (method, code)
    for code in OD62_CODES:
        scope = by_code[code]["scope"]
        assert "rpc" in scope["interfaces"], code
        assert set(FILE_METHODS) <= set(scope["methods"]), code


def test_od62_compatibility_impact_mentions_file_methods() -> None:
    """BC-03 與 SC-04 的 compatibility_impact 須寫明對 0.3.2 file.* 用戶端的變更。"""
    changes = yaml.safe_load(
        (REPO / "contracts/behavior_changes.yaml").read_text(encoding="utf-8")
    )["changes"]
    by_id = {entry["id"]: entry for entry in changes}
    for change_id in ("BC-03", "SC-04"):
        entry = by_id[change_id]
        assert "file.push" in entry["compatibility_impact"], change_id
        assert "file.pull" in entry["compatibility_impact"], change_id
        assert "OD-62" in entry["related_decisions"], change_id


def test_pull_first_write_partially_confirmed_then_drained_returns_aborted(tmp_path: Path) -> None:
    """第一筆寫入只確認 1 byte 即因 drain 中止（sol S1-6485e90-2 重現）：已寫出 → COMMAND_ABORTED_IN_FLIGHT。"""
    from sw_core.serial_port import TxResult

    mgr, s, _bridge, port, target = _ready(tmp_path)
    target.files["/tmp/y"] = b"hello"
    original_write = port.write

    def first_write_partial(data: bytes, **kwargs):
        if not port.writes:
            port.writes.append(data[:1])
            return TxResult("completed", 1, len(data), "draining")
        return original_write(data, **kwargs)

    port.write = first_write_partial
    result = _pull(mgr, tmp_path)

    assert result["ok"] is False, result
    assert result["error_code"] == "COMMAND_ABORTED_IN_FLIGHT", result
    assert s.foreground_busy is False
