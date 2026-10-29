"""Slice 2.1 必要案例集合凍結與承接率斷路器的測試（P5-T0b，§11.3、§12.2、OD-02、OD-13）。

只讀取 repo 內的案例鎖與凍結檔，並以暫存目錄執行凍結腳本；不啟動 daemon、不接真實序列埠。
腳本以 importlib 在行程內載入（不開子行程），因此維持 memory_only。
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import sys

import pytest
import yaml

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACC = ROOT / "tests" / "acceptance"
FREEZE = ACC / "breaker" / "slice_2.1.json"
LOCK = ACC / "cases.lock.json"
CASES = ACC / "cases.yaml"


def _load(name: str, rel: str):
    """以檔案路徑載入腳本；檔案不存在時 exec_module 會丟出 FileNotFoundError（紅燈原因）。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _freeze() -> dict:
    return json.loads(FREEZE.read_text(encoding="utf-8"))


def _effective() -> dict:
    """凍結檔＋擁有者逐名核准的修訂（`slice_2.1.amendments.json`；OD-34 起）的有效凍結內容。"""
    return _load("freeze_slice_cases", "scripts/rust/freeze_slice_cases.py").load_effective_freeze(FREEZE)


def test_freeze_matches_lock():
    """分母以凍結時的 required_case_ids 為準（扣除擁有者逐名核准、記於修訂檔的延後案例），並與
    lock["sets"]["slice_2.1"] 嚴格相等（lock 為 add-only，後續切片只新增 slice_2.1 以外的案例）。
    cases_lock_sha256 僅作 provenance：lock 每次 add-only 重算都會改位元組，只有 cases_revision 相同時才比對。
    分母指紋 denominator_sha256 以 lock 現行的 case_attrs 重算比對，確認 required 案例的屬性未被改動。
    另釘住凍結欄位（schema、slice、窗口、Stage 2 起算日）與 cases.yaml 重算的集合，
    cases_yaml_sha256 同樣只在 cases_revision 相同時比對。凍結檔本體不因修訂改寫：有效分母恆為其子集，
    且被扣除者每一筆都附 OD 編號（OD-34）。"""
    base = _freeze()
    fr = _effective()
    assert set(fr["required_case_ids"]) <= set(base["required_case_ids"])
    removed = {r["case_id"]: r["decision"] for a in fr["amendments"] for r in a["removed"]}
    assert set(base["required_case_ids"]) - set(fr["required_case_ids"]) == set(removed)
    assert all(od.startswith("OD-") for od in removed.values())
    for key in ("frozen_at", "window", "stage2_started_at", "schema_version", "slice", "denominator_set"):
        assert fr[key] == base[key], key
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    cases_doc = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    assert fr["required_case_ids"] == lock["sets"]["slice_2.1"]
    assert fr["denominator"] == len(lock["sets"]["slice_2.1"])
    canon = json.dumps([[cid, lock["case_attrs"][cid]] for cid in fr["required_case_ids"]],
                       ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert fr["denominator_sha256"] == hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert fr["schema_version"] == 1 and fr["slice"] == "2.1" and fr["denominator_set"] == "slice_2.1"
    assert fr["window"] == {"start": fr["frozen_at"], "weeks": 3}
    assert fr["stage2_started_at"] == fr["frozen_at"]
    # P6 Task 0：後續切片以 add-only 追加案例時 cases.yaml revision 會前進（YYYY-MM-DD.N）；凍結時的 revision 不得晚於現行值。
    # parity-amendments-2026-10-26 #183（P6 審查 F4）：以 (YYYY,MM,DD,N) 整數 tuple 比較（字串序在 N≥10 時錯：".10" < ".9"）。
    # 位元組指紋只在 revision 相同時比對（見下方 if）；revision 前進後改由 case_fingerprints 逐案驗證。
    assert _revision_key(fr["cases_revision"]) <= _revision_key(cases_doc["revision"])
    recomputed = _load("regen_lock", "tests/acceptance/regen_lock.py").compute_sets(cases_doc)["slice_2.1"]
    assert fr["required_case_ids"] == recomputed
    if fr["cases_revision"] == lock["cases_revision"]:
        assert fr["cases_lock_sha256"] == hashlib.sha256(LOCK.read_bytes()).hexdigest()
        assert fr["cases_yaml_sha256"] == hashlib.sha256(CASES.read_bytes()).hexdigest()


def _revision_key(rev: str) -> tuple[int, int, int, int]:
    """cases.yaml revision ``YYYY-MM-DD.N`` → (YYYY, MM, DD, N)；格式不符即 AssertionError。"""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})\.(\d+)", str(rev))
    assert m, f"revision 格式必須為 YYYY-MM-DD.N：{rev!r}"
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


