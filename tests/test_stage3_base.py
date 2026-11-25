from __future__ import annotations

import copy
import hashlib
import json
import pickle
import re
from pathlib import Path

import pytest

from tools.stage3 import canary_policy, jsonio
from tools.stage3.proc import CmdResult, run_json
from tools.stage3.reason_codes import HarnessError, load_reason_codes

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "tests" / "acceptance" / "canary_policy.json"

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _policy_doc() -> dict:
    return json.loads(POLICY.read_text(encoding="utf-8"))


def _write_policy(tmp_path: Path, doc: dict, name: str = "canary_policy.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def _frozen_doc(frozen_at: str = "2026-10-30T00:00:00+00:00") -> dict:
    doc = _policy_doc()
    doc["status"] = "frozen"
    doc["frozen_by"] = "owner"
    doc["frozen_at"] = frozen_at
    doc["decision_ref"] = "OD-10-CANARY-FREEZE"
    doc["platforms"]["linux"]["evidence_owner"] = "owner-linux"
    doc["platforms"]["windows"]["evidence_owner"] = "owner-windows"
    return doc


def _expect_invalid(path: Path) -> None:
    with pytest.raises(HarnessError) as ei:
        canary_policy.load_policy(path)
    assert ei.value.code == "HARNESS_CANARY_POLICY_INVALID"


def test_dumps_compact_stable():
    assert jsonio.dumps_compact({"b": 1, "a": "中"}) == '{"a":"中","b":1}'


def test_dumps_report_trailing_newline():
    s = jsonio.dumps_report({})
    assert s.endswith("\n")
    assert json.loads(s) == {}


def test_parse_iso_rejects_naive():
    with pytest.raises(ValueError):
        jsonio.parse_iso("2026-11-02T01:00:00")


def test_parse_iso_accepts_z():
    assert jsonio.parse_iso("2026-11-02T01:00:00Z") == jsonio.parse_iso("2026-11-02T01:00:00+00:00")


def test_read_ndjson_reports_malformed(tmp_path):
    p = tmp_path / "x.ndjson"
    p.write_text('{"a":1}\n{bad\n{"b":2}\n', encoding="utf-8")
    recs, bad = jsonio.read_ndjson(p)
    assert (len(recs), bad) == (2, [2])


def test_record_id_ignores_own_field():
    rid = jsonio.record_id({"a": 1})
    assert rid == jsonio.record_id({"a": 1, "record_id": "x"})
    assert len(rid) == 20


def test_atomic_write_replaces(tmp_path):
    p = tmp_path / "f.txt"
    jsonio.atomic_write_text(p, "one")
    jsonio.atomic_write_text(p, "two")
    assert p.read_text(encoding="utf-8") == "two"
    assert not list(tmp_path.glob("*.tmp"))


def test_run_json_non_object():
    def fake(argv, **kw):
        return CmdResult(rc=0, stdout="[1]", stderr="", elapsed_s=0.0, timed_out=False)

    _, doc = run_json(fake, ["x"])
    assert doc is None


def test_harness_error_unknown_code():
    with pytest.raises(ValueError):
        HarnessError("HARNESS_NOPE")


def test_load_policy_rejects_floor(tmp_path):
    doc = _policy_doc()
    doc["platforms"]["linux"]["quotas"]["submits"] = 4999
    _expect_invalid(_write_policy(tmp_path, doc))


def test_load_policy_rejects_windows_flash(tmp_path):
    doc = _policy_doc()
    doc["platforms"]["windows"]["quotas"]["cycles_min_per_type"]["flash"] = 1
    _expect_invalid(_write_policy(tmp_path, doc))


def test_load_policy_rejects_frozen_without_owner(tmp_path):
    doc = _policy_doc()
    doc["status"] = "frozen"
    doc["frozen_by"] = None
    _expect_invalid(_write_policy(tmp_path, doc))


def test_load_policy_rejects_single_type_quota(tmp_path):
    doc = _policy_doc()
    doc["platforms"]["linux"]["quotas"]["cycles_min_per_type"] = {"recovery": 5}
    _expect_invalid(_write_policy(tmp_path, doc))


def test_load_policy_rejects_share_above_half(tmp_path):
    doc = _policy_doc()
    doc["platforms"]["linux"]["quotas"]["cycles_max_type_share"] = 0.6
    _expect_invalid(_write_policy(tmp_path, doc))


def test_load_policy_rejects_zero_resend_window(tmp_path):
    doc = _policy_doc()
    doc["safety"]["resend_window_s"] = 0       # 0 只剩同秒才判為重送／人工重放，實質停用不可豁免的安全訊號
    _expect_invalid(_write_policy(tmp_path, doc))


def test_load_policy_rejects_other_cmd_id_regex(tmp_path):
    doc = _policy_doc()
    doc["submit_counting"]["cmd_id_regex"] = ".*"
    _expect_invalid(_write_policy(tmp_path, doc))


def test_load_policy_requires_decision_in_addenda(tmp_path):
    p = _write_policy(tmp_path, _frozen_doc())
    addenda = tmp_path / "owner-decisions-addenda.md"
    addenda.write_text("# addenda\n| OTHER-DECISION | owner | 2026-10-30 |\n", encoding="utf-8")
    with pytest.raises(HarnessError) as ei:
        canary_policy.load_policy(p, addenda_path=addenda)
    assert ei.value.code == "HARNESS_CANARY_POLICY_INVALID"
    addenda.write_text("# addenda\n| OD-10-CANARY-FREEZE | owner | 2026-10-30 |\n", encoding="utf-8")
    assert canary_policy.load_policy(p, addenda_path=addenda).status == "frozen"
    assert canary_policy.load_policy(p).status == "frozen"


def test_local_date_uses_offset_changes():
    q = canary_policy.PlatformQuota(
        active_days=14,
        submits=5000,
        cycles_total=50,
        cycles_min_per_type={},
        cycles_max_type_share=0.4,
        evidence_owner=None,
        utc_offset_minutes=480,
        utc_offset_changes=(("2026-11-03T00:00:00+00:00", 0),),
    )
    assert canary_policy.local_date(q, "2026-11-02T17:00:00+00:00") == "2026-11-03"
    assert canary_policy.local_date(q, "2026-11-03T17:00:00+00:00") == "2026-11-03"


def test_reason_codes_loaded_via_resources(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reg = load_reason_codes()
    assert "HARNESS_CANARY_POLICY_INVALID" in reg


def test_assert_frozen_before_draft():
    pol = canary_policy.load_policy(POLICY)
    with pytest.raises(HarnessError) as ei:
        canary_policy.assert_frozen_before(pol, "2026-11-02T01:00:00+00:00")
    assert ei.value.code == "HARNESS_CANARY_POLICY_NOT_FROZEN"


def test_assert_frozen_before_late(tmp_path):
    late = canary_policy.load_policy(_write_policy(tmp_path, _frozen_doc("2026-11-05T00:00:00+00:00")))
    with pytest.raises(HarnessError) as ei:
        canary_policy.assert_frozen_before(late, "2026-11-02T01:00:00+00:00")
    assert ei.value.code == "HARNESS_CANARY_POLICY_NOT_FROZEN"
    early_dir = tmp_path / "early"
    early_dir.mkdir()
    early = canary_policy.load_policy(_write_policy(early_dir, _frozen_doc("2026-10-30T00:00:00+00:00")))
    canary_policy.assert_frozen_before(early, "2026-11-02T01:00:00+00:00")


# ---- 審查補強（先前被漏掉的缺陷；每個測試在修正前都會失敗）----


def test_read_ndjson_roundtrips_unicode_line_separators(tmp_path):
    p = tmp_path / "x.ndjson"
    rec = {"type": "snapshot", "error": "a b \u0085 c d"}   # ensure_ascii=False 會原樣輸出，str.splitlines() 會誤切
    jsonio.append_ndjson(p, rec)
    jsonio.append_ndjson(p, {"type": "note"})
    recs, bad = jsonio.read_ndjson(p)
    assert (recs, bad) == ([rec, {"type": "note"}], [])


def test_append_ndjson_isolates_torn_tail(tmp_path):
    p = tmp_path / "torn.ndjson"
    p.write_bytes(b'{"a":1}\n{"b":')            # 前次寫到一半中斷，沒有結尾換行
    jsonio.append_ndjson(p, {"c": 3})
    recs, bad = jsonio.read_ndjson(p)
    assert recs == [{"a": 1}, {"c": 3}] and bad == [2]


def test_writers_emit_lf_only(tmp_path):
    p = tmp_path / "f.ndjson"
    jsonio.append_ndjson(p, {"a": 1})
    jsonio.atomic_write_text(tmp_path / "r.json", jsonio.dumps_report({"a": 1}))
    assert b"\r" not in p.read_bytes() and b"\r" not in (tmp_path / "r.json").read_bytes()


def test_local_date_normalizes_non_utc_input():
    q = canary_policy.PlatformQuota(active_days=14, submits=5000, cycles_total=50, cycles_min_per_type={},
                                    cycles_max_type_share=0.4, evidence_owner=None, utc_offset_minutes=480,
                                    utc_offset_changes=())
    assert canary_policy.local_date(q, "2026-11-02T17:00:00+08:00") == "2026-11-02"   # 本就是 UTC+8 的 17:00，不可再加一次 480 分
    assert canary_policy.local_date(q, "2026-11-02T09:00:00+00:00") == "2026-11-02"
    assert canary_policy.local_date(q, "2026-11-02T17:00:00Z") == "2026-11-03"


@pytest.mark.parametrize("mutate", [
    lambda d: d["submit_counting"].__setitem__("require_terminal_result", False),
    lambda d: d["submit_counting"].__setitem__("dedup_key", ["cmd_id"]),
    lambda d: d["submit_counting"].__setitem__("source", "cli_calls"),
    lambda d: d["submit_counting"].__setitem__("exclude_source_prefixes", []),
    lambda d: d["safety"].__setitem__("allowed_tx_source_regex", ".*"),
], ids=["terminal_result_false", "dedup_key", "source", "no_excludes", "match_all_tx_regex"])
def test_load_policy_rejects_relaxed_counting_semantics(tmp_path, mutate):
    doc = _policy_doc()
    mutate(doc)
    _expect_invalid(_write_policy(tmp_path, doc))


def test_reason_codes_resource_read_not_masked_by_cache(tmp_path, monkeypatch):
    from tools.stage3 import reason_codes

    reason_codes._load_registry.cache_clear()          # 其他測試早已讓 lru_cache 變熱，不清除就測不到資源讀取
    monkeypatch.chdir(tmp_path)
    assert "HARNESS_CANARY_POLICY_INVALID" in reason_codes.load_reason_codes()


# ---- 審查補強：load_policy 其餘拒絕規則（原先只測約 7 條，凍結後不可更正，逐條釘住）----


def _linux_quotas(doc: dict) -> dict:
    return doc["platforms"]["linux"]["quotas"]


def _mut_unknown_cycle_type(d: dict) -> None:
    _linux_quotas(d)["cycles_min_per_type"]["bogus_type"] = 1


def _mut_sum_exceeds_total(d: dict) -> None:
    q = _linux_quotas(d)
    q["cycles_max_type_share"] = 0.5
    q["cycles_min_per_type"]["dut_reboot"] = 25
    q["cycles_min_per_type"]["handoff_release_attach"] = 25


def _mut_single_type_over_cap(d: dict) -> None:
    q = _linux_quotas(d)                      # share 0.4 × total 50 → 單類型上限 20
    q["cycles_min_per_type"]["dut_reboot"] = 21
    q["cycles_min_per_type"]["handoff_release_attach"] = 0


def _mut_regex_not_compiling(d: dict) -> None:
    d["safety"]["allowed_tx_source_regex"] = "("


def _mut_offset_out_of_range(d: dict) -> None:
    d["platforms"]["linux"]["utc_offset_minutes"] = 900


def _mut_naive_offset_change(d: dict) -> None:
    d["platforms"]["linux"]["utc_offset_changes"] = [["2026-11-03T00:00:00", 0]]


def _mut_offset_changes_not_ascending(d: dict) -> None:
    d["platforms"]["linux"]["utc_offset_changes"] = [
        ["2026-11-03T00:00:00+00:00", 0], ["2026-11-02T00:00:00+00:00", 480]]


def _mut_offset_change_out_of_range(d: dict) -> None:
    d["platforms"]["linux"]["utc_offset_changes"] = [["2026-11-03T00:00:00+00:00", 900]]


@pytest.mark.parametrize("mutate", [
    _mut_unknown_cycle_type,
    _mut_sum_exceeds_total,
    _mut_single_type_over_cap,
    _mut_regex_not_compiling,
    _mut_offset_out_of_range,
    _mut_naive_offset_change,
    _mut_offset_changes_not_ascending,
    _mut_offset_change_out_of_range,
], ids=lambda f: f.__name__.removeprefix("_mut_"))
def test_load_policy_rejects_remaining_rules(tmp_path, mutate):
    doc = _policy_doc()
    mutate(doc)
    _expect_invalid(_write_policy(tmp_path, doc))


def test_load_policy_rejects_frozen_without_platform_owner(tmp_path):
    doc = _frozen_doc()
    doc["platforms"]["linux"]["evidence_owner"] = None
    _expect_invalid(_write_policy(tmp_path, doc))


# ---- 審查補強 R-T1：空白凍結欄位與 NDJSON 嚴格解碼（先前會被靜默接受）----

@pytest.mark.parametrize("field", ["frozen_by", "decision_ref"])
@pytest.mark.parametrize("value", ["", "   "])
def test_load_policy_rejects_blank_frozen_fields(tmp_path, field, value):
    doc = _frozen_doc()
    doc[field] = value
    _expect_invalid(_write_policy(tmp_path, doc))


@pytest.mark.parametrize("value", ["", " \t"])
def test_load_policy_rejects_blank_platform_owner(tmp_path, value):
    doc = _frozen_doc()
    doc["platforms"]["windows"]["evidence_owner"] = value
    _expect_invalid(_write_policy(tmp_path, doc))


def test_load_policy_rejects_blank_frozen_by_in_draft(tmp_path):
    doc = _policy_doc()                       # draft 仍不得留下空白的 frozen_by（非 null 即須有實質內容）
    doc["frozen_by"] = " "
    _expect_invalid(_write_policy(tmp_path, doc))


def test_read_ndjson_flags_invalid_utf8_line(tmp_path):
    p = tmp_path / "x.ndjson"
    p.write_bytes(b'{"a":1}\n{"cmd_id":"\xff\xfe"}\n{"b":2}\n')   # dumps_compact 只寫合法 UTF-8，非法位元組即為損毀
    recs, bad = jsonio.read_ndjson(p)
    assert (recs, bad) == ([{"a": 1}, {"b": 2}], [2])


# ---- 審查補強 R-T1-2：政策完整性規則（先前變異測試全數存活）----


def test_load_policy_sha256_is_file_bytes_digest(tmp_path):
    p = _write_policy(tmp_path, _policy_doc())
    pol = canary_policy.load_policy(p)
    assert pol.sha256 == hashlib.sha256(p.read_bytes()).hexdigest()
    p.write_bytes(p.read_bytes() + b"\n")                  # 凍結後任何位元組變動都必須改變 sha256
    assert canary_policy.load_policy(p).sha256 != pol.sha256


@pytest.mark.parametrize("field", ["frozen_by", "frozen_at", "decision_ref"])
def test_load_policy_rejects_frozen_with_null_identity_field(tmp_path, field):
    doc = _frozen_doc()                                    # 平台 evidence_owner 已填，不會先被 owner 規則攔下
    doc[field] = None
    _expect_invalid(_write_policy(tmp_path, doc))


def test_assert_frozen_before_rejects_draft_even_with_frozen_at(tmp_path):
    doc = _policy_doc()
    doc["frozen_at"] = "2026-10-30T00:00:00+00:00"         # draft 可留有時間欄位，但狀態仍是 draft
    pol = canary_policy.load_policy(_write_policy(tmp_path, doc))
    assert pol.status == "draft"
    with pytest.raises(HarnessError) as ei:
        canary_policy.assert_frozen_before(pol, "2026-11-02T01:00:00+00:00")
    assert ei.value.code == "HARNESS_CANARY_POLICY_NOT_FROZEN"


def test_assert_frozen_before_equal_timestamp_is_allowed(tmp_path):
    ts = "2026-11-02T01:00:00+00:00"
    pol = canary_policy.load_policy(_write_policy(tmp_path, _frozen_doc(ts)))
    canary_policy.assert_frozen_before(pol, ts)            # 只拒絕「晚於」，相等允許
    canary_policy.assert_frozen_before(pol, "2026-11-02T09:00:00+08:00")   # 同一瞬間的其他時區表示法


def _quota_with_changes(changes) -> canary_policy.PlatformQuota:
    return canary_policy.PlatformQuota(
        active_days=14, submits=5000, cycles_total=50, cycles_min_per_type={},
        cycles_max_type_share=0.4, evidence_owner=None, utc_offset_minutes=480,
        utc_offset_changes=tuple(changes))


def test_offset_at_applies_last_applicable_change_across_two_boundaries():
    q = _quota_with_changes([("2026-11-03T00:00:00+00:00", 0), ("2026-11-10T00:00:00+00:00", 60)])
    assert canary_policy.offset_at(q, "2026-11-02T00:00:00+00:00") == 480     # 第一個變更之前：預設值
    assert canary_policy.offset_at(q, "2026-11-05T00:00:00+00:00") == 0       # 兩個變更之間
    assert canary_policy.offset_at(q, "2026-11-11T00:00:00+00:00") == 60      # 第二個變更之後：最後一筆
    # 第二個變更已生效（23:30 UTC + 60 分 = 次日 00:30）；只看第一筆就停止的實作會得到 2026-11-10
    assert canary_policy.local_date(q, "2026-11-10T23:30:00+00:00") == "2026-11-11"
    assert canary_policy.local_date(q, "2026-11-02T17:00:00+00:00") == "2026-11-03"
    assert canary_policy.local_date(q, "2026-11-03T17:00:00+00:00") == "2026-11-03"


# ---- 審查補強 R-T1-3：JSON 解碼的例外範圍（整數位數上限為 ValueError、過深巢狀為 RecursionError）----

PATHOLOGICAL_JSON = ["1" * 5000, "[" * 100000]


@pytest.mark.parametrize("payload", PATHOLOGICAL_JSON, ids=["int_digits", "deep_nesting"])
def test_read_ndjson_treats_pathological_lines_as_malformed(tmp_path, payload):
    p = tmp_path / "x.ndjson"
    p.write_text('{"a":1}\n' + payload + '\n{"b":2}\n', encoding="utf-8")
    recs, bad = jsonio.read_ndjson(p)
    assert (recs, bad) == ([{"a": 1}, {"b": 2}], [2])


@pytest.mark.parametrize("payload", PATHOLOGICAL_JSON, ids=["int_digits", "deep_nesting"])
def test_run_json_pathological_stdout_returns_none(payload):
    def fake(argv, **kw):
        return CmdResult(rc=0, stdout=payload, stderr="", elapsed_s=0.0, timed_out=False)

    res, doc = run_json(fake, ["x"])
    assert res.rc == 0 and doc is None


@pytest.mark.parametrize("payload", [p.encode("utf-8") for p in PATHOLOGICAL_JSON], ids=["int_digits", "deep_nesting"])
def test_load_policy_pathological_json_is_policy_invalid(tmp_path, payload):
    p = tmp_path / "canary_policy.json"
    p.write_bytes(payload)
    with pytest.raises(HarnessError) as ei:
        canary_policy.load_policy(p)
    assert ei.value.code == "HARNESS_CANARY_POLICY_INVALID"


# ---- 審查補強 R-T1-4：共用 helper 契約（先前僅間接覆蓋，變異後全數存活）----


def test_utc_now_iso_is_fixed_width_microseconds():
    s = jsonio.utc_now_iso()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00", s), s
    assert jsonio.parse_iso(s).utcoffset().total_seconds() == 0


def test_dumps_report_sorts_keys_and_keeps_unicode():
    s = jsonio.dumps_report({"b": 1, "a": "中"})
    assert s.index('"a"') < s.index('"b"')                 # sort_keys=True
    assert "中" in s and "\\u" not in s                     # ensure_ascii=False
    assert s.endswith("\n")


def test_read_ndjson_non_object_line_is_malformed(tmp_path):
    p = tmp_path / "x.ndjson"
    p.write_text('{"a":1}\n[1]\n"str"\n\n{"b":2}\n', encoding="utf-8")   # 合法 JSON 但非物件 → 壞行；空白行略過
    recs, bad = jsonio.read_ndjson(p)
    assert (recs, bad) == ([{"a": 1}, {"b": 2}], [2, 3])


def test_load_reason_codes_returns_fresh_copies():
    reg = load_reason_codes()
    reg["HARNESS_CANARY_POLICY_INVALID"]["meaning"] = "tampered"
    del reg["HARNESS_ABORTED"]
    again = load_reason_codes()
    assert again["HARNESS_CANARY_POLICY_INVALID"]["meaning"] != "tampered"
    assert "HARNESS_ABORTED" in again


def test_harness_error_detail_and_message():
    err = HarnessError("HARNESS_CANARY_POLICY_INVALID", "submits low")
    assert (err.code, err.detail) == ("HARNESS_CANARY_POLICY_INVALID", "submits low")
    assert str(err) == "HARNESS_CANARY_POLICY_INVALID: submits low"
    assert HarnessError("HARNESS_ABORTED").detail == ""


def test_harness_error_copy_and_pickle_roundtrip():
    err = HarnessError("HARNESS_CANARY_POLICY_INVALID", "detail")
    for clone in (copy.copy(err), copy.deepcopy(err), pickle.loads(pickle.dumps(err))):
        assert type(clone) is HarnessError
        assert (clone.code, clone.detail) == (err.code, err.detail)
