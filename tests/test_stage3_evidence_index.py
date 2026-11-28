"""Stage 0～3 出口證據索引的紅燈測試。"""

from __future__ import annotations

import hashlib
import io
import inspect
import json
import tarfile
import threading
import time
from copy import copy
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

import stage3_fakes as fakes
import fakes_evidence
from tools.stage3 import evidence_index

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

# 本檔屬 memory_only；T1 的 revision 讀取明確注入固定 bytes 替身。
evidence_index.DEFAULT_SOURCE_READER = fakes_evidence.read_seed_revision


def _evidence_api():
    """回傳本 Task 的正式索引介面。"""
    return evidence_index


def _allow_unrelated_public_evidence_for_rule_test(monkeypatch) -> None:
    """隔離 BO-P9-19 外的舊規則測試；專用 BO-P9-19 測試走正式判定。"""
    monkeypatch.setattr(evidence_index, "_bop919_pending", lambda _item: False)


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
    kwargs = {
        "evidence_root": root / "docs" / "rust-refactor" / "evidence",
        "cases": cases or {},
    }
    if "catalog" in inspect.signature(evidence_index.item_status).parameters:
        kwargs["catalog"] = cat
    return evidence_index.item_status(evidence_index.load_index(idx), cat[item_id], **kwargs)


def _platform_detail(idx: Path, root: Path, cat: dict, item_id: str, platform: str, cases: dict | None = None):
    """讀取單平台底層證據判定，供 raw-report 公開 gate 外的規則測試使用。"""
    api = _evidence_api()
    return api._platform_status(
        api.load_index(idx),
        cat[item_id],
        platform,
        evidence_root=_evidence_root(root),
        cases=cases or {},
        l1_lock=None,
        catalog=cat,
        source_reader=api.DEFAULT_SOURCE_READER,
    )


