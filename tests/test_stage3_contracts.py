from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
ACC = ROOT / "tests" / "acceptance"
EVIDENCE = ROOT / "docs" / "rust-refactor" / "evidence"
DENOMINATORS = EVIDENCE / "denominators"
SEED_REVISION = "59d8c7f1eb7de1ae2a90cefd6df21ce945d695d2"

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _load(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.mark.parametrize("schema,example", [
    ("ab_manifest.schema.json", "examples/ab_manifest.l15.example.json"),
    ("ab_manifest.schema.json", "examples/ab_manifest.l2.example.json"),
    ("ab_manifest.schema.json", "examples/ab_manifest.l3.example.json"),
    ("canary_policy.schema.json", "canary_policy.json"),
    ("stage3_evidence_index.schema.json", "../../docs/rust-refactor/evidence/index.json"),
])
def test_example_validates(schema, example):
    jsonschema.validate(_load(ACC / example), _load(ACC / schema))


def test_ledger_example_records_validate():
    schema = _load(ACC / "canary_ledger_record.schema.json")
    lines = (ACC / "examples" / "canary_records.example.ndjson").read_text(encoding="utf-8").splitlines()
    kinds = set()
    for line in lines:
        rec = json.loads(line)
        jsonschema.validate(rec, schema)
        kinds.add(rec["type"])
    assert kinds == {"snapshot", "submit", "submit_result", "wal_signal", "harvest_anomaly", "note",
                     "incident_open", "incident_close", "carryover", "policy_ref"}
    assert not any("payload_b64" in line for line in lines)


@pytest.mark.parametrize("name", [
    "ab_manifest", "canary_policy", "canary_ledger_record", "stage3_evidence_index",
    "stage3_required_cases", "legacy_obligations_scope", "legacy_obligations_resolutions",
])
def test_new_schemas_have_schema_and_id(name):
    doc = _load(ACC / f"{name}.schema.json")
    assert doc["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert doc["$id"] == f"https://serialwrap.local/schemas/{name}"


def test_canary_policy_draft_keeps_od10_floor():
    pol = _load(ACC / "canary_policy.json")
    assert pol["status"] == "draft" and pol["frozen_at"] is None and pol["frozen_by"] is None
    for plat in ("linux", "windows"):
        q = pol["platforms"][plat]["quotas"]
        assert (q["active_days"], q["submits"], q["cycles_total"]) == (14, 5000, 50)
        assert sum(q["cycles_min_per_type"].values()) <= q["cycles_total"]
        assert sum(1 for v in q["cycles_min_per_type"].values() if v >= 1) >= 3
        assert 0 < q["cycles_max_type_share"] <= 0.5
    assert pol["platforms"]["windows"]["quotas"]["cycles_min_per_type"]["flash"] == 0
    assert pol["submit_counting"]["cmd_id_regex"] == "^[0-9a-f]{32}$"


def test_stage_exit_catalog_shape():
    items = _load(ACC / "stage_exits.json")["items"]
    ids = [i["item_id"] for i in items]
    assert ids and len(ids) == len(set(ids))
    assert {i["stage"] for i in items} == {"0", "1A", "1B", "2.1", "2.2", "2.3", "2.4", "3"}
    assert not any(i["stage"] == "3" and i["candidate_longrun"] for i in items)
    assert {i["item_id"] for i in items if i["candidate_longrun"]} == {
        "S1B-L3-MIXED-48H-WINDOWS", "S24-L3-LRMIXED-48H-LINUX", "S24-L3-LRMIXED-48H-WINDOWS"}
    reference_ids = {"S3-CANARY-LINUX", "S3-CANARY-WINDOWS", "S3-INCIDENTS-CLOSED"}
    assert {i["item_id"] for i in items if i["release_requirement"] == "reference"} == reference_ids
    assert all(i["release_requirement"] in {"required", "reference"} for i in items)
    assert all(i["release_requirement"] == "required" for i in items if i["item_id"] not in reference_ids)
    for i in items:
        if i["layer"] in ("L2", "L3", "hw", "canary", "manual", "supervisor", "spike"):
            assert i["owner_blocked"] is True, i["item_id"]
        assert "allow_xfail" not in i
        assert i["evidence_kind"] in ("junit", "functest-report", "bridge-selftest", "regression-report", "realhw-report",
                                      "gate-report", "ab-report", "canary-report", "drill-report", "od09-freeze",
                                      "manual-procedure"), i["item_id"]
    assert {i["item_id"] for i in items if i["stage"] == "3" and i["layer"] == "L3"} >= {
        "S3-L3-P0P1-REMOTE-LINUX", "S3-L3-P0P1-REMOTE-WINDOWS"}
    for item in items:
        if item["item_id"] in {"S3-L3-P0P1-REMOTE-LINUX", "S3-L3-P0P1-REMOTE-WINDOWS"}:
            assert item["required_families"] == ["P0", "P1", "remote", "WIN"]
    assert {"S0-SPIKE2-WINTASK-72H", "S0-SPIKE3-WIN-WAL-EDR"} <= set(ids)


def test_l1_items_have_frozen_nodeid_sets():   # R5-F3，failing-first：L1 出口必須有事先凍結的 nodeid 分母
    doc = _load(ACC / "stage_exits.json")
    assert doc["schema_version"] == 3
    lock = _load(ACC / "l1_required.lock.json")["sets"]
    for i in doc["items"]:
        if i["layer"] != "L1":
            assert i["required_l1_set"] is None, i["item_id"]
            continue
        sid = i["required_l1_set"]
        assert sid in lock, i["item_id"]
        plats = ("linux", "windows") if i["platform"] == "both" else (i["platform"],)
        for p in plats:
            ids = lock[sid][p]["nodeids"]
            assert ids and ids == sorted(set(ids)), (i["item_id"], p)
    assert {i["item_id"]: i["required_l1_set"] for i in doc["items"] if i["stage"] == "3" and i["layer"] == "L1"} == {
        "S3-L1-RELEASE-CONTRACT-LINUX": "s3_release_contract", "S3-L1-RELEASE-CONTRACT-WINDOWS": "s3_release_contract"}
    assert all(n.startswith("tests/contract_blackbox/") for p in ("linux", "windows")
               for n in lock["s3_release_contract"][p]["nodeids"])


def test_reason_codes_cover_sources_and_do_not_collide():
    reg = _load(ROOT / "tools" / "stage3" / "stage3_reason_codes.json")["codes"]
    used: set[str] = set()
    for p in (ROOT / "tools" / "stage3").rglob("*.py"):
        used |= set(re.findall(r"HARNESS_[A-Z0-9_]+", p.read_text(encoding="utf-8")))
    assert used <= set(reg), sorted(used - set(reg))
    product = (ROOT / "contracts" / "error_codes.yaml").read_text(encoding="utf-8")
    for code, desc in reg.items():
        assert code.startswith("HARNESS_") and code not in product
        assert desc["meaning"] and desc["action"]


def test_retirement_switch_default_off():
    r = _load(ROOT / "packaging" / "python_daemon_retirement.json")
    assert r["retired"] is False
    assert r["evidence_index"] == "docs/rust-refactor/evidence/index.json"
    assert r["stage3_target_artifact_ids"] == {"linux": None, "windows": None}


# ---- 審查補強：schema 負向測試（先前只有正向範例驗證，下列規則在 schema 中曾被靜默忽略）----

def _validator(name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_load(ACC / name))


def _example(name: str) -> dict:
    return _load(ACC / "examples" / name)


AB_EXAMPLES = ("ab_manifest.l15.example.json", "ab_manifest.l2.example.json", "ab_manifest.l3.example.json")


@pytest.mark.parametrize("example", AB_EXAMPLES)
def test_ab_manifest_requires_stage3_tool_tree(example):
    doc = _example(example)
    doc["tools"] = [t for t in doc["tools"] if t["path"] != "tools/stage3"]
    assert list(_validator("ab_manifest.schema.json").iter_errors(doc))


@pytest.mark.parametrize("example,wrong", [
    ("ab_manifest.l15.example.json", "l2_regression"),
    ("ab_manifest.l15.example.json", "l3_realhw"),
    ("ab_manifest.l2.example.json", "l15_functest"),
    ("ab_manifest.l3.example.json", "l15_functest"),
])
def test_ab_manifest_adapter_must_match_layer(example, wrong):
    doc = _example(example)
    doc["suite"]["adapter"] = wrong
    assert list(_validator("ab_manifest.schema.json").iter_errors(doc))


@pytest.mark.parametrize("example,path", [
    ("ab_manifest.l15.example.json", "func-test"),
    ("ab_manifest.l15.example.json", "tools/stage3"),
    ("ab_manifest.l2.example.json", "regression"),
    ("ab_manifest.l3.example.json", "realhw"),
])
def test_ab_manifest_required_tools_must_be_trees(example, path):
    doc = _example(example)
    for t in doc["tools"]:
        if t["path"] == path:
            t.pop("tree_sha256")
            t["sha256"] = "0" * 64
    assert list(_validator("ab_manifest.schema.json").iter_errors(doc))


def test_ab_manifest_sides_and_reverify_order_fixed():
    v = _validator("ab_manifest.schema.json")
    doc = _example("ab_manifest.l15.example.json")
    swapped = copy.deepcopy(doc)
    swapped["sides"][0]["role"], swapped["sides"][1]["role"] = "candidate", "baseline"
    assert list(v.iter_errors(swapped))
    wrong_order = copy.deepcopy(doc)
    wrong_order["reverify"] = {"enabled": True, "order": ["A", "B"]}
    assert list(v.iter_errors(wrong_order))
    no_auth = copy.deepcopy(_example("ab_manifest.l2.example.json"))
    no_auth["authorization"] = None          # Task 3：AbManifest.authorization 為 Authorization | None
    assert not list(v.iter_errors(no_auth))


def test_canary_example_record_ids_match_content():
    # plan 10 Task 1 Step 3：範例每行的 record_id 必須以 jsonio.record_id 由內容計算，改內容須同步重算
    from tools.stage3 import jsonio
    lines = (ACC / "examples" / "canary_records.example.ndjson").read_text(encoding="utf-8").splitlines()
    for no, line in enumerate(lines, 1):
        rec = json.loads(line)
        assert rec["record_id"] == jsonio.record_id(rec), f"第 {no} 行 record_id 與內容不符"


def test_ledger_snapshot_verified_requires_tree_digests():
    v = _validator("canary_ledger_record.schema.json")
    recs = [json.loads(x) for x in (ACC / "examples" / "canary_records.example.ndjson").read_text(encoding="utf-8").splitlines()]
    snap = next(r for r in recs if r["type"] == "snapshot")
    assert snap["artifact_check"] == "verified"
    for field in ("bundle_tree_sha256", "runtime_tree_sha256"):
        bad = copy.deepcopy(snap)
        bad[field] = None
        assert list(v.iter_errors(bad)), field          # R4-F1：verified 時兩個內容指紋必為非 null
    ok = copy.deepcopy(snap)
    ok.update(artifact_check="unresolved", bundle_tree_sha256=None, runtime_tree_sha256=None)
    assert not list(v.iter_errors(ok))
    inc = next(r for r in recs if r["type"] == "incident_open")
    inc["artifact_id"] = None                           # Task 11：open_manual(artifact_id: str | None)
    assert not list(v.iter_errors(inc))


def test_evidence_index_l1_set_and_stage3_tree_rules():
    v = _validator("stage3_evidence_index.schema.json")
    h = "a" * 64
    entry = {"entry_id": "0" * 20, "exit_catalog_sha256": h, "candidate_round_id": "a" * 32,
             "archive_sha256": h, "item_id": "S3-L1-RELEASE-CONTRACT-LINUX", "platform": "linux", "result": "PASS",
             "artifact_id": h, "runtime_digest": h, "bundle_tree_sha256": h, "runtime_tree_sha256": h,
             "contract_revision": "c" * 40, "tool_revision": "b" * 40, "case_list_sha256": h,
             "required_cases_sha256": h, "l1_set_sha256": h,
             "workload_sha256": None, "environment_id": h, "produced_at": "2026-11-02T00:00:00+00:00",
             "started_at": "2026-11-02T00:00:00+00:00", "producer": "ingest:junit", "files": [{"path": "a.json", "sha256": h}],
             "xfail_cases": [], "reuse_of": None, "note": "", "ledger_through": None, "ledger_records": None}
    base = {"candidate_rounds": [], "current_candidate_round_id": "a" * 32, "entries": [entry],
            "legacy_obligations": {"scope": None, "resolutions": None}, "schema_version": 2,
            "stage_targets": {}, "target_history": []}
    assert not list(v.iter_errors(base))
    junit_null = copy.deepcopy(base)
    junit_null["entries"][0]["l1_set_sha256"] = None
    assert list(v.iter_errors(junit_null))              # R5-F3：ingest:junit 必填非 null
    other_set = copy.deepcopy(base)
    other_set["entries"][0]["producer"] = "ingest:functest-report"
    assert list(v.iter_errors(other_set))               # 其他 producer 必為 null
    target = {"artifact_id": h, "runtime_digest": h, "contract_revision": "c" * 40, "tool_revision": "b" * 40,
              "case_list_sha256": h, "exit_catalog_sha256": h, "required_cases_sha256": h,
              "required_cases_lock": {"path": "denominators/stage3_required_cases.lock.json", "sha256": h},
              "candidate_round_id": "a" * 32, "rc_tag": "v1.0.0-rc.1", "archive_sha256": h,
              "legacy_scope_sha256": h, "set_by": "owner", "set_at": "2026-11-02T00:00:00+00:00"}
    other_stage_target = {"artifact_id": h, "runtime_digest": h, "contract_revision": "c" * 40,
                          "tool_revision": "b" * 40, "case_list_sha256": h, "exit_catalog_sha256": h,
                          "required_cases_sha256": None, "required_cases_lock": None,
                          "candidate_round_id": None, "rc_tag": None, "archive_sha256": None,
                          "legacy_scope_sha256": None, "set_by": "owner",
                          "set_at": "2026-11-02T00:00:00+00:00"}
    empty = {"candidate_rounds": [], "current_candidate_round_id": None, "entries": [],
             "legacy_obligations": {"scope": None, "resolutions": None}, "schema_version": 2, "target_history": []}
    assert list(v.iter_errors(dict(empty, stage_targets={"3": {"linux": target}})))          # Stage 3 target 必帶兩個 tree
    assert not list(v.iter_errors(dict(empty, stage_targets={"3": {"linux": dict(target, bundle_tree_sha256=h, runtime_tree_sha256=h)}})))
    assert not list(v.iter_errors(dict(empty, stage_targets={"2.4": {"linux": other_stage_target}})))
    assert list(v.iter_errors(dict(empty, stage_targets={
        "2.4": {"linux": dict(other_stage_target, bundle_tree_sha256=h)}
    })))  # 兩個內容樹指紋必須成對


@pytest.mark.parametrize("field", ["exit_catalog_sha256", "required_cases_sha256", "required_cases_lock"])
def test_stage3_target_requires_frozen_exit_and_case_denominators(field):
    target = _stage3_target()
    target.pop(field)
    assert list(_validator("stage3_evidence_index.schema.json").iter_errors(_stage3_doc("3", "linux", target)))


def test_policy_schema_pins_conservative_counting():
    pol = _load(ACC / "canary_policy.json")
    pol["submit_counting"]["require_terminal_result"] = False
    assert list(_validator("canary_policy.schema.json").iter_errors(pol))


# ---- 審查補強：stage_exits.json 衍生欄位規則釘測（依計畫 Task 1 的 evidence_kind／required_* 規則）----

_STAGE_CASE_SET = {"0": "stage0", "1A": "cli_1A", "1B": "cli_1B", "2.1": "slice_2.1",
                   "2.2": "slice_2.2", "2.3": "slice_2.3", "2.4": "slice_2.4", "3": "slice_2.4"}


def _expected_evidence_kind(i: dict) -> str:
    lay, st, iid = i["layer"], i["stage"], i["item_id"]
    if lay == "L1":
        return "junit"
    if iid == "S0-L15-BRIDGE":
        return "bridge-selftest"
    if iid in ("S0-L3-BASELINE-LINUX-48H", "S0-L3-BASELINE-WINDOWS-48H", "S1B-L3-MIXED-48H-WINDOWS") \
            or iid.startswith("S24-L3-LRMIXED-48H-"):
        return "gate-report"
    if st == "3" and "-AB-" in iid:
        return "ab-report"
    if lay == "L1.5" and st != "3":
        return "functest-report"
    if lay == "L2" and st != "3":
        return "regression-report"
    if lay == "L3":
        return "realhw-report"
    if lay == "canary" or iid == "S3-INCIDENTS-CLOSED":
        return "canary-report"
    if lay == "hw":
        return "drill-report"
    if lay == "gate":
        return "od09-freeze"
    return "manual-procedure"


def test_stage_exits_derived_fields_follow_plan_rules():
    items = _load(ACC / "stage_exits.json")["items"]
    case_sets = _load(ACC / "cases.lock.json")["sets"]
    for i in items:
        want_case = _STAGE_CASE_SET[i["stage"]] if i["layer"] in ("L1.5", "L2", "L3") else None
        assert i["required_case_set"] == want_case, i["item_id"]
        if want_case:
            assert want_case in case_sets, i["item_id"]
        assert (i["required_l1_set"] is not None) == (i["layer"] == "L1"), i["item_id"]
        assert i["evidence_kind"] == _expected_evidence_kind(i), i["item_id"]


# ---- 審查補強 R-T1：sides 逐欄位負向、stage 3 target 全欄位驗證、evidence entry 的 files 規則與路徑規則 ----

SIDE_MUTATIONS = {
    "cli_exe_removed": lambda s: s.pop("cli_exe"),
    "daemon_exe_removed": lambda s: s.pop("daemon_exe"),
    "bundle_dir_removed": lambda s: s.pop("bundle_dir"),
    "release_manifest_removed": lambda s: s.pop("release_manifest"),
    "activate_argv_removed": lambda s: s.pop("activate_argv"),
    "deactivate_argv_removed": lambda s: s.pop("deactivate_argv"),
    "unknown_key": lambda s: s.__setitem__("extra", 1),
    "bad_release_manifest_sha256": lambda s: s["release_manifest"].__setitem__("sha256", "xyz"),
    "empty_cli_exe": lambda s: s.__setitem__("cli_exe", ""),
}


@pytest.mark.parametrize("example", AB_EXAMPLES)
def test_ab_manifest_sides_validate_each_side(example):
    v = _validator("ab_manifest.schema.json")
    assert not list(v.iter_errors(_example(example)))            # 基準範例仍合法
    for side_index in (0, 1):                                    # prefixItems 兩側都必須套用 Side 規則
        for name, mutate in SIDE_MUTATIONS.items():
            doc = _example(example)
            mutate(doc["sides"][side_index])
            assert list(v.iter_errors(doc)), (example, side_index, name)


_STAGE3_TARGET_HASH = "a" * 64


def _stage3_target() -> dict:
    h = _STAGE3_TARGET_HASH
    return {"artifact_id": h, "runtime_digest": h, "contract_revision": "c" * 40, "tool_revision": "b" * 40,
            "case_list_sha256": h, "exit_catalog_sha256": h, "required_cases_sha256": h,
            "required_cases_lock": {"path": "denominators/stage3_required_cases.lock.json", "sha256": h},
            "candidate_round_id": "a" * 32, "rc_tag": "v1.0.0-rc.1", "archive_sha256": h,
            "legacy_scope_sha256": h, "set_by": "owner", "set_at": "2026-11-02T00:00:00+00:00",
            "bundle_tree_sha256": h, "runtime_tree_sha256": h}


def _stage3_doc(stage: str, platform: str, target: object) -> dict:
    return {"candidate_rounds": [], "current_candidate_round_id": "a" * 32, "entries": [],
            "legacy_obligations": {"scope": None, "resolutions": None}, "schema_version": 2,
            "target_history": [], "stage_targets": {stage: {platform: target}}}


def test_stage3_target_complete_is_accepted():
    v = _validator("stage3_evidence_index.schema.json")
    assert not list(v.iter_errors(_stage3_doc("3", "linux", _stage3_target())))


@pytest.mark.parametrize("mutate", [
    lambda t: t.__setitem__("artifact_id", "garbage"),
    lambda t: t.__setitem__("extra_key", 1),
    lambda t: t.__setitem__("bundle_tree_sha256", "x"),
    lambda t: t.__setitem__("runtime_tree_sha256", "y"),
    lambda t: t.pop("bundle_tree_sha256"),
], ids=["garbage_artifact_id", "extra_key", "bad_bundle_tree", "bad_runtime_tree", "missing_bundle_tree"])
def test_stage3_target_fields_are_fully_validated(mutate):
    t = _stage3_target()
    mutate(t)
    assert list(_validator("stage3_evidence_index.schema.json").iter_errors(_stage3_doc("3", "linux", t)))


def test_stage3_target_rejects_bogus_platform():
    assert list(_validator("stage3_evidence_index.schema.json").iter_errors(
        _stage3_doc("3", "macos", _stage3_target())))


@pytest.mark.parametrize("bad", ["not-an-object", "oops", ["x"], None])
def test_stage3_target_must_be_object(bad):
    v = _validator("stage3_evidence_index.schema.json")
    assert list(v.iter_errors(_stage3_doc("3", "linux", bad)))
    assert list(v.iter_errors({"entries": [], "schema_version": 2, "target_history": [],
                               "stage_targets": {"3": bad}}))


def _evidence_base() -> dict:
    h = "a" * 64
    entry = {"entry_id": "0" * 20, "exit_catalog_sha256": h, "candidate_round_id": "a" * 32,
             "archive_sha256": h, "item_id": "S3-L1-RELEASE-CONTRACT-LINUX", "platform": "linux", "result": "PASS",
             "artifact_id": h, "runtime_digest": h, "bundle_tree_sha256": h, "runtime_tree_sha256": h,
             "contract_revision": "c" * 40, "tool_revision": "b" * 40, "case_list_sha256": h,
             "required_cases_sha256": h, "l1_set_sha256": h,
             "workload_sha256": None, "environment_id": h, "produced_at": "2026-11-02T00:00:00+00:00",
             "started_at": "2026-11-02T00:00:00+00:00", "producer": "ingest:junit",
             "files": [{"path": "a.json", "sha256": h}],
             "xfail_cases": [], "reuse_of": None, "note": "", "ledger_through": None, "ledger_records": None}
    return {"candidate_rounds": [], "current_candidate_round_id": "a" * 32, "entries": [entry],
            "legacy_obligations": {"scope": None, "resolutions": None}, "schema_version": 2,
            "stage_targets": {}, "target_history": []}


@pytest.mark.parametrize("result", ["PASS", "XFAIL"])
def test_evidence_pass_or_xfail_requires_files(result):
    doc = _evidence_base()
    doc["entries"][0]["result"] = result
    doc["entries"][0]["files"] = []
    assert list(_validator("stage3_evidence_index.schema.json").iter_errors(doc))


def test_evidence_fail_may_have_no_files():
    doc = _evidence_base()
    doc["entries"][0]["result"] = "FAIL"
    doc["entries"][0]["files"] = []
    assert not list(_validator("stage3_evidence_index.schema.json").iter_errors(doc))


def _restricted_fail_entry() -> dict:
    h = "a" * 64
    return {
        "entry_id": "0" * 20,
        "result": "FAIL",
        "source_visibility": "restricted",
        "artifact_id": h,
        "platform": "linux",
        "item_id": "S3-L1-RELEASE-CONTRACT-LINUX",
        "candidate_round_id": "a" * 32,
        "context_id": h,
        "private_source_sha256": h,
        "failure_summary": "已核實受測案例失敗。",
        "verified_by": "reviewer-id",
        "verified_at": "2026-11-02T00:00:00+00:00",
    }


def test_restricted_fail_index_entry_is_minimal_and_fail_only():
    v = _validator("stage3_evidence_index.schema.json")
    doc = {"candidate_rounds": [], "current_candidate_round_id": "a" * 32,
           "entries": [_restricted_fail_entry()], "legacy_obligations": {"scope": None, "resolutions": None},
           "schema_version": 2, "stage_targets": {}, "target_history": []}
    assert not list(v.iter_errors(doc))
    for result in ("PASS", "XFAIL"):
        bad = copy.deepcopy(doc)
        bad["entries"][0]["result"] = result
        assert list(v.iter_errors(bad)), result
    for field in ("verified_by", "private_source_sha256", "context_id"):
        bad = copy.deepcopy(doc)
        del bad["entries"][0][field]
        assert list(v.iter_errors(bad)), field
    bad = copy.deepcopy(doc)
    bad["entries"][0]["unlisted"] = "value"
    assert list(v.iter_errors(bad))


@pytest.mark.parametrize("path", ["/abs/x.json", "C:/x.json", "C:\\x.json", "../x.json", "a/../b.json",
                                  "a\\b.json", "..", "a/.."])
def test_evidence_file_path_must_be_relative_and_contained(path):
    doc = _evidence_base()
    doc["entries"][0]["files"] = [{"path": path, "sha256": "a" * 64}]
    assert list(_validator("stage3_evidence_index.schema.json").iter_errors(doc)), path


@pytest.mark.parametrize("path", ["a.json", "sub/dir/a.json", "a..b.json", "x/..y/z.json"])
def test_evidence_file_relative_paths_are_accepted(path):
    doc = _evidence_base()
    doc["entries"][0]["files"] = [{"path": path, "sha256": "a" * 64}]
    assert not list(_validator("stage3_evidence_index.schema.json").iter_errors(doc)), path


def test_stage3_manifest_examples_bind_exit_and_denominator_identity():
    catalog = {item["item_id"]: item for item in _load(ACC / "stage_exits.json")["items"]}
    expected_items = {
        "ab_manifest.l15.example.json": "S3-L15-RELEASE-AB-LINUX",
        "ab_manifest.l2.example.json": "S3-L2-FULL-AB-LINUX",
        "ab_manifest.l3.example.json": "S3-L3-P0P1-REMOTE-LINUX",
    }
    for name, expected_id in expected_items.items():
        doc = _example(name)
        assert doc["stage"] == "3"
        assert doc["exit_item_id"] == expected_id
        assert doc["required_case_set"] == "slice_2.4"
        assert doc["required_cases_lock"]["path"] == "tests/acceptance/stage3_required_cases.lock.json"
        assert doc["candidate_context"]["sha256"]
        item = catalog[doc["exit_item_id"]]
        assert item["release_requirement"] == "required"
        assert item["layer"].lower() == doc["layer"]


def _seed_source_bytes() -> dict[str, bytes]:
    return {
        "tests/acceptance/cases.yaml": (DENOMINATORS / "cases.yaml").read_bytes(),
        "tests/acceptance/cases.lock.json": (DENOMINATORS / "cases.lock.json").read_bytes(),
    }


def _injected_seed_reader(_revision: str, repo_path: str) -> bytes:
    return _seed_source_bytes()[repo_path]


def _copy_seed_evidence(destination: Path) -> None:
    target = destination / "denominators"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("cases.yaml", "cases.lock.json", "source-manifest.json"):
        (target / name).write_bytes((DENOMINATORS / name).read_bytes())


def test_public_seed_at_designated_revision_accepted():
    from tools.stage3 import required_cases

    manifest = _load(DENOMINATORS / "source-manifest.json")
    errors = required_cases.verify_seed_sources(manifest, evidence_root=EVIDENCE,
                                                source_reader=_injected_seed_reader)
    assert errors == []


def test_public_seed_rejects_undesignated_source():
    from tools.stage3 import required_cases

    manifest = _load(DENOMINATORS / "source-manifest.json")
    manifest["sources"][0]["revision"] = "0" * 40
    errors = required_cases.verify_seed_sources(manifest, evidence_root=EVIDENCE,
                                                source_reader=_injected_seed_reader)
    assert errors


def test_public_seed_rejects_sha_mismatch(tmp_path):
    from tools.stage3 import required_cases

    evidence_root = tmp_path / "evidence"
    _copy_seed_evidence(evidence_root)
    attached = evidence_root / "denominators" / "cases.yaml"
    changed = attached.read_bytes() + b"# altered\n"
    attached.write_bytes(changed)
    manifest = _load(evidence_root / "denominators" / "source-manifest.json")
    manifest["sources"][0]["sha256"] = hashlib.sha256(changed).hexdigest()
    errors = required_cases.verify_seed_sources(manifest, evidence_root=evidence_root,
                                                source_reader=_injected_seed_reader)
    assert errors


def test_public_seed_rejects_disguised_runtime_data(tmp_path):
    from tools.stage3 import required_cases

    evidence_root = tmp_path / "evidence"
    _copy_seed_evidence(evidence_root)
    synthetic = b'{"source":"uart","payload":"not a static contract"}\n'
    attached = evidence_root / "denominators" / "cases.yaml"
    attached.write_bytes(synthetic)
    manifest = _load(evidence_root / "denominators" / "source-manifest.json")
    manifest["sources"][0].update({
        "path": "tests/acceptance/runtime-output.ndjson",
        "sha256": hashlib.sha256(synthetic).hexdigest(),
    })
    errors = required_cases.verify_seed_sources(manifest, evidence_root=evidence_root,
                                                source_reader=lambda _rev, _path: synthetic)
    assert errors


STAGE3_REQUIRED_EXIT_SPECS = (
    ("S3-L15-RELEASE-AB-LINUX", "linux", "L1.5", None),
    ("S3-L15-RELEASE-AB-WINDOWS", "windows", "L1.5", None),
    ("S3-L2-FULL-AB-LINUX", "linux", "L2", None),
    ("S3-L2-AB-WINDOWS", "windows", "L2", None),
    ("S3-L3-P0P1-REMOTE-LINUX", "linux", "L3", ("P0", "P1", "remote", "WIN")),
    ("S3-L3-P0P1-REMOTE-WINDOWS", "windows", "L3", ("P0", "P1", "remote", "WIN")),
)


def _seed_required_case_ids(case_doc: dict, case_lock: dict, layer: str, platform: str,
                            required_families: tuple[str, ...] | None) -> set[str]:
    by_id = {case["case_id"]: case for case in case_doc["cases"]}
    selected = set()
    for case_id in case_lock["sets"]["slice_2.4"]:
        case = by_id[case_id]
        if case_id == "lr-mixed":
            continue
        if case["layer"] != layer or case["necessity"] != "required" or platform not in case["platforms"]:
            continue
        if layer != "L3" and required_families is not None and case.get("family") not in required_families:
            continue
        selected.add(case_id)
    return selected


def _required_cases_repo(tmp_path: Path) -> tuple[Path, object]:
    import yaml

    root = tmp_path / "checkout"
    acceptance = root / "tests" / "acceptance"
    acceptance.mkdir(parents=True)
    (acceptance / "cases.yaml").write_bytes((DENOMINATORS / "cases.yaml").read_bytes())
    (acceptance / "cases.lock.json").write_bytes((DENOMINATORS / "cases.lock.json").read_bytes())
    initial_lock = (ACC / "stage3_required_cases.lock.json").read_bytes()
    (acceptance / "stage3_required_cases.lock.json").write_bytes(initial_lock)

    items = []
    for item_id, platform, layer, families in STAGE3_REQUIRED_EXIT_SPECS:
        items.append({
            "item_id": item_id,
            "stage": "3",
            "platform": platform,
            "layer": layer,
            "title": item_id,
            "spec_ref": "§11.6",
            "owner_blocked": layer != "L1.5",
            "candidate_longrun": False,
            "evidence_kind": "realhw-report" if layer == "L3" else "ab-report",
            "required_case_set": "slice_2.4",
            "required_l1_set": None,
            "release_requirement": "required",
            "required_families": list(families) if families is not None else None,
        })
    (acceptance / "stage_exits.json").write_text(
        json.dumps({"schema_version": 3, "items": items}, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    _copy_seed_evidence(root / "docs" / "rust-refactor" / "evidence")
    original_sources = _seed_source_bytes()
    original_required_lock = initial_lock

    def source_reader(revision: str, repo_path: str) -> bytes:
        if repo_path == "tests/acceptance/stage3_required_cases.lock.json" and revision == "base-revision":
            return original_required_lock
        return original_sources[repo_path]

    return root, source_reader


def test_required_cases_seed_preserves_full_denominator():
    from tools.stage3 import required_cases
    import yaml

    lock = _load(ACC / "stage3_required_cases.lock.json")
    cases = yaml.safe_load((DENOMINATORS / "cases.yaml").read_text(encoding="utf-8"))
    case_lock = _load(DENOMINATORS / "cases.lock.json")
    current_cases = yaml.safe_load((ACC / "cases.yaml").read_text(encoding="utf-8"))
    current_case_lock = _load(ACC / "cases.lock.json")
    catalog = _load(ACC / "stage_exits.json")
    assert lock["schema_version"] == 1
    assert lock["seed"]["revision"] == SEED_REVISION
    assert lock["seed"]["decision_ref"] == "OD-113"
    assert lock["seed"]["excluded_case_ids"] == ["lr-mixed"]
    assert lock["seed"]["cases"]["path"] == "denominators/cases.yaml"
    assert lock["seed"]["cases"]["sha256"] == hashlib.sha256((DENOMINATORS / "cases.yaml").read_bytes()).hexdigest()
    assert lock["seed"]["case_lock"]["path"] == "denominators/cases.lock.json"
    assert lock["seed"]["case_lock"]["sha256"] == hashlib.sha256((DENOMINATORS / "cases.lock.json").read_bytes()).hexdigest()
    assert (DENOMINATORS / "source-manifest.json").is_file()
    seed_exit_ids = {item_id for item_id, _, _, _ in STAGE3_REQUIRED_EXIT_SPECS}
    assert seed_exit_ids <= set(lock["exits"])
    for item_id, platform, layer, families in STAGE3_REQUIRED_EXIT_SPECS:
        assert platform in lock["exits"][item_id]
        expected = _seed_required_case_ids(cases, case_lock, layer, platform, families)
        actual = required_cases.required_for_exit(lock, item_id, platform)
        assert expected <= actual
        assert "lr-mixed" not in actual
        assert actual
    current_expected = required_cases._expected_exit_cases(catalog, current_cases, current_case_lock)
    assert {
        item_id: {platform: set(entry["case_ids"]) for platform, entry in platforms.items()}
        for item_id, platforms in lock["exits"].items()
    } == {
        item_id: {platform: set(entry["case_ids"]) for platform, entry in platforms.items()}
        for item_id, platforms in current_expected.items()
    }


def _append_required_case(root: Path, case_id: str, *, family: str) -> dict:
    import yaml

    acceptance = root / "tests" / "acceptance"
    cases_path = acceptance / "cases.yaml"
    cases = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    template = next(case for case in cases["cases"] if case["case_id"] == "win-task-logon-trigger")
    new_case = copy.deepcopy(template)
    new_case["case_id"] = case_id
    new_case["title"] = "新增 Windows 原生必要案例"
    new_case["family"] = family
    cases["cases"].append(new_case)
    cases_path.write_text(yaml.safe_dump(cases, allow_unicode=True, sort_keys=False), encoding="utf-8")

    case_lock_path = acceptance / "cases.lock.json"
    case_lock = _load(case_lock_path)
    for set_name in ("cli_1B", "slice_2.1", "slice_2.2", "slice_2.3", "slice_2.4"):
        case_lock["sets"][set_name].append(case_id)
        case_lock["sets"][set_name].sort()
    attr_keys = ("suite", "layer", "platforms", "expected", "necessity", "daemon_slice", "cli_stage", "stage0_required")
    case_lock["case_attrs"][case_id] = {key: new_case[key] for key in attr_keys}
    case_lock_path.write_text(json.dumps(case_lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return new_case


def test_required_cases_seed_floor_accepts_formal_growth(tmp_path):
    from tools.stage3 import required_cases
    import yaml

    root, source_reader = _required_cases_repo(tmp_path)
    acceptance = root / "tests" / "acceptance"
    new_case_id = "win-task-logon-trigger-legal-growth-fixture"
    _append_required_case(root, new_case_id, family="WIN")
    lock_path = acceptance / "stage3_required_cases.lock.json"
    lock = _load(lock_path)
    ids = lock["exits"]["S3-L3-P0P1-REMOTE-WINDOWS"]["windows"]["case_ids"]
    ids.append(new_case_id)
    ids.sort()
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    seed_cases = yaml.safe_load((DENOMINATORS / "cases.yaml").read_text(encoding="utf-8"))
    seed_case_lock = _load(DENOMINATORS / "cases.lock.json")
    seed_floor = _seed_required_case_ids(seed_cases, seed_case_lock, "L3", "windows", None)
    seed_floor.discard("lr-mixed")
    actual = required_cases.required_for_exit(lock, "S3-L3-P0P1-REMOTE-WINDOWS", "windows")
    assert seed_floor <= actual

    current_cases = yaml.safe_load((acceptance / "cases.yaml").read_text(encoding="utf-8"))
    current_case_lock = _load(acceptance / "cases.lock.json")
    expected = required_cases._expected_exit_cases(_load(acceptance / "stage_exits.json"), current_cases, current_case_lock)
    assert actual == set(expected["S3-L3-P0P1-REMOTE-WINDOWS"]["windows"]["case_ids"])
    assert required_cases.main(["--check", "--base", "base-revision"], repo_root=root,
                               source_reader=source_reader) == 0


def test_required_cases_lock_matches_its_schema():
    lock = _load(ACC / "stage3_required_cases.lock.json")
    schema = _load(ACC / "stage3_required_cases.schema.json")
    jsonschema.validate(lock, schema)
    duplicate = copy.deepcopy(lock)
    item_id = next(iter(duplicate["exits"]))
    platform = next(iter(duplicate["exits"][item_id]))
    ids = duplicate["exits"][item_id][platform]["case_ids"]
    ids.append(ids[0])
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(duplicate))


def _closed_object_schemas(schema):
    found = {}

    def visit(value, path):
        if isinstance(value, dict):
            if value.get("additionalProperties") is False:
                found[path] = value
            for key, child in value.items():
                visit(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(schema, "")
    return found


def test_required_cases_validator_keys_match_schema():
    from tools.stage3 import required_cases

    schema = _load(ACC / "stage3_required_cases.schema.json")
    schema_objects = _closed_object_schemas(schema)
    assert set(required_cases._LOCK_OBJECT_SHAPES) == set(schema_objects)
    for path, object_schema in schema_objects.items():
        properties = frozenset(object_schema["properties"])
        required = frozenset(object_schema["required"])
        assert required_cases._LOCK_OBJECT_SHAPES[path] == (properties, required)


def _mutate_closed_lock_object(lock, path, mutation):
    if path == "":
        target = lock
    elif path == "$defs.Seed":
        target = lock["seed"]
    elif path == "$defs.FrozenFile":
        target = lock["seed"]["cases"]
    elif path == "$defs.ExitPlatform":
        item_id = next(iter(lock["exits"]))
        platform = next(iter(lock["exits"][item_id]))
        target = lock["exits"][item_id][platform]
    else:
        raise AssertionError(f"test helper does not know schema object {path}")

    if mutation == "unknown":
        target["unexpected_stage3_key"] = True
    else:
        key = {
            "": "schema_version",
            "$defs.Seed": "excluded_case_ids",
            "$defs.FrozenFile": "sha256",
            "$defs.ExitPlatform": "case_ids",
        }[path]
        del target[key]


@pytest.mark.parametrize("mode", ["--check", "--add-only"])
@pytest.mark.parametrize("mutation", ["unknown", "missing"])
@pytest.mark.parametrize("path", ["", "$defs.Seed", "$defs.FrozenFile", "$defs.ExitPlatform"])
def test_required_cases_commands_reject_schema_key_drift(tmp_path, capsys, mode, mutation, path):
    from tools.stage3 import required_cases

    root, source_reader = _required_cases_repo(tmp_path)
    lock_path = root / "tests" / "acceptance" / "stage3_required_cases.lock.json"
    lock = _load(lock_path)
    _mutate_closed_lock_object(lock, path, mutation)
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    before = lock_path.read_bytes()

    result = required_cases.main([mode], repo_root=root, source_reader=source_reader)

    assert result != 0
    diagnostic = capsys.readouterr().err
    assert "schema 欄位" in diagnostic
    assert lock_path.read_bytes() == before


@pytest.mark.parametrize("mode", ["--check", "--add-only"])
@pytest.mark.parametrize("empty_path", ["exits", "platforms"])
def test_required_cases_commands_reject_empty_schema_maps(tmp_path, capsys, mode, empty_path):
    from tools.stage3 import required_cases

    root, source_reader = _required_cases_repo(tmp_path)
    lock_path = root / "tests" / "acceptance" / "stage3_required_cases.lock.json"
    lock = _load(lock_path)
    if empty_path == "exits":
        lock["exits"].clear()
    else:
        item_id = next(iter(lock["exits"]))
        lock["exits"][item_id].clear()
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    before = lock_path.read_bytes()

    result = required_cases.main([mode], repo_root=root, source_reader=source_reader)

    assert result != 0
    assert "schema minProperties" in capsys.readouterr().err
    assert lock_path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["unknown", "missing"])
@pytest.mark.parametrize("path", ["", "$defs.Seed", "$defs.FrozenFile", "$defs.ExitPlatform"])
def test_required_cases_add_only_rejects_invalid_candidate_shape(tmp_path, monkeypatch, capsys, mutation, path):
    from tools.stage3 import required_cases

    root, source_reader = _required_cases_repo(tmp_path)
    lock_path = root / "tests" / "acceptance" / "stage3_required_cases.lock.json"
    lock = _load(lock_path)
    # 留出一個尚未登記的目錄出口，讓 add-only 會建立候選鎖並進入第二次驗證。
    removed_item = next(iter(lock["exits"]))
    del lock["exits"][removed_item]
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    before = lock_path.read_bytes()

    validate_lock = required_cases._validate_lock

    def corrupt_candidate(candidate, expected, *, allow_missing=False):
        if not allow_missing:
            _mutate_closed_lock_object(candidate, path, mutation)
        return validate_lock(candidate, expected, allow_missing=allow_missing)

    monkeypatch.setattr(required_cases, "_validate_lock", corrupt_candidate)
    result = required_cases.main(["--add-only"], repo_root=root, source_reader=source_reader)

    assert result != 0
    assert "schema 欄位" in capsys.readouterr().err
    assert lock_path.read_bytes() == before


def test_required_cases_add_only_rejects_family_reclassification(tmp_path):
    from tools.stage3 import required_cases
    import yaml

    root, source_reader = _required_cases_repo(tmp_path)
    cases_path = root / "tests" / "acceptance" / "cases.yaml"
    cases = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    target = next(case for case in cases["cases"] if case["case_id"] == "win-task-logon-trigger")
    target["family"] = "F1"
    cases_path.write_text(yaml.safe_dump(cases, allow_unicode=True, sort_keys=False), encoding="utf-8")
    lock_path = root / "tests" / "acceptance" / "stage3_required_cases.lock.json"
    before = lock_path.read_bytes()

    result = required_cases.main(["--add-only"], repo_root=root, source_reader=source_reader)
    assert result != 0
    assert lock_path.read_bytes() == before


def test_required_cases_lock_cannot_drop_existing_id(tmp_path):
    from tools.stage3 import required_cases

    root, source_reader = _required_cases_repo(tmp_path)
    lock_path = root / "tests" / "acceptance" / "stage3_required_cases.lock.json"
    lock = _load(lock_path)
    ids = lock["exits"]["S3-L3-P0P1-REMOTE-WINDOWS"]["windows"]["case_ids"]
    ids.pop()
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    before = lock_path.read_bytes()

    result = required_cases.main(["--check", "--base", "base-revision"], repo_root=root,
                                 source_reader=source_reader)
    assert result != 0
    assert lock_path.read_bytes() == before


def test_required_cases_lock_registers_new_case(tmp_path):
    from tools.stage3 import required_cases
    import yaml

    root, source_reader = _required_cases_repo(tmp_path)
    acceptance = root / "tests" / "acceptance"
    cases_path = acceptance / "cases.yaml"
    cases = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    template = next(case for case in cases["cases"] if case["case_id"] == "win-task-logon-trigger")
    new_case = copy.deepcopy(template)
    new_case["case_id"] = "win-task-logon-trigger-add-only-fixture"
    new_case["title"] = "新增 Windows 原生必要案例"
    cases["cases"].append(new_case)
    cases_path.write_text(yaml.safe_dump(cases, allow_unicode=True, sort_keys=False), encoding="utf-8")

    case_lock_path = acceptance / "cases.lock.json"
    case_lock = _load(case_lock_path)
    new_case_id = new_case["case_id"]
    for set_name in ("cli_1B", "slice_2.1", "slice_2.2", "slice_2.3", "slice_2.4"):
        case_lock["sets"][set_name].append(new_case_id)
        case_lock["sets"][set_name].sort()
    attr_keys = ("suite", "layer", "platforms", "expected", "necessity", "daemon_slice", "cli_stage", "stage0_required")
    case_lock["case_attrs"][new_case_id] = {key: new_case[key] for key in attr_keys}
    case_lock_path.write_text(json.dumps(case_lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    result = required_cases.main(["--add-only"], repo_root=root, source_reader=source_reader)
    assert result == 0
    updated = _load(acceptance / "stage3_required_cases.lock.json")
    assert new_case_id in required_cases.required_for_exit(updated, "S3-L3-P0P1-REMOTE-WINDOWS", "windows")


def test_required_cases_l3_new_family_is_required_and_add_only_registers_it(tmp_path):
    from tools.stage3 import required_cases

    root, source_reader = _required_cases_repo(tmp_path)
    acceptance = root / "tests" / "acceptance"
    new_case_id = "win-task-logon-trigger-new-family-fixture"
    _append_required_case(root, new_case_id, family="F1")

    assert required_cases.main(["--check"], repo_root=root, source_reader=source_reader) != 0
    assert required_cases.main(["--add-only"], repo_root=root, source_reader=source_reader) == 0
    updated = _load(acceptance / "stage3_required_cases.lock.json")
    assert new_case_id in required_cases.required_for_exit(updated, "S3-L3-P0P1-REMOTE-WINDOWS", "windows")


def test_required_cases_add_only_registers_new_exit(tmp_path):
    from tools.stage3 import required_cases

    root, source_reader = _required_cases_repo(tmp_path)
    acceptance = root / "tests" / "acceptance"
    catalog_path = acceptance / "stage_exits.json"
    catalog = _load(catalog_path)
    new_exit = copy.deepcopy(next(
        item for item in catalog["items"] if item["item_id"] == "S3-L3-P0P1-REMOTE-WINDOWS"
    ))
    new_exit["item_id"] = "S3-L3-P0P1-REMOTE-WINDOWS-ADDED-FIXTURE"
    new_exit["title"] = "新增 Stage 3 出口測試 fixture"
    catalog["items"].append(new_exit)
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                            encoding="utf-8")

    result = required_cases.main(["--add-only", "--base", "base-revision"], repo_root=root,
                                 source_reader=source_reader)
    assert result == 0
    updated = _load(acceptance / "stage3_required_cases.lock.json")
    assert "S3-L3-P0P1-REMOTE-WINDOWS-ADDED-FIXTURE" in updated["exits"]


def test_required_cases_add_only_registers_new_platform(tmp_path):
    from tools.stage3 import required_cases

    root, source_reader = _required_cases_repo(tmp_path)
    acceptance = root / "tests" / "acceptance"
    catalog_path = acceptance / "stage_exits.json"
    catalog = _load(catalog_path)
    item = next(item for item in catalog["items"] if item["item_id"] == "S3-L15-RELEASE-AB-LINUX")
    item["platform"] = "both"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                            encoding="utf-8")

    result = required_cases.main(["--add-only", "--base", "base-revision"], repo_root=root,
                                 source_reader=source_reader)
    assert result == 0
    updated = _load(acceptance / "stage3_required_cases.lock.json")
    assert "windows" in updated["exits"]["S3-L15-RELEASE-AB-LINUX"]


def test_required_cases_add_only_rejects_base_exit_shrink_without_writing(tmp_path, capsys):
    from tools.stage3 import required_cases

    root, source_reader = _required_cases_repo(tmp_path)
    acceptance = root / "tests" / "acceptance"
    catalog_path = acceptance / "stage_exits.json"
    catalog = _load(catalog_path)
    new_exit = copy.deepcopy(next(
        item for item in catalog["items"] if item["item_id"] == "S3-L3-P0P1-REMOTE-WINDOWS"
    ))
    new_exit["item_id"] = "S3-L3-P0P1-REMOTE-WINDOWS-ADDED-FIXTURE"
    new_exit["title"] = "新增 Stage 3 出口測試 fixture"
    catalog["items"] = [
        item for item in catalog["items"] if item["item_id"] != "S3-L3-P0P1-REMOTE-LINUX"
    ]
    catalog["items"].append(new_exit)
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                            encoding="utf-8")

    lock_path = acceptance / "stage3_required_cases.lock.json"
    lock = _load(lock_path)
    del lock["exits"]["S3-L3-P0P1-REMOTE-LINUX"]
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                         encoding="utf-8")
    before = lock_path.read_bytes()

    result = required_cases.main(["--add-only", "--base", "base-revision"], repo_root=root,
                                 source_reader=source_reader)
    assert result != 0
    assert "既有出口不可刪除：S3-L3-P0P1-REMOTE-LINUX" in capsys.readouterr().err
    assert lock_path.read_bytes() == before


def test_required_cases_base_rejects_reclassification_of_existing_l3_case(tmp_path):
    from tools.stage3 import required_cases
    import yaml

    root, source_reader = _required_cases_repo(tmp_path)
    acceptance = root / "tests" / "acceptance"
    new_case_id = "win-task-logon-trigger-base-family-fixture"
    _append_required_case(root, new_case_id, family="WIN")
    assert required_cases.main(["--add-only"], repo_root=root, source_reader=source_reader) == 0
    base_blobs = {
        "tests/acceptance/cases.yaml": (acceptance / "cases.yaml").read_bytes(),
        "tests/acceptance/stage3_required_cases.lock.json":
            (acceptance / "stage3_required_cases.lock.json").read_bytes(),
    }

    cases_path = acceptance / "cases.yaml"
    cases = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    target = next(case for case in cases["cases"] if case["case_id"] == new_case_id)
    target["family"] = "F1"
    cases_path.write_text(yaml.safe_dump(cases, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def base_reader(revision: str, repo_path: str) -> bytes | None:
        if revision == "base-revision" and repo_path in base_blobs:
            return base_blobs[repo_path]
        return source_reader(revision, repo_path)

    assert required_cases.main(["--check", "--base", "base-revision"], repo_root=root,
                               source_reader=base_reader) != 0


def test_required_cases_check_rejects_seed_shrink_when_base_has_no_lock(tmp_path, capsys):
    from tools.stage3 import required_cases
    import yaml

    root, source_reader = _required_cases_repo(tmp_path)
    cases_path = root / "tests" / "acceptance" / "cases.yaml"
    cases = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    target = next(case for case in cases["cases"] if case["case_id"] == "win-task-logon-trigger")
    target["family"] = "F1"
    cases_path.write_text(yaml.safe_dump(cases, allow_unicode=True, sort_keys=False), encoding="utf-8")
    lock_path = root / "tests" / "acceptance" / "stage3_required_cases.lock.json"
    lock = _load(lock_path)
    ids = lock["exits"]["S3-L3-P0P1-REMOTE-WINDOWS"]["windows"]["case_ids"]
    ids.remove("win-task-logon-trigger")
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def no_base_lock(revision: str, repo_path: str) -> bytes | None:
        if revision == "base-revision" and repo_path == "tests/acceptance/stage3_required_cases.lock.json":
            return None
        return source_reader(revision, repo_path)

    assert required_cases.main(["--check", "--base", "base-revision"], repo_root=root,
                               source_reader=no_base_lock) != 0
    assert "固定 seed 基準" in capsys.readouterr().err


_S3_L1_PLAN_SELECTORS = {
    "linux": {
        "argv": ["-q", "tests/contract_blackbox", "-m", "core_portable or posix_only"],
        "paths": ["tests/contract_blackbox"],
        "markexpr": "core_portable or posix_only",
    },
    "windows": {
        "argv": ["-q", "tests/contract_blackbox", "-m", "core_portable or windows_native"],
        "paths": ["tests/contract_blackbox"],
        "markexpr": "core_portable or windows_native",
    },
}


def test_s3_release_contract_manifest_matches_frozen_nodeids_and_plan_selector():
    manifest = _load(EVIDENCE / "l1-collect" / "s3_release_contract" / "source-manifest.json")
    lock = _load(ACC / "l1_required.lock.json")
    assert "本 repo 指定 revision 的測試原始碼" in manifest["source_note"]
    assert "與 L1 鎖等值" in manifest["source_note"]
    assert "原始輸出不公開" in manifest["source_note"]
    expected_fields = {
        "collector", "os", "python", "checkout", "collection_argv", "selector", "return_code", "summary",
        "raw_sha256", "raw_bytes", "raw_lines", "nodeid_count", "nodeids_sha256",
    }
    for platform, selector in _S3_L1_PLAN_SELECTORS.items():
        frozen = lock["sets"]["s3_release_contract"][platform]
        entry = manifest["platforms"][platform]
        assert set(entry) == expected_fields
        nodeids = frozen["nodeids"]
        digest_input = ("\n".join(sorted(nodeids)) + "\n").encode("utf-8")
        assert entry["nodeid_count"] == len(nodeids)
        assert entry["nodeids_sha256"] == hashlib.sha256(digest_input).hexdigest()
        assert frozen["selector"] == entry["selector"] == selector


def test_s3_release_contract_manifest_has_no_derived_files_or_local_paths():
    manifest_path = EVIDENCE / "l1-collect" / "s3_release_contract" / "source-manifest.json"
    manifest = _load(manifest_path)
    assert not (manifest_path.parent / "linux.collect.json").exists()
    assert not (manifest_path.parent / "windows.collect.json").exists()

    local_path = re.compile(r"(?i)(?:/(?:home|tmp|mnt|users)/|[a-z]:\\(?:users|temp)\\|\\\\[^\\]+\\)")

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, child in value.items():
                yield from strings(key)
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)

    assert not any(key in {"derived_file", "derived_sha256"} for key in strings(manifest))
    assert not any(local_path.search(value) for value in strings(manifest))


def _valid_run_context() -> dict:
    return {
        "candidate_round_id": "a" * 32,
        "rc_tag": "v1.0.0-rc.1",
        "platform": "linux",
        "archive_sha256": "b" * 64,
        "artifact_id": "c" * 64,
        "required_cases_sha256": "d" * 64,
        "run_id": "run-001",
    }


def test_candidate_context_has_exact_frozen_fields(tmp_path):
    from tools.stage3 import candidate_context

    path = tmp_path / "run-context.json"
    value = _valid_run_context()
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    actual = candidate_context.load_run_context(path)
    assert set(actual) == set(value)
    assert dict(actual) == value


@pytest.mark.parametrize("field", [
    "candidate_round_id", "rc_tag", "platform", "archive_sha256", "artifact_id", "required_cases_sha256", "run_id",
])
def test_candidate_context_rejects_missing_field(tmp_path, field):
    from tools.stage3 import candidate_context

    value = _valid_run_context()
    del value[field]
    path = tmp_path / "run-context.json"
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError):
        candidate_context.load_run_context(path)


@pytest.mark.parametrize("field,value", [
    ("candidate_round_id", "not-a-round"),
    ("rc_tag", "1.0.0"),
    ("platform", "macos"),
    ("archive_sha256", "short"),
    ("artifact_id", None),
    ("required_cases_sha256", "short"),
    ("run_id", "x"),
    ("unexpected", "value"),
])
def test_candidate_context_rejects_bad_identity_fields(tmp_path, field, value):
    from tools.stage3 import candidate_context

    context = _valid_run_context()
    context[field] = value
    path = tmp_path / "run-context.json"
    path.write_text(json.dumps(context, sort_keys=True), encoding="utf-8")
    with pytest.raises(ValueError):
        candidate_context.load_run_context(path)


def _legacy_scope_fixture() -> dict:
    h = "a" * 64
    return {
        "schema_version": 1,
        "exit_catalog_sha256": h,
        "sources": [{
            "source_id": "serialwrap-rust-spec-final",
            "version": "1.0",
            "snapshot": {"path": "legacy/spec-snapshot.json", "sha256": h},
        }],
        "obligations": [{
            "key": "S0-L1-BC:linux",
            "stage": "0",
            "platform": "linux",
            "item_id": "S0-L1-BC",
            "source_refs": ["serialwrap-rust-spec-final §11.6"],
            "required_scope": {"case_ids": ["bc-01"]},
            "required_scope_sha256": h,
            "safety_invariant": False,
            "allowed_evidence": {"kinds": ["junit"], "case_ids": ["bc-01"]},
        }],
        "review": {
            "reviewer_id": "reviewer-id",
            "approved_at": "2026-11-02T00:00:00+00:00",
            "decision_ref": "OD-113",
            "record": {"path": "legacy/scope-review.md", "sha256": h},
        },
    }


def _legacy_resolutions_fixture() -> dict:
    return {
        "schema_version": 1,
        "scope_sha256": "a" * 64,
        "resolutions": [{"key": "S0-L1-BC:linux", "method": "pending"}],
    }


def test_legacy_obligation_schemas_accept_minimal_unresolved_scope():
    scope_schema = _load(ACC / "legacy_obligations_scope.schema.json")
    resolutions_schema = _load(ACC / "legacy_obligations_resolutions.schema.json")
    jsonschema.validate(_legacy_scope_fixture(), scope_schema)
    jsonschema.validate(_legacy_resolutions_fixture(), resolutions_schema)


def test_legacy_obligation_schemas_reject_missing_sources_or_approval():
    scope_schema = _validator("legacy_obligations_scope.schema.json")
    missing_sources = _legacy_scope_fixture()
    missing_sources["sources"] = []
    assert list(scope_schema.iter_errors(missing_sources))
    missing_approval = _legacy_scope_fixture()
    missing_approval["review"]["decision_ref"] = None
    assert list(scope_schema.iter_errors(missing_approval))


def test_legacy_obligation_resolutions_reject_unknown_method_and_manual_pass():
    schema = _validator("legacy_obligations_resolutions.schema.json")
    bad_method = _legacy_resolutions_fixture()
    bad_method["resolutions"][0]["method"] = "owner_says_pass"
    assert list(schema.iter_errors(bad_method))
    manual_pass = _legacy_resolutions_fixture()
    manual_pass["resolutions"][0]["result"] = "PASS"
    assert list(schema.iter_errors(manual_pass))
