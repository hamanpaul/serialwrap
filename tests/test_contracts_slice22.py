"""P6 Slice 2.2 契約登記檢查：SC-28／SC-29／SC-31 依 P0 schema、案例凍結、架構與調和表細化、conftest 隔離。"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest
import yaml

from tools.contracts.catalog import load_catalogs

# repo 規則（tests/test_resource_markers.py）：每個 item 恰一個平台 marker 與資源 marker。
pytestmark = [pytest.mark.core_portable]

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEW_SC = {"SC-28": "sc28-console-backend-env", "SC-29": "sc29-com0com-serial-console", "SC-31": "sc31-console-stale-context-rust"}


def _changes() -> dict[str, dict]:
    doc = yaml.safe_load((ROOT / "contracts" / "behavior_changes.yaml").read_text(encoding="utf-8"))
    return {row["id"]: row for row in doc["changes"]}


@pytest.mark.memory_only
def test_catalog_still_loads() -> None:
    load_catalogs(ROOT)                                     # P0 loader：任一結構錯誤即 CatalogError


@pytest.mark.memory_only
def test_new_sc_rows_follow_p0_schema() -> None:
    rows = _changes()
    for sc, case_id in NEW_SC.items():
        r = rows[sc]
        assert r["kind"] == "SC" and r["scope"] == "rust_only" and r["phases"] == ["P6"], sc
        assert r["applies_to"]["python"] is None and r["applies_to"]["rust"] == {"stage": "slice-2.2"}, sc
        assert r["case_ids"] == [case_id], sc
        assert r["compatibility_impact"].strip() and r["approval_conditions"].strip(), sc
    assert "SERIALWRAP_CONSOLE_BACKEND" in rows["SC-28"]["title"]
    assert "windows.serial_console" in rows["SC-29"]["title"] and "OD-01" in rows["SC-29"]["related_decisions"]


@pytest.mark.memory_only
def test_sc30_reserved_for_p7() -> None:
    rows = _changes()
    ids = {i for i in rows if i.startswith("SC-")}
    # SC-34／SC-35 由 P1-OC 登記；SC-36 由 P4 Task 19 登記（OD-15）；SC-37 由 P5 依 OD-21 登記（merge afebff0）；
    # SC-39 由主線依 OD-48 登記（merge 733381e6）；SC-08 依 OD-14 #3 撤回（編號不重用）。
    # P7 Task 0（08 §0.5）登記 SC-30 後：保留號確實由 P7 取用（phases 只有 P7），P6 未搶號。
    assert ids == ({f"SC-{i:02d}" for i in range(1, 32)} | {"SC-34", "SC-35", "SC-36", "SC-37", "SC-39"}) - {"SC-08"}
    assert rows["SC-30"]["phases"] == ["P7"]
    assert rows["SC-30"]["scope"] == "rust_only"


@pytest.mark.memory_only
def test_p1_rows_not_rewritten() -> None:
    rows = _changes()
    for i in ("BC-02", "SC-13", "SC-15", "SC-16"):
        assert rows[i]["status"] in {"APPROVED", "IMPLEMENTED", "VERIFIED"}, i


@pytest.mark.memory_only
def test_reconciliation_and_architecture_refined() -> None:
    recon = (ROOT / "docs/rust-refactor/plans/spec-reconciliation.md").read_text(encoding="utf-8")
    for token in ("| SC-28 |", "| SC-29 |", "| SC-31 |", "| CON-EVT-01 |", "| CON-LINE-01 |"):
        assert token in recon, token
    ses = next(l for l in recon.splitlines() if l.startswith("| SES-16.1 |"))
    assert "input_epoch" in ses
    arch = (ROOT / "docs/rust-refactor/plans/rust-architecture.md").read_text(encoding="utf-8")
    for token in ("input_epoch", "ConsoleMux", "MuxHandle", "human_admission_blocked", "InputStale", "HumanAdmissionClosed"):
        assert token in arch, token
    assert "owner_gen_at_capture" not in arch
    assert "回放時只送 `owner_gen`" not in arch
    p8 = (ROOT / "docs/rust-refactor/plans/09-slice24-transfer-flash.md").read_text(encoding="utf-8")
    assert "clear_all_and_bump_owner_gen" not in p8 and "invalidate_human_input" in p8


@pytest.mark.memory_only
def test_resource_limits_slice22_rows() -> None:
    rows = {r["name"]: r for r in yaml.safe_load((ROOT / "contracts/resource_limits.yaml").read_text(encoding="utf-8"))["limits"]}
    expect = {"console_hub_rx_queue": (256, "reject"), "console_hub_ctrl_queue": (64, "reject"),
              "console_hub_pending_events": (64, "block_sender"), "console_out_ring_display_bytes": (1048576, "drop_oldest"),
              "console_control_max_bytes": (65536, "close_connection"), "console_line_buffer_bytes": (16385, "reject"),
              "human_tx_backlog_bytes": (262144, "clear_all"), "telnet_consoles_per_session": (32, "pause_accept"),
              "com0com_reader_channel": (64, "block_sender")}
    assert {k: (rows[k]["value"], rows[k]["overflow"]) for k in expect} == expect


@pytest.mark.memory_only
def test_conftest_and_harness_isolate_console_backend_env() -> None:
    assert '"SERIALWRAP_CONSOLE_BACKEND"' in (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    assert '"SERIALWRAP_CONSOLE_BACKEND"' in (ROOT / "tests/contract_blackbox/bb_harness.py").read_text(encoding="utf-8")


@pytest.mark.process_rpc
def test_slice22_case_set_frozen_and_mapped() -> None:
    subprocess.run([sys.executable, str(ROOT / "tests/acceptance/regen_lock.py"), "--check"], check=True)
    lock = set(json.loads((ROOT / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))["sets"]["slice_2.2"])
    assert set(NEW_SC.values()) <= lock
    assert {"bc02-deferred-overflow-clear", "bc02-mailbox-stale-input-not-replayed", "sc13-lease-expiry-tick",
            "sc15-console-ring-drop-oldest", "sc16-console-line-reject-log", "f4-interactive-line-cmd-defined",
            "p0-blog-clean", "p1-wal-reset", "p1-wal-fullrun", "cowork-01-agent-bad-cmd", "ha-01-human-then-agent",
            "hk-02-agent-preempt-human", "win-telnet-raw-owner", "l15-multi-source-contention-console"} <= lock
    emap = yaml.safe_load((ROOT / "tests/acceptance/slice22_evidence_map.yaml").read_text(encoding="utf-8"))["cases"]
    assert set(emap) == lock, "evidence map 的分母必須等於凍結的 slice_2.2 集合"
