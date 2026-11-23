from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
ACC = ROOT / "tests" / "acceptance"

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


@pytest.mark.parametrize("name", ["ab_manifest", "canary_policy", "canary_ledger_record", "stage3_evidence_index"])
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
    assert len(ids) == 67 and len(set(ids)) == 67
    assert {i["stage"] for i in items} == {"0", "1A", "1B", "2.1", "2.2", "2.3", "2.4", "3"}
    assert {i["item_id"] for i in items if i["candidate_longrun"]} == {
        "S1B-L3-MIXED-48H-WINDOWS", "S24-L3-LRMIXED-48H-LINUX", "S24-L3-LRMIXED-48H-WINDOWS",
        "S3-L3-AB-48H-LINUX", "S3-L3-AB-48H-WINDOWS"}
    for i in items:
        if i["layer"] in ("L2", "L3", "hw", "canary", "manual", "supervisor", "spike"):
            assert i["owner_blocked"] is True, i["item_id"]
        assert "allow_xfail" not in i
        assert i["evidence_kind"] in ("junit", "functest-report", "bridge-selftest", "regression-report", "realhw-report",
                                      "gate-report", "ab-report", "canary-report", "drill-report", "od09-freeze",
                                      "manual-procedure"), i["item_id"]
    assert {i["item_id"] for i in items if i["stage"] == "3" and i["layer"] == "L3"} >= {
        "S3-L3-P0P1-REMOTE-LINUX", "S3-L3-P0P1-REMOTE-WINDOWS"}
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
    entry = {"entry_id": "0" * 20, "item_id": "S3-L1-RELEASE-CONTRACT-LINUX", "platform": "linux", "result": "PASS",
             "artifact_id": h, "runtime_digest": h, "bundle_tree_sha256": h, "runtime_tree_sha256": h,
             "contract_revision": "c" * 40, "tool_revision": "t" * 40, "case_list_sha256": h, "l1_set_sha256": h,
             "workload_sha256": None, "environment_id": h, "produced_at": "2026-11-02T00:00:00+00:00",
             "started_at": "2026-11-02T00:00:00+00:00", "producer": "ingest:junit", "files": [{"path": "a.json", "sha256": h}],
             "xfail_cases": [], "reuse_of": None, "note": "", "ledger_through": None, "ledger_records": None}
    base = {"entries": [entry], "schema_version": 2, "stage_targets": {}, "target_history": []}
    assert not list(v.iter_errors(base))
    junit_null = copy.deepcopy(base)
    junit_null["entries"][0]["l1_set_sha256"] = None
    assert list(v.iter_errors(junit_null))              # R5-F3：ingest:junit 必填非 null
    other_set = copy.deepcopy(base)
    other_set["entries"][0]["producer"] = "ingest:functest-report"
    assert list(v.iter_errors(other_set))               # 其他 producer 必為 null
    target = {"artifact_id": h, "runtime_digest": h, "contract_revision": "c" * 40, "tool_revision": "t" * 40,
              "case_list_sha256": h, "set_by": "x", "set_at": "2026-11-02T00:00:00+00:00"}
    empty = {"entries": [], "schema_version": 2, "target_history": []}
    assert list(v.iter_errors(dict(empty, stage_targets={"3": {"linux": target}})))          # Stage 3 target 必帶兩個 tree
    assert not list(v.iter_errors(dict(empty, stage_targets={"3": {"linux": dict(target, bundle_tree_sha256=h, runtime_tree_sha256=h)}})))
    assert not list(v.iter_errors(dict(empty, stage_targets={"2.4": {"linux": target}})))   # 其他 stage 可不記
    assert list(v.iter_errors(dict(empty, stage_targets={"2.4": {"linux": dict(target, bundle_tree_sha256=h)}})))  # 兩者成對


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
    return {"artifact_id": h, "runtime_digest": h, "contract_revision": "c" * 40, "tool_revision": "t" * 40,
            "case_list_sha256": h, "set_by": "x", "set_at": "2026-11-02T00:00:00+00:00",
            "bundle_tree_sha256": h, "runtime_tree_sha256": h}


def _stage3_doc(stage: str, platform: str, target: object) -> dict:
    return {"entries": [], "schema_version": 2, "target_history": [], "stage_targets": {stage: {platform: target}}}


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
    entry = {"entry_id": "0" * 20, "item_id": "S3-L1-RELEASE-CONTRACT-LINUX", "platform": "linux", "result": "PASS",
             "artifact_id": h, "runtime_digest": h, "bundle_tree_sha256": h, "runtime_tree_sha256": h,
             "contract_revision": "c" * 40, "tool_revision": "t" * 40, "case_list_sha256": h, "l1_set_sha256": h,
             "workload_sha256": None, "environment_id": h, "produced_at": "2026-11-02T00:00:00+00:00",
             "started_at": "2026-11-02T00:00:00+00:00", "producer": "ingest:junit",
             "files": [{"path": "a.json", "sha256": h}],
             "xfail_cases": [], "reuse_of": None, "note": "", "ledger_through": None, "ledger_records": None}
    return {"entries": [entry], "schema_version": 2, "stage_targets": {}, "target_history": []}


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
