"""Slice 2.1 範圍清單與資源上限契約的一致性檢查（不 import sw_core 的 Rust 部分）。"""
from __future__ import annotations

import json
import pathlib
import re

import pytest
import yaml

# 純檔案／YAML 一致性檢查：與平台無關、不啟動行程或 PTY（規則 2：模組層放平台 marker，資源 marker 逐項標示）。
pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCOPE = ROOT / "tests" / "acceptance" / "slice21_scope.yaml"
LIMITS = ROOT / "contracts" / "resource_limits.yaml"

DEFERRED = {
    "session.console_attach": "2.2", "session.console_detach": "2.2", "session.console_list": "2.2",
    "event.rule_set": "2.3", "event.rule_delete": "2.3", "event.rule_list": "2.3", "event.rule_get": "2.3",
    "event.com_enable": "2.3", "event.com_disable": "2.3", "event.com_status": "2.3",
    "event.reset": "2.3", "event.reload": "2.3", "event.tail": "2.3",
    "file.push": "2.4", "file.pull": "2.4",
}


def test_scope_lists_exact_deferred_methods():
    scope = yaml.safe_load(SCOPE.read_text(encoding="utf-8"))
    got = {row["method"]: row["slice"] for row in scope["deferred_methods"]}
    assert got == DEFERRED
    assert scope["deferred_response"] == {"ok": False, "error_code": "METHOD_NOT_FOUND", "keys": ["method", "version"]}


def test_scope_served_methods_cover_rpc_catalog():
    scope = yaml.safe_load(SCOPE.read_text(encoding="utf-8"))
    served = set(scope["served_methods"])
    assert len(served) == 39
    assert served.isdisjoint(DEFERRED)
    assert {"health.ping", "health.status", "daemon.stop", "command.submit", "device.release",
            "device.attach", "session.recover", "log.tail_raw", "wal.range", "mcu.status"} <= served


def test_unsupported_profile_features_are_declared():
    scope = yaml.safe_load(SCOPE.read_text(encoding="utf-8"))
    assert scope["unsupported_profile_features"] == ["post_login_cmd", "console_unlock_env"]
    assert scope["unsupported_profile_behavior"] == {
        "auto_tx": False, "state": "ATTACHED", "last_error": None, "submit_error_code": "SESSION_NOT_READY"}
    assert scope["sentinel_console_input"] == "discard"


def test_resource_limits_contract_values():
    limits = yaml.safe_load(LIMITS.read_text(encoding="utf-8"))
    by_name = {row["name"]: row for row in limits["limits"]}
    expect = {
        "mailbox_control": 256, "mailbox_data_chunks": 1024, "mailbox_data_chunk_bytes": 8192,
        "tx_writer_jobs": 64, "cmd_pending_max": 256, "cmd_history_max": 512,
        "bg_capture_max_count": 64, "bg_capture_max_chars": 4194304, "rx_window_max_chars": 131072,
        "rx_rate_window_entries": 4096, "deferred_input_max_bytes": 262144,
        "rpc_connections": 128, "rpc_request_line_bytes": 65536, "interactive_tombstones": 16,
        "drain_timeout_ms": 3000, "mailbox_stash_max": 16,
        # R1-F8：TX 帳本只保留未結算 op；不得以 drop_oldest／evict 淘汰在途證據
        "tx_ledger_live_ops": 256, "tx_ledger_unresolved_gaps": 256, "tx_op_wal_gaps": 16, "tx_settle_queue": 256,
        # parity-amendments-2026-10-26 #38：撤銷 op tombstone 上限（Python `_TX_ACCOUNT_MAX`）
        "fence_cancelled_ops_max": 512,
    }
    assert {k: by_name[k]["value"] for k in expect} == expect
    assert by_name["fence_cancelled_ops_max"]["overflow"] == "drop_oldest"
    assert {k: by_name[k]["overflow"] for k in ("tx_ledger_live_ops", "tx_ledger_unresolved_gaps", "tx_op_wal_gaps", "tx_settle_queue")} == {
        "tx_ledger_live_ops": "reject_new_and_drain", "tx_ledger_unresolved_gaps": "merge_oldest",
        "tx_op_wal_gaps": "merge_oldest", "tx_settle_queue": "retain_entry"}
    for row in limits["limits"]:
        assert row["overflow"] in {"reject", "block_sender", "drop_oldest", "clear_all", "truncate_head",
                                   "evict_oldest_done", "pause_accept", "close_connection", "timeout", "merge_oldest",
                                   "reject_new_and_drain", "retain_entry"}


