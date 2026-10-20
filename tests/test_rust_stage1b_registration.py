"""Stage 1B 註冊、契約與完成度檢查（P4-T16）。

驗證：
1. behavior_changes.yaml 中所有 P4 變更項（SC-18 等）皆推進至 IMPLEMENTED 或 VERIFIED，
   且 P0 凍結之 case_ids 完整對應到 p4_case_map.yaml 的承接測試節點（占位節點不算承接）。
2. 本 Phase 不得新增或刪除錯誤碼（contracts/error_codes.yaml 對齊凍結基準）。
3. crates/sw-cli/src/delegate.rs 的 DELEGATED_GROUPS 清空（20 個頂層群組全部原生）。
4. scripts/rust/check_p4_cases.py --check 證據檢驗：exit 0 通過；exit 2（僅 BLOCKED-OWNER 證據待補）以 pytest.xfail
   明示分流；其餘非零一律 FAIL（不得以 xfail 吞掉）。測試只把 summary 寫入暫存目錄，不改寫 target/p4-evidence。
5. check_p4_cases 拒絕將 docker 證據計入 L2 真板案例（OD-15）。

本檔不含任何無條件 xfail：阻塞原因以 BLOCKED-PREREQ／BLOCKED-OWNER 字樣明列於失敗訊息或 xfail 理由。
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
CHECK_SCRIPT = ROOT / "scripts" / "rust" / "check_p4_cases.py"
P4_CHANGES = [
    "SC-18", "SC-20", "SC-21", "SC-22", "SC-23", "SC-24", "SC-25", "SC-27",
    "DF-05", "DF-07", "DF-10", "DF-11", "DF-12", "DF-13", "DF-16", "BC-05",
]
PLACEHOLDER_PREFIX = "sw-cli::p4_case_placeholders::"
# 需待併入 feature/rust-refactor（P1-T16 資產修正）才能轉 IMPLEMENTED 的變更項。
BLOCKED_PREREQ_CHANGES = {"DF-13": "BLOCKED-PREREQ：需併入 feature/rust-refactor 的 P1-T16 minicom 資產修正後，補 Rust 端資產測試再轉 IMPLEMENTED"}


def test_stage1b_behavior_changes_implemented_with_frozen_case_ids() -> None:
    bc = {
        e["id"]: e
        for e in yaml.safe_load((ROOT / "contracts/behavior_changes.yaml").read_text(encoding="utf-8"))["changes"]
    }
    cmap = {
        c["case_id"]: c
        for c in yaml.safe_load((ROOT / "tests/acceptance/p4_case_map.yaml").read_text(encoding="utf-8"))["cases"]
    }
    failures: list[str] = []
    for cid in P4_CHANGES:
        status = bc[cid]["status"]
        if status not in ("IMPLEMENTED", "VERIFIED"):
            reason = BLOCKED_PREREQ_CHANGES.get(cid, "")
            failures.append(f"{cid}: status={status!r}（需 IMPLEMENTED／VERIFIED）{' ' + reason if reason else ''}")
        for case in bc[cid]["case_ids"]:  # P0 凍結，不得改名
            if case not in cmap:
                failures.append(f"{cid}:{case} 不在 p4_case_map.yaml")
                continue
            real = [t for t in cmap[case]["tests"] if not str(t).startswith(PLACEHOLDER_PREFIX)]
            if not real:
                failures.append(f"{cid}:{case} 只綁占位節點，無承接測試")
    assert not failures, "\n".join(failures)


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


def test_no_delegated_groups_left_in_rust_source() -> None:
    src = (ROOT / "crates/sw-cli/src/delegate.rs").read_text(encoding="utf-8")
    assert "pub const DELEGATED_GROUPS: [&str; 0] = [];" in src


def test_p4_required_cases_have_passing_evidence(tmp_path: Path) -> None:
    """讀取真實證據目錄（target/p4-evidence）；summary 寫入暫存目錄，不改寫官方 summary.json。

    exit 0：全部必要案例證據齊全；exit 2：只剩 BLOCKED-OWNER（擁有者機台）證據待補，明示 xfail 分流；
    其餘非零（含 L1 證據不足、契約失配、工具錯誤）一律 FAIL。
    """
    p = subprocess.run(
        [
            sys.executable,
            str(CHECK_SCRIPT),
            "--check",
            "--summary-out",
            str(tmp_path / "summary.json"),
        ],
        capture_output=True,
        text=True,
    )
    if p.returncode == 2:
        pytest.xfail("BLOCKED-OWNER：僅擁有者機台證據待補（check_p4_cases.py exit 2）")
    assert p.returncode == 0, p.stdout + p.stderr


def test_check_p4_cases_rejects_docker_for_l2(tmp_path: Path) -> None:
    """合成一份 evidence_layer: 'L1.5-docker' 報告綁到 L2 案例 → check_p4_cases.py --check exit 1 且訊息含 not_real_board。

    證據目錄與 summary 皆位於暫存目錄，不觸及官方 target/p4-evidence。
    """
    fake_ev = tmp_path / "evidence"
    fake_ev.mkdir()
    (fake_ev / "rm-live-e2e.json").write_text(
        json.dumps(
            {
                "evidence_layer": "L1.5-docker",
                "records": [
                    {
                        "case_id": "rm-live-e2e",
                        "round": "rust",
                        "platform": "windows",
                        "commit": "0" * 40,
                        "artifact_sha256": "0" * 64,
                        "result": "PASS",
                        "artifact_path": "target/none.bin",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    p = subprocess.run(
        [
            sys.executable,
            str(CHECK_SCRIPT),
            "--check",
            "--evidence-dir",
            str(fake_ev),
            "--l1-results",
            str(tmp_path / "l1"),
            "--summary-out",
            str(tmp_path / "summary.json"),
        ],
        capture_output=True,
        text=True,
    )
    assert p.returncode == 1, p.stdout + p.stderr
    assert "not_real_board" in (p.stdout + p.stderr)
