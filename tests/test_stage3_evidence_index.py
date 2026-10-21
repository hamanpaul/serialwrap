"""Stage 0～3 出口證據索引的紅燈測試。"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import fakes_od09 as fakes
import fakes_evidence

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _evidence_api():
    """延遲載入本 Task 模組，保持未實作階段可完成全套 collection。"""
    from tools.stage3 import evidence_index

    return evidence_index


def _ab_manifest_api():
    from tools.stage3 import ab_manifest

    return ab_manifest


def _harness_error_type():
    from tools.stage3.reason_codes import HarnessError

    return HarnessError


def _jsonio_api():
    from tools.stage3 import jsonio

    return jsonio


def _status(idx: Path, root: Path, cat: dict, item_id: str, cases: dict | None = None):
    evidence_index = _evidence_api()
    return evidence_index.item_status(
        evidence_index.load_index(idx),
        cat[item_id],
        evidence_root=root,
        cases=cases or {},
    )


def _empty_index(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "index.json"
    path.write_text(
        json.dumps(
            {"entries": [], "schema_version": 2, "stage_targets": {}, "target_history": []},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _set_target(api, idx: Path, *, stage: str, platform: str, bundle: Path, bundle_dir: bool = False) -> None:
    api.set_target(
        idx,
        stage=stage,
        platform=platform,
        release_manifest=bundle / "release-manifest.json",
        contract_revision="c" * 40,
        tool_revision="t" * 40,
        case_list_sha256="0" * 64,
        set_by="test",
        now=fakes.ts(30, 0),
        bundle_dir=bundle if bundle_dir else None,
    )


def _history_target(row: dict) -> dict:
    """接受 history 直接存 target 或以 target 欄包住的兩種介面表示。"""
    if "target" in row:
        return row["target"]
    return {key: value for key, value in row.items() if key not in {"stage", "platform"}}


def _reuse_fixture(
    root: Path,
    *,
    previous_result: str = "PASS",
    previous_tree: str | None = None,
    previous_tree_none: bool = False,
    current_tree: str | None = None,
    previous_workload: str | None = "w" * 64,
    current_workload: str | None = "w" * 64,
    add_previous: bool = True,
    delete_previous_file: bool = False,
):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(root, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    target = evidence_index.load_index(idx)["stage_targets"]["3"]["linux"]
    previous_bundle = fakes_evidence.make_bundle(
        root / "bundles" / "stage-2.4" / "linux",
        stage="2.4",
        target="x86_64-unknown-linux-gnu",
    )
    evidence_index.set_target(
        idx,
        stage="2.4",
        platform="linux",
        release_manifest=previous_bundle / "release-manifest.json",
        contract_revision="c" * 40,
        tool_revision="t" * 40,
        case_list_sha256="0" * 64,
        set_by="test",
        now=fakes.ts(30, 0),
    )
    previous_target = evidence_index.load_index(idx)["stage_targets"]["2.4"]["linux"]
    previous = fakes_evidence.entry(
        root,
        "S24-L3-LRMIXED-48H-LINUX",
        previous_result,
        platform="linux",
        n=1,
        artifact_id="a" * 64,
        bundle_tree_sha256=previous_target.get("bundle_tree_sha256"),
        runtime_tree_sha256=(
            None
            if previous_tree_none
            else previous_tree if previous_tree is not None else target["runtime_tree_sha256"]
        ),
        workload_sha256=previous_workload,
    )
    if add_previous:
        evidence_index.add_entry(idx, previous, catalog=cat)
    if delete_previous_file and previous.files:
        (root / previous.files[0].path).unlink()
    current = fakes_evidence.entry(
        root,
        "S3-L3-AB-48H-LINUX",
        "PASS",
        platform="linux",
        n=2,
        reuse_of=previous.entry_id,
        runtime_tree_sha256=current_tree if current_tree is not None else target["runtime_tree_sha256"],
        workload_sha256=current_workload,
    )
    evidence_index.add_entry(idx, current, catalog=cat)
    return idx, cat, previous, current


def test_fail_is_sticky_for_same_artifact(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "FAIL", platform="linux", n=1),
        catalog=cat,
    )
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux", n=2),
        catalog=cat,
    )
    assert _status(idx, tmp_path, cat, "S3-ROLLBACK-LINUX")[0] == "FAIL"


def test_both_platform_item_needs_both_targets_and_entries(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S3-INCIDENTS-CLOSED", "PASS", platform="linux"),
        catalog=cat,
    )
    assert _status(idx, tmp_path, cat, "S3-INCIDENTS-CLOSED")[0] == "MISSING"
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S3-INCIDENTS-CLOSED", "PASS", platform="windows"),
        catalog=cat,
    )
    assert _status(idx, tmp_path, cat, "S3-INCIDENTS-CLOSED")[0] == "SATISFIED"


def test_contract_change_makes_pass_stale(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="d" * 40)
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(
            tmp_path,
            "S3-ROLLBACK-LINUX",
            "PASS",
            platform="linux",
            contract_revision="c" * 40,
        ),
        catalog=cat,
    )
    assert _status(idx, tmp_path, cat, "S3-ROLLBACK-LINUX")[0] == "STALE"


def test_newer_canary_report_revokes_earlier_pass(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    old = fakes_evidence.entry(
        tmp_path,
        "S3-CANARY-LINUX",
        "PASS",
        platform="linux",
        n=1,
        ledger_through=fakes.ts(20, 0),
        ledger_records=5000,
    )
    evidence_index.add_entry(idx, old, catalog=cat)
    assert _status(idx, tmp_path, cat, "S3-CANARY-LINUX")[0] == "SATISFIED"
    new = fakes_evidence.entry(
        tmp_path,
        "S3-CANARY-LINUX",
        "INSUFFICIENT",
        platform="linux",
        n=2,
        ledger_through=fakes.ts(21, 0),
        ledger_records=5002,
    )
    evidence_index.add_entry(idx, new, catalog=cat)
    status, reasons = _status(idx, tmp_path, cat, "S3-CANARY-LINUX")
    assert status == "MISSING"
    assert any(
        reason.startswith(f"linux: superseded: latest canary report {new.entry_id} is INSUFFICIENT")
        and old.entry_id in reason
        for reason in reasons
    )
    st = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=cat, evidence_root=tmp_path, cases={}
    )
    assert st["items"]["S3-CANARY-LINUX"]["status"] == "MISSING"
    assert st["items"]["S3-CANARY-LINUX"]["satisfying_entries"] == []
    assert st["result"] == "BLOCKED"
    closed = fakes_evidence.entry(
        tmp_path,
        "S3-CANARY-LINUX",
        "PASS",
        platform="linux",
        n=3,
        ledger_through=fakes.ts(22, 0),
        ledger_records=5003,
    )
    evidence_index.add_entry(idx, closed, catalog=cat)
    st = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=cat, evidence_root=tmp_path, cases={}
    )
    assert st["items"]["S3-CANARY-LINUX"]["status"] == "SATISFIED"
    assert st["items"]["S3-CANARY-LINUX"]["satisfying_entries"] == [closed.entry_id]


def test_canary_current_report_ordered_by_ledger_not_ingest_time(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(
            tmp_path,
            "S3-INCIDENTS-CLOSED",
            "INSUFFICIENT",
            platform="linux",
            n=1,
            ledger_through=fakes.ts(21, 0),
            ledger_records=5002,
        ),
        catalog=cat,
    )
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(
            tmp_path,
            "S3-INCIDENTS-CLOSED",
            "PASS",
            platform="linux",
            n=5,
            ledger_through=fakes.ts(20, 0),
            ledger_records=5000,
        ),
        catalog=cat,
    )
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S3-INCIDENTS-CLOSED", "PASS", platform="windows"),
        catalog=cat,
    )
    status, reasons = _status(idx, tmp_path, cat, "S3-INCIDENTS-CLOSED")
    assert status == "MISSING"
    assert any(reason.startswith("linux: superseded: latest canary report ") for reason in reasons)
    assert not any(reason.startswith("windows: ") for reason in reasons)


def test_sideloaded_file_retarget_cannot_reuse_pass(tmp_path):
    evidence_index = _evidence_api()
    ab_manifest = _ab_manifest_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    bundle = tmp_path / "bundles" / "linux"
    doc = json.loads((bundle / "release-manifest.json").read_text(encoding="utf-8"))
    old = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux", n=1)
    target = evidence_index.load_index(idx)["stage_targets"]["3"]["linux"]
    assert old.bundle_tree_sha256 == target["bundle_tree_sha256"] == ab_manifest.bundle_tree_sha256(bundle)
    assert old.runtime_tree_sha256 == target["runtime_tree_sha256"] == ab_manifest.runtime_tree_sha256(bundle)
    evidence_index.add_entry(idx, old, catalog=cat)
    assert _status(idx, tmp_path, cat, "S3-ROLLBACK-LINUX")[0] == "SATISFIED"
    (bundle / "bin" / "version.dll").write_bytes(b"MZ")
    assert ab_manifest.verify_bundle(bundle, doc, label="linux") == []
    before = idx.read_bytes()
    with pytest.raises(HarnessError) as ei:
        evidence_index.set_target(
            idx,
            stage="3",
            platform="linux",
            release_manifest=bundle / "release-manifest.json",
            contract_revision="c" * 40,
            tool_revision="t" * 40,
            case_list_sha256="0" * 64,
            set_by="t",
            now=fakes.ts(30, 0),
            bundle_dir=bundle,
        )
    assert ei.value.code == "HARNESS_ARTIFACT_MISMATCH"
    assert "bundle tree changed under same manifest: linux" in str(ei.value)
    assert idx.read_bytes() == before
    raw = json.loads(idx.read_text(encoding="utf-8"))
    raw["stage_targets"]["3"]["linux"]["bundle_tree_sha256"] = ab_manifest.bundle_tree_sha256(bundle)
    raw["stage_targets"]["3"]["linux"]["runtime_tree_sha256"] = ab_manifest.runtime_tree_sha256(bundle)
    idx.write_text(json.dumps(raw), encoding="utf-8")
    status, reasons = _status(idx, tmp_path, cat, "S3-ROLLBACK-LINUX")
    assert status == "STALE"
    assert f"linux: bundle tree differs from target: {old.entry_id}" in reasons
    st = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=cat, evidence_root=tmp_path, cases={}
    )
    assert st["items"]["S3-ROLLBACK-LINUX"]["satisfying_entries"] == []
    assert st["result"] != "PASS"


def test_non_cumulative_pass_not_superseded(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    passed = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux", n=1)
    evidence_index.add_entry(idx, passed, catalog=cat)
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "INSUFFICIENT", platform="linux", n=2),
        catalog=cat,
    )
    status = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=cat, evidence_root=tmp_path, cases={}
    )
    assert status["items"]["S3-ROLLBACK-LINUX"]["status"] == "SATISFIED"
    assert status["items"]["S3-ROLLBACK-LINUX"]["satisfying_entries"] == [passed.entry_id]


def test_canary_entry_requires_ledger_version(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    invalid_entries = (
        fakes_evidence.entry(
            tmp_path, "S3-CANARY-LINUX", "PASS", platform="linux", n=1, ledger_through=None
        ),
        fakes_evidence.entry(
            tmp_path, "S3-CANARY-LINUX", "PASS", platform="linux", n=2, ledger_records=None
        ),
        fakes_evidence.entry(
            tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux", n=3, ledger_records=1
        ),
    )
    for entry in invalid_entries:
        before = idx.read_bytes()
        with pytest.raises(HarnessError) as ei:
            evidence_index.add_entry(idx, entry, catalog=cat)
        assert ei.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"
        assert idx.read_bytes() == before


def test_canary_fail_still_sticky_under_latest_rule(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(
            tmp_path,
            "S3-CANARY-WINDOWS",
            "FAIL",
            platform="windows",
            n=1,
            ledger_through=fakes.ts(10, 0),
            ledger_records=10,
        ),
        catalog=cat,
    )
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(
            tmp_path,
            "S3-CANARY-WINDOWS",
            "PASS",
            platform="windows",
            n=2,
            ledger_through=fakes.ts(11, 0),
            ledger_records=20,
        ),
        catalog=cat,
    )
    assert _status(idx, tmp_path, cat, "S3-CANARY-WINDOWS")[0] == "FAIL"


def test_unknown_item_rejected(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    with pytest.raises(HarnessError) as ei:
        evidence_index.add_entry(
            idx, fakes_evidence.entry(tmp_path, "S9-X", "PASS", platform="linux"), catalog=cat
        )
    assert ei.value.code == "HARNESS_EVIDENCE_UNKNOWN_ITEM"


def test_duplicate_entry_rejected(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    entry = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux")
    evidence_index.add_entry(idx, entry, catalog=cat)
    with pytest.raises(HarnessError) as ei:
        evidence_index.add_entry(idx, entry, catalog=cat)
    assert ei.value.code == "HARNESS_EVIDENCE_DUPLICATE"


def test_pass_without_files_rejected(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    with pytest.raises(HarnessError) as ei:
        evidence_index.add_entry(
            idx,
            fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux", files=()),
            catalog=cat,
        )
    assert ei.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"


def test_manual_pass_rejected_for_tool_items(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    manual_tool_entry = fakes_evidence.entry(
        tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux", producer="manual"
    )
    with pytest.raises(HarnessError) as ei:
        evidence_index.add_entry(idx, manual_tool_entry, catalog=cat)
    assert ei.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"
    manual_procedure_entry = fakes_evidence.entry(
        tmp_path, "S24-P2-1-MCU-FLASH", "PASS", platform="linux", producer="manual", n=1
    )
    evidence_index.add_entry(idx, manual_procedure_entry, catalog=cat)
    assert manual_procedure_entry.entry_id in {
        row["entry_id"] for row in evidence_index.load_index(idx)["entries"]
    }


def test_wrong_platform_rejected(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    with pytest.raises(HarnessError) as ei:
        evidence_index.add_entry(
            idx,
            fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="windows"),
            catalog=cat,
        )
    assert ei.value.code == "HARNESS_EVIDENCE_INGEST_INVALID"


def test_skip_is_not_pass(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "SKIP", platform="linux"),
        catalog=cat,
    )
    assert _status(idx, tmp_path, cat, "S3-ROLLBACK-LINUX")[0] == "MISSING"


def test_tool_revision_change_stale(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(
            tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux", tool_revision="z" * 40
        ),
        catalog=cat,
    )
    assert _status(idx, tmp_path, cat, "S3-ROLLBACK-LINUX")[0] == "STALE"


def test_case_list_change_stale(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(
            tmp_path,
            "S3-L2-FULL-AB-LINUX",
            "PASS",
            platform="linux",
            case_list_sha256="9" * 64,
        ),
        catalog=cat,
    )
    assert _status(idx, tmp_path, cat, "S3-L2-FULL-AB-LINUX")[0] == "STALE"


def test_xfail_requires_unexpired_sentinel(tmp_path):
    evidence_index = _evidence_api()
    cat = evidence_index.load_exit_catalog()
    future_cases = {
        "fixture-xfail": {
            "expected": "XFAIL",
            "sentinel": {"issue": "#1", "signature": "fixture", "expires": fakes.ts(11, 0)},
        }
    }
    future_root = tmp_path / "future"
    future_idx = fakes_evidence.evidence_index(future_root, stage="3", contract="c" * 40)
    evidence_index.add_entry(
        future_idx,
        fakes_evidence.entry(
            future_root,
            "S3-ROLLBACK-LINUX",
            "XFAIL",
            platform="linux",
            xfail_cases=("fixture-xfail",),
        ),
        catalog=cat,
    )
    assert _status(future_idx, future_root, cat, "S3-ROLLBACK-LINUX", future_cases)[0] == "SATISFIED"

    expired_root = tmp_path / "expired"
    expired_idx = fakes_evidence.evidence_index(expired_root, stage="3", contract="c" * 40)
    evidence_index.add_entry(
        expired_idx,
        fakes_evidence.entry(
            expired_root,
            "S3-ROLLBACK-LINUX",
            "XFAIL",
            platform="linux",
            xfail_cases=("fixture-xfail",),
        ),
        catalog=cat,
    )
    expired_cases = {
        "fixture-xfail": {
            "expected": "XFAIL",
            "sentinel": {"issue": "#1", "signature": "fixture", "expires": fakes.ts(9, 0)},
        }
    }
    assert _status(expired_idx, expired_root, cat, "S3-ROLLBACK-LINUX", expired_cases)[0] == "MISSING"

    pass_expected_root = tmp_path / "pass-expected"
    pass_expected_idx = fakes_evidence.evidence_index(
        pass_expected_root, stage="3", contract="c" * 40
    )
    evidence_index.add_entry(
        pass_expected_idx,
        fakes_evidence.entry(
            pass_expected_root,
            "S3-ROLLBACK-LINUX",
            "XFAIL",
            platform="linux",
            xfail_cases=("f4-background-result-tail-consistent",),
        ),
        catalog=cat,
    )
    pass_expected_cases = {
        "f4-background-result-tail-consistent": {
            "expected": "PASS",
            "sentinel": {"issue": "#1", "signature": "fixture", "expires": fakes.ts(11, 0)},
        }
    }
    assert (
        _status(
            pass_expected_idx,
            pass_expected_root,
            cat,
            "S3-ROLLBACK-LINUX",
            pass_expected_cases,
        )[0]
        == "MISSING"
    )


def test_hash_mismatch_detected(tmp_path):
    evidence_index = _evidence_api()
    cat = evidence_index.load_exit_catalog()
    changed_root = tmp_path / "changed"
    changed_idx = fakes_evidence.evidence_index(changed_root, stage="3", contract="c" * 40)
    changed = fakes_evidence.entry(
        changed_root, "S3-ROLLBACK-LINUX", "PASS", platform="linux", n=1
    )
    evidence_index.add_entry(changed_idx, changed, catalog=cat)
    changed_path = changed_root / changed.files[0].path
    changed_path.write_text("tampered\n", encoding="utf-8")
    assert _status(changed_idx, changed_root, cat, "S3-ROLLBACK-LINUX")[0] == "HASH_MISMATCH"

    deleted_root = tmp_path / "deleted"
    deleted_idx = fakes_evidence.evidence_index(deleted_root, stage="3", contract="c" * 40)
    deleted = fakes_evidence.entry(
        deleted_root, "S3-ROLLBACK-LINUX", "PASS", platform="linux", n=1
    )
    evidence_index.add_entry(deleted_idx, deleted, catalog=cat)
    (deleted_root / deleted.files[0].path).unlink()
    assert _status(deleted_idx, deleted_root, cat, "S3-ROLLBACK-LINUX")[0] == "HASH_MISMATCH"


def test_reuse_by_runtime_digest(tmp_path):
    idx, cat, _previous, _current = _reuse_fixture(tmp_path / "same")
    assert _status(idx, tmp_path / "same", cat, "S3-L3-AB-48H-LINUX")[0] == "SATISFIED"

    stale_idx, stale_cat, _previous, _current = _reuse_fixture(
        tmp_path / "workload-drift", previous_workload="x" * 64, current_workload="w" * 64
    )
    assert _status(stale_idx, tmp_path / "workload-drift", stale_cat, "S3-L3-AB-48H-LINUX")[0] == "STALE"


def test_reuse_requires_runtime_tree_match(tmp_path):
    ab_manifest = _ab_manifest_api()
    dirty_source = tmp_path / "dirty-source"
    dirty_bundle = tmp_path / "bundle-with-sidecar"
    fakes_evidence.make_bundle(dirty_bundle, stage="3", target="x86_64-unknown-linux-gnu")
    (dirty_bundle / "bin" / "version.dll").write_bytes(b"MZ")
    changed_runtime_tree = ab_manifest.runtime_tree_sha256(dirty_bundle)
    idx, cat, _previous, _current = _reuse_fixture(
        dirty_source, previous_tree=changed_runtime_tree
    )
    status, reasons = _status(idx, dirty_source, cat, "S3-L3-AB-48H-LINUX")
    assert status == "STALE"
    assert "linux: reuse identity differs: runtime_tree_sha256" in reasons

    none_source = tmp_path / "none-source"
    none_idx, none_cat, _previous, _current = _reuse_fixture(
        none_source, previous_tree_none=True
    )
    # S24 來源 target 未記錄 runtime tree 時，不得當作相符的跨 Stage 證據。
    status, reasons = _status(none_idx, none_source, none_cat, "S3-L3-AB-48H-LINUX")
    assert status == "STALE"
    assert "linux: reuse identity differs: runtime_tree_sha256" in reasons


def test_entry_tree_none_against_tree_target_stale(tmp_path):
    evidence_index = _evidence_api()
    tree_root = tmp_path / "tree-target"
    tree_idx = fakes_evidence.evidence_index(tree_root, stage="3", contract="c" * 40)
    tree_cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(
        tree_idx,
        fakes_evidence.entry(
            tree_root,
            "S3-ROLLBACK-LINUX",
            "PASS",
            platform="linux",
            bundle_tree_sha256=None,
            runtime_tree_sha256=None,
        ),
        catalog=tree_cat,
    )
    assert _status(tree_idx, tree_root, tree_cat, "S3-ROLLBACK-LINUX")[0] == "STALE"

    runtime_root = tmp_path / "runtime-drift"
    runtime_idx = fakes_evidence.evidence_index(runtime_root, stage="3", contract="c" * 40)
    runtime_cat = evidence_index.load_exit_catalog()
    target = evidence_index.load_index(runtime_idx)["stage_targets"]["3"]["linux"]
    evidence_index.add_entry(
        runtime_idx,
        fakes_evidence.entry(
            runtime_root,
            "S3-ROLLBACK-LINUX",
            "PASS",
            platform="linux",
            runtime_tree_sha256="e" * 64,
        ),
        catalog=runtime_cat,
    )
    assert target["runtime_tree_sha256"] != "e" * 64
    assert _status(runtime_idx, runtime_root, runtime_cat, "S3-ROLLBACK-LINUX")[0] == "STALE"

    no_tree_root = tmp_path / "no-tree-target"
    no_tree_idx = fakes_evidence.evidence_index(
        no_tree_root, stage="2.4", contract="c" * 40, platforms=("linux",)
    )
    no_tree_cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(
        no_tree_idx,
        fakes_evidence.entry(
            no_tree_root,
            "S24-L3-LRMIXED-48H-LINUX",
            "PASS",
            platform="linux",
            bundle_tree_sha256=None,
            runtime_tree_sha256=None,
        ),
        catalog=no_tree_cat,
    )
    assert _status(no_tree_idx, no_tree_root, no_tree_cat, "S24-L3-LRMIXED-48H-LINUX")[0] == "SATISFIED"


def test_reuse_of_failed_or_missing_entry_stale(tmp_path):
    failed_idx, failed_cat, _previous, _current = _reuse_fixture(
        tmp_path / "failed", previous_result="FAIL"
    )
    assert _status(failed_idx, tmp_path / "failed", failed_cat, "S3-L3-AB-48H-LINUX")[0] == "STALE"

    missing_root = tmp_path / "missing"
    evidence_index = _evidence_api()
    missing_idx = fakes_evidence.evidence_index(missing_root, stage="3", contract="c" * 40)
    missing_cat = evidence_index.load_exit_catalog()
    missing_current = fakes_evidence.entry(
        missing_root,
        "S3-L3-AB-48H-LINUX",
        "PASS",
        platform="linux",
        reuse_of="absent-entry",
    )
    evidence_index.add_entry(missing_idx, missing_current, catalog=missing_cat)
    assert _status(missing_idx, missing_root, missing_cat, "S3-L3-AB-48H-LINUX")[0] == "STALE"

    deleted_idx, deleted_cat, _previous, _current = _reuse_fixture(
        tmp_path / "deleted-source", delete_previous_file=True
    )
    assert _status(
        deleted_idx, tmp_path / "deleted-source", deleted_cat, "S3-L3-AB-48H-LINUX"
    )[0] == "STALE"


def test_reuse_cycle_stale(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    a = fakes_evidence.entry(
        tmp_path, "S3-L3-AB-48H-LINUX", "PASS", platform="linux", n=1, reuse_of="entry-b"
    )
    b = fakes_evidence.entry(
        tmp_path,
        "S24-L3-LRMIXED-48H-LINUX",
        "PASS",
        platform="linux",
        n=2,
        reuse_of="entry-a",
    )
    a = replace(a, entry_id="entry-a")
    b = replace(b, entry_id="entry-b")
    evidence_index.add_entry(idx, a, catalog=cat)
    evidence_index.add_entry(idx, b, catalog=cat)
    status, reasons = _status(idx, tmp_path, cat, "S3-L3-AB-48H-LINUX")
    assert status == "STALE"
    assert any("reuse cycle" in reason for reason in reasons)


def test_stage_blocked_until_all_items(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux"),
        catalog=cat,
    )
    result = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=cat, evidence_root=tmp_path, cases={}
    )
    assert result["result"] == "BLOCKED"


def test_stage_status_lists_satisfying_entries(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    entry = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux")
    evidence_index.add_entry(idx, entry, catalog=cat)
    result = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=cat, evidence_root=tmp_path, cases={}
    )
    assert result["items"]["S3-ROLLBACK-LINUX"]["satisfying_entries"] == [entry.entry_id]


def test_set_target_per_platform_appends_history(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    before = evidence_index.load_index(idx)
    windows_before = before["stage_targets"]["3"]["windows"]
    linux_bundle = tmp_path / "bundles" / "linux"
    _set_target(evidence_index, idx, stage="3", platform="linux", bundle=linux_bundle, bundle_dir=True)
    after = evidence_index.load_index(idx)
    assert len(after["target_history"]) == 3
    assert after["stage_targets"]["3"]["linux"] == _history_target(after["target_history"][-1])
    assert after["stage_targets"]["3"]["windows"] == windows_before


def test_set_target_stage3_records_bundle_tree(tmp_path):
    evidence_index = _evidence_api()
    ab_manifest = _ab_manifest_api()
    idx = _empty_index(tmp_path)
    bundle = fakes_evidence.make_bundle(
        tmp_path / "bundles" / "linux", stage="3", target="x86_64-unknown-linux-gnu"
    )
    _set_target(evidence_index, idx, stage="3", platform="linux", bundle=bundle, bundle_dir=True)
    target = evidence_index.load_index(idx)["stage_targets"]["3"]["linux"]
    assert target["bundle_tree_sha256"] == ab_manifest.bundle_tree_sha256(bundle)
    assert target["runtime_tree_sha256"] == ab_manifest.runtime_tree_sha256(bundle)
    _set_target(evidence_index, idx, stage="3", platform="linux", bundle=bundle, bundle_dir=True)
    after = evidence_index.load_index(idx)
    assert len(after["target_history"]) == 2
    first = _history_target(after["target_history"][0])
    latest = _history_target(after["target_history"][1])
    assert first["bundle_tree_sha256"] == latest["bundle_tree_sha256"]


def test_set_target_same_manifest_new_tree_refused(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = _empty_index(tmp_path)
    bundle = fakes_evidence.make_bundle(
        tmp_path / "bundles" / "linux", stage="2.4", target="x86_64-unknown-linux-gnu"
    )
    _set_target(evidence_index, idx, stage="2.4", platform="linux", bundle=bundle, bundle_dir=True)
    (bundle / "assets" / "extra.txt").parent.mkdir(parents=True, exist_ok=True)
    (bundle / "assets" / "extra.txt").write_text("sidecar\n", encoding="utf-8")
    before = idx.read_bytes()
    with pytest.raises(HarnessError) as ei:
        _set_target(evidence_index, idx, stage="2.4", platform="linux", bundle=bundle, bundle_dir=True)
    assert ei.value.code == "HARNESS_ARTIFACT_MISMATCH"
    assert "bundle tree changed under same manifest: linux" in ei.value.detail
    assert idx.read_bytes() == before


def test_set_target_stage3_requires_bundle_dir(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = _empty_index(tmp_path)
    bundle = fakes_evidence.make_bundle(
        tmp_path / "bundles" / "linux", stage="3", target="x86_64-unknown-linux-gnu"
    )
    before = idx.read_bytes()
    with pytest.raises(HarnessError) as ei:
        evidence_index.set_target(
            idx,
            stage="3",
            platform="linux",
            release_manifest=bundle / "release-manifest.json",
            contract_revision="c" * 40,
            tool_revision="t" * 40,
            case_list_sha256="0" * 64,
            set_by="test",
            now=fakes.ts(30, 0),
            bundle_dir=None,
        )
    assert ei.value.code == "HARNESS_MANIFEST_INVALID"
    assert idx.read_bytes() == before


def test_set_target_stage3_refuses_drifted_bundle(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = _empty_index(tmp_path)
    bundle = fakes_evidence.make_bundle(
        tmp_path / "bundles" / "linux", stage="3", target="x86_64-unknown-linux-gnu"
    )
    (bundle / "bin" / "serialwrapd").write_bytes(b"replaced launcher")
    before = idx.read_bytes()
    with pytest.raises(HarnessError) as ei:
        _set_target(evidence_index, idx, stage="3", platform="linux", bundle=bundle, bundle_dir=True)
    assert ei.value.code == "HARNESS_ARTIFACT_MISMATCH"
    assert "release component sha256 mismatch: linux daemon.launcher" in ei.value.detail
    assert idx.read_bytes() == before


def test_set_target_stage3_manifest_outside_bundle(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = _empty_index(tmp_path)
    bundle = fakes_evidence.make_bundle(
        tmp_path / "bundles" / "linux", stage="3", target="x86_64-unknown-linux-gnu"
    )
    outside = tmp_path / "release-manifest.json"
    shutil.copyfile(bundle / "release-manifest.json", outside)
    with pytest.raises(HarnessError) as ei:
        evidence_index.set_target(
            idx,
            stage="3",
            platform="linux",
            release_manifest=outside,
            contract_revision="c" * 40,
            tool_revision="t" * 40,
            case_list_sha256="0" * 64,
            set_by="test",
            now=fakes.ts(30, 0),
            bundle_dir=bundle,
        )
    assert ei.value.code == "HARNESS_MANIFEST_INVALID"


def test_environment_id_requires_keys(tmp_path):
    evidence_index = _evidence_api()
    missing = dict(fakes_evidence.ENV_DOC)
    missing.pop("av_product")
    with pytest.raises(ValueError):
        evidence_index.environment_id(missing)
    ordered = dict(fakes_evidence.ENV_DOC)
    reversed_keys = dict(reversed(list(ordered.items())))
    assert evidence_index.environment_id(ordered) == evidence_index.environment_id(reversed_keys)


def test_index_file_stable_format(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    jsonio = _jsonio_api()
    assert idx.read_text(encoding="utf-8") == jsonio.dumps_report(json.loads(idx.read_text(encoding="utf-8")))


def test_render_report_marks_owner_blocked(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    report = evidence_index.render_report(
        evidence_index.load_index(idx), cat, evidence_root=tmp_path, cases={}
    )
    assert "BLOCKED-OWNER" in report
