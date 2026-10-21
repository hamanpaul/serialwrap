"""Slice 2.1 必要案例集合凍結與承接率斷路器的測試（P5-T0b，§11.3、§12.2、OD-02、OD-13）。

只讀取 repo 內的案例鎖與凍結檔，並以暫存目錄執行凍結腳本；不啟動 daemon、不接真實序列埠。
腳本以 importlib 在行程內載入（不開子行程），因此維持 memory_only。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACC = ROOT / "tests" / "acceptance"
FREEZE = ACC / "breaker" / "slice_2.1.json"
LOCK = ACC / "cases.lock.json"


def _load(name: str, rel: str):
    """以檔案路徑載入腳本；檔案不存在時 exec_module 會丟出 FileNotFoundError（紅燈原因）。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _freeze() -> dict:
    return json.loads(FREEZE.read_text(encoding="utf-8"))


def test_freeze_matches_lock():
    """分母以凍結時的 required_case_ids 為準，並與 lock["sets"]["slice_2.1"] 嚴格相等（lock 為 add-only，
    後續切片只新增 slice_2.1 以外的案例）。cases_lock_sha256 僅作 provenance：lock 每次 add-only 重算都會改位元組，
    只有 cases_revision 相同時才比對。分母指紋 denominator_sha256 以 lock 現行的 case_attrs 重算比對，
    確認 required 案例的屬性未被改動。"""
    fr = _freeze()
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    assert fr["required_case_ids"] == lock["sets"]["slice_2.1"]
    assert fr["denominator"] == len(lock["sets"]["slice_2.1"])
    canon = json.dumps([[cid, lock["case_attrs"][cid]] for cid in fr["required_case_ids"]],
                       ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    assert fr["denominator_sha256"] == hashlib.sha256(canon.encode("utf-8")).hexdigest()
    if fr["cases_revision"] == lock["cases_revision"]:
        assert fr["cases_lock_sha256"] == hashlib.sha256(LOCK.read_bytes()).hexdigest()


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