EVENTS = {"stop_request", "command_cancel", "lease_expiry", "drain_receipt", "release_request"}


def _limits_doc() -> dict:
    return yaml.safe_load(LIMITS.read_text(encoding="utf-8"))


def _freeze_gaps(doc: dict) -> list[str]:
    """§5.3 凍結證據缺口；空 list 表示獨立審閱紀錄齊備且無未裁決項目（P5 Task 0 雙審修正）。"""
    lat = doc["event_latency"]
    gaps = []
    if lat.get("freeze_status") != "frozen":
        gaps.append(f"freeze_status={lat.get('freeze_status')!r}")
    for key in ("reviewed_by", "reviewed_at", "review_conclusion"):
        if not lat.get(key):
            gaps.append(f"{key} 未附")
    if lat.get("reviewed_by") and lat.get("reviewed_by") == lat.get("owner"):
        gaps.append("reviewed_by 與 owner 相同")
    for row in doc["limits"]:
        decision = row.get("owner_decision")
        if decision and decision.get("status") == "pending_owner_decision":
            gaps.append(f"{row['name']} 待擁有者裁決（{decision.get('id')}）")
    return gaps


def test_event_latency_contract_values():
    """§5.3 內容結構（與凍結狀態無關，恆定執行）。"""
    lat = _limits_doc()["event_latency"]
    assert lat["method_revision"] and lat["owner"]
    assert lat["repeats"] == 3 and lat["samples_per_run"] == 500
    assert lat["gate_rule"] == "every_run_max_le_max_ms"           # p99 只作診斷值
    rows = {r["event"]: r for r in lat["events"]}
    assert set(rows) == EVENTS
    for r in rows.values():
        assert {"accept_point", "complete_point", "max_ms_idle", "max_ms_flood", "load", "l1_bound"} <= set(r)
        assert r["l1_bound"] == "at_most_one_data_item_before_control"
    assert lat["flood_load"] == {"bytes_per_s": 20000, "seconds": 30, "chunk_bytes": 256}


def test_event_latency_od45_l1_gate_and_external_split():
    """OD-45：五事件皆有 L1 結構 gate（nodeid 須在 s21_core.linux 凍結鎖），外部只量三項；口徑改變須換 method_revision。"""
    lat = _limits_doc()["event_latency"]
    assert lat["method_revision"] != lat.get("previous_method_revision") and lat["method_revision"] != "2026-10-15.1"
    rows = {r["event"]: r for r in lat["events"]}
    assert {e for e, r in rows.items() if r.get("measure") == "external"} == {"stop_request", "command_cancel", "release_request"}
    assert {e for e, r in rows.items() if r.get("measure") == "l1_only"} == {"lease_expiry", "drain_receipt"}
    lock = json.loads((ROOT / "tests" / "acceptance" / "l1_required.lock.json").read_text(encoding="utf-8"))
    frozen = set(lock["sets"]["s21_core"]["linux"]["nodeids"])
    for event, r in rows.items():
        assert r.get("l1_gate"), event
        assert set(r["l1_gate"]) <= frozen, (event, set(r["l1_gate"]) - frozen)
        if r["measure"] == "external":
            assert r.get("external_point"), event
    assert lat["external_method"]["tool"] == "scripts/rust/event_latency_measure.py"
    assert lat["external_method"]["includes_connect"] is False
    # 獨立審閱紀錄不得由實作者自填：未附入時三欄皆為 null 且未 frozen
    if lat.get("reviewed_by") is None:
        assert lat["freeze_status"] != "frozen" and lat.get("reviewed_at") is None and lat.get("review_conclusion") is None


def test_event_latency_contract_frozen_before_slice_start():
    """§5.3：開工前凍結並有獨立審閱紀錄。證據不足時 skip（INSUFFICIENT_EVIDENCE），不得 PASS 放行。"""
    gaps = _freeze_gaps(_limits_doc())
    if gaps:
        pytest.skip("INSUFFICIENT_EVIDENCE：§5.3 凍結證據不足（" + "；".join(gaps) + "），不得作為正式門檻引用")


def test_sw_core_has_no_relocated_modules():
    src = ROOT / "crates" / "sw-core" / "src"
    names = {p.stem for p in src.glob("*.rs")}
    assert "state_migrate" not in names and "bench_registry" not in names