def test_revision_key_orders_numerically():
    assert _revision_key("2026-10-24.9") < _revision_key("2026-10-24.10")
    assert _revision_key("2026-10-24.10") < _revision_key("2026-10-25.1")
    assert _revision_key("2026-09-30.99") < _revision_key("2026-10-01.1")
    for bad in ("2026-10-24", "2026-10-24.x", "20261024.1", "2026-10-24.1 "):
        with pytest.raises(AssertionError):
            _revision_key(bad)


def _case_fingerprint(case: dict) -> str:
    return hashlib.sha256(json.dumps(case, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                          .encode("utf-8")).hexdigest()


@pytest.mark.parametrize("slice_id", ["2.1", "2.2"])
def test_freeze_case_fingerprints_match_current_cases(slice_id):
    """parity-amendments-2026-10-26 #183（P6 審查 F4）：revision 前進後整檔位元組指紋（cases_yaml_sha256）不再比對，
    必要案例的非 case_attrs 欄位（source_ref、title、implemented_by…）被改動會無聲通過；凍結檔因此記錄每個必要案例
    完整 YAML 映射的 sha256（canonical JSON），此處逐案重算比對（補強，非削弱）。"""
    base_path = ACC / "breaker" / f"slice_{slice_id}.json"
    fr = json.loads(base_path.read_text(encoding="utf-8"))
    cases = {c["case_id"]: c for c in yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]}
    fps = fr.get("case_fingerprints")
    if fps is None:
        # 凍結本體早於 #183（Slice 2.1）：本體不改寫（OD-34 修訂鏈以本體雜湊為錨），指紋記於旁置檔，
        # 數值即 #183 當時（c2924a5）寫入本體者。
        side = base_path.with_name(f"slice_{slice_id}.case_fingerprints.json")
        assert side.exists(), f"slice_{slice_id}.json 缺 case_fingerprints，且無旁置檔 {side.name}"
        fps = json.loads(side.read_text(encoding="utf-8"))
    assert isinstance(fps, dict), f"slice_{slice_id} 的 case_fingerprints 必須為物件"
    assert sorted(fps) == sorted(fr["required_case_ids"])
    # 有效分母（扣除擁有者逐名核准、記於 `slice_<X>.amendments.json` 的延後案例，OD-34）逐案比對；
    # 被延後者已不在本切片分母內，其 daemon_slice 依裁決改變。
    effective = _load("freeze_slice_cases", "scripts/rust/freeze_slice_cases.py").load_effective_freeze(base_path)
    assert set(effective["required_case_ids"]) <= set(fr["required_case_ids"])
    # OD-52：擁有者逐名核准的凍結後欄位變更以「指紋更新」修訂記錄（舊指紋須等於前一版有效指紋，見
    # `load_effective_freeze`）；有效指紋只換掉被逐名核准的案例，其餘仍以凍結時的指紋比對。
    eff_fps = effective["case_fingerprints"]
    updated = {u["case_id"] for a in effective["amendments"] for u in a.get("fingerprint_updates", [])}
    assert {cid for cid in fps if eff_fps[cid] != fps[cid]} == updated
    assert all(od.startswith("OD-") for a in effective["amendments"] for od in a["decisions"])
    changed = [cid for cid in effective["required_case_ids"] if _case_fingerprint(cases[cid]) != eff_fps[cid]]
    assert not changed, f"凍結後必要案例內容被改動：{changed}"


def test_freeze_writes_case_fingerprints(tmp_path):
    freeze = _load("freeze_slice_cases", "scripts/rust/freeze_slice_cases.py")
    out = tmp_path / "slice_2.1.json"
    assert freeze.main(["--slice", "2.1", "--date", "2026-10-21", "--out", str(out)]) == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    cases = {c["case_id"]: c for c in yaml.safe_load(CASES.read_text(encoding="utf-8"))["cases"]}
    assert doc["case_fingerprints"] == {cid: _case_fingerprint(cases[cid]) for cid in doc["required_case_ids"]}