def _empty_index(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "index.json"
    path.write_bytes(
        (
            json.dumps(
                {
                    "candidate_rounds": [],
                    "current_candidate_round_id": None,
                    "entries": [],
                    "legacy_obligations": {"resolutions": None, "scope": None},
                    "schema_version": 2,
                    "stage_targets": {},
                    "target_history": [],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )
    return path


def _set_target(api, idx: Path, *, stage: str, platform: str, bundle: Path, bundle_dir: bool = False) -> None:
    kwargs = dict(
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
    if stage == "3":
        index = api.load_index(idx)
        current_round = index.get("current_candidate_round_id")
        if not current_round:
            current_round = api.begin_candidate_round(
                idx, rc_tag="v1.0.0-rc.1", set_by="test", now=fakes.ts(29, 0)
            )
        kwargs.update(
            archive=fakes.rc_archive(bundle),
            candidate_round_id=current_round,
        )
    api.set_target(idx, **kwargs)


def _history_target(row: dict) -> dict:
    """接受 history 直接存 target 或以 target 欄包住的兩種介面表示。"""
    if "target" in row:
        return row["target"]
    return {key: value for key, value in row.items() if key not in {"stage", "platform"}}


def _evidence_root(root: Path) -> Path:
    return Path(root) / "docs" / "rust-refactor" / "evidence"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )


def _refresh_scope_review(index_path: Path, evidence_root: Path) -> None:
    """重算合成核定附件與 FrozenFile，讓測試落到 scope 語意檢查。"""
    index = json.loads(index_path.read_bytes())
    pointer = index["legacy_obligations"]["scope"]
    scope_path = evidence_root / pointer["path"]
    scope = json.loads(scope_path.read_bytes())
    source_hashes = [
        {
            "source_id": source["source_id"],
            "sha256": hashlib.sha256((evidence_root / source["snapshot"]["path"]).read_bytes()).hexdigest(),
        }
        for source in scope["sources"]
    ]
    review_path = evidence_root / scope["review"]["record"]["path"]
    review = json.loads(review_path.read_bytes())
    scope_body = {key: value for key, value in scope.items() if key != "review"}
    review["source_hashes"] = source_hashes
    review["scope_sha256"] = hashlib.sha256(
        json.dumps(scope_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _write_json(review_path, review)
    scope["review"]["record"]["sha256"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
    _write_json(scope_path, scope)
    pointer["sha256"] = hashlib.sha256(scope_path.read_bytes()).hexdigest()
    _write_json(index_path, index)


def _install_legacy_resolutions(index_path: Path, evidence_root: Path, rows: list[dict]) -> dict:
    index = _evidence_api().load_index(index_path)
    pointer = index["legacy_obligations"]["scope"]
    path = evidence_root / "legacy-obligations-resolutions.json"
    _write_json(path, {
        "schema_version": 1,
        "scope_sha256": pointer["sha256"],
        "resolutions": rows,
    })
    index["legacy_obligations"]["resolutions"] = {
        "path": path.relative_to(evidence_root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    _write_json(index_path, index)
    return _evidence_api().load_index(index_path)


def _freeze_stage0_target(index_path: Path, root: Path, *, platform: str = "linux") -> dict:
    api = _evidence_api()
    bundle = fakes_evidence.make_bundle(
        root / "legacy-bundles" / platform,
        stage="0",
        target="x86_64-pc-windows-msvc" if platform == "windows" else "x86_64-unknown-linux-gnu",
    )
    api.set_target(
        index_path,
        stage="0",
        platform=platform,
        release_manifest=bundle / "release-manifest.json",
        contract_revision="c" * 40,
        tool_revision="t" * 40,
        case_list_sha256="0" * 64,
        set_by="test",
        now=fakes.ts(1, 0),
    )
    return api.load_index(index_path)["stage_targets"]["0"][platform]


def _make_noncatalog_od09_obligation(
    index_path: Path,
    evidence_root: Path,
    *,
    evidence_kind: str = "od09-freeze",
) -> tuple[dict, dict]:
    """把一筆合成非目錄來源義務改為靜態核對 kind，並同步凍結 target。"""
    api = _evidence_api()
    index = api.load_index(index_path)
    scope_pointer = index["legacy_obligations"]["scope"]
    scope_path = evidence_root / scope_pointer["path"]
    scope = json.loads(scope_path.read_bytes())
    inventory_source = next(
        row for row in scope["sources"] if row["source_id"] == "fixture:legacy-source-inventory"
    )
    snapshot_path = evidence_root / inventory_source["snapshot"]["path"]
    snapshot = json.loads(snapshot_path.read_bytes())
    source_obligation = snapshot["obligations"][0]
    key = source_obligation["key"]
    source_obligation["evidence_kinds"] = [evidence_kind]
    _write_json(snapshot_path, snapshot)
    inventory_source["snapshot"]["sha256"] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    scope_obligation = next(row for row in scope["obligations"] if row["key"] == key)
    scope_obligation["allowed_evidence"]["kinds"] = [evidence_kind]
    _write_json(scope_path, scope)
    _refresh_scope_review(index_path, evidence_root)

    index = api.load_index(index_path)
    frozen_scope_sha = index["legacy_obligations"]["scope"]["sha256"]
    for platform in ("linux", "windows"):
        index["stage_targets"]["3"][platform]["legacy_scope_sha256"] = frozen_scope_sha
    for target in index["target_history"]:
        if target.get("stage") == "3":
            target["legacy_scope_sha256"] = frozen_scope_sha
    _write_json(index_path, index)
    return api.load_index(index_path), scope_obligation


def _named_od_resolution(evidence_root: Path, obligation: dict, *, bad: str | None = None) -> dict:
    decision_ref = "OD-DECISION-42"
    od_path = evidence_root / "owner-decisions" / "OD-42.md"
    alternative_path = evidence_root / "owner-decisions" / "replacement-evidence.json"
    record_path = evidence_root / "owner-decisions" / "named-resolution.json"
    _write_json(alternative_path, {"verification": "owner-approved public evidence"})
    od_path.parent.mkdir(parents=True, exist_ok=True)
    od_path.write_text("# OD-42\n具名核定與替代證據範圍。\n", encoding="utf-8")

    def frozen(path: Path) -> dict[str, str]:
        return {
            "path": path.relative_to(evidence_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    od_file = frozen(od_path)
    alternative_file = frozen(alternative_path)
    record = {
        "schema_version": 1,
        "origin_od": "OD-42",
        "key": obligation["key"],
        "platform": obligation["platform"],
        "scope_sha256": obligation["required_scope_sha256"],
        "decision_ref": decision_ref,
        "reviewer_id": "owner-reviewer-42",
        "approved_at": "2026-11-01T00:00:00+00:00",
        "od_document": od_file,
        "alternative_evidence": [alternative_file],
    }
    if bad == "key":
        record["key"] = "legacy:unrelated"
    elif bad == "platform":
        record["platform"] = "windows" if obligation["platform"] == "linux" else "linux"
    elif bad == "scope":
        record["scope_sha256"] = "0" * 64
    elif bad == "no_alternative":
        record["alternative_evidence"] = []
    elif bad == "reviewer":
        record["reviewer_id"] = ""
    _write_json(record_path, record)
    evidence = [frozen(record_path), od_file, alternative_file]
    return {
        "key": obligation["key"],
        "method": "new_od",
        "decision_ref": decision_ref,
        "evidence": evidence,
    }


def _mutated_tar_archive(source_path: Path, destination: Path, variant: str, version: str) -> Path:
    """建立格式正確但布局或內容不符的 Linux RC archive。"""
    with tarfile.open(source_path, "r:gz") as source, tarfile.open(destination, "w:gz") as output:
        changed_file = False
        changed_path = False
        changed_link = False
        for original in source.getmembers():
            member = copy(original)
            if variant == "absolute_path" and not changed_path:
                member.name = "/absolute/outside"
                changed_path = True
            if variant == "wrong_root":
                parts = member.name.split("/", 1)
                member.name = "unexpected-root" + ("/" + parts[1] if len(parts) > 1 else "")
            if variant == "symlink_escape" and original.issym() and not changed_link:
                member.linkname = "../../../../outside"
                changed_link = True
            payload = source.extractfile(original) if original.isfile() else None
            if original.isfile():
                assert payload is not None
                with payload:
                    data = payload.read()
                if variant == "different_content" and not changed_file:
                    data += b"changed"
                    changed_file = True
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))
            else:
                output.addfile(member)
        if variant == "escape":
            info = tarfile.TarInfo("../escaped")
            info.size = 1
            output.addfile(info, io.BytesIO(b"x"))
        elif variant == "multiple_roots":
            info = tarfile.TarInfo(f"serialwrap-{version}-linux-x86_64/versions/other/extra")
            info.size = 1
            output.addfile(info, io.BytesIO(b"x"))
        elif variant == "device_file":
            info = tarfile.TarInfo(f"serialwrap-{version}-linux-x86_64/versions/{version}/device")
            info.type = tarfile.CHRTYPE
            info.devmajor = 1
            info.devminor = 3
            output.addfile(info)
        if variant == "symlink_escape":
            assert changed_link
    return destination


def _od09_resolution_rows(index_path: Path, root: Path, method: str) -> tuple[list[dict], str]:
    api = _evidence_api()
    catalog = api.load_exit_catalog()
    target = _freeze_stage0_target(index_path, root, platform="linux")
    evidence = fakes_evidence.entry(
        root,
        "S0-OD09-DEFERRED-FROZEN",
        "PASS",
        platform="linux",
        n=15 if method == "retest" else 16,
    )
    api.add_entry(index_path, evidence, catalog=catalog)
    evidence_root = _evidence_root(root)
    index = api.load_index(index_path)
    scope = json.loads((evidence_root / index["legacy_obligations"]["scope"]["path"]).read_bytes())
    selected = next(
        row for row in scope["obligations"]
        if row["item_id"] == "S0-OD09-DEFERRED-FROZEN" and row["platform"] == "linux"
    )
    rows = [{"key": row["key"], "method": "pending"} for row in scope["obligations"]]
    resolution = next(row for row in rows if row["key"] == selected["key"])
    if method == "retest":
        target_path = evidence_root / "legacy-targets" / "od09-linux.json"
        _write_json(target_path, target)
        target_bytes = target_path.read_bytes()
        resolution.update({
            "method": "retest",
            "entry_id": evidence.entry_id,
            "target_sha256": hashlib.sha256(target_bytes).hexdigest(),
            "files": [{
                "path": target_path.relative_to(evidence_root).as_posix(),
                "sha256": hashlib.sha256(target_bytes).hexdigest(),
            }],
        })
    else:
        resolution.update({"method": "reuse", "entry_id": evidence.entry_id})
    return rows, selected["key"]


def _reuse_fixture(
    root: Path,
    *,
    previous_result: str = "PASS",
    previous_tree: str | None = None,
    previous_tree_none: bool = False,
    current_tree: str | None = None,
    previous_workload: str | None = "7" * 64,
    current_workload: str | None = "7" * 64,
    add_previous: bool = True,
    delete_previous_file: bool = False,
):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(root, stage="2.4", contract="c" * 40)
    cat = dict(evidence_index.load_exit_catalog())
    # 此 helper 覆蓋 reuse 身分／報告範圍；以相同凍結集合建立有效基線。
    # 不同集合的拒絕由 test_reuse_requires_matching_platform_kind_and_frozen_cases 驗證。
    source_item = cat["S0-L3-BASELINE-LINUX-48H"]
    target_item = cat["S24-L3-LRMIXED-48H-LINUX"]
    cat[source_item.item_id] = replace(source_item, required_case_set=target_item.required_case_set)
    target = evidence_index.load_index(idx)["stage_targets"]["2.4"]["linux"]
    previous_bundle = fakes_evidence.make_bundle(
        root / "bundles" / "stage-0" / "linux",
        stage="0",
        target="x86_64-unknown-linux-gnu",
    )
    evidence_index.set_target(
        idx,
        stage="0",
        platform="linux",
        release_manifest=previous_bundle / "release-manifest.json",
        contract_revision="c" * 40,
        tool_revision="t" * 40,
        case_list_sha256="0" * 64,
        set_by="test",
        now=fakes.ts(30, 0),
    )
    previous_target = evidence_index.load_index(idx)["stage_targets"]["0"]["linux"]
    previous = fakes_evidence.entry(
        root,
        "S0-L3-BASELINE-LINUX-48H",
        previous_result,
        platform="linux",
        n=1,
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
        (_evidence_root(root) / previous.files[0].path).unlink()
    current = fakes_evidence.entry(
        root,
        "S24-L3-LRMIXED-48H-LINUX",
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
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = api.load_exit_catalog()
    api.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S3-INCIDENTS-CLOSED", "PASS", platform="linux"),
        catalog=cat,
    )
    assert _platform_detail(idx, tmp_path, cat, "S3-INCIDENTS-CLOSED", "linux")[0] == "SATISFIED"
    assert _platform_detail(idx, tmp_path, cat, "S3-INCIDENTS-CLOSED", "windows")[0] == "MISSING"
    assert _status(idx, tmp_path, cat, "S3-INCIDENTS-CLOSED")[0] == "STALE"
    api.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S3-INCIDENTS-CLOSED", "PASS", platform="windows"),
        catalog=cat,
    )
    assert all(
        _platform_detail(idx, tmp_path, cat, "S3-INCIDENTS-CLOSED", platform)[0] == "SATISFIED"
        for platform in ("linux", "windows")
    )
    assert _status(idx, tmp_path, cat, "S3-INCIDENTS-CLOSED")[0] == "STALE"


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
    assert _platform_detail(idx, tmp_path, cat, "S3-CANARY-LINUX", "linux")[0] == "SATISFIED"
    assert _status(idx, tmp_path, cat, "S3-CANARY-LINUX")[0] == "STALE"
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
    assert status == "STALE"
    assert any(
        reason.startswith(f"linux: superseded: latest canary report {new.entry_id} is INSUFFICIENT")
        and old.entry_id in reason
        for reason in reasons
    )
    st = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=cat, evidence_root=_evidence_root(tmp_path), cases={}
    )
    assert st["items"]["S3-CANARY-LINUX"]["status"] == "STALE"
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
    assert _platform_detail(idx, tmp_path, cat, "S3-CANARY-LINUX", "linux")[0] == "SATISFIED"
    st = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=cat, evidence_root=_evidence_root(tmp_path), cases={}
    )
    assert st["items"]["S3-CANARY-LINUX"]["status"] == "STALE"
    assert st["items"]["S3-CANARY-LINUX"]["satisfying_entries"] == []


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
    linux_status, linux_reasons, _ = _platform_detail(
        idx, tmp_path, cat, "S3-INCIDENTS-CLOSED", "linux"
    )
    assert status == "STALE"
    assert linux_status == "MISSING"
    assert any(reason.startswith("linux: superseded: latest canary report ") for reason in reasons)
    assert any(reason.startswith("superseded: latest canary report ") for reason in linux_reasons)
    assert not any(reason.startswith("windows: ") for reason in reasons)


def test_sideloaded_file_retarget_cannot_reuse_pass(tmp_path, monkeypatch):
    _allow_unrelated_public_evidence_for_rule_test(monkeypatch)
    evidence_index = _evidence_api()
    ab_manifest = _ab_manifest_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    bundle = tmp_path / "bundles" / "linux"
    doc = json.loads((bundle / "release-manifest.json").read_bytes())
    old = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux", n=1)
    target = evidence_index.load_index(idx)["stage_targets"]["3"]["linux"]
    assert old.bundle_tree_sha256 == target["bundle_tree_sha256"] == ab_manifest.bundle_tree_sha256(bundle)
    assert old.runtime_tree_sha256 == target["runtime_tree_sha256"] == ab_manifest.runtime_tree_sha256(bundle)
    evidence_index.add_entry(idx, old, catalog=cat)
    assert _platform_detail(idx, tmp_path, cat, "S3-ROLLBACK-LINUX", "linux")[0] == "SATISFIED"
    initial_status, initial_reasons = _status(idx, tmp_path, cat, "S3-ROLLBACK-LINUX")
    assert initial_status == "STALE"
    assert "raw report binding unverifiable" in initial_reasons
    (bundle / "bin" / "version.dll").write_bytes(b"MZ")
    fakes.refresh_bundle_sums(bundle)
    assert ab_manifest.verify_bundle(bundle, doc, label="linux") == []
    current_round = evidence_index.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
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
            set_by="t",
            now=fakes.ts(30, 0),
            bundle_dir=bundle,
            archive=fakes.rc_archive(bundle),
            candidate_round_id=current_round,
        )
    assert ei.value.code == "HARNESS_ARTIFACT_MISMATCH"
    assert "bundle tree changed under same manifest: linux" in str(ei.value)
    assert idx.read_bytes() == before
    raw = json.loads(idx.read_bytes())
    raw["stage_targets"]["3"]["linux"]["bundle_tree_sha256"] = ab_manifest.bundle_tree_sha256(bundle)
    raw["stage_targets"]["3"]["linux"]["runtime_tree_sha256"] = ab_manifest.runtime_tree_sha256(bundle)
    _write_json(idx, raw)
    status, reasons = _status(idx, tmp_path, cat, "S3-ROLLBACK-LINUX")
    assert status == "STALE"
    assert f"linux: bundle tree differs from target: {old.entry_id}" in reasons
    st = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=cat, evidence_root=_evidence_root(tmp_path), cases={}
    )
    assert st["items"]["S3-ROLLBACK-LINUX"]["satisfying_entries"] == []
    assert st["result"] != "PASS"


def test_non_cumulative_pass_not_superseded(tmp_path, monkeypatch):
    _allow_unrelated_public_evidence_for_rule_test(monkeypatch)
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
    detail, _detail_reasons, detail_entries = _platform_detail(
        idx, tmp_path, cat, "S3-ROLLBACK-LINUX", "linux"
    )
    assert detail == "SATISFIED"
    assert detail_entries == [passed.entry_id]
    status = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=cat, evidence_root=_evidence_root(tmp_path), cases={}
    )
    assert status["items"]["S3-ROLLBACK-LINUX"]["status"] == "STALE"
    assert status["items"]["S3-ROLLBACK-LINUX"]["satisfying_entries"] == []
    assert "raw report binding unverifiable" in status["items"]["S3-ROLLBACK-LINUX"]["reasons"]


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
    assert _platform_detail(idx, tmp_path, cat, "S3-ROLLBACK-LINUX", "linux")[0] == "MISSING"
    status, reasons = _status(idx, tmp_path, cat, "S3-ROLLBACK-LINUX")
    assert status == "STALE"
    assert any("raw report binding unverifiable" in reason for reason in reasons)


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


def test_xfail_requires_unexpired_sentinel(tmp_path, monkeypatch):
    _allow_unrelated_public_evidence_for_rule_test(monkeypatch)
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
    assert _platform_detail(future_idx, future_root, cat, "S3-ROLLBACK-LINUX", "linux", future_cases)[0] == "SATISFIED"
    assert _status(future_idx, future_root, cat, "S3-ROLLBACK-LINUX", future_cases)[0] == "STALE"

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
    assert _platform_detail(expired_idx, expired_root, cat, "S3-ROLLBACK-LINUX", "linux", expired_cases)[0] == "MISSING"
    assert _status(expired_idx, expired_root, cat, "S3-ROLLBACK-LINUX", expired_cases)[0] == "STALE"

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
        == "STALE"
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
    changed_path = _evidence_root(changed_root) / changed.files[0].path
    changed_path.write_bytes(b"tampered\n")
    assert _platform_detail(changed_idx, changed_root, cat, "S3-ROLLBACK-LINUX", "linux")[0] == "HASH_MISMATCH"
    assert _status(changed_idx, changed_root, cat, "S3-ROLLBACK-LINUX")[0] == "STALE"

    deleted_root = tmp_path / "deleted"
    deleted_idx = fakes_evidence.evidence_index(deleted_root, stage="3", contract="c" * 40)
    deleted = fakes_evidence.entry(
        deleted_root, "S3-ROLLBACK-LINUX", "PASS", platform="linux", n=1
    )
    evidence_index.add_entry(deleted_idx, deleted, catalog=cat)
    (_evidence_root(deleted_root) / deleted.files[0].path).unlink()
    assert _platform_detail(deleted_idx, deleted_root, cat, "S3-ROLLBACK-LINUX", "linux")[0] == "HASH_MISMATCH"
    assert _status(deleted_idx, deleted_root, cat, "S3-ROLLBACK-LINUX")[0] == "STALE"


def test_reuse_by_runtime_digest(tmp_path, monkeypatch):
    _allow_unrelated_public_evidence_for_rule_test(monkeypatch)
    idx, cat, _previous, _current = _reuse_fixture(tmp_path / "same")
    assert _status(idx, tmp_path / "same", cat, "S24-L3-LRMIXED-48H-LINUX")[0] == "SATISFIED"

    stale_idx, stale_cat, _previous, _current = _reuse_fixture(
        tmp_path / "workload-drift", previous_workload="8" * 64, current_workload="7" * 64
    )
    assert _status(stale_idx, tmp_path / "workload-drift", stale_cat, "S24-L3-LRMIXED-48H-LINUX")[0] == "STALE"


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
    status, reasons = _status(idx, dirty_source, cat, "S24-L3-LRMIXED-48H-LINUX")
    assert status == "STALE"
    assert "linux: reuse identity differs: runtime_tree_sha256" in reasons

    none_source = tmp_path / "none-source"
    none_idx, none_cat, _previous, _current = _reuse_fixture(
        none_source, previous_tree_none=True
    )
    # S24 來源 target 未記錄 runtime tree 時，不得當作相符的跨 Stage 證據。
    status, reasons = _status(none_idx, none_source, none_cat, "S24-L3-LRMIXED-48H-LINUX")
    assert status == "STALE"
    assert "linux: reuse identity differs: runtime_tree_sha256" in reasons


def test_entry_tree_none_against_tree_target_stale(tmp_path, monkeypatch):
    _allow_unrelated_public_evidence_for_rule_test(monkeypatch)
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
    # 將舊版歷史 target 還原成契約允許的無 tree 形狀；一般 fixture 的 bundle
    # 含 SHA256SUMS，set_target 會自動記錄兩個 tree digest。
    no_tree_index = json.loads(no_tree_idx.read_bytes())
    no_tree_index["stage_targets"]["2.4"]["linux"].pop("bundle_tree_sha256", None)
    no_tree_index["stage_targets"]["2.4"]["linux"].pop("runtime_tree_sha256", None)
    no_tree_history = next(
        row for row in reversed(no_tree_index["target_history"])
        if row.get("stage") == "2.4" and row.get("platform") == "linux"
    )
    no_tree_history.pop("bundle_tree_sha256", None)
    no_tree_history.pop("runtime_tree_sha256", None)
    _write_json(no_tree_idx, no_tree_index)
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
    assert _status(failed_idx, tmp_path / "failed", failed_cat, "S24-L3-LRMIXED-48H-LINUX")[0] == "STALE"

    missing_root = tmp_path / "missing"
    evidence_index = _evidence_api()
    missing_idx = fakes_evidence.evidence_index(missing_root, stage="2.4", contract="c" * 40)
    missing_cat = evidence_index.load_exit_catalog()
    missing_current = fakes_evidence.entry(
        missing_root,
        "S24-L3-LRMIXED-48H-LINUX",
        "PASS",
        platform="linux",
        reuse_of="absent-entry",
    )
    evidence_index.add_entry(missing_idx, missing_current, catalog=missing_cat)
    assert _status(missing_idx, missing_root, missing_cat, "S24-L3-LRMIXED-48H-LINUX")[0] == "STALE"

    deleted_idx, deleted_cat, _previous, _current = _reuse_fixture(
        tmp_path / "deleted-source", delete_previous_file=True
    )
    assert _status(
        deleted_idx, tmp_path / "deleted-source", deleted_cat, "S24-L3-LRMIXED-48H-LINUX"
    )[0] == "STALE"


def test_reuse_refuses_source_item_with_sticky_fail(tmp_path):
    root = tmp_path / "source-fail"
    idx, cat, _previous, _current = _reuse_fixture(root)
    evidence_index = _evidence_api()
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(
            root,
            "S0-L3-BASELINE-LINUX-48H",
            "FAIL",
            platform="linux",
            n=3,
        ),
        catalog=cat,
    )

    assert _status(idx, root, cat, "S0-L3-BASELINE-LINUX-48H")[0] == "FAIL"
    assert _status(idx, root, cat, "S24-L3-LRMIXED-48H-LINUX")[0] == "STALE"


def test_reuse_refuses_source_after_stage_target_reset(tmp_path, monkeypatch):
    """來源 stage 的 target 重設後，舊來源 entry 不再符合目前 target，沿用須為 STALE（5a038a1）。"""
    _allow_unrelated_public_evidence_for_rule_test(monkeypatch)
    root = tmp_path / "retarget"
    idx, cat, _previous, _current = _reuse_fixture(root)
    assert _status(idx, root, cat, "S24-L3-LRMIXED-48H-LINUX")[0] == "SATISFIED"
    evidence_index = _evidence_api()
    reset_bundle = fakes_evidence.make_bundle(
        root / "bundles" / "stage-0-reset" / "linux",
        stage="0",
        target="aarch64-unknown-linux-gnu",
    )
    evidence_index.set_target(
        idx,
        stage="0",
        platform="linux",
        release_manifest=reset_bundle / "release-manifest.json",
        contract_revision="c" * 40,
        tool_revision="t" * 40,
        case_list_sha256="0" * 64,
        set_by="test",
        now=fakes.ts(30, 1),
        bundle_dir=reset_bundle,
    )

    assert _status(idx, root, cat, "S24-L3-LRMIXED-48H-LINUX")[0] == "STALE"


def test_reuse_refuses_source_when_its_stage_has_no_target(tmp_path, monkeypatch):
    """來源 entry 所屬 stage 沒有目前 target 時不得沿用（5a038a1）。"""
    _allow_unrelated_public_evidence_for_rule_test(monkeypatch)
    root = tmp_path / "no-source-target"
    idx, cat, _previous, _current = _reuse_fixture(root)
    assert _status(idx, root, cat, "S24-L3-LRMIXED-48H-LINUX")[0] == "SATISFIED"
    data = json.loads(idx.read_bytes())
    del data["stage_targets"]["0"]
    _write_json(idx, data)

    assert _status(idx, root, cat, "S24-L3-LRMIXED-48H-LINUX")[0] == "STALE"


def test_reuse_cycle_stale(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="2.4", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    a = fakes_evidence.entry(
        tmp_path, "S24-L3-LRMIXED-48H-LINUX", "PASS", platform="linux", n=1, reuse_of="entry-b"
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
    cycle_index = evidence_index.load_index(idx)
    cycle_index["entries"] = [evidence_index._entry_dict(a), evidence_index._entry_dict(b)]
    status, reasons = evidence_index.item_status(
        cycle_index,
        cat["S24-L3-LRMIXED-48H-LINUX"],
        evidence_root=_evidence_root(tmp_path),
        cases={},
    )
    assert status == "STALE"
    assert any("reuse cycle" in reason for reason in reasons)


def test_stage_blocked_until_all_required_items(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux"),
        catalog=cat,
    )
    result = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=cat, evidence_root=_evidence_root(tmp_path), cases={}
    )
    assert result["result"] == "BLOCKED"


def test_stage_status_lists_satisfying_entries(tmp_path, monkeypatch):
    _allow_unrelated_public_evidence_for_rule_test(monkeypatch)
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    entry = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux")
    evidence_index.add_entry(idx, entry, catalog=cat)
    detail, _detail_reasons, satisfying = _platform_detail(
        idx, tmp_path, cat, "S3-ROLLBACK-LINUX", "linux"
    )
    assert detail == "SATISFIED"
    assert satisfying == [entry.entry_id]
    result = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=cat, evidence_root=_evidence_root(tmp_path), cases={}
    )
    assert result["items"]["S3-ROLLBACK-LINUX"]["status"] == "STALE"
    assert result["items"]["S3-ROLLBACK-LINUX"]["satisfying_entries"] == []
    assert any(
        "raw report binding unverifiable" in reason
        for reason in result["items"]["S3-ROLLBACK-LINUX"]["reasons"]
    )


def test_set_target_per_platform_appends_history(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="2.4", contract="c" * 40)
    before = evidence_index.load_index(idx)
    windows_before = before["stage_targets"]["2.4"]["windows"]
    linux_bundle = tmp_path / "bundles" / "linux"
    _set_target(evidence_index, idx, stage="2.4", platform="linux", bundle=linux_bundle, bundle_dir=True)
    after = evidence_index.load_index(idx)
    assert len(after["target_history"]) == 3
    assert after["stage_targets"]["2.4"]["linux"] == _history_target(after["target_history"][-1])
    assert after["stage_targets"]["2.4"]["windows"] == windows_before


def test_set_target_same_manifest_new_tree_refused(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = _empty_index(tmp_path)
    bundle = fakes_evidence.make_bundle(
        tmp_path / "bundles" / "linux", stage="2.4", target="x86_64-unknown-linux-gnu"
    )
    _set_target(evidence_index, idx, stage="2.4", platform="linux", bundle=bundle, bundle_dir=True)
    (bundle / "assets" / "extra.txt").parent.mkdir(parents=True, exist_ok=True)
    extra = bundle / "assets" / "extra.txt"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_bytes(b"sidecar\n")
    fakes.refresh_bundle_sums(bundle)
    before = idx.read_bytes()
    with pytest.raises(HarnessError) as ei:
        evidence_index.set_target(
            idx,
            stage="2.4",
            platform="linux",
            release_manifest=bundle / "release-manifest.json",
            contract_revision="c" * 40,
            tool_revision="t" * 40,
            case_list_sha256="0" * 64,
            set_by="test",
            now=fakes.ts(30, 0),
            bundle_dir=bundle,
        )
    assert ei.value.code == "HARNESS_ARTIFACT_MISMATCH"
    assert "bundle tree changed under same manifest: linux" in ei.value.detail
    assert idx.read_bytes() == before


def test_set_target_stage3_requires_bundle_dir(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    bundle = tmp_path / "bundles" / "linux"
    current_round = evidence_index.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
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
            archive=fakes.rc_archive(bundle),
            candidate_round_id=current_round,
        )
    assert ei.value.code == "HARNESS_MANIFEST_INVALID"
    assert idx.read_bytes() == before


def test_set_target_stage3_refuses_drifted_bundle(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    bundle = tmp_path / "bundles" / "linux"
    current_round = evidence_index.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    (bundle / "bin" / "serialwrapd").write_bytes(b"replaced launcher")
    fakes.refresh_bundle_sums(bundle)
    before = idx.read_bytes()
    with pytest.raises(HarnessError) as ei:
        _set_target(evidence_index, idx, stage="3", platform="linux", bundle=bundle, bundle_dir=True)
    assert ei.value.code == "HARNESS_ARTIFACT_MISMATCH"
    assert "release component sha256 mismatch: linux daemon.launcher" in ei.value.detail
    assert idx.read_bytes() == before


def test_set_target_stage3_manifest_outside_bundle(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    bundle = tmp_path / "bundles" / "linux"
    current_round = evidence_index.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    outside = tmp_path / "release-manifest.json"
    outside.write_bytes((bundle / "release-manifest.json").read_bytes())
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
            archive=fakes.rc_archive(bundle),
            candidate_round_id=current_round,
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


def test_missing_l1_lock_makes_l1_item_missing(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    status, reasons = evidence_index.item_status(
        evidence_index.load_index(idx),
        catalog["S3-L1-RELEASE-CONTRACT-LINUX"],
        evidence_root=_evidence_root(tmp_path),
        cases={},
        l1_lock={"schema_version": 1, "sets": {}},
    )
    assert status == "STALE"
    assert "raw report binding unverifiable" in reasons
    assert "linux: no frozen l1 set: s3_release_contract" in reasons


def test_index_file_stable_format(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    jsonio = _jsonio_api()
    assert idx.read_bytes() == jsonio.dumps_report(json.loads(idx.read_bytes())).encode("utf-8")


def test_render_report_marks_owner_blocked(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    cat = evidence_index.load_exit_catalog()
    report = evidence_index.render_report(
        evidence_index.load_index(idx), cat, evidence_root=_evidence_root(tmp_path), cases={}
    )
    assert "BLOCKED-OWNER" in report


def _stage3_subset(catalog: dict, *item_ids: str) -> dict:
    return {item_id: catalog[item_id] for item_id in item_ids}


def _raise_code(error_type, code: str, operation) -> None:
    with pytest.raises(error_type) as exc:
        operation()
    assert exc.value.code == code


def test_removed_stage3_longrun_ids_cannot_satisfy_exit(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    previous = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux")
    evidence_index.add_entry(idx, previous, catalog=catalog)

    for item_id in ("S3-L3-AB-48H-LINUX", "S3-L3-AB-48H-WINDOWS"):
        assert item_id not in catalog
        for reuse_of in (None, previous.entry_id):
            before = idx.read_bytes()
            obsolete = fakes_evidence.entry(
                tmp_path, item_id, "PASS", platform="linux", reuse_of=reuse_of
            )
            _raise_code(
                HarnessError,
                "HARNESS_EVIDENCE_UNKNOWN_ITEM",
                lambda obsolete=obsolete: evidence_index.add_entry(idx, obsolete, catalog=catalog),
            )
            assert idx.read_bytes() == before


def test_check_index_append_only_all_entries():
    evidence_index = _evidence_api()
    ordinary = {
        "entry_id": "1" * 20,
        "item_id": "S3-ROLLBACK-LINUX",
        "platform": "linux",
        "result": "PASS",
        "artifact_id": "a" * 64,
        "files": [
            {"path": "report-a.json", "sha256": "b" * 64},
            {"path": "report-b.json", "sha256": "c" * 64},
        ],
        "xfail_cases": ["case-a", "case-b"],
        "note": "fixture",
    }
    fail = {"entry_id": "2" * 20, "item_id": "S3-ROLLBACK-LINUX", "result": "FAIL"}
    insufficient = {"entry_id": "3" * 20, "item_id": "S3-ROLLBACK-LINUX", "result": "INSUFFICIENT"}
    restricted = {
        "entry_id": "4" * 20,
        "item_id": "S3-CANARY-LINUX",
        "platform": "linux",
        "result": "FAIL",
        "source_visibility": "restricted",
        "artifact_id": "c" * 64,
        "candidate_round_id": "d" * 32,
        "context_id": "e" * 64,
        "private_source_sha256": "f" * 64,
        "failure_summary": "去識別摘要",
        "verified_by": "fixture-reviewer",
        "verified_at": "2026-11-01T00:00:00+00:00",
    }
    rows = [ordinary, fail, insufficient, restricted]
    base = {"entries": rows}
    before = json.dumps(base, sort_keys=True)

    for index, original in enumerate(rows):
        deleted = {"entries": rows[:index] + rows[index + 1 :]}
        assert evidence_index.check_index_append_only(base, deleted)
        for field, value in original.items():
            changed_rows = json.loads(json.dumps(rows))
            if field == "entry_id":
                changed_rows[index][field] = "9" * 20
            elif field == "result":
                changed_rows[index][field] = "FAIL" if value == "PASS" else "PASS"
            elif field.endswith("sha256"):
                changed_rows[index][field] = "9" * 64
            elif isinstance(value, list):
                changed_rows[index][field] = [*value, "fixture-added-value"]
            elif isinstance(value, str):
                changed_rows[index][field] = value + "-changed"
            else:
                changed_rows[index][field] = {"changed": True}
            assert evidence_index.check_index_append_only(base, {"entries": changed_rows})

        changed_nested = json.loads(json.dumps(rows))
        if original.get("files"):
            changed_nested[index]["files"][0]["sha256"] = "9" * 64
            assert evidence_index.check_index_append_only(base, {"entries": changed_nested})

    appended = {"entries": [*rows, {"entry_id": "5" * 20, "result": "PASS"}]}
    assert evidence_index.check_index_append_only(base, appended) == []
    assert evidence_index.check_index_append_only(base, {"entries": list(reversed(rows))}) == []
    reordered = {"entries": [dict(reversed(list(row.items()))) for row in rows]}
    assert evidence_index.check_index_append_only(base, reordered) == []
    nested_reordered = json.loads(json.dumps(base))
    for row in nested_reordered["entries"]:
        if isinstance(row.get("files"), list):
            row["files"] = [dict(reversed(list(file.items()))) for file in row["files"]]
    assert evidence_index.check_index_append_only(base, nested_reordered) == []
    array_reordered = json.loads(json.dumps(base))
    array_reordered["entries"][0]["files"].reverse()
    assert evidence_index.check_index_append_only(base, array_reordered)
    assert json.dumps(base, sort_keys=True) == before

    duplicate = {"entries": [*rows, dict(rows[0])]}
    malformed = ({}, {"entries": None}, {"entries": [{"result": "PASS"}]})
    for invalid in (duplicate, *malformed):
        assert evidence_index.check_index_append_only(base, invalid)


def test_restricted_entry_is_fail_only(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    restricted = fakes_evidence.restricted_entry(
        tmp_path, "S3-ROLLBACK-LINUX", platform="linux"
    )
    evidence_index.add_entry(idx, restricted, catalog=catalog)
    reloaded = evidence_index.load_index(idx)
    assert reloaded["entries"][-1]["source_visibility"] == "restricted"
    status, _ = _status(idx, tmp_path, catalog, "S3-ROLLBACK-LINUX")
    assert status == "FAIL"
    stage = evidence_index.stage_status(
        reloaded,
        "3",
        catalog=catalog,
        evidence_root=_evidence_root(tmp_path),
        cases={},
    )
    assert stage["result"] == "FAIL"
    assert stage["items"]["S3-ROLLBACK-LINUX"]["satisfying_entries"] == []

    invalid_cases = []
    for result in ("PASS", "XFAIL"):
        invalid_cases.append(replace(restricted, result=result))
    invalid_cases.append(replace(restricted, failure_summary="原始命令輸出不得公開：FIXTURE_PRIVATE_PAYLOAD"))
    invalid_cases.append(replace(restricted, verified_by="FIXTURE_MACHINE\\FIXTURE_USER"))
    for invalid in invalid_cases:
        before = idx.read_bytes()
        _raise_code(
            HarnessError,
            "HARNESS_EVIDENCE_INGEST_INVALID",
            lambda invalid=invalid: evidence_index.add_entry(idx, invalid, catalog=catalog),
        )
        assert idx.read_bytes() == before

    minimal = fakes_evidence.restricted_entry(
        tmp_path, "S3-ROLLBACK-LINUX", platform="linux"
    )
    missing_context = replace(minimal, context_id="")
    missing_verifier = replace(minimal, verified_by="")
    missing_hash = replace(minimal, private_source_sha256="")
    for invalid in (missing_context, missing_verifier, missing_hash):
        before = idx.read_bytes()
        _raise_code(
            HarnessError,
            "HARNESS_EVIDENCE_INGEST_INVALID",
            lambda invalid=invalid: evidence_index.add_entry(idx, invalid, catalog=catalog),
        )
        assert idx.read_bytes() == before

    # 載入端也必須拒絕封閉 schema 之外的欄位。
    malformed_idx = fakes_evidence.evidence_index(
        tmp_path / "unknown-field", stage="3", contract="c" * 40
    )
    malformed = json.loads(malformed_idx.read_bytes())
    from dataclasses import asdict

    raw_restricted = fakes_evidence.restricted_entry(
        tmp_path / "unknown-field", "S3-ROLLBACK-LINUX", platform="linux"
    )
    malformed["entries"].append({**asdict(raw_restricted), "private_path": "hidden"})
    _write_json(malformed_idx, malformed)
    _raise_code(
        HarnessError,
        "HARNESS_MANIFEST_INVALID",
        lambda: evidence_index.load_index(malformed_idx),
    )


def test_default_missing_canary_does_not_block_required_stage(tmp_path, monkeypatch):
    _allow_unrelated_public_evidence_for_rule_test(monkeypatch)
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = api.load_exit_catalog()
    required_id = "S3-OD09-ALL-FROZEN"
    reference_ids = ("S3-CANARY-LINUX", "S3-CANARY-WINDOWS", "S3-INCIDENTS-CLOSED")
    subset = _stage3_subset(catalog, required_id, *reference_ids)
    for platform in ("linux", "windows"):
        api.add_entry(
            idx,
            fakes_evidence.entry(tmp_path, required_id, "PASS", platform=platform),
            catalog=catalog,
        )
    result = api.stage_status(
        api.load_index(idx),
        "3",
        catalog=subset,
        evidence_root=_evidence_root(tmp_path),
        cases={},
    )
    assert all(result["items"][item_id]["status"] == "STALE" for item_id in reference_ids)
    assert all(result["items"][item_id]["satisfying_entries"] == [] for item_id in reference_ids)
    assert result["result"] == "PASS"


def test_owner_required_canary_blocks_without_evidence(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    required = "S3-OD09-ALL-FROZEN"
    canary = "S3-CANARY-LINUX"
    subset = _stage3_subset(catalog, required, canary)
    subset[canary] = replace(subset[canary], release_requirement="required")
    for platform in ("linux", "windows"):
        evidence_index.add_entry(
            idx,
            fakes_evidence.entry(tmp_path, required, "PASS", platform=platform),
            catalog=catalog,
        )
    result = evidence_index.stage_status(
        evidence_index.load_index(idx),
        "3",
        catalog=subset,
        evidence_root=_evidence_root(tmp_path),
        cases={},
    )
    assert result["items"][canary]["status"] == "STALE"
    assert any("raw report binding unverifiable" in reason for reason in result["items"][canary]["reasons"])
    assert result["result"] == "BLOCKED"


def test_canary_role_change_after_target_makes_evidence_stale(tmp_path, monkeypatch):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    canary_id = "S3-CANARY-LINUX"
    item = catalog[canary_id]
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, canary_id, "PASS", platform="linux"),
        catalog=catalog,
    )
    before, _ = _status(idx, tmp_path, catalog, canary_id)
    assert before == "STALE"

    source_catalog = json.loads(
        (Path(__file__).resolve().parent / "acceptance" / "stage_exits.json").read_bytes()
    )
    changed = next(row for row in source_catalog["items"] if row["item_id"] == canary_id)
    changed["release_requirement"] = "required"
    changed_path = tmp_path / "stage_exits.json"
    _write_json(changed_path, source_catalog)
    monkeypatch.setattr(evidence_index, "STAGE_EXITS_PATH", changed_path)
    changed_catalog = evidence_index.load_exit_catalog(changed_path)
    status, reasons = _status(idx, tmp_path, changed_catalog, canary_id)
    assert status == "STALE"
    assert any("exit catalog changed after target freeze" in reason for reason in reasons)
    assert changed_catalog[canary_id].release_requirement == "required"


def test_reference_fail_blocks_stage_even_when_canary_is_optional(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    rollback_id = "S3-ROLLBACK-LINUX"
    canary_id = "S3-CANARY-LINUX"
    subset = _stage3_subset(catalog, rollback_id, canary_id)
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, rollback_id, "PASS", platform="linux"),
        catalog=catalog,
    )
    evidence_index.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, canary_id, "FAIL", platform="linux"),
        catalog=catalog,
    )
    result = evidence_index.stage_status(
        evidence_index.load_index(idx),
        "3",
        catalog=subset,
        evidence_root=_evidence_root(tmp_path),
        cases={},
    )
    assert result["items"][canary_id]["status"] == "FAIL"
    assert result["result"] == "FAIL"


def _set_pair_in_round(evidence_index, index_path: Path, round_id: str, root: Path, *, linux_bundle: Path | None = None):
    for platform in ("linux", "windows"):
        bundle = linux_bundle if platform == "linux" and linux_bundle is not None else root / "bundles" / platform
        evidence_index.set_target(
            index_path,
            stage="3",
            platform=platform,
            release_manifest=bundle / "release-manifest.json",
            contract_revision="c" * 40,
            tool_revision="t" * 40,
            case_list_sha256="0" * 64,
            set_by="test",
            now=fakes.ts(30, 0),
            bundle_dir=bundle,
            archive=fakes.rc_archive(bundle),
            candidate_round_id=round_id,
        )


def _new_linux_bundle(root: Path) -> Path:
    bundle = fakes_evidence.make_bundle(
        root / "bundles" / "linux-rc2", stage="3", target="x86_64-unknown-linux-gnu"
    )
    manifest_path = bundle / "release-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["created_at"] = "2026-11-30T00:00:00+00:00"
    _write_json(manifest_path, manifest)
    fakes.refresh_bundle_sums(bundle)
    return bundle


def test_fail_requires_new_rc_and_both_platforms_full_rerun(tmp_path, monkeypatch):
    _allow_unrelated_public_evidence_for_rule_test(monkeypatch)
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    ids = ("S3-ROLLBACK-LINUX", "S3-ROLLBACK-WINDOWS")
    subset = _stage3_subset(catalog, *ids)
    old_entries = []
    for item_id, platform in zip(ids, ("linux", "windows")):
        old = fakes_evidence.entry(tmp_path, item_id, "PASS", platform=platform, n=1)
        evidence_index.add_entry(idx, old, catalog=catalog)
        old_entries.append(old)
    failure = fakes_evidence.entry(tmp_path, ids[0], "FAIL", platform="linux", n=2)
    evidence_index.add_entry(idx, failure, catalog=catalog)
    before = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=subset, evidence_root=_evidence_root(tmp_path), cases={}
    )
    assert before["result"] == "FAIL"

    round_id = evidence_index.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    _set_pair_in_round(evidence_index, idx, round_id, tmp_path, linux_bundle=_new_linux_bundle(tmp_path))
    for item_id, platform in zip(ids, ("linux", "windows")):
        old_status, _ = _status(idx, tmp_path, subset, item_id)
        assert old_status == "STALE"
        fresh = fakes_evidence.entry(tmp_path, item_id, "PASS", platform=platform, n=3)
        evidence_index.add_entry(idx, fresh, catalog=catalog)
        detail, _detail_reasons, satisfying = _platform_detail(
            idx, tmp_path, subset, item_id, platform
        )
        assert detail == "SATISFIED"
        assert satisfying == [fresh.entry_id]
    after = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=subset, evidence_root=_evidence_root(tmp_path), cases={}
    )
    assert after["result"] == "BLOCKED"
    assert all(after["items"][item_id]["status"] == "STALE" for item_id in ids)
    assert all(after["items"][item_id]["satisfying_entries"] == [] for item_id in ids)
    assert all(
        any("raw report binding unverifiable" in reason for reason in after["items"][item_id]["reasons"])
        for item_id in ids
    )
    assert failure.entry_id in {row["entry_id"] for row in evidence_index.load_index(idx)["entries"]}


def test_old_round_fail_still_blocks_same_artifact(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    subset = _stage3_subset(catalog, "S3-ROLLBACK-LINUX", "S3-CANARY-LINUX")
    failure = fakes_evidence.entry(tmp_path, "S3-CANARY-LINUX", "FAIL", platform="linux")
    evidence_index.add_entry(idx, failure, catalog=catalog)
    next_round = evidence_index.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    before_retarget = idx.read_bytes()
    with pytest.raises(HarnessError) as error:
        _set_pair_in_round(evidence_index, idx, next_round, tmp_path)
    assert error.value.code == "HARNESS_ARTIFACT_MISMATCH"
    assert idx.read_bytes() == before_retarget
    result = evidence_index.stage_status(
        evidence_index.load_index(idx), "3", catalog=subset, evidence_root=_evidence_root(tmp_path), cases={}
    )
    assert result["items"]["S3-CANARY-LINUX"]["status"] == "FAIL"
    assert result["result"] == "FAIL"
    assert failure.artifact_id == evidence_index.load_index(idx)["stage_targets"]["3"]["linux"]["artifact_id"]


def test_candidate_round_requires_both_targets(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    entry = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux")
    evidence_index.add_entry(idx, entry, catalog=catalog)
    round_id = evidence_index.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    assert len(round_id) == 32 and all(char in "0123456789abcdef" for char in round_id)
    rounds_after_begin = evidence_index.load_index(idx)
    assert rounds_after_begin["current_candidate_round_id"] == round_id
    assert len(rounds_after_begin["candidate_rounds"]) == 2
    before_backwards_round = idx.read_bytes()
    with pytest.raises(HarnessError):
        evidence_index.begin_candidate_round(
            idx, rc_tag="v1.0.0-rc.1", set_by="test", now=fakes.ts(30, 0)
        )
    assert idx.read_bytes() == before_backwards_round
    old_status, _ = _status(idx, tmp_path, catalog, "S3-ROLLBACK-LINUX")
    assert old_status == "STALE"
    before = idx.read_bytes()
    _raise_code(
        HarnessError,
        "HARNESS_MANIFEST_INVALID",
        lambda: evidence_index.run_context(
            evidence_index.load_index(idx), platform="linux", run_id="before-pair"
        ),
    )
    assert idx.read_bytes() == before

    linux = tmp_path / "bundles" / "linux"
    before_unknown_round = idx.read_bytes()
    with pytest.raises(HarnessError):
        evidence_index.set_target(
            idx,
            stage="3",
            platform="linux",
            release_manifest=linux / "release-manifest.json",
            contract_revision="c" * 40,
            tool_revision="t" * 40,
            case_list_sha256="0" * 64,
            set_by="test",
            now=fakes.ts(30, 0),
            bundle_dir=linux,
            archive=fakes.rc_archive(linux),
            candidate_round_id="0" * 32,
        )
    assert idx.read_bytes() == before_unknown_round
    evidence_index.set_target(
        idx,
        stage="3",
        platform="linux",
        release_manifest=linux / "release-manifest.json",
        contract_revision="c" * 40,
        tool_revision="t" * 40,
        case_list_sha256="0" * 64,
        set_by="test",
        now=fakes.ts(30, 0),
        bundle_dir=linux,
        archive=fakes.rc_archive(linux),
        candidate_round_id=round_id,
    )
    _raise_code(
        HarnessError,
        "HARNESS_MANIFEST_INVALID",
        lambda: evidence_index.run_context(
            evidence_index.load_index(idx), platform="linux", run_id="linux-only"
        ),
    )
    with pytest.raises(HarnessError):
        evidence_index.set_target(
            idx,
            stage="3",
            platform="linux",
            release_manifest=linux / "release-manifest.json",
            contract_revision="c" * 40,
            tool_revision="t" * 40,
            case_list_sha256="0" * 64,
            set_by="test",
            now=fakes.ts(30, 1),
            bundle_dir=linux,
            archive=fakes.rc_archive(linux),
            candidate_round_id=round_id,
        )
    windows = tmp_path / "bundles" / "windows"
    evidence_index.set_target(
        idx,
        stage="3",
        platform="windows",
        release_manifest=windows / "release-manifest.json",
        contract_revision="c" * 40,
        tool_revision="t" * 40,
        case_list_sha256="0" * 64,
        set_by="test",
        now=fakes.ts(30, 0),
        bundle_dir=windows,
        archive=fakes.rc_archive(windows),
        candidate_round_id=round_id,
    )
    context = evidence_index.run_context(
        evidence_index.load_index(idx), platform="linux", run_id="linux-complete"
    )
    assert context["candidate_round_id"] == round_id
    assert set(context) == {
        "candidate_round_id",
        "rc_tag",
        "platform",
        "archive_sha256",
        "artifact_id",
        "required_cases_sha256",
        "run_id",
    }
    mismatched = json.loads(json.dumps(evidence_index.load_index(idx)))
    mismatched["stage_targets"]["3"]["windows"]["required_cases_sha256"] = "0" * 64
    with pytest.raises(HarnessError):
        evidence_index.run_context(mismatched, platform="linux", run_id="mismatched-locks")


def test_round_id_and_archive_are_entry_identity(tmp_path, monkeypatch):
    _allow_unrelated_public_evidence_for_rule_test(monkeypatch)
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    old = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-WINDOWS", "PASS", platform="windows")
    evidence_index.add_entry(idx, old, catalog=catalog)
    next_round = evidence_index.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    _set_pair_in_round(evidence_index, idx, next_round, tmp_path)
    status, _ = _status(idx, tmp_path, catalog, "S3-ROLLBACK-WINDOWS")
    assert status == "STALE"
    new = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-WINDOWS", "PASS", platform="windows", n=2)
    evidence_index.add_entry(idx, new, catalog=catalog)
    assert _platform_detail(idx, tmp_path, catalog, "S3-ROLLBACK-WINDOWS", "windows")[0] == "SATISFIED"
    assert _status(idx, tmp_path, catalog, "S3-ROLLBACK-WINDOWS")[0] == "STALE"

    data = evidence_index.load_index(idx)
    new_row = next(row for row in data["entries"] if row["entry_id"] == new.entry_id)
    for field, invalid_value in (
        ("candidate_round_id", "0" * 32),
        ("archive_sha256", "f" * 64),
    ):
        corrupted = json.loads(json.dumps(data))
        row = next(row for row in corrupted["entries"] if row["entry_id"] == new.entry_id)
        row[field] = invalid_value
        status, _ = evidence_index.item_status(
            corrupted,
            catalog["S3-ROLLBACK-WINDOWS"],
            evidence_root=_evidence_root(tmp_path),
            cases={},
        )
        assert status == "STALE"


def test_od09_and_legacy_round_exceptions(tmp_path, monkeypatch):
    _allow_unrelated_public_evidence_for_rule_test(monkeypatch)
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    frozen_path = Path(__file__).resolve().parents[1] / "realhw" / "acceptance" / "longrun_acceptance.yaml"
    frozen_before = frozen_path.read_bytes()
    entries = []
    for platform in ("linux", "windows"):
        current = fakes_evidence.entry(tmp_path, "S3-OD09-ALL-FROZEN", "PASS", platform=platform)
        evidence_index.add_entry(idx, current, catalog=catalog)
        entries.append(current)
    round_id = evidence_index.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    _set_pair_in_round(evidence_index, idx, round_id, tmp_path)
    assert frozen_path.read_bytes() == frozen_before
    for current in entries:
        old_status, _, _ = _platform_detail(
            idx, tmp_path, catalog, "S3-OD09-ALL-FROZEN", current.platform
        )
        assert old_status == "STALE"
    status, reasons = _status(idx, tmp_path, catalog, "S3-OD09-ALL-FROZEN")
    assert status == "STALE"
    assert any("candidate_round_id" in reason for reason in reasons)
    fresh_entries = []
    for platform in ("linux", "windows"):
        fresh = fakes_evidence.entry(tmp_path, "S3-OD09-ALL-FROZEN", "PASS", platform=platform, n=2)
        evidence_index.add_entry(idx, fresh, catalog=catalog)
        fresh_entries.append(fresh)
    assert all(
        _platform_detail(idx, tmp_path, catalog, "S3-OD09-ALL-FROZEN", platform)[0] == "SATISFIED"
        for platform in ("linux", "windows")
    )
    assert _status(idx, tmp_path, catalog, "S3-OD09-ALL-FROZEN")[0] == "SATISFIED"
    historical = fakes_evidence.evidence_index(
        tmp_path / "history", stage="2.4", contract="c" * 40, platforms=("linux",)
    )
    hcat = evidence_index.load_exit_catalog()
    hentry = fakes_evidence.entry(
        tmp_path / "history", "S24-L3-LRMIXED-48H-LINUX", "PASS", platform="linux"
    )
    evidence_index.add_entry(historical, hentry, catalog=hcat)
    assert _status(historical, tmp_path / "history", hcat, hentry.item_id)[0] == "SATISFIED"


def test_target_rejects_shrunken_required_cases(tmp_path, monkeypatch):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    from tools.stage3 import required_cases

    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    bundle = tmp_path / "bundles" / "linux"
    round_id = evidence_index.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    original = required_cases.validate_current_lock
    monkeypatch.setattr(
        required_cases,
        "validate_current_lock",
        lambda *args, **kwargs: ["分母鎖不可刪除既有案例：S3-L15-RELEASE-AB-WINDOWS/windows"],
    )
    before = idx.read_bytes()
    _raise_code(
        HarnessError,
        "HARNESS_MANIFEST_INVALID",
        lambda: evidence_index.set_target(
            idx,
            stage="3",
            platform="linux",
            release_manifest=bundle / "release-manifest.json",
            contract_revision="c" * 40,
            tool_revision="u" * 40,
            case_list_sha256="0" * 64,
            set_by="test",
            now=fakes.ts(30, 1),
            bundle_dir=bundle,
            archive=fakes.rc_archive(bundle),
            candidate_round_id=round_id,
        ),
    )
    assert idx.read_bytes() == before
    monkeypatch.setattr(required_cases, "validate_current_lock", original)


def test_required_cases_lock_growth_stales_old_entries(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    item_id = "S3-L2-FULL-AB-LINUX"
    old = fakes_evidence.entry(tmp_path, item_id, "PASS", platform="linux")
    evidence_index.add_entry(idx, old, catalog=catalog)
    base_lock_path = Path(__file__).resolve().parent / "acceptance" / "stage3_required_cases.lock.json"
    grown_lock = json.loads(base_lock_path.read_bytes())
    grown_lock["exits"][item_id]["linux"]["case_ids"].append("fixture-added-required-case")
    grown_path = tmp_path / "grown-required-cases.lock.json"
    _write_json(grown_path, grown_lock)
    status, reasons = evidence_index.item_status(
        evidence_index.load_index(idx),
        catalog[item_id],
        evidence_root=_evidence_root(tmp_path),
        cases={},
        required_lock=grown_path,
    )
    assert status == "STALE"
    assert any("required cases changed" in reason for reason in reasons)

    evidence_root = _evidence_root(tmp_path)
    target = evidence_index.load_index(idx)["stage_targets"]["3"]["linux"]
    frozen = target["required_cases_lock"]
    frozen_path = evidence_root / frozen["path"]
    frozen_path.write_bytes(b"tampered lock")
    status, _ = evidence_index.item_status(
        evidence_index.load_index(idx),
        catalog[item_id],
        evidence_root=evidence_root,
        cases={},
    )
    assert status in {"HASH_MISMATCH", "STALE", "MISSING"}


def test_required_seed_rejects_attachment_sha_mismatch(tmp_path):
    from tools.stage3 import required_cases

    fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    evidence_root = _evidence_root(tmp_path)
    manifest_path = evidence_root / "denominators" / "source-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    attachment = evidence_root / manifest["sources"][0]["attachment"]
    attachment.write_bytes(attachment.read_bytes() + b"changed")
    errors = required_cases.verify_seed_sources(
        manifest, evidence_root=evidence_root, source_reader=fakes_evidence.read_seed_revision
    )
    assert errors


def test_legacy_scope_covers_all_required_obligations(tmp_path):
    evidence_index = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    evidence_root = _evidence_root(tmp_path)
    assert evidence_index.validate_legacy_scope(
        evidence_index.load_index(idx), catalog=catalog, evidence_root=evidence_root
    ) == []
    scenarios = ("missing-catalog-platform", "missing-source-obligation", "duplicate-key", "missing-source-map")
    for scenario in scenarios:
        root = tmp_path / scenario
        bad_idx = fakes_evidence.evidence_index(root, stage="3", contract="c" * 40)
        bad_evidence_root = _evidence_root(root)
        pointer = evidence_index.load_index(bad_idx)["legacy_obligations"]["scope"]
        scope_path = bad_evidence_root / pointer["path"]
        scope = json.loads(scope_path.read_bytes())
        if scenario == "missing-catalog-platform":
            target_row = next(
                row for row in scope["obligations"]
                if row["item_id"] == "S0-L1-BC" and row["platform"] == "windows"
            )
            scope["obligations"].remove(target_row)
        elif scenario == "missing-source-obligation":
            scope["obligations"] = [
                row for row in scope["obligations"] if not row["item_id"].startswith("LEGACY-")
            ]
        elif scenario == "duplicate-key":
            scope["obligations"].append(dict(scope["obligations"][0]))
        else:
            missing_ref = scope["obligations"][0]["source_refs"][0]
            scope["sources"] = [row for row in scope["sources"] if row["source_id"] != missing_ref]
        _write_json(scope_path, scope)
        _refresh_scope_review(bad_idx, bad_evidence_root)
        errors = evidence_index.validate_legacy_scope(
            evidence_index.load_index(bad_idx), catalog=catalog, evidence_root=bad_evidence_root
        )
        assert errors

        if scenario == "missing-catalog-platform":
            round_id = evidence_index.begin_candidate_round(
                bad_idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
            )
            before = bad_idx.read_bytes()
            bundle = root / "bundles" / "linux"
            _raise_code(
                HarnessError,
                "HARNESS_MANIFEST_INVALID",
                lambda: evidence_index.set_target(
                    bad_idx,
                    stage="3",
                    platform="linux",
                    release_manifest=bundle / "release-manifest.json",
                    contract_revision="c" * 40,
                    tool_revision="t" * 40,
                    case_list_sha256="0" * 64,
                    set_by="test",
                    now=fakes.ts(30, 1),
                    bundle_dir=bundle,
                    archive=fakes.rc_archive(bundle),
                    candidate_round_id=round_id,
                ),
            )
            assert bad_idx.read_bytes() == before

    missing_review_root = tmp_path / "missing-review"
    missing_review_idx = fakes_evidence.evidence_index(
        missing_review_root, stage="3", contract="c" * 40
    )
    missing_review_evidence = _evidence_root(missing_review_root)
    missing_review_index = evidence_index.load_index(missing_review_idx)
    missing_review_pointer = missing_review_index["legacy_obligations"]["scope"]
    missing_review_path = missing_review_evidence / missing_review_pointer["path"]
    missing_review_scope = json.loads(missing_review_path.read_bytes())
    missing_review_scope["review"]["record"] = {
        "path": "missing-review-record.json",
        "sha256": "0" * 64,
    }
    _write_json(missing_review_path, missing_review_scope)
    missing_review_pointer["sha256"] = hashlib.sha256(missing_review_path.read_bytes()).hexdigest()
    _write_json(missing_review_idx, missing_review_index)
    assert evidence_index.validate_legacy_scope(
        evidence_index.load_index(missing_review_idx),
        catalog=catalog,
        evidence_root=missing_review_evidence,
    )

    bad_source_root = tmp_path / "bad-source-snapshot"
    bad_source_idx = fakes_evidence.evidence_index(
        bad_source_root, stage="3", contract="c" * 40
    )
    bad_source_evidence = _evidence_root(bad_source_root)
    bad_source_scope = json.loads(
        (bad_source_evidence / evidence_index.load_index(bad_source_idx)["legacy_obligations"]["scope"]["path"]).read_bytes()
    )
    source_snapshot = bad_source_evidence / bad_source_scope["sources"][0]["snapshot"]["path"]
    source_snapshot.write_bytes(source_snapshot.read_bytes() + b"tampered")
    assert evidence_index.validate_legacy_scope(
        evidence_index.load_index(bad_source_idx),
        catalog=catalog,
        evidence_root=bad_source_evidence,
    )


@pytest.mark.parametrize("method", ["pending", "missing_entry", "wrong_target_hash", "cycle"])
def test_legacy_resolution_revalidates_evidence(tmp_path, method):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    evidence_root = _evidence_root(tmp_path)
    index = evidence_index.load_index(idx)
    scope = json.loads((evidence_root / index["legacy_obligations"]["scope"]["path"]).read_bytes())
    rows = []
    for i, obligation in enumerate(scope["obligations"]):
        row = {"key": obligation["key"], "method": "pending"}
        if i == 0 and method != "pending":
            row = {"key": obligation["key"], "method": "retest", "entry_id": "f" * 20}
            if method == "wrong_target_hash":
                row["target_sha256"] = "0" * 64
            if method == "cycle":
                row["files"] = [{"path": "../outside.json", "sha256": "a" * 64}]
        rows.append(row)
    resolutions = {
        "schema_version": 1,
        "scope_sha256": index["legacy_obligations"]["scope"]["sha256"],
        "resolutions": rows,
    }
    resolution_path = evidence_root / "legacy-obligations-resolutions.json"
    _write_json(resolution_path, resolutions)
    index["legacy_obligations"]["resolutions"] = {
        "path": resolution_path.relative_to(evidence_root).as_posix(),
        "sha256": hashlib.sha256(resolution_path.read_bytes()).hexdigest(),
    }
    _write_json(idx, index)
    status = evidence_index.legacy_status(
        evidence_index.load_index(idx), catalog=catalog, evidence_root=evidence_root, cases={}
    )
    assert status["result"] == "BLOCKED"
    assert status["obligations"][rows[0]["key"]]["status"] != "PASS"


def test_legacy_valid_retest_resolves_only_covered_obligation(tmp_path):
    """有效的歷史 target、entry 與附件只解決其清冊義務。"""
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    evidence_root = _evidence_root(tmp_path)
    item_id = "S0-L15-FUNCTEST"
    platform = "linux"
    bundle = fakes_evidence.make_bundle(
        tmp_path / "legacy-bundles" / platform,
        stage="0",
        target="x86_64-unknown-linux-gnu",
    )
    evidence_index.set_target(
        idx,
        stage="0",
        platform=platform,
        release_manifest=bundle / "release-manifest.json",
        contract_revision="c" * 40,
        tool_revision="t" * 40,
        case_list_sha256="0" * 64,
        set_by="test",
        now=fakes.ts(1, 0),
    )
    entry = fakes_evidence.entry(
        tmp_path,
        item_id,
        "PASS",
        platform=platform,
        n=5,
    )
    evidence_index.add_entry(idx, entry, catalog=catalog)
    target = evidence_index.load_index(idx)["stage_targets"]["0"][platform]
    target_path = evidence_root / "legacy-targets" / "stage0-linux.json"
    _write_json(target_path, target)

    index = evidence_index.load_index(idx)
    scope = json.loads(
        (evidence_root / index["legacy_obligations"]["scope"]["path"]).read_bytes()
    )
    selected = next(
        row for row in scope["obligations"]
        if row["item_id"] == item_id and row["platform"] == platform
    )
    resolutions = [
        {"key": row["key"], "method": "pending"}
        for row in scope["obligations"]
    ]
    resolution = next(row for row in resolutions if row["key"] == selected["key"])
    target_bytes = target_path.read_bytes()
    resolution.update(
        {
            "method": "retest",
            "entry_id": entry.entry_id,
            "target_sha256": hashlib.sha256(target_bytes).hexdigest(),
            "files": [
                {
                    "path": target_path.relative_to(evidence_root).as_posix(),
                    "sha256": hashlib.sha256(target_bytes).hexdigest(),
                }
            ],
        }
    )
    resolution_path = evidence_root / "legacy-obligations-resolutions.json"
    _write_json(
        resolution_path,
        {
            "schema_version": 1,
            "scope_sha256": index["legacy_obligations"]["scope"]["sha256"],
            "resolutions": resolutions,
        },
    )
    index["legacy_obligations"]["resolutions"] = {
        "path": resolution_path.relative_to(evidence_root).as_posix(),
        "sha256": hashlib.sha256(resolution_path.read_bytes()).hexdigest(),
    }
    _write_json(idx, index)
    status = evidence_index.legacy_status(
        evidence_index.load_index(idx),
        catalog=catalog,
        evidence_root=evidence_root,
        cases={},
    )
    assert status["result"] == "BLOCKED"
    assert status["obligations"][selected["key"]]["entry_ids"] == [entry.entry_id]


def test_legacy_new_od_requires_exact_named_scope(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    evidence_root = _evidence_root(tmp_path)
    index = evidence_index.load_index(idx)
    scope = json.loads((evidence_root / index["legacy_obligations"]["scope"]["path"]).read_bytes())
    rows = [
        {"key": row["key"], "method": "pending"}
        for row in scope["obligations"]
    ]
    rows[0] = {
        "key": rows[0]["key"],
        "method": "new_od",
        "decision_ref": "OD-UNRELATED",
        "evidence": [],
    }
    resolutions_path = evidence_root / "legacy-obligations-resolutions.json"
    _write_json(
        resolutions_path,
        {
            "schema_version": 1,
            "scope_sha256": index["legacy_obligations"]["scope"]["sha256"],
            "resolutions": rows,
        },
    )
    index["legacy_obligations"]["resolutions"] = {
        "path": resolutions_path.relative_to(evidence_root).as_posix(),
        "sha256": hashlib.sha256(resolutions_path.read_bytes()).hexdigest(),
    }
    _write_json(idx, index)
    status = evidence_index.legacy_status(
        evidence_index.load_index(idx), catalog=catalog, evidence_root=evidence_root, cases={}
    )
    assert status["result"] == "BLOCKED"
    assert status["obligations"][rows[0]["key"]]["status"] != "PASS"


def test_legacy_retest_uses_single_platform_for_both_catalog_item(tmp_path):
    """both 目錄項可依凍結義務的平台獨立驗證 retest。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    rows, key = _od09_resolution_rows(idx, tmp_path, "retest")
    _install_legacy_resolutions(idx, _evidence_root(tmp_path), rows)
    status = api.legacy_status(
        api.load_index(idx),
        catalog=api.load_exit_catalog(),
        evidence_root=_evidence_root(tmp_path),
        cases={},
    )
    assert status["obligations"][key]["status"] == "PASS"
    assert status["result"] == "BLOCKED"


def test_legacy_reuse_revalidates_same_platform_source(tmp_path):
    """reuse 必須符合 R1-05 並遞迴確認來源 entry 仍屬 satisfying set。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    rows, key = _od09_resolution_rows(idx, tmp_path, "reuse")
    _install_legacy_resolutions(idx, _evidence_root(tmp_path), rows)
    status = api.legacy_status(
        api.load_index(idx),
        catalog=api.load_exit_catalog(),
        evidence_root=_evidence_root(tmp_path),
        cases={},
    )
    assert status["obligations"][key]["status"] == "PASS"
    assert status["result"] == "BLOCKED"


@pytest.mark.parametrize("bad", ["key", "platform", "scope", "no_alternative", "reviewer"])
def test_legacy_new_od_requires_exact_named_decision_and_alternative(tmp_path, bad):
    """new_od 逐筆核對原 OD、義務、平台、範圍、核定人及替代附件。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    _indexed, obligation = _make_noncatalog_od09_obligation(idx, _evidence_root(tmp_path))
    evidence_root = _evidence_root(tmp_path)
    index = api.load_index(idx)
    scope = json.loads((evidence_root / index["legacy_obligations"]["scope"]["path"]).read_bytes())
    rows = [{"key": row["key"], "method": "pending"} for row in scope["obligations"]]
    resolution = _named_od_resolution(evidence_root, obligation, bad=bad)
    rows[next(i for i, row in enumerate(rows) if row["key"] == obligation["key"])] = resolution
    _install_legacy_resolutions(idx, evidence_root, rows)
    status = api.legacy_status(
        api.load_index(idx),
        catalog=api.load_exit_catalog(),
        evidence_root=evidence_root,
        cases={},
    )
    assert status["obligations"][obligation["key"]]["status"] != "PASS"


def test_legacy_new_od_accepts_exact_named_scope_with_alternative(tmp_path):
    """完整具名核定及非 BO-P9-19 靜態替代證據可解決單筆義務。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    _indexed, obligation = _make_noncatalog_od09_obligation(idx, _evidence_root(tmp_path))
    evidence_root = _evidence_root(tmp_path)
    scope = json.loads(
        (evidence_root / api.load_index(idx)["legacy_obligations"]["scope"]["path"]).read_bytes()
    )
    rows = [{"key": row["key"], "method": "pending"} for row in scope["obligations"]]
    rows[next(i for i, row in enumerate(rows) if row["key"] == obligation["key"])] = _named_od_resolution(
        evidence_root, obligation
    )
    _install_legacy_resolutions(idx, evidence_root, rows)
    status = api.legacy_status(
        api.load_index(idx),
        catalog=api.load_exit_catalog(),
        evidence_root=evidence_root,
        cases={},
    )
    assert status["obligations"][obligation["key"]]["status"] == "PASS"
    assert status["result"] == "BLOCKED"


def test_legacy_new_od_stays_blocked_for_bop919_required_kind(tmp_path):
    """具名新 OD 不繞過仍受 BO-P9-19 約束的必要執行來源。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    evidence_root = _evidence_root(tmp_path)
    _indexed, obligation = _make_noncatalog_od09_obligation(
        idx, evidence_root, evidence_kind="manual-procedure"
    )
    scope = json.loads(
        (evidence_root / api.load_index(idx)["legacy_obligations"]["scope"]["path"]).read_bytes()
    )
    rows = [{"key": row["key"], "method": "pending"} for row in scope["obligations"]]
    rows[next(i for i, row in enumerate(rows) if row["key"] == obligation["key"])] = _named_od_resolution(
        evidence_root, obligation
    )
    _install_legacy_resolutions(idx, evidence_root, rows)
    status = api.legacy_status(
        api.load_index(idx),
        catalog=api.load_exit_catalog(),
        evidence_root=evidence_root,
        cases={},
    )
    result = status["obligations"][obligation["key"]]
    assert result["status"] == "BLOCKED"
    assert any("BO-P9-19" in reason for reason in result["reasons"])


@pytest.mark.parametrize("corruption", ["missing_key", "target_scope_sha"])
def test_legacy_resolution_set_and_target_scope_must_match(tmp_path, corruption):
    """resolution 鍵須完整，兩平台 target 的 scope SHA 須相同且正確。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    rows, key = _od09_resolution_rows(idx, tmp_path, "retest")
    if corruption == "missing_key":
        rows.pop()
    _install_legacy_resolutions(idx, _evidence_root(tmp_path), rows)
    if corruption == "target_scope_sha":
        index = api.load_index(idx)
        index["stage_targets"]["3"]["windows"]["legacy_scope_sha256"] = "0" * 64
        _write_json(idx, index)
    status = api.legacy_status(
        api.load_index(idx),
        catalog=api.load_exit_catalog(),
        evidence_root=_evidence_root(tmp_path),
        cases={},
    )
    assert all(row["status"] != "PASS" for row in status["obligations"].values())
    assert status["obligations"][key]["status"] == "BLOCKED"


def test_bop919_public_positive_stays_blocked_pending_owner_decision(tmp_path):
    evidence_index = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = evidence_index.load_exit_catalog()
    status = evidence_index.legacy_status(
        evidence_index.load_index(idx),
        catalog=catalog,
        evidence_root=_evidence_root(tmp_path),
        cases={},
    )
    assert status["result"] == "BLOCKED"
    assert status["obligations"]


@pytest.mark.parametrize(
    "payload",
    [
        {"private_source_sha256": "a" * 64, "failure_summary": "去識別摘要"},
        {
            "private_source_sha256": "b" * 64,
            "redacted_crc32": "c" * 8,
            "crc_checked": True,
            "payload": "[redacted]",
        },
    ],
    ids=("hash-only", "redacted-crc"),
)
def test_hash_only_or_redacted_crc_cannot_satisfy_a8(tmp_path, payload):
    """未有核定替代驗證時，公開 hash 或遮罩 CRC 一律保持未滿足。"""
    evidence_index = _evidence_api()
    catalog = evidence_index.load_exit_catalog()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    item_id = "S3-L3-P0P1-REMOTE-LINUX"
    evidence_root = _evidence_root(tmp_path)
    report = evidence_root / "restricted-source-summary.json"
    _write_json(report, payload)
    frozen_file = _ab_manifest_api().FrozenFile(
        path=report.relative_to(evidence_root).as_posix(),
        sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
    )
    public_summary = fakes_evidence.entry(
        tmp_path,
        item_id,
        "PASS",
        platform="linux",
        files=(frozen_file,),
    )
    evidence_index.add_entry(idx, public_summary, catalog=catalog)
    status, reasons = evidence_index.item_status(
        evidence_index.load_index(idx),
        catalog[item_id],
        evidence_root=evidence_root,
        cases={},
    )
    assert status == "STALE"
    assert any("BO-P9-19" in reason for reason in reasons)


def test_concurrent_fail_entries_are_not_lost(tmp_path, monkeypatch):
    """同時寫入的正式 FAIL 都留在同一份索引。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = api.load_exit_catalog()
    entries = [
        fakes_evidence.restricted_entry(
            tmp_path,
            "S3-ROLLBACK-LINUX",
            platform="linux",
            failure_summary=f"並行失敗 {number}",
            context_id=f"{number:064x}",
        )
        for number in (1, 2)
    ]
    original_write = api._write_index

    def delayed_write(path, value):
        time.sleep(0.05)
        original_write(path, value)

    monkeypatch.setattr(api, "_write_index", delayed_write)
    ready = threading.Barrier(3)
    errors = []

    def append(row):
        try:
            ready.wait()
            api.add_entry(idx, row, catalog=catalog)
        except BaseException as exc:  # 由主執行緒統一回報 worker 錯誤。
            errors.append(exc)

    workers = [threading.Thread(target=append, args=(row,)) for row in entries]
    for worker in workers:
        worker.start()
    ready.wait()
    for worker in workers:
        worker.join(timeout=10)
    assert not errors
    indexed = {row["entry_id"] for row in api.load_index(idx)["entries"]}
    assert {row.entry_id for row in entries} <= indexed


def test_concurrent_targets_and_round_switch_keep_all_writes(tmp_path, monkeypatch):
    """兩平台 target 與輪次切換／FAIL 寫入交錯時不互相覆蓋。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = api.load_exit_catalog()
    old_fail = fakes_evidence.restricted_entry(
        tmp_path,
        "S3-ROLLBACK-LINUX",
        platform="linux",
        failure_summary="交錯寫入 FAIL",
        context_id="e" * 64,
    )
    original_write = api._write_index

    def delayed_write(path, value):
        time.sleep(0.05)
        original_write(path, value)

    monkeypatch.setattr(api, "_write_index", delayed_write)
    ready = threading.Barrier(3)
    errors = []

    def run(call):
        try:
            ready.wait()
            call()
        except BaseException as exc:
            errors.append(exc)

    round_call = lambda: api.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    fail_call = lambda: api.add_entry(idx, old_fail, catalog=catalog)
    workers = [threading.Thread(target=run, args=(round_call,)), threading.Thread(target=run, args=(fail_call,))]
    for worker in workers:
        worker.start()
    ready.wait()
    for worker in workers:
        worker.join(timeout=10)
    assert not errors
    indexed = api.load_index(idx)
    assert indexed["current_candidate_round_id"]
    assert any(row["entry_id"] == old_fail.entry_id for row in indexed["entries"])

    # 兩平台 target 更新也必須保留在同一索引。
    target_root = tmp_path / "concurrent-targets"
    target_idx = fakes_evidence.evidence_index(target_root, stage="2.4", contract="c" * 40, platforms=())
    bundles = {
        platform: fakes_evidence.make_bundle(
            target_root / "bundles" / platform,
            stage="2.4",
            target="x86_64-pc-windows-msvc" if platform == "windows" else "x86_64-unknown-linux-gnu",
        )
        for platform in ("linux", "windows")
    }
    target_errors = []
    ready = threading.Barrier(3)

    def set_one(platform):
        bundle = bundles[platform]
        try:
            ready.wait()
            api.set_target(
                target_idx,
                stage="2.4",
                platform=platform,
                release_manifest=bundle / "release-manifest.json",
                contract_revision="c" * 40,
                tool_revision="t" * 40,
                case_list_sha256="0" * 64,
                set_by="test",
                now=fakes.ts(30, 1),
                bundle_dir=bundle,
            )
        except BaseException as exc:
            target_errors.append(exc)

    workers = [threading.Thread(target=set_one, args=(platform,)) for platform in ("linux", "windows")]
    for worker in workers:
        worker.start()
    ready.wait()
    for worker in workers:
        worker.join(timeout=10)
    assert not target_errors
    target_index = api.load_index(target_idx)
    assert set(target_index["stage_targets"]["2.4"]) == {"linux", "windows"}


def test_stage3_repackaged_old_context_is_stale(tmp_path):
    """外層改成新輪次但保留舊 context 的 entry 不得滿足必要項。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = api.load_exit_catalog()
    previous = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-WINDOWS", "PASS", platform="windows", n=1)
    api.add_entry(idx, previous, catalog=catalog)
    round_id = api.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    _set_pair_in_round(api, idx, round_id, tmp_path)
    current_target = api.load_index(idx)["stage_targets"]["3"]["windows"]
    wrapped = replace(
        previous,
        candidate_round_id=round_id,
        archive_sha256=current_target["archive_sha256"],
        required_cases_sha256=current_target["required_cases_sha256"],
    )
    wrapped = replace(wrapped, entry_id=fakes_evidence._entry_id(wrapped))
    api.add_entry(idx, wrapped, catalog=catalog)
    status, reasons = _status(idx, tmp_path, catalog, "S3-ROLLBACK-WINDOWS")
    assert status == "STALE"
    assert any("context" in reason.lower() for reason in reasons)


def test_stage3_context_rejects_unregistered_report_references(tmp_path):
    """T1 未定義的 context 報告引用不作為可核驗來源接受。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = api.load_exit_catalog()
    original = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux", n=20)
    api.add_entry(idx, original, catalog=catalog)

    evidence_root = _evidence_root(tmp_path)
    context_file = original.files[-1]
    context_path = evidence_root / context_file.path
    context = json.loads(context_path.read_bytes())
    context["source_refs"] = [{
        "path": original.files[0].path,
        "sha256": original.files[0].sha256,
    }]
    _write_json(context_path, context)
    updated_context_file = replace(
        context_file,
        sha256=hashlib.sha256(context_path.read_bytes()).hexdigest(),
    )
    updated = replace(original, files=original.files[:-1] + (updated_context_file,))
    updated = replace(updated, entry_id=fakes_evidence._entry_id(updated))
    indexed = api.load_index(idx)
    indexed["entries"] = [api._entry_dict(updated)]
    _write_json(idx, indexed)

    status, reasons = _status(idx, tmp_path, catalog, original.item_id)
    assert status == "STALE"
    assert any("context" in reason.lower() for reason in reasons)


def test_stage3_required_entry_cannot_reuse_prior_evidence(tmp_path):
    """最後 RC 的 Stage 3 必要執行項不得沿用其他 entry。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = api.load_exit_catalog()
    source = fakes_evidence.entry(tmp_path, "S3-CANARY-LINUX", "PASS", platform="linux", n=1)
    api.add_entry(idx, source, catalog=catalog)
    current = fakes_evidence.entry(
        tmp_path,
        "S3-ROLLBACK-LINUX",
        "PASS",
        platform="linux",
        n=2,
        reuse_of=source.entry_id,
    )
    api.add_entry(idx, current, catalog=catalog)
    status, reasons = _status(idx, tmp_path, catalog, "S3-ROLLBACK-LINUX")
    assert status != "SATISFIED"
    assert any("Stage 3" in reason and "reuse" in reason.lower() for reason in reasons)


@pytest.mark.parametrize("mismatch", ["platform", "kind", "required_case_set"])
def test_reuse_requires_matching_platform_kind_and_frozen_cases(tmp_path, mismatch):
    """reuse 必須對齊平台、證據種類與凍結的必要案例集合。"""
    api = _evidence_api()
    root = tmp_path / mismatch
    idx, catalog, source, current = _reuse_fixture(root)
    indexed = api.load_index(idx)
    by_id = {row["entry_id"]: row for row in indexed["entries"]}
    current_row = by_id[current.entry_id]
    source_row = by_id[source.entry_id]
    checked_catalog = dict(catalog)
    target_item = checked_catalog[current.item_id]
    source_item = checked_catalog[source.item_id]
    if mismatch == "platform":
        current_row["platform"] = "windows"
    elif mismatch == "kind":
        checked_catalog[source.item_id] = replace(source_item, evidence_kind="realhw-report")
    else:
        checked_catalog[source.item_id] = replace(source_item, required_case_set="different-frozen-set")
    del target_item
    ok, _reason = api._reuse_ok(
        current_row,
        by_id,
        _evidence_root(root),
        index=indexed,
        catalog=checked_catalog,
        cases={},
        l1_lock=None,
    )
    assert not ok


def test_begin_candidate_round_rejects_same_rc_artifact_change(tmp_path):
    """同一 RC 標籤不得改綁另一份 bundle artifact。"""
    api = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40, platforms=("linux",))
    original = tmp_path / "bundles" / "linux"
    round_id = api.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.1", set_by="test", now=fakes.ts(30, 0)
    )
    changed = fakes_evidence.make_bundle(
        tmp_path / "same-rc-different-artifact", stage="3", target="x86_64-unknown-linux-gnu"
    )
    manifest = json.loads((changed / "release-manifest.json").read_bytes())
    manifest["created_at"] = "2026-11-30T00:00:00+00:00"
    _write_json(changed / "release-manifest.json", manifest)
    fakes.refresh_bundle_sums(changed)
    before = idx.read_bytes()
    with pytest.raises(HarnessError) as error:
        api.set_target(
            idx,
            stage="3",
            platform="linux",
            release_manifest=changed / "release-manifest.json",
            contract_revision="c" * 40,
            tool_revision="t" * 40,
            case_list_sha256="0" * 64,
            set_by="test",
            now=fakes.ts(30, 1),
            bundle_dir=changed,
            archive=fakes.rc_archive(changed),
            candidate_round_id=round_id,
        )
    assert error.value.code in {"HARNESS_ARTIFACT_MISMATCH", "HARNESS_MANIFEST_INVALID"}
    assert "rc" in str(error.value).lower() or "artifact" in str(error.value).lower(), str(error.value)
    assert idx.read_bytes() == before
    assert original.exists()


def test_formal_fail_requires_next_candidate_round(tmp_path):
    """目前 artifact 已有正式 FAIL 時，新輪必須使用緊接的下一個 RC。"""
    api = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = api.load_exit_catalog()
    api.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "FAIL", platform="linux", n=12),
        catalog=catalog,
    )
    before = idx.read_bytes()
    with pytest.raises(HarnessError):
        api.begin_candidate_round(idx, rc_tag="v1.0.0-rc.3", set_by="test", now=fakes.ts(12, 0))
    assert idx.read_bytes() == before


def test_bop919_blocks_any_stub_for_affected_required_kind(tmp_path):
    """原始報告無可驗證綁定時維持 STALE，並保留 BO-P9-19 原因。"""
    api = _evidence_api()
    catalog = api.load_exit_catalog()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    item_id = "S3-ROLLBACK-LINUX"
    api.add_entry(idx, fakes_evidence.entry(tmp_path, item_id, "PASS", platform="linux"), catalog=catalog)
    status, reasons = _status(idx, tmp_path, catalog, item_id)
    assert status == "STALE"
    assert any("BO-P9-19 pending" in reason for reason in reasons)
    assert any("raw report binding unverifiable" in reason for reason in reasons)


@pytest.mark.parametrize(
    ("item_id", "suffix", "payload"),
    [
        (
            "S3-L15-RELEASE-AB-LINUX",
            ".json",
            {"summary": {"source": {"private_source_sha256": "a" * 64}}},
        ),
        ("S3-L3-P0P1-REMOTE-LINUX", ".txt", "只含公開摘要，未提供可核驗執行來源。\n"),
        ("S3-ROLLBACK-LINUX", ".json", {"stub": True}),
    ],
    ids=("nested-summary-ab-kind", "other-attachment-format-realhw-kind", "arbitrary-drill-stub"),
)
def test_bop919_blocks_nested_summaries_formats_and_other_required_kinds(tmp_path, item_id, suffix, payload):
    """巢狀摘要、非 JSON 附件與其他受影響必要 kind 均保持 STALE。"""
    api = _evidence_api()
    catalog = api.load_exit_catalog()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    evidence_root = _evidence_root(tmp_path)
    report = evidence_root / f"bop919-{item_id}{suffix}"
    if isinstance(payload, str):
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(payload, encoding="utf-8")
    else:
        _write_json(report, payload)
    frozen_file = _ab_manifest_api().FrozenFile(
        path=report.relative_to(evidence_root).as_posix(),
        sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
    )
    entry = fakes_evidence.entry(
        tmp_path,
        item_id,
        "PASS",
        platform="linux",
        files=(frozen_file,),
    )
    api.add_entry(idx, entry, catalog=catalog)
    status, reasons = _status(idx, tmp_path, catalog, item_id)
    assert status == "STALE"
    assert any("BO-P9-19 pending" in reason for reason in reasons)
    assert any("raw report binding unverifiable" in reason for reason in reasons)


def test_bop919_blocks_canary_kind_when_owner_marks_it_required(tmp_path):
    """Canary 僅在目錄升為必要項時套用 BO-P9-19。"""
    api = _evidence_api()
    catalog = api.load_exit_catalog()
    item_id = "S3-CANARY-LINUX"
    required_item = replace(catalog[item_id], release_requirement="required")
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    entry = fakes_evidence.entry(tmp_path, item_id, "PASS", platform="linux", n=19)
    api.add_entry(idx, entry, catalog=catalog)
    status_args = {
        "evidence_root": _evidence_root(tmp_path),
        "cases": {},
    }
    if "catalog" in inspect.signature(api.item_status).parameters:
        status_args["catalog"] = {**catalog, item_id: required_item}
    status, reasons = api.item_status(api.load_index(idx), required_item, **status_args)
    assert status == "STALE"
    assert any("BO-P9-19 pending" in reason for reason in reasons)
    assert any("raw report binding unverifiable" in reason for reason in reasons)


def test_stage3_target_rejects_non_archive_without_mutating_index(tmp_path):
    """target 必須驗證 archive 型別與內容，失敗時 index bytes 不變。"""
    api = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40, platforms=("linux",))
    round_id = api.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    bundle = tmp_path / "bundles" / "linux"
    fake_archive = tmp_path / "not-an-archive.tar.gz"
    fake_archive.write_bytes(b"ordinary file")
    before = idx.read_bytes()
    with pytest.raises(HarnessError) as error:
        api.set_target(
            idx,
            stage="3",
            platform="linux",
            release_manifest=bundle / "release-manifest.json",
            contract_revision="c" * 40,
            tool_revision="t" * 40,
            case_list_sha256="0" * 64,
            set_by="test",
            now=fakes.ts(30, 1),
            bundle_dir=bundle,
            archive=fake_archive,
            candidate_round_id=round_id,
        )
    assert "archive" in str(error.value).lower(), str(error.value)
    assert idx.read_bytes() == before


@pytest.mark.parametrize(
    "variant",
    ["different_content", "escape", "absolute_path", "symlink_escape", "device_file", "wrong_root", "multiple_roots"],
)
def test_stage3_target_rejects_archive_bundle_mismatch_without_mutating_index(tmp_path, variant):
    """拒絕不同內容、絕對／逃逸路徑、逃逸連結、裝置檔與錯誤／多重版本根。"""
    api = _evidence_api()
    HarnessError = _harness_error_type()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40, platforms=("linux",))
    round_id = api.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    bundle = tmp_path / "bundles" / "linux"
    manifest = json.loads((bundle / "release-manifest.json").read_bytes())
    good_archive = fakes.rc_archive(bundle)
    archive = _mutated_tar_archive(
        good_archive,
        tmp_path / f"{variant}.tar.gz",
        variant,
        manifest["bundle_version"],
    )
    before = idx.read_bytes()
    with pytest.raises(HarnessError) as error:
        api.set_target(
            idx,
            stage="3",
            platform="linux",
            release_manifest=bundle / "release-manifest.json",
            contract_revision="c" * 40,
            tool_revision="t" * 40,
            case_list_sha256="0" * 64,
            set_by="test",
            now=fakes.ts(30, 1),
            bundle_dir=bundle,
            archive=archive,
            candidate_round_id=round_id,
        )
    assert error.value.code in {"HARNESS_ARTIFACT_MISMATCH", "HARNESS_MANIFEST_INVALID"}
    assert idx.read_bytes() == before


def test_item_status_revalidates_frozen_seed_attachments(tmp_path):
    """target 凍結後移除 seed 附件不得保留 SATISFIED。"""
    api = _evidence_api()
    catalog = api.load_exit_catalog()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    item_id = "S3-ROLLBACK-LINUX"
    entry = fakes_evidence.entry(tmp_path, item_id, "PASS", platform="linux")
    api.add_entry(idx, entry, catalog=catalog)
    target = api.load_index(idx)["stage_targets"]["3"]["linux"]
    lock_path = _evidence_root(tmp_path) / target["required_cases_lock"]["path"]
    assert lock_path.is_file()
    seed_path = _evidence_root(tmp_path) / "denominators" / "cases.yaml"
    seed_path.unlink()
    status, reasons = _status(idx, tmp_path, catalog, item_id)
    assert status in {"HASH_MISMATCH", "STALE", "MISSING"}
    assert any("seed" in reason.lower() or "required" in reason.lower() for reason in reasons)


def test_stage_status_revalidates_frozen_seed_attachments(tmp_path):
    """stage_status 與 item_status 共用 seed 附件重驗與 fail-closed 判定。"""
    api = _evidence_api()
    catalog = api.load_exit_catalog()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    item_id = "S3-ROLLBACK-LINUX"
    api.add_entry(idx, fakes_evidence.entry(tmp_path, item_id, "PASS", platform="linux"), catalog=catalog)
    (_evidence_root(tmp_path) / "denominators" / "cases.lock.json").unlink()
    result = api.stage_status(
        api.load_index(idx),
        "3",
        catalog={item_id: catalog[item_id]},
        evidence_root=_evidence_root(tmp_path),
        cases={},
    )
    item = result["items"][item_id]
    assert item["status"] in {"HASH_MISMATCH", "STALE", "MISSING"}
    assert item["satisfying_entries"] == []
    assert result["result"] != "PASS"


def test_item_status_revalidates_required_case_governance(tmp_path):
    """target 與 entry 同指違反 T1 分母治理的鎖時也不得 SATISFIED。"""
    api = _evidence_api()
    catalog = api.load_exit_catalog()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    evidence_root = _evidence_root(tmp_path)
    index = api.load_index(idx)
    lock_doc = json.loads(
        (evidence_root / index["stage_targets"]["3"]["linux"]["required_cases_lock"]["path"]).read_bytes()
    )
    case_ids = lock_doc["exits"]["S3-L2-FULL-AB-LINUX"]["linux"]["case_ids"]
    assert case_ids
    case_ids.pop()
    frozen_bytes = (json.dumps(lock_doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = hashlib.sha256(frozen_bytes).hexdigest()
    relative = f"stage3-required-cases.lock.{digest}.json"
    (evidence_root / relative).write_bytes(frozen_bytes)
    (tmp_path / "tests" / "acceptance" / "stage3_required_cases.lock.json").write_bytes(frozen_bytes)
    for platform in ("linux", "windows"):
        target = index["stage_targets"]["3"][platform]
        target["required_cases_sha256"] = digest
        target["required_cases_lock"] = {"path": relative, "sha256": digest}
    for target in index["target_history"]:
        if target.get("stage") == "3":
            target["required_cases_sha256"] = digest
            target["required_cases_lock"] = {"path": relative, "sha256": digest}
    _write_json(idx, index)
    entry = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux", n=18)
    api.add_entry(idx, entry, catalog=catalog)
    status, reasons = _status(idx, tmp_path, catalog, "S3-ROLLBACK-LINUX")
    assert status != "SATISFIED"
    assert any("governance" in reason.lower() or "required case" in reason.lower() for reason in reasons)


@pytest.mark.parametrize(
    "corruption",
    ["missing_scope", "pending", "pending_row", "bad_scope_hash", "invalid_resolution"],
)
def test_legacy_formal_fail_precedes_scope_errors(tmp_path, corruption):
    """適用 artifact 的已成立正式 FAIL 優先於各種清冊錯誤。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = api.load_exit_catalog()
    bundle = fakes_evidence.make_bundle(tmp_path / "legacy-target", stage="0", target="x86_64-unknown-linux-gnu")
    api.set_target(
        idx,
        stage="0",
        platform="linux",
        release_manifest=bundle / "release-manifest.json",
        contract_revision="c" * 40,
        tool_revision="t" * 40,
        case_list_sha256="0" * 64,
        set_by="test",
        now=fakes.ts(1, 0),
    )
    api.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S0-L1-BC", "FAIL", platform="linux"),
        catalog=catalog,
    )
    indexed = api.load_index(idx)
    if corruption == "missing_scope":
        indexed["legacy_obligations"]["scope"] = None
    elif corruption == "bad_scope_hash":
        indexed["legacy_obligations"]["scope"]["sha256"] = "0" * 64
    elif corruption == "invalid_resolution":
        indexed["legacy_obligations"]["resolutions"] = {"path": "../bad.json", "sha256": "0" * 64}
    elif corruption == "pending":
        indexed["legacy_obligations"]["resolutions"] = None
    elif corruption == "pending_row":
        scope_path = _evidence_root(tmp_path) / indexed["legacy_obligations"]["scope"]["path"]
        scope = json.loads(scope_path.read_bytes())
        pending_rows = [{"key": row["key"], "method": "pending"} for row in scope["obligations"]]
        indexed = _install_legacy_resolutions(idx, _evidence_root(tmp_path), pending_rows)
    _write_json(idx, indexed)
    status = api.legacy_status(indexed, catalog=catalog, evidence_root=_evidence_root(tmp_path), cases={})
    assert status["result"] == "FAIL"
    assert any(row["status"] == "FAIL" for row in status["obligations"].values())


def test_legacy_scope_fails_closed_without_source_obligation_keys(tmp_path):
    """無機器可讀來源義務鍵時清冊明確標示 SPEC-GAP。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    evidence_root = _evidence_root(tmp_path)
    indexed = api.load_index(idx)
    scope_path = evidence_root / indexed["legacy_obligations"]["scope"]["path"]
    scope = json.loads(scope_path.read_bytes())
    inventory = next(row for row in scope["sources"] if row["source_id"] == "fixture:legacy-source-inventory")
    source_path = evidence_root / inventory["snapshot"]["path"]
    source_doc = json.loads(source_path.read_bytes())
    source_doc.pop("obligations")
    _write_json(source_path, source_doc)
    inventory["snapshot"]["sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    for source in scope["sources"]:
        if source["source_id"] == inventory["source_id"]:
            continue
        source_path = evidence_root / source["snapshot"]["path"]
        source_doc = json.loads(source_path.read_bytes())
        source_doc.pop("obligations", None)
        _write_json(source_path, source_doc)
        source["snapshot"]["sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    _write_json(scope_path, scope)
    _refresh_scope_review(idx, evidence_root)
    status = api.legacy_status(
        api.load_index(idx),
        catalog=api.load_exit_catalog(),
        evidence_root=evidence_root,
        cases={},
    )
    assert status["result"] == "BLOCKED"
    assert any("SPEC-GAP" in reason for row in status["obligations"].values() for reason in row["reasons"])


def test_legacy_scope_rejects_mixed_machine_readable_and_unreadable_sources(tmp_path):
    """只盤點可讀來源而略過其他已宣告來源時，整份範圍必須 BLOCKED。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    evidence_root = _evidence_root(tmp_path)
    indexed = api.load_index(idx)
    pointer = indexed["legacy_obligations"]["scope"]
    scope_path = evidence_root / pointer["path"]
    scope = json.loads(scope_path.read_bytes())
    readable_sources = []
    for source in scope["sources"]:
        source_doc = json.loads((evidence_root / source["snapshot"]["path"]).read_bytes())
        if isinstance(source_doc.get("obligations"), list) and source_doc["obligations"]:
            readable_sources.append(source["source_id"])
    assert len(readable_sources) >= 2
    unreadable_source = next(
        row for row in scope["sources"] if row["source_id"] == readable_sources[-1]
    )
    unreadable_path = evidence_root / unreadable_source["snapshot"]["path"]
    unreadable_doc = json.loads(unreadable_path.read_bytes())
    unreadable_doc.pop("obligations", None)
    _write_json(unreadable_path, unreadable_doc)
    unreadable_source["snapshot"]["sha256"] = hashlib.sha256(unreadable_path.read_bytes()).hexdigest()
    _write_json(scope_path, scope)
    _refresh_scope_review(idx, evidence_root)
    remaining_readable = []
    for source in scope["sources"]:
        source_doc = json.loads((evidence_root / source["snapshot"]["path"]).read_bytes())
        if source_doc.get("obligations"):
            remaining_readable.append(source["source_id"])
    assert remaining_readable
    assert len(remaining_readable) == len(readable_sources) - 1
    assert unreadable_source["source_id"] not in remaining_readable
    errors = api.validate_legacy_scope(
        api.load_index(idx),
        catalog=api.load_exit_catalog(),
        evidence_root=evidence_root,
    )
    assert any("SPEC-GAP" in reason and "source" in reason.lower() for reason in errors)
    status = api.legacy_status(
        api.load_index(idx),
        catalog=api.load_exit_catalog(),
        evidence_root=evidence_root,
        cases={},
    )
    assert status["result"] == "BLOCKED"
    assert any("SPEC-GAP" in reason for row in status["obligations"].values() for reason in row["reasons"])


def test_legacy_scope_rejects_empty_inventory_even_after_scope_rows_removed(tmp_path):
    """空 inventory 不能藉由同步刪 scope 列並重算 hash 偽裝為完整。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    evidence_root = _evidence_root(tmp_path)
    indexed = api.load_index(idx)
    pointer = indexed["legacy_obligations"]["scope"]
    scope_path = evidence_root / pointer["path"]
    scope = json.loads(scope_path.read_bytes())
    inventory = next(row for row in scope["sources"] if row["source_id"] == "fixture:legacy-source-inventory")
    inventory_path = evidence_root / inventory["snapshot"]["path"]
    inventory_doc = json.loads(inventory_path.read_bytes())
    inventory_doc["obligations"] = []
    _write_json(inventory_path, inventory_doc)
    inventory["snapshot"]["sha256"] = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    scope["obligations"] = [row for row in scope["obligations"] if row["stage"] != "legacy"]
    _write_json(scope_path, scope)
    _refresh_scope_review(idx, evidence_root)

    errors = api.validate_legacy_scope(
        api.load_index(idx),
        catalog=api.load_exit_catalog(),
        evidence_root=evidence_root,
    )
    assert any("SPEC-GAP" in reason and "empty" in reason.lower() for reason in errors)
    status = api.legacy_status(
        api.load_index(idx),
        catalog=api.load_exit_catalog(),
        evidence_root=evidence_root,
        cases={},
    )
    assert status["result"] == "BLOCKED"
    assert any("SPEC-GAP" in reason for row in status["obligations"].values() for reason in row["reasons"])


@pytest.mark.parametrize("item_id", ["S3-ROLLBACK-LINUX", "S3-CANARY-LINUX"])
@pytest.mark.parametrize("report_kind", ["old_report", "arbitrary_stub"])
def test_execution_evidence_stays_stale_without_verifiable_raw_report_binding(
    tmp_path, monkeypatch, item_id, report_kind
):
    """目前 context 不足以把舊報告或任意 stub 綁定成可滿足證據。"""
    _allow_unrelated_public_evidence_for_rule_test(monkeypatch)
    api = _evidence_api()
    catalog = api.load_exit_catalog()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    evidence_root = _evidence_root(tmp_path)
    if report_kind == "old_report":
        report_doc = {
            "item_id": item_id,
            "result": "PASS",
            "artifact_id": "previous-artifact",
            "candidate_round_id": "0" * 32,
            "report_revision": "previous-run",
        }
    else:
        report_doc = {"arbitrary": "stub", "claimed_result": "PASS"}
    report_path = evidence_root / "unbound" / f"{item_id}-{report_kind}.json"
    _write_json(report_path, report_doc)
    from tools.stage3.ab_manifest import FrozenFile

    report_file = FrozenFile(
        path=report_path.relative_to(evidence_root).as_posix(),
        sha256=hashlib.sha256(report_path.read_bytes()).hexdigest(),
    )
    entry = fakes_evidence.entry(
        tmp_path,
        item_id,
        "PASS",
        platform="linux",
        files=(report_file,),
    )
    api.add_entry(idx, entry, catalog=catalog)
    detail, _detail_reasons, satisfying = _platform_detail(
        idx, tmp_path, catalog, item_id, "linux"
    )
    assert detail == "SATISFIED"
    assert satisfying == [entry.entry_id]
    subset = _stage3_subset(catalog, item_id)
    result = api.stage_status(
        api.load_index(idx),
        "3",
        catalog=subset,
        evidence_root=evidence_root,
        cases={},
    )
    row = result["items"][item_id]
    assert row["status"] == "STALE"
    assert row["satisfying_entries"] == []
    assert any("raw report binding unverifiable" in reason for reason in row["reasons"])


def test_unverifiable_reference_item_does_not_change_stage_completion(tmp_path):
    """reference 個別保持 STALE，但不加入 stage 完成性所需集合。"""
    api = _evidence_api()
    catalog = api.load_exit_catalog()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    for platform in ("linux", "windows"):
        api.add_entry(
            idx,
            fakes_evidence.entry(tmp_path, "S3-OD09-ALL-FROZEN", "PASS", platform=platform),
            catalog=catalog,
        )
    api.add_entry(
        idx,
        fakes_evidence.entry(tmp_path, "S3-CANARY-LINUX", "PASS", platform="linux"),
        catalog=catalog,
    )
    subset = _stage3_subset(catalog, "S3-OD09-ALL-FROZEN", "S3-CANARY-LINUX")
    result = api.stage_status(
        api.load_index(idx),
        "3",
        catalog=subset,
        evidence_root=_evidence_root(tmp_path),
        cases={},
    )
    assert result["items"]["S3-OD09-ALL-FROZEN"]["status"] == "SATISFIED"
    assert result["items"]["S3-CANARY-LINUX"]["status"] == "STALE"
    assert result["items"]["S3-CANARY-LINUX"]["satisfying_entries"] == []
    assert result["result"] == "PASS"


@pytest.mark.parametrize("same_artifact", [True, False], ids=["same-artifact-revision", "replacement-artifact"])
def test_legacy_formal_fail_applies_only_to_current_target_artifact(tmp_path, same_artifact):
    """同 artifact 改 revision 仍保留 FAIL；替換 artifact 後舊 FAIL 留在歷史。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = api.load_exit_catalog()
    original = _freeze_stage0_target(idx, tmp_path)
    failed = fakes_evidence.entry(tmp_path, "S0-L1-BC", "FAIL", platform="linux")
    api.add_entry(idx, failed, catalog=catalog)
    replacement_bundle = fakes.make_bundle(
        tmp_path / "legacy-bundles" / "replacement",
        version="0.9.0" if same_artifact else "0.9.1",
        stage="0",
        target="x86_64-unknown-linux-gnu",
    )
    api.set_target(
        idx,
        stage="0",
        platform="linux",
        release_manifest=replacement_bundle / "release-manifest.json",
        contract_revision="d" * 40,
        tool_revision="u" * 40,
        case_list_sha256="1" * 64,
        set_by="test",
        now=fakes.ts(2, 0),
    )
    indexed = api.load_index(idx)
    current = indexed["stage_targets"]["0"]["linux"]
    assert indexed["target_history"]
    assert failed.entry_id in {row["entry_id"] for row in indexed["entries"]}
    if same_artifact:
        assert current["artifact_id"] == original["artifact_id"]
        assert current["contract_revision"] != original["contract_revision"]
        assert api._legacy_formal_failures(indexed, catalog)
    else:
        assert current["artifact_id"] != failed.artifact_id
        current_pass = fakes_evidence.entry(
            tmp_path,
            "S0-L1-BC",
            "PASS",
            platform="linux",
            n=2,
        )
        api.add_entry(idx, current_pass, catalog=catalog)
        resolution_entry = fakes_evidence.entry(
            tmp_path,
            "S0-OD09-DEFERRED-FROZEN",
            "PASS",
            platform="linux",
            n=3,
        )
        api.add_entry(idx, resolution_entry, catalog=catalog)
        indexed = api.load_index(idx)
        evidence_root = _evidence_root(tmp_path)
        scope_path = evidence_root / indexed["legacy_obligations"]["scope"]["path"]
        scope = json.loads(scope_path.read_bytes())
        selected = next(
            row for row in scope["obligations"]
            if row["item_id"] == "S0-OD09-DEFERRED-FROZEN" and row["platform"] == "linux"
        )
        resolution_rows = [
            {"key": row["key"], "method": "pending"}
            for row in scope["obligations"]
        ]
        resolution = next(row for row in resolution_rows if row["key"] == selected["key"])
        target_path = evidence_root / "legacy-targets" / "replacement-linux.json"
        _write_json(target_path, current)
        target_sha = hashlib.sha256(target_path.read_bytes()).hexdigest()
        resolution.update({
            "method": "retest",
            "entry_id": resolution_entry.entry_id,
            "target_sha256": target_sha,
            "files": [{
                "path": target_path.relative_to(evidence_root).as_posix(),
                "sha256": target_sha,
            }],
        })
        _install_legacy_resolutions(idx, evidence_root, resolution_rows)
        indexed = api.load_index(idx)
        assert not api._legacy_formal_failures(indexed, catalog)
        legacy = api.legacy_status(
            indexed,
            catalog=catalog,
            evidence_root=evidence_root,
            cases={},
        )
        assert legacy["result"] != "FAIL"
        assert legacy["obligations"][selected["key"]]["status"] == "PASS"
        assert not any(row["status"] == "FAIL" for row in legacy["obligations"].values())


def test_legacy_scope_rejects_missing_source_obligation_after_all_hashes_recomputed(tmp_path):
    """來源盤點的多筆非目錄義務少一筆時，重算附件雜湊後仍要 BLOCKED。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    evidence_root = _evidence_root(tmp_path)
    indexed = api.load_index(idx)
    scope_path = evidence_root / indexed["legacy_obligations"]["scope"]["path"]
    scope = json.loads(scope_path.read_bytes())
    noncatalog = [row for row in scope["obligations"] if row["stage"] == "legacy"]
    assert len(noncatalog) >= 2
    scope["obligations"].remove(noncatalog[-1])
    _write_json(scope_path, scope)
    _refresh_scope_review(idx, evidence_root)
    errors = api.validate_legacy_scope(
        api.load_index(idx),
        catalog=api.load_exit_catalog(),
        evidence_root=evidence_root,
    )
    assert any("omits source inventory obligations" in reason for reason in errors)


def test_required_cases_lock_snapshot_uses_content_addressed_immutable_path(tmp_path):
    """合法只增一案後新輪可凍結，舊鎖附件保留而舊 entry 轉 STALE。"""
    api = _evidence_api()
    idx = fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)
    catalog = api.load_exit_catalog()
    old_entry = fakes_evidence.entry(tmp_path, "S3-ROLLBACK-LINUX", "PASS", platform="linux", n=7)
    api.add_entry(idx, old_entry, catalog=catalog)
    old_index = api.load_index(idx)
    old_target = old_index["stage_targets"]["3"]["linux"]
    old_pointer = old_target["required_cases_lock"]
    assert old_pointer["sha256"] in old_pointer["path"]
    frozen = _evidence_root(tmp_path) / old_pointer["path"]
    old_bytes = frozen.read_bytes()
    assert hashlib.sha256(old_bytes).hexdigest() == old_pointer["sha256"]

    acceptance = tmp_path / "tests" / "acceptance"
    case_doc = yaml.safe_load((acceptance / "cases.yaml").read_bytes())
    case_lock = json.loads((acceptance / "cases.lock.json").read_bytes())
    required_lock = json.loads((acceptance / "stage3_required_cases.lock.json").read_bytes())
    added_id = "r1-fixture-stage3-l2-added"
    new_case = deepcopy(case_doc["cases"][0])
    new_case.update({
        "case_id": added_id,
        "title": "Stage 3 分母只增 fixture",
        "source_ref": "tests/test_stage3_evidence_index.py",
        "layer": "L2",
        "platforms": ["linux"],
        "necessity": "required",
        "family": "F1",
    })
    case_doc["cases"].append(new_case)
    case_lock["sets"]["slice_2.4"].append(added_id)
    case_lock["sets"]["slice_2.4"].sort()
    case_lock["case_attrs"][added_id] = {
        key: new_case.get(key)
        for key in ("suite", "layer", "platforms", "expected", "necessity", "daemon_slice", "cli_stage", "stage0_required")
    }
    case_lock["case_attrs"] = dict(sorted(case_lock["case_attrs"].items()))
    required_lock["exits"]["S3-L2-FULL-AB-LINUX"]["linux"]["case_ids"].append(added_id)
    required_lock["exits"]["S3-L2-FULL-AB-LINUX"]["linux"]["case_ids"].sort()
    (acceptance / "cases.yaml").write_text(yaml.safe_dump(case_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _write_json(acceptance / "cases.lock.json", case_lock)
    _write_json(acceptance / "stage3_required_cases.lock.json", required_lock)

    scope_index = api.load_index(idx)
    scope_path = _evidence_root(tmp_path) / scope_index["legacy_obligations"]["scope"]["path"]
    scope = json.loads(scope_path.read_bytes())
    for obligation in scope["obligations"]:
        item = catalog.get(obligation.get("item_id"))
        if item is None or item.required_case_set != "slice_2.4":
            continue
        obligation["required_scope"]["case_ids"] = list(case_lock["sets"]["slice_2.4"])
        obligation["required_scope_sha256"] = hashlib.sha256(
            json.dumps(obligation["required_scope"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        obligation["allowed_evidence"]["case_ids"] = list(case_lock["sets"]["slice_2.4"])
    obligations_by_key = {row["key"]: row for row in scope["obligations"]}
    for source in scope["sources"]:
        source_path = _evidence_root(tmp_path) / source["snapshot"]["path"]
        source_doc = json.loads(source_path.read_bytes())
        for source_obligation in source_doc.get("obligations", []):
            scoped = obligations_by_key[source_obligation["key"]]
            source_obligation["platform"] = scoped["platform"]
            source_obligation["case_ids"] = list(scoped["required_scope"]["case_ids"])
            source_obligation["evidence_kinds"] = list(scoped["allowed_evidence"]["kinds"])
        _write_json(source_path, source_doc)
        source["snapshot"]["sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    _write_json(scope_path, scope)
    _refresh_scope_review(idx, _evidence_root(tmp_path))

    next_round = api.begin_candidate_round(
        idx, rc_tag="v1.0.0-rc.2", set_by="test", now=fakes.ts(30, 0)
    )
    for platform in ("linux", "windows"):
        bundle = tmp_path / "bundles" / platform
        api.set_target(
            idx,
            stage="3",
            platform=platform,
            release_manifest=bundle / "release-manifest.json",
            contract_revision="c" * 40,
            tool_revision="t" * 40,
            case_list_sha256="0" * 64,
            set_by="test",
            now=fakes.ts(30, 1),
            bundle_dir=bundle,
            archive=fakes.rc_archive(bundle),
            candidate_round_id=next_round,
        )

    new_index = api.load_index(idx)
    new_target = new_index["stage_targets"]["3"]["linux"]
    assert new_target["required_cases_sha256"] != old_target["required_cases_sha256"]
    assert new_target["required_cases_lock"]["path"] != old_pointer["path"]
    assert frozen.read_bytes() == old_bytes
    old_status, _ = _status(idx, tmp_path, catalog, "S3-ROLLBACK-LINUX")
    assert old_status == "STALE"
    old_snapshot_status = api._required_cases_validation(
        old_target,
        evidence_root=_evidence_root(tmp_path),
        required_lock=None,
        source_reader=fakes_evidence.read_seed_revision,
    )
    assert old_snapshot_status is not None and old_snapshot_status[0] == "STALE"


def test_memory_only_stage3_fixture_never_starts_git(tmp_path, monkeypatch):
    """memory_only fixture 對所有指定 revision 讀取使用固定 bytes 替身。"""
    def forbidden(*args, **kwargs):
        raise AssertionError("memory_only path attempted to read a Git revision")

    monkeypatch.setattr(evidence_index, "_git_seed_reader", forbidden)
    fakes_evidence.evidence_index(tmp_path, stage="3", contract="c" * 40)

