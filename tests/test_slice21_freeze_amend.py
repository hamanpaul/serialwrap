"""Slice 2.1 凍結修訂（OD-34 起擁有者逐名核准的延後；`freeze_slice_cases.py --amend`）的測試。

以子行程執行 `regen_lock.py` 建立延後前／後的暫存鎖（process_rpc），只寫暫存目錄。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib

import pytest
import yaml

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

ROOT = pathlib.Path(__file__).resolve().parents[1]
ACC = ROOT / "tests" / "acceptance"
FREEZE = ACC / "breaker" / "slice_2.1.json"
LOCK = ACC / "cases.lock.json"
CASES = ACC / "cases.yaml"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _freeze() -> dict:
    return json.loads(FREEZE.read_text(encoding="utf-8"))


def _defer_in_tmp(tmp_path, cid: str, to_slice: str, od: str):
    """在暫存目錄複製 cases.yaml／lock 並以 regen_lock 逐名核准延後（OD 編號）；回傳 (cases, lock)。"""
    import subprocess
    import sys as _sys

    regen = [_sys.executable, str(ACC / "regen_lock.py"), "--add-only"]
    cases_doc = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    target = next(c for c in cases_doc["cases"] if c["case_id"] == cid)
    # 先建「延後前」的暫存鎖（該案例在 2.1），再改 daemon_slice 並以核准延後重算。
    target["daemon_slice"] = "2.1"
    cases_doc["revision"] = "2099-01-01.0"
    tcases = tmp_path / "cases.yaml"
    tlock = tmp_path / "cases.lock.json"
    tcases.write_text(yaml.safe_dump(cases_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    pre = subprocess.run([*regen, "--cases", str(tcases), "--lock", str(tlock)], capture_output=True, text=True,
                         encoding="utf-8")
    assert pre.returncode == 0, pre.stderr
    target["daemon_slice"] = to_slice
    cases_doc["revision"] = "2099-01-01.1"
    tcases.write_text(yaml.safe_dump(cases_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    r = subprocess.run([_sys.executable, str(ACC / "regen_lock.py"), "--add-only", "--cases", str(tcases), "--lock", str(tlock),
                        "--allow-defer", f"{cid}:2.1:{to_slice}:{od}"], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return tcases, tlock


def test_freeze_amend_records_owner_deferral_and_keeps_base(tmp_path, monkeypatch):
    """OD-34：凍結檔不得覆寫；擁有者逐名核准的延後以 `--amend` 追加修訂紀錄（附 OD 編號、新 provenance），
    有效分母＝凍結分母扣除核准延後者；觀測窗口與 Stage 2 起算日不變。未核准的移除、未引用 OD、鎖未同步延後皆拒絕。"""
    freeze = _load("freeze_slice_cases", "scripts/rust/freeze_slice_cases.py")
    bdir = tmp_path / "breaker"
    bdir.mkdir()
    monkeypatch.setattr(freeze, "BREAKER_DIR", bdir)
    base_doc = _freeze()
    base_path = bdir / "slice_2.1.json"
    base_path.write_bytes(FREEZE.read_bytes())
    base_bytes = base_path.read_bytes()
    cid = "l15-delayed-reply"
    assert cid in base_doc["required_case_ids"]
    work = tmp_path / "work"
    work.mkdir()
    tcases, tlock = _defer_in_tmp(work, cid, "2.3", "OD-34")
    common = ["--slice", "2.1", "--date", "2026-10-28", "--lock", str(tlock), "--cases", str(tcases)]
    # 拒絕：未引用 OD／OD 不合法／延後清單與鎖不符
    assert freeze.main(["--amend", *common, "--defer", f"{cid}:2.3"]) == 2
    assert freeze.main(["--amend", *common, "--defer", f"{cid}:2.3:ODX"]) == 2
    assert freeze.main(["--amend", *common]) == 2                                     # 鎖少了 cid 但未核准
    assert freeze.main(["--amend", *common, "--defer", f"{cid}:2.2:OD-34"]) == 2     # 切片與鎖不符
    assert not (bdir / "slice_2.1.amendments.json").exists()
    assert freeze.main(["--amend", *common, "--defer", f"{cid}:2.3:OD-34"]) == 0
    assert base_path.read_bytes() == base_bytes, "凍結檔本體不得改寫"
    eff = freeze.load_effective_freeze(base_path)
    assert eff["required_case_ids"] == [c for c in base_doc["required_case_ids"] if c != cid]
    assert eff["denominator"] == base_doc["denominator"] - 1
    assert eff["window"] == base_doc["window"] and eff["stage2_started_at"] == base_doc["stage2_started_at"]
    assert eff["frozen_at"] == base_doc["frozen_at"]
    assert eff["cases_revision"] == "2099-01-01.1"
    assert eff["cases_lock_sha256"] == hashlib.sha256(tlock.read_bytes()).hexdigest()
    amend = eff["amendments"][-1]
    assert amend["removed"] == [{"case_id": cid, "to_slice": "2.3", "decision": "OD-34"}]
    assert amend["base_freeze_sha256"] == hashlib.sha256(base_bytes).hexdigest()
    # 修訂檔遭竄改（移除項與核准不符）即拒絕載入
    tampered = json.loads((bdir / "slice_2.1.amendments.json").read_text(encoding="utf-8"))
    tampered[-1]["required_case_ids"] = tampered[-1]["required_case_ids"][1:]
    (bdir / "slice_2.1.amendments.json").write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError):
        freeze.load_effective_freeze(base_path)