def test_freeze_precedes_any_gate_run():
    frozen = _freeze()["frozen_at"]
    for report in sorted((ROOT / "target" / "slice21-gate").glob("*/report.json")):
        started = json.loads(report.read_text(encoding="utf-8"))["started_at"][:10]
        assert frozen <= started, f"{report}：閘門報告早於分母凍結（{started} < {frozen}）"


def test_freeze_refuses_overwrite(tmp_path):
    freeze = _load("freeze_slice_cases", "scripts/rust/freeze_slice_cases.py")
    out = tmp_path / "slice_2.1.json"
    assert freeze.main(["--slice", "2.1", "--date", "2026-10-21", "--out", str(out)]) == 0
    first = out.read_bytes()
    assert freeze.main(["--slice", "2.1", "--date", "2026-10-22", "--out", str(out)]) == 1
    assert out.read_bytes() == first
    assert sorted(p.name for p in tmp_path.iterdir()) == ["slice_2.1.json"]


def test_freeze_accepts_frozen_at_alias_and_rejects_non_iso_dates(tmp_path):
    freeze = _load("freeze_slice_cases", "scripts/rust/freeze_slice_cases.py")
    out = tmp_path / "slice_2.1.json"
    assert freeze.main(["--slice", "2.1", "--frozen-at", "2026-10-21", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["frozen_at"] == "2026-10-21"
    for bad in ("20261021", "2026-W43-3"):
        assert freeze.main(["--slice", "2.1", "--date", bad, "--out", str(tmp_path / "x.json")]) == 2
    assert not (tmp_path / "x.json").exists()


def test_freeze_later_slice_inherits_stage2_origin(tmp_path, monkeypatch):
    freeze = _load("freeze_slice_cases", "scripts/rust/freeze_slice_cases.py")
    monkeypatch.setattr(freeze, "BREAKER_DIR", tmp_path)
    out = tmp_path / "slice_2.2.json"
    # 缺 Slice 2.1 凍結檔：無法取得 OD-02 單一起點，拒絕（exit 2）
    assert freeze.main(["--slice", "2.2", "--date", "2026-12-01", "--out", str(out)]) == 2
    (tmp_path / "slice_2.1.json").write_text(json.dumps({"stage2_started_at": "2026-10-21"}), encoding="utf-8")
    # 未提供 --stage2-started-at 時沿用 2.1 的起算日（不以 2.2 開工日重置 24 週）
    assert freeze.main(["--slice", "2.2", "--date", "2026-12-01", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["stage2_started_at"] == "2026-10-21"
    # 指定不同的起算日：拒絕
    assert freeze.main(["--slice", "2.3", "--date", "2026-12-01", "--stage2-started-at", "2026-12-01",
                        "--out", str(tmp_path / "slice_2.3.json")]) == 2


def test_freeze_rejects_stage2_later_than_date(tmp_path, monkeypatch):
    freeze = _load("freeze_slice_cases", "scripts/rust/freeze_slice_cases.py")
    monkeypatch.setattr(freeze, "BREAKER_DIR", tmp_path)
    assert freeze.main(["--slice", "2.1", "--date", "2026-10-21", "--stage2-started-at", "2026-10-22",
                        "--out", str(tmp_path / "slice_2.1.json")]) == 2


def _breaker_files(tmp_path, snaps: list[dict] | None, rows_text: str | None = None):
    freeze_path = tmp_path / "freeze.json"
    freeze_path.write_text(json.dumps({"window": {"start": "2026-11-02", "weeks": 3},
                                       "stage2_started_at": "2026-10-21"}), encoding="utf-8")
    snap_path = tmp_path / "snap.ndjson"
    if rows_text is not None:
        snap_path.write_text(rows_text, encoding="utf-8")
    elif snaps is not None:
        snap_path.write_text("".join(json.dumps(s) + "\n" for s in snaps), encoding="utf-8")
    return freeze_path, snap_path


def test_breaker_trips_on_flat_rate():
    sb = _load("slice_breaker", "scripts/rust/slice_breaker.py")
    fr = {"window": {"start": "2026-11-02", "weeks": 3}, "stage2_started_at": "2026-10-21", "denominator": 49}
    snaps = [{"date": "2026-11-02", "rate": 0.40}, {"date": "2026-11-09", "rate": 0.44},
             {"date": "2026-11-16", "rate": 0.48}]
    out = sb.evaluate(fr, snaps, today="2026-11-23", stage2_estimate_weeks=4)
    assert out["tripped"] is True
    assert round(out["window_delta_pp"], 1) == 8.0


def test_breaker_exact_ten_pp_does_not_trip_and_9_99_trips():
    sb = _load("slice_breaker", "scripts/rust/slice_breaker.py")
    fr = {"window": {"start": "2026-11-02", "weeks": 3}, "stage2_started_at": "2026-10-21"}
    exact = [{"date": "2026-11-02", "rate": 0.20}, {"date": "2026-11-23", "rate": 0.30}]
    assert sb.evaluate(fr, exact, today="2026-11-23", stage2_estimate_weeks=0)["tripped"] is False
    just_under = [{"date": "2026-11-02", "rate": 0.20}, {"date": "2026-11-23", "rate": 0.2999}]
    assert sb.evaluate(fr, just_under, today="2026-11-23", stage2_estimate_weeks=0)["tripped"] is True


def test_breaker_flags_insufficient_evidence_after_window():
    sb = _load("slice_breaker", "scripts/rust/slice_breaker.py")
    fr = {"window": {"start": "2026-11-02", "weeks": 3}, "stage2_started_at": "2026-10-21"}
    out = sb.evaluate(fr, [], today="2026-12-31", stage2_estimate_weeks=0)
    assert out["tripped"] is False and out["insufficient_evidence"] is True and out["reason"]
    assert sb.evaluate(fr, [], today="2026-11-10", stage2_estimate_weeks=0)["insufficient_evidence"] is False


def test_breaker_main_exit_codes(tmp_path, capsys):
    sb = _load("slice_breaker", "scripts/rust/slice_breaker.py")
    freeze_path, snap_path = _breaker_files(tmp_path, [])
    args = ["--freeze", str(freeze_path), "--snapshots", str(snap_path), "--today", "2026-12-31"]
    # 窗口已結束、快照為空：證據不足，exit 3（不得是 0）
    assert sb.main(args) == 3
    assert json.loads(capsys.readouterr().out)["insufficient_evidence"] is True
    # 快照檔路徑錯誤：輸入錯誤 exit 2（不得被當成零筆快照）
    assert sb.main(["--freeze", str(freeze_path), "--snapshots", str(tmp_path / "typo.ndjson"),
                    "--today", "2026-12-31"]) == 2
    # 凍結檔不存在：exit 2（不得與趨勢觸發的 exit 1 混淆）
    assert sb.main(["--freeze", str(tmp_path / "none.json"), "--snapshots", str(snap_path),
                    "--today", "2026-12-31"]) == 2
    # 平坦趨勢：exit 1
    freeze_path, snap_path = _breaker_files(tmp_path, [
        {"date": "2026-11-02", "rate": 0.40}, {"date": "2026-11-16", "rate": 0.48}])
    assert sb.main(["--freeze", str(freeze_path), "--snapshots", str(snap_path), "--today", "2026-11-23"]) == 1
    # 正常增長：exit 0
    freeze_path, snap_path = _breaker_files(tmp_path, [
        {"date": "2026-11-02", "rate": 0.40}, {"date": "2026-11-23", "rate": 0.60}])
    assert sb.main(["--freeze", str(freeze_path), "--snapshots", str(snap_path), "--today", "2026-11-23"]) == 0


def test_breaker_rejects_percent_style_rate(tmp_path):
    sb = _load("slice_breaker", "scripts/rust/slice_breaker.py")
    fr = {"window": {"start": "2026-11-02", "weeks": 3}, "stage2_started_at": "2026-10-21"}
    with pytest.raises(ValueError):
        sb.evaluate(fr, [{"date": "2026-11-02", "rate": 10}, {"date": "2026-11-09", "rate": 10.5}],
                    today="2026-11-23", stage2_estimate_weeks=0)
    freeze_path, snap_path = _breaker_files(tmp_path, [{"date": "2026-11-02", "rate": 10},
                                                        {"date": "2026-11-09", "rate": 10.5}])
    assert sb.main(["--freeze", str(freeze_path), "--snapshots", str(snap_path), "--today", "2026-11-23"]) == 2


def test_breaker_counts_only_valid_pass():
    sb = _load("slice_breaker", "scripts/rust/slice_breaker.py")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    ids = lock["sets"]["slice_2.1"]
    results = [
        {"case_id": ids[0], "verdict": "PASS"},
        {"case_id": ids[1], "verdict": "SKIP"},
        {"case_id": ids[2], "verdict": "EVIDENCE_MISSING"},
    ]
    assert sb.acceptance_rate(lock, results, set_name="slice_2.1") == 1 / len(ids)


def test_breaker_requires_every_verdict_of_a_case_to_pass():
    sb = _load("slice_breaker", "scripts/rust/slice_breaker.py")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    ids = lock["sets"]["slice_2.1"]
    mixed = [{"case_id": ids[0], "verdict": "PASS"}, {"case_id": ids[0], "verdict": "FAIL"},
             {"case_id": ids[1], "verdict": "PASS"}, {"case_id": ids[1], "verdict": "INSUFFICIENT_EVIDENCE"}]
    assert sb.acceptance_rate(lock, mixed, set_name="slice_2.1") == 0.0
    all_pass = [{"case_id": ids[0], "verdict": "PASS"}, {"case_id": ids[0], "verdict": "PASS"}]
    assert sb.acceptance_rate(lock, all_pass, set_name="slice_2.1") == 1 / len(ids)


def test_breaker_denominator_uses_frozen_ids_when_lock_grows():
    sb = _load("slice_breaker", "scripts/rust/slice_breaker.py")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    ids = lock["sets"]["slice_2.1"]
    frozen = ids[:4]
    results = [{"case_id": ids[0], "verdict": "PASS"}]
    assert sb.acceptance_rate(lock, results, set_name="slice_2.1", required_case_ids=frozen) == 0.25


def test_freeze_regular_file_parent_exits_2(tmp_path):
    """父路徑是一般檔案屬寫入錯誤（exit 2），不得被誤判為「凍結檔已存在」（exit 1）。"""
    freeze = _load("freeze_slice_cases", "scripts/rust/freeze_slice_cases.py")
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    assert freeze.main(["--slice", "2.1", "--date", "2026-10-21", "--out", str(blocker / "slice_2.1.json")]) == 2


def test_freeze_unwritable_directory_exits_2(tmp_path):
    """目錄不可寫屬寫入錯誤（exit 2）。root 可寫入唯讀目錄、Windows 無 POSIX 權限位元，故略過。"""
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("權限測試需非 root 的 POSIX 環境")
    freeze = _load("freeze_slice_cases", "scripts/rust/freeze_slice_cases.py")
    locked = tmp_path / "ro"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert freeze.main(["--slice", "2.1", "--date", "2026-10-21", "--out", str(locked / "slice_2.1.json")]) == 2
    finally:
        locked.chmod(0o700)
    assert list(locked.iterdir()) == []


def test_build_freeze_rejects_lock_set_drift(tmp_path):
    """鎖檔集合與 cases.yaml 重算結果不一致（手改或過期）時必須拒絕，不得寫入永久固定的分母。"""
    freeze = _load("freeze_slice_cases", "scripts/rust/freeze_slice_cases.py")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    lock["sets"]["slice_2.1"] = lock["sets"]["slice_2.1"][:-1]
    drifted = tmp_path / "cases.lock.json"
    drifted.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(ValueError):
        freeze.build_freeze(slice_id="2.1", date="2026-10-21", stage2_started_at="2026-10-21",
                            lock_path=drifted, yaml_path=CASES)


def test_freeze_stdio_is_utf8_under_cp1252(monkeypatch):
    """cp1252 stdio 下輸出繁中訊息不得擲 UnicodeEncodeError（否則寫入成功後仍以 exit 1 結束）。"""
    freeze = _load("freeze_slice_cases", "scripts/rust/freeze_slice_cases.py")
    raw_out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw_out, encoding="cp1252", write_through=True))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252", write_through=True))
    freeze._force_utf8_stdio()
    print("凍結檔已寫入")
    assert "凍結檔已寫入".encode("utf-8") in raw_out.getvalue()


def test_breaker_rejects_sparse_or_late_snapshots(tmp_path):
    """窗口已結束時，快照只涵蓋窗口前段或只涵蓋後段都不是足夠證據，不得以 exit 0 放行。"""
    sb = _load("slice_breaker", "scripts/rust/slice_breaker.py")
    fr = {"window": {"start": "2026-11-02", "weeks": 3}, "stage2_started_at": "2026-10-21"}
    early_only = [{"date": "2026-11-02", "rate": 0.0}, {"date": "2026-11-09", "rate": 0.10}]
    out = sb.evaluate(fr, early_only, today="2026-12-31", stage2_estimate_weeks=0)
    assert out["tripped"] is False and out["insufficient_evidence"] is True and "首尾" in out["reason"]
    late_only = [{"date": "2026-11-16", "rate": 0.20}, {"date": "2026-11-23", "rate": 0.30}]
    assert sb.evaluate(fr, late_only, today="2026-12-31", stage2_estimate_weeks=0)["insufficient_evidence"] is True
    for rows in (early_only, late_only):
        freeze_path, snap_path = _breaker_files(tmp_path, rows)
        assert sb.main(["--freeze", str(freeze_path), "--snapshots", str(snap_path), "--today", "2026-12-31"]) == 3


def test_breaker_od02_resubmit_boundary_and_exit_code(tmp_path):
    """OD-02：2026-10-21 至 2027-04-07 恰為 24.0 週（不超過上限）；預估須為有限且非負的數值，否則拒絕。"""
    sb = _load("slice_breaker", "scripts/rust/slice_breaker.py")
    fr = {"window": {"start": "2026-11-02", "weeks": 3}, "stage2_started_at": "2026-10-21"}
    flat_ok = [{"date": "2026-11-02", "rate": 0.40}, {"date": "2026-11-23", "rate": 0.60}]
    assert sb.evaluate(fr, flat_ok, today="2027-04-07", stage2_estimate_weeks=0)["od02_resubmit"] is False
    assert sb.evaluate(fr, flat_ok, today="2027-04-07", stage2_estimate_weeks=0.1)["od02_resubmit"] is True
    assert sb.evaluate(fr, flat_ok, today="2027-04-08", stage2_estimate_weeks=0)["od02_resubmit"] is True
    for bad in (float("nan"), float("inf"), -40.0, True):
        with pytest.raises(ValueError):
            sb.evaluate(fr, flat_ok, today="2027-04-07", stage2_estimate_weeks=bad)
    freeze_path, snap_path = _breaker_files(tmp_path, flat_ok)
    base = ["--freeze", str(freeze_path), "--snapshots", str(snap_path), "--today", "2027-01-27"]
    assert sb.main(base + ["--stage2-estimate-weeks", "12"]) == 1
    assert sb.main(base + ["--stage2-estimate-weeks", "nan"]) == 2
    assert sb.main(base + ["--stage2-estimate-weeks", "-40"]) == 2


def test_breaker_snapshot_denominator_cross_check(tmp_path):
    """快照附上 passed／denominator 時須與凍結分母一致，且 rate 須等於 passed÷denominator。"""
    sb = _load("slice_breaker", "scripts/rust/slice_breaker.py")
    freeze_path = tmp_path / "freeze.json"
    freeze_path.write_text(json.dumps({"window": {"start": "2026-11-02", "weeks": 3},
                                       "stage2_started_at": "2026-10-21", "denominator": 49}), encoding="utf-8")
    snap_path = tmp_path / "snap.ndjson"

    def run(rows: list[dict]) -> int:
        snap_path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return sb.main(["--freeze", str(freeze_path), "--snapshots", str(snap_path), "--today", "2026-11-23"])

    ok = [{"date": "2026-11-02", "rate": 20 / 49, "passed": 20, "denominator": 49},
          {"date": "2026-11-23", "rate": 30 / 49, "passed": 30, "denominator": 49}]
    assert run(ok) == 0
    shrunk = [dict(ok[0]), {"date": "2026-11-23", "rate": 30 / 48, "passed": 30, "denominator": 48}]
    assert run(shrunk) == 2
    mismatch = [dict(ok[0]), {"date": "2026-11-23", "rate": 0.7, "passed": 30, "denominator": 49}]
    assert run(mismatch) == 2


def test_breaker_stdio_is_utf8_under_cp1252(monkeypatch):
    """cp1252 stdio 下判定器輸出繁中原因時仍須以判定碼結束，不得因 UnicodeEncodeError 誤為 exit 1。"""
    sb = _load("slice_breaker", "scripts/rust/slice_breaker.py")
    raw_err = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252", write_through=True))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw_err, encoding="cp1252", write_through=True))
    sb._force_utf8_stdio()
    print("證據不足", file=sys.stderr)
    assert "證據不足".encode("utf-8") in raw_err.getvalue()

