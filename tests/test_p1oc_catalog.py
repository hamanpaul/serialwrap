"""P1-OC T1「契約簿記」失敗測試（OD-14，TDD 紅燈階段）。

驗證 `contracts/behavior_changes.yaml` 依 OD-14 登記 SC-34／SC-35、撤回 SC-08、
全數裁決擁有者確認清單 #1～#7（pending 清空），並檢驗 `tools/contracts/catalog.py`
的 `owner_item`／pending `item` 驗證已放寬為「≥1 的整數」（供 P8 SC-33 以 item 8 登記）。
本檔不得在模組頂層 import `tools.contracts.*`，一律以 `_load()` 延遲 import，
讓整個 suite 仍可完成 collection。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = Path(__file__).resolve().parents[1]


def _load():
    """延遲載入 tools.contracts.catalog 模組。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module("tools.contracts.catalog")


def _bc():
    """以 P0 loader 讀取真實 contracts/behavior_changes.yaml，回傳 (changes, confirmations, invariants)。"""
    return _load().load_behavior_changes(ROOT)


def _synth_doc(owner_item: object, pending_items: list[object]) -> dict:
    """回傳最小合成 behavior_changes 文件：一筆 pending_default 的 BC-01 加上指定的 pending 項目。

    文件其餘部分皆合法（六項安全不變量齊備、approval 符合 BC 規則），
    使斷言只受 owner_item／pending item 規則影響。
    """
    cat = _load()
    change = {
        "id": "BC-01",
        "kind": "BC",
        "title": "合成變更",
        "status": "APPROVED",
        "approval": {
            "owner": "hamanpaul",
            "date": "2026-10-16",
            "decision_ref": "OD-04",
            "basis": "owner_decision",
        },
        "owner_confirmation": "pending_default",
        "owner_item": owner_item,
        "default_if_pending": "x",
        "applies_to": {"python": {"min_version": "0.4.0"}, "rust": {"stage": "stage-1a"}},
        "approved_at_revision": 1,
        "spec_refs": ["§1.1"],
        "reconciliation_refs": [],
        "scope": "both",
        "ab_participation": "diff",
        "phases": ["P1"],
        "case_ids": ["synth-bc01-case"],
        "compatibility_impact": "合成",
        "approval_conditions": "合成",
        "related_decisions": [],
    }
    pending = [
        {
            "item": item,
            "topic": "合成主題",
            "refs": ["REF-1"],
            "default": "x",
            "affects": ["BC-01"],
        }
        for item in pending_items
    ]
    invariants = [
        {"id": inv_id, "title": "合成", "spec_ref": "§11.3", "error_codes": [], "case_ids": []}
        for inv_id in cat.SAFETY_INVARIANTS
    ]
    return {
        "schema_version": 1,
        "revision": 1,
        "changes": [change],
        "pending_owner_confirmations": pending,
        "safety_invariants": invariants,
    }


def test_sc34_sc35_registered_with_owner_decision():
    """SC-34（item 1）與 SC-35（item 2）以 OD-14 核准登記，欄位、案例與階段符合計畫。"""
    changes, _, _ = _bc()
    by = {c.id: c for c in changes}
    for cid, item in (("SC-34", 1), ("SC-35", 2)):
        c = by[cid]
        assert c.scope == "both" and c.ab_participation == "diff"
        assert c.owner_confirmation == "confirmed" and c.owner_item == item
        assert c.approval.basis == "owner_decision" and c.approval.decision_ref == "OD-14"
        assert c.approval.owner == "hamanpaul" and c.approval.date == "2026-10-16"
    assert by["SC-34"].case_ids == ("sc34-attach-recover-last-error-field",)
    assert by["SC-35"].case_ids == ("sc35-passthrough-quiet-ready", "sc35-passthrough-command-completion")
    assert list(by["SC-35"].phases) == ["P1", "P5", "P7", "P8"]


def test_sc08_withdrawn():
    """SC-08 依 OD-14 #3 撤回：整筆自登記移除，其案例 ID 亦不再出現。"""
    changes, _, _ = _bc()
    assert "SC-08" not in {c.id for c in changes}
    assert "sc08-uds-perm-0600" not in {cid for c in changes for cid in c.case_ids}


def test_no_pending_confirmations_remain():
    """OD-14 全數裁決後無待確認項目；#1～#7 對應的變更皆為 confirmed 並帶正確 owner_item。"""
    changes, confirmations, _ = _bc()
    assert confirmations == ()
    assert [c.id for c in changes if c.owner_confirmation == "pending_default"] == []
    confirmed = {c.id: c.owner_item for c in changes if c.owner_confirmation == "confirmed"}
    assert confirmed == {"SC-04": 4, "DF-02": 5, "SC-26": 6, "SC-27": 7, "SC-34": 1, "SC-35": 2}
    for cid in ("SC-04", "DF-02", "SC-26", "SC-27"):
        c = next(x for x in changes if x.id == cid)
        assert "OD-14" in c.related_decisions and c.default_if_pending is None


def test_owner_item_above_seven_accepted():
    """owner_item 與 pending item 放寬為 ≥1 的整數：8 合法，不再受 1..7 上限限制。"""
    problems = _load().validate_behavior_changes_doc(_synth_doc(owner_item=8, pending_items=[8]))
    assert problems == []


def test_owner_item_zero_rejected():
    """owner_item 為 0 仍須被拒，且訊息改為「必須為 null 或 ≥1 的整數」。"""
    problems = _load().validate_behavior_changes_doc(_synth_doc(owner_item=0, pending_items=[8]))
    assert any("必須為 null 或 ≥1 的整數" in p for p in problems), problems


def test_pending_item_duplicate_still_rejected():
    """放寬範圍後，pending item 重複仍須被拒（回報 item 重複: 8）。"""
    problems = _load().validate_behavior_changes_doc(_synth_doc(owner_item=8, pending_items=[8, 8]))
    assert any("item 重複: 8" in p for p in problems), problems


def test_file_revision_bumped():
    """檔案頂層 revision 不小於 SC-34／SC-35 的 approved_at_revision，且二者相等（本 Task 遞增 revision）。"""
    cat = _load()
    doc = cat.load_yaml_strict(ROOT / "contracts" / "behavior_changes.yaml")
    changes, _, _ = _bc()
    by = {c.id: c for c in changes}
    assert doc["revision"] >= by["SC-34"].approved_at_revision
    assert by["SC-34"].approved_at_revision == by["SC-35"].approved_at_revision