CASES = ROOT / "tests" / "acceptance" / "cases.yaml"
LOCK = ROOT / "tests" / "acceptance" / "cases.lock.json"
PLAN06 = ROOT / "docs" / "rust-refactor" / "plans" / "06-slice21-daemon-core.md"
# Task 0b 文件化的 L1.5／Windows 必要案例列舉（11 個 l15-scenario ＋ 7 個 windows-native，共 18 個）。
# 分母以 cases.lock.json 為權威；本清單只作可讀記錄，須與 lock 交集完全相等（見 test_l15_and_windows_required_listed）。
# OD-34（2026-10-28）：l15-delayed-reply 依賴 readiness reprobe（P7-T5），已逐名延至 Slice 2.3（凍結修訂 #1），不再屬 2.1 分母。
L15_WIN_LISTED = [
    "l15-half-prompt", "l15-slow-reader-backpressure", "l15-transport-disconnect",
    "l15-multi-source-contention-cli", "l15-stale-event-delay", "l15-wal-interference", "l15-reject-zero-tx",
    "l15-exclusive-open", "l15-reboot-min-guard", "l15-release-inflight",
    "win-com-open-exclusive", "win-com-txrx-bridge", "win-com-cancel-close", "win-wal-av-rotate",
    "win-wal-av-unlink", "win-wal-av-reader-handle", "win-wal-f12-progress",
]


def _slice_key(slice_id: str) -> tuple[int, ...]:
    return tuple(int(x) for x in slice_id.split("."))


def _after_21(slice_id) -> bool:
    return slice_id is not None and _slice_key(slice_id) > (2, 1)


def test_scope_matches_cases_yaml():
    """範圍清單的跨切片案例與 cases.yaml（daemon_slice > 2.1）一致；regression 以 family 比對，early_evidence 另列且不在分母。"""
    scope = yaml.safe_load(SCOPE.read_text(encoding="utf-8"))
    cases = {c["case_id"]: c for c in yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]}
    early = {row["case_id"] for row in scope["early_evidence_cases"]}
    later = {cid for cid, c in cases.items() if _after_21(c["daemon_slice"])}
    func_items = {i for row in scope["cross_slice_cases"] if row["suite"] == "func-test" for i in row["items"]}
    reg_families = {f for row in scope["cross_slice_cases"] if row["suite"] == "regression" for f in row["items"]}
    assert func_items == {cid for cid in later if cases[cid]["suite"] == "func-test"}
    assert reg_families == {cases[cid]["family"] for cid in later - early if cases[cid]["suite"] == "regression"}
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    # 切片標籤必須與 cases.yaml 的 daemon_slice 一致（不只比對案例名稱）
    for row in scope["cross_slice_cases"]:
        if row["suite"] == "func-test":
            for cid in row["items"]:
                assert cases[cid]["daemon_slice"] == row["slice"], cid
        elif row["suite"] == "regression":
            for cid in later - early:
                if cases[cid]["suite"] == "regression" and cases[cid]["family"] in row["items"]:
                    assert cases[cid]["daemon_slice"] == row["slice"], cid
    for row in scope["early_evidence_cases"]:
        assert _after_21(lock["case_attrs"][row["case_id"]]["daemon_slice"]), row["case_id"]
        assert row["case_id"] not in lock["sets"]["slice_2.1"], row["case_id"]
        assert row["lock_slice"] == lock["case_attrs"][row["case_id"]]["daemon_slice"], row["case_id"]


def test_l15_and_windows_required_listed():
    """lock slice_2.1 中 suite ∈ {l15-scenario, windows-native} 的每個 id 都必須出現在 Task 21 執行清單，
    或 BLOCKED-OWNER 總表的 case_ids 欄（第 3 欄）。分母來源為 lock，不得以硬編清單取代。"""
    cases = {c["case_id"]: c for c in yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]}
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    required = {cid for cid in lock["sets"]["slice_2.1"] if cases[cid]["suite"] in {"l15-scenario", "windows-native"}}
    # 文件化子檢查：列舉清單必須與 lock 的對應子集完全相等（增減都要同步改計畫與本清單）
    assert set(L15_WIN_LISTED) == required
    plan = PLAN06.read_text(encoding="utf-8")
    task21 = plan[plan.index("## Task 21："):plan.index("## Task 22：")]
    bo_case_ids = []
    for line in plan[plan.index("## BLOCKED-OWNER 總表"):].splitlines():
        if line.startswith("| BO-"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            bo_case_ids.append(cells[2])
    bo_text = "\n".join(bo_case_ids)
    missing = sorted(cid for cid in required if cid not in task21 and cid not in bo_text)
    assert not missing, missing
