"""Stage 1B 註冊、契約與完成度檢查（P4-T16）。

驗證：
1. behavior_changes.yaml 中所有 P4 變更項（SC-18 等）皆推進至 IMPLEMENTED 或 VERIFIED，
   且 P0 凍結之 case_ids 完整對應到 p4_case_map.yaml 的承接測試節點。
2. 本 Phase 不得新增或刪除錯誤碼（contracts/error_codes.yaml 對齊凍結基準）。
3. crates/sw-cli/src/delegate.rs 的 DELEGATED_GROUPS 清空（20 個頂層群組全部原生）。
4. scripts/rust/check_p4_cases.py --check 證據檢驗通過。
5. check_p4_cases 拒絕將 docker 證據計入 L2 真板案例（OD-15）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.core_portable,
    pytest.mark.process_rpc,
]

ROOT = Path(__file__).resolve().parents[1]
P4_CHANGES = [
    "SC-18", "SC-20", "SC-21", "SC-22", "SC-23", "SC-24", "SC-25", "SC-27",
    "DF-05", "DF-07", "DF-10", "DF-11", "DF-12", "DF-13", "DF-16", "BC-05",
]


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：部分 P4 行為變更尚未改為 IMPLEMENTED/VERIFIED")
def test_stage1b_behavior_changes_implemented_with_frozen_case_ids() -> None:
    bc = {
        e["id"]: e
        for e in yaml.safe_load((ROOT / "contracts/behavior_changes.yaml").read_text(encoding="utf-8"))["changes"]
    }
    cmap = {
        c["case_id"]: c
        for c in yaml.safe_load((ROOT / "tests/acceptance/p4_case_map.yaml").read_text(encoding="utf-8"))["cases"]
    }
    for cid in P4_CHANGES:
        assert bc[cid]["status"] in ("IMPLEMENTED", "VERIFIED"), cid  # P0 CHANGE_STATUS 大寫
        for case in bc[cid]["case_ids"]:  # P0 凍結，不得改名
            assert case in cmap and cmap[case]["tests"], f"{cid}:{case} 無承接測試"


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：p4_required.lock.json 尚未凍結或錯誤碼集合未對齊")
def test_stage1b_adds_no_error_codes() -> None:
    codes = {
        e["code"]
        for e in yaml.safe_load((ROOT / "contracts/error_codes.yaml").read_text(encoding="utf-8"))["codes"]
    }
    for forbidden in [
        "DAEMON_STILL_RUNNING",
        "ROLLBACK_UNAVAILABLE",
        "UPDATE_BUSY",
        "UPDATE_VERIFY_FAILED",
        "UPDATE_START_FAILED",
    ]:
        assert forbidden not in codes
    lock_path = ROOT / "tests/acceptance/p4_required.lock.json"
    assert lock_path.exists(), "p4_required.lock.json 尚未凍結"
    base = set(json.loads(lock_path.read_text(encoding="utf-8"))["error_codes_at_freeze"])
    assert codes == base, "本 Phase 不得新增或刪除錯誤碼（§3.1、§9.2）"


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：crates/sw-cli/src/delegate.rs 仍有委派群組殘留")
def test_no_delegated_groups_left_in_rust_source() -> None:
    src = (ROOT / "crates/sw-cli/src/delegate.rs").read_text(encoding="utf-8")
    assert "pub const DELEGATED_GROUPS: [&str; 0] = [];" in src


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：check_p4_cases.py --check 尚未全綠（缺鎖或證據待補）")
def test_p4_required_cases_have_passing_evidence() -> None:
    p = subprocess.run(
        [sys.executable, str(ROOT / "scripts/rust/check_p4_cases.py"), "--check"],
        capture_output=True,
        text=True,
    )
    # exit 2 為 BLOCKED-OWNER 證據待補；exit 0 為全過
    assert p.returncode == 0, p.stdout + p.stderr


@pytest.mark.xfail(strict=True, reason="P4-T16 紅燈：check_p4_cases 尚未支援 docker 報告在 L2 拒絕的檢查")
def test_check_p4_cases_rejects_docker_for_l2(tmp_path: Path) -> None:
    """合成一份 evidence_layer: 'L1.5-docker' 報告綁到 L2 案例 → check_p4_cases.py --check exit 1 且訊息含 not_real_board。"""
    check_script = ROOT / "scripts/rust/check_p4_cases.py"
    fake_ev = tmp_path / "evidence"
    fake_ev.mkdir()

    p = subprocess.run(
        [sys.executable, str(check_script), "--check", "--evidence-dir", str(fake_ev)],
        capture_output=True,
        text=True,
    )
    assert p.returncode == 1, p.stdout + p.stderr
    assert "not_real_board" in (p.stdout + p.stderr)
