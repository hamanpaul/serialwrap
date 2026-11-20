"""P6 Task 13：Slice 2.2 案例證據對照表的靜態一致性（07-slice22-console.md Task 13 Step 1）。

只驗對照表與凍結鎖、L1 鎖、程序文件的靜態關係；函式存在不等於已執行且通過——執行證據由
`scripts/rust/s22_gate_check.py`（R5-F3）判定。func-test／scenario 參照不是 L1 nodeid：此處只驗被引用的
case／scenario 檔存在（計畫 Step 1 的 `_fn_exists` 對這類參照會讀不存在的路徑，故分流）。
"""
from __future__ import annotations

import json
import pathlib
import re
import shlex

import pytest
import yaml

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))
EMAP = yaml.safe_load((ROOT / "tests/acceptance/slice22_evidence_map.yaml").read_text(encoding="utf-8"))
INVARIANT_TESTS = {   # §11.3 不變量 → 至少一個具名自動化測試
    "zero_tx_on_reject": "crates/sw-core/tests/session_core_lease.rs::open_state_matrix",
    "no_forbidden_interleave": "tests/contract_blackbox/slice22/test_console_bc02.py::test_normal_suspend_loses_no_keystroke",
    "no_stale_context_replay": "tests/contract_blackbox/slice22/test_console_bc02.py::test_banner_during_suspend_drops_deferred",
    "no_reply_before_handoff": "tests/contract_blackbox/slice22/test_console_pty.py::test_release_closes_consoles_before_reply",
    "no_auto_resend": "crates/sw-core/tests/human_tx_lane.rs::human_raw_partial_not_resent",
    "insufficient_evidence_not_pass": "tests/test_acceptance_slice22.py::test_blocked_owner_cases_are_not_pass",
}


def _fn_exists(ref: str) -> bool:
    path, _, name = ref.partition("::")
    text = (ROOT / path).read_text(encoding="utf-8")
    return re.search(rf"\b(def|fn) {re.escape(name.split('::')[-1])}\b", text) is not None


def _runner_target_exists(ref: str) -> bool:
    """`func-test/runner.py -c <id>`／`func-test/runner.py … --scenarios --case <id>` → 對應 YAML 存在。"""
    argv = shlex.split(ref)
    if not argv or argv[0] != "func-test/runner.py":
        return False
    flag = "-c" if "-c" in argv else "--case"
    cid = argv[argv.index(flag) + 1]
    sub = "scenarios" if "--scenarios" in argv else "cases"
    return (ROOT / "func-test" / sub / f"{cid}.yaml").is_file()


def _is_nodeid_ref(ref: str) -> bool:
    return ref.partition("::")[0].endswith((".py", ".rs"))


def test_map_keys_equal_frozen_slice22_set() -> None:
    assert EMAP["frozen_lock_sha1"]
    assert set(EMAP["cases"]) == set(LOCK["sets"]["slice_2.2"]), "分母以凍結鎖檔為準，不得縮小"


def test_no_pending_entries() -> None:
    assert [k for k, v in EMAP["cases"].items() if v["status"] == "PENDING"] == []


def test_automated_entries_point_to_existing_tests() -> None:
    for cid, v in EMAP["cases"].items():
        if v["status"] == "AUTOMATED":
            assert v["tests"], cid
            for t in v["tests"]:
                assert (_fn_exists(t) if _is_nodeid_ref(t) else _runner_target_exists(t)), (cid, t)


def test_blocked_owner_cases_are_not_pass() -> None:
    for cid, v in EMAP["cases"].items():
        if v["status"] == "BLOCKED-OWNER":
            assert (ROOT / v["procedure"]).is_file() and v["evidence_schema"] and v.get("result") is None, cid
            item = str(v["blocked_item"])
            assert re.fullmatch(r"BO-S2[12]-\d{2}", item), (cid, item)
            runbook = (ROOT / v["procedure"]).read_text(encoding="utf-8")
            assert v["procedure"].startswith("docs/rust-refactor/procedures/") or f"### {item}" in runbook, (cid, item)
    assert set(v["status"] for v in EMAP["cases"].values()) <= {"AUTOMATED", "BLOCKED-OWNER", "NOT_APPLICABLE"}


def test_section_11_3_invariants_have_named_tests() -> None:
    for inv, ref in INVARIANT_TESTS.items():
        assert _fn_exists(ref), inv


def test_lock_only_grew_since_task0() -> None:
    assert set(LOCK["sets"]["slice_2.2"]) >= set(EMAP["cases"]), "鎖檔只能 add-only（regen_lock.py --add-only）"


def test_automated_tests_are_in_frozen_l1_sets() -> None:   # R5-F3：每個 AUTOMATED 測試都必須在某個凍結 L1 集合內
    l1 = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))["sets"]
    frozen = {n for sid, plats in l1.items() if sid.startswith("s22_gate:") for p in plats.values() for n in p["nodeids"]}
    for cid, v in EMAP["cases"].items():
        if v["status"] == "AUTOMATED":
            for t in v["tests"]:
                path, _, name = t.partition("::")
                if not path.endswith((".py", ".rs")):
                    continue        # func-test／scenario 執行紀錄另由 L1.5 報告判定，不是 L1 nodeid
                nid = t if path.endswith(".py") else f"cargo:{path.split('/')[1]}::{pathlib.Path(path).stem}::{name}"
                assert nid in frozen, (cid, t)
