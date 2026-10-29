"""OD-61：wal_s1w_perf 的 Linux 門檻以擁有者 WSL2 主機（ext4 vhdx）量測為準，由編排者在本機量測後匯入 gate；
雲端 virtio 結果只作診斷。gate 須能接受外部匯入的 wal_s1w 證據檔（附主機類型標註），並核對可追溯性。"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("slice21_gate_judge", ROOT / "scripts" / "rust" / "slice21_gate_judge.py")
judge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(judge)

HEAD = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
FORMAL = {"streams": 4, "records": 10000, "payload": 1024, "rotate_bytes": 67108864, "prefill_ratio": 0.9375, "rate": 0}


HOST = {"cpu": "AMD Test CPU", "cpu_count": 24, "memory_bytes": 1 << 36, "os_build": "6.18.54.1-microsoft-standard-WSL2",
        "storage_device": "/dev/sdd", "fs_type": "ext4"}
WORKLOAD = {"total_records": 40000, "rotate_bytes": 67108864, "prefill_bytes": [62915031] * 4}
# OD-66 以數值重算判定（不採證據自報的 verdict）：PASS 夾具的數值同時符合 §1.2 與 OD-66；FAIL 夾具兩者皆超標。
_GOOD = ({"p50": 0.003, "p95": 0.008, "p99": 0.015}, 2.1)
_BAD = ({"p50": 0.3, "p95": 0.4, "p99": 0.5}, 150.0)


def _doc(verdict="PASS", virt="wsl2", rev=HEAD, append=None, rotate=None, **params):
    good_append, good_rotate = _GOOD if verdict == "PASS" else _BAD
    return {"schema": "wal_s1w.v1", "verdict": verdict, "failures": [] if verdict == "PASS" else ["append_p99 0.5 ms > 0.02 ms"],
            "params": {**FORMAL, **params}, "thresholds": {"platform": "linux"},
            "append_ms": dict(append or good_append), "rotate_max_ms": good_rotate if rotate is None else rotate, **WORKLOAD,
            "env": {"virtualization": virt, "git_revision": rev, "source_tree_dirty": False, "os": "posix", **HOST}}


def _py(append_ms=(1.010, 2.251, 3.207), rotate_ms=902.3, **env):
    """同主機同時段 Python 基線（`scripts/stage0/wal_microbench.py` 輸出形狀；OD-66）。"""
    p50, p95, p99 = (round(v * 1e6) for v in append_ms)
    return {"append_ns": {"n": 39996, "p50": p50, "p95": p95, "p99": p99}, "rotate_max_ns": round(rotate_ms * 1e6), **WORKLOAD,
            "env": {"virtualization": "wsl2", "os": "linux", **HOST, **env}}


def test_imported_wsl2_evidence_is_formal():
    # OD-66：WSL2 證據須附同主機同時段 Python 基線（oracle 變更：原案未附基線即可 PASS）。
    st, reasons = judge.wal_s1w_verdict(None, imported=_doc(), host_label="owner-wsl2-ext4-vhdx", formal_params=FORMAL,
                                        python_baseline=_py())
    assert st == "PASS", reasons
    st, reasons = judge.wal_s1w_verdict(None, imported=_doc("FAIL"), host_label="owner-wsl2-ext4-vhdx", formal_params=FORMAL,
                                        python_baseline=_py())
    assert st == "FAIL"


@pytest.mark.parametrize("doc,label,why", [
    (_doc(virt="vm:kvm"), "owner-wsl2", "vm"),                  # 雲端 virtio 只作診斷
    (_doc(), "", "標註"),                                        # 未附主機類型標註
    (_doc(rev="unknown"), "owner-wsl2", "git_revision"),         # 無法追溯 commit
    (_doc(rev="0" * 40), "owner-wsl2", "git_revision"),          # 不是本分支歷史
    (_doc(records=100), "owner-wsl2", "params"),                 # 非正式量測參數
    ({**_doc(), "schema": "x"}, "owner-wsl2", "schema"),
])
def test_imported_evidence_rejected_when_not_traceable_or_not_formal(doc, label, why):
    st, reasons = judge.wal_s1w_verdict(None, imported=doc, host_label=label, formal_params=FORMAL)
    assert st == "EVIDENCE_MISSING" and any(why in r for r in reasons), reasons


def test_local_cloud_measurement_is_diagnostic_only():
    """OD-61：未匯入時，雲端（vm）量測只作診斷 → EVIDENCE_MISSING（不判 PASS／FAIL）；本機 WSL2／原生量測照判。"""
    st, reasons = judge.wal_s1w_verdict(_doc("FAIL", virt="vm:kvm"), formal_params=FORMAL)
    assert st == "EVIDENCE_MISSING" and any("診斷" in r for r in reasons)
    assert judge.wal_s1w_verdict(_doc("FAIL", virt="native"), formal_params=FORMAL)[0] == "FAIL"
    assert judge.wal_s1w_verdict(_doc(virt="native"), formal_params=FORMAL)[0] == "PASS"   # 原生：§1.2、免 Python 基線
    assert judge.wal_s1w_verdict(None, formal_params=FORMAL)[0] == "EVIDENCE_MISSING"


def test_stale_import_rejected_when_wal_code_changed_since(monkeypatch):
    """匯入證據的 commit 之後若 WAL 寫入相關路徑有變更，證據不再代表受測版本 → EVIDENCE_MISSING。"""
    monkeypatch.setattr(judge, "_paths_changed_since", lambda rev, paths: ["crates/sw-core/src/wal.rs"])
    st, reasons = judge.wal_s1w_verdict(None, imported=_doc(), host_label="owner-wsl2", formal_params=FORMAL)
    assert st == "EVIDENCE_MISSING" and any("wal.rs" in r for r in reasons)


def test_cli_and_gate_wiring(tmp_path):
    imp = tmp_path / "imp.json"
    imp.write_text(json.dumps(_doc()), encoding="utf-8")
    py = tmp_path / "py.json"
    py.write_text(json.dumps(_py()), encoding="utf-8")
    out = tmp_path / "v.json"
    assert judge.main(["wal-s1w-verdict", "--import", str(imp), "--python-baseline", str(py), "--host-label", "owner-wsl2",
                       "--out", str(out)]) == 0
    v = json.loads(out.read_text(encoding="utf-8"))
    assert v["verdict"] == "PASS" and v["source"] == "import" and v["host_label"] == "owner-wsl2"
    # OD-66：報告標明所用門檻
    assert v["thresholds"]["host"] == "wsl2" and "OD-66" in v["thresholds"]["source"]
    text = (ROOT / "scripts" / "rust" / "slice21_gate.sh").read_text(encoding="utf-8")
    assert "SW_GATE_WAL_S1W_IMPORT" in text and "SW_GATE_WAL_S1W_HOST" in text and "wal-s1w-verdict" in text
    assert "SW_GATE_WAL_S1W_PY_BASELINE" in text and "--python-baseline" in text
    import yaml

    exp = yaml.safe_load((ROOT / "tests" / "acceptance" / "slice21_gate_expect.yaml").read_text(encoding="utf-8"))
    assert exp["wal_s1w"]["formal_params"] == FORMAL and exp["wal_s1w"]["relevant_paths"]


@pytest.mark.parametrize("dirty", [True, None, "unknown"])
def test_imported_evidence_requires_clean_source_tree(dirty):
    """sol P5-4f8acdf-2：量測時工作樹不乾淨（或未記錄）時 git_revision 不代表受測碼 → EVIDENCE_MISSING。"""
    doc = _doc()
    if dirty is None:
        doc["env"].pop("source_tree_dirty")
    else:
        doc["env"]["source_tree_dirty"] = dirty
    st, reasons = judge.wal_s1w_verdict(None, imported=doc, host_label="owner-wsl2", formal_params=FORMAL)
    assert st == "EVIDENCE_MISSING" and any("source_tree_dirty" in r for r in reasons), reasons


def test_relevant_paths_cover_build_profile_manifests():
    """sol P5-4f8acdf-2：release profile（workspace Cargo.toml）與 crate manifest 變更也使舊證據失效。"""
    import yaml

    exp = yaml.safe_load((ROOT / "tests" / "acceptance" / "slice21_gate_expect.yaml").read_text(encoding="utf-8"))
    paths = exp["wal_s1w"]["relevant_paths"]
    for need in ("Cargo.toml", "crates/sw-core/Cargo.toml", "crates/sw-pal/Cargo.toml"):
        assert need in paths, need
    # sol P5-86b54d8-2：append 熱路徑經 sw-proto（crc32_hex、WalRecord::to_line／pyjson、pycompat）。
    for need in ("crates/sw-proto/src/wal_types.rs", "crates/sw-proto/src/pyjson.rs", "crates/sw-proto/src/pycompat.rs",
                 "crates/sw-proto/Cargo.toml"):
        assert any(need == p or need.startswith(p.rstrip("/") + "/") for p in paths), need



# --------------------------------------------------------------------------- OD-66（擁有者裁決 (a)，2026-10-29）

OD66 = {"append_p50_ms": 0.050, "append_p95_ms": 0.100, "append_p99_ms": 0.200, "rotate_max_ms": 100.0}
SEC12 = {"append_p50_ms": 0.005, "append_p95_ms": 0.010, "append_p99_ms": 0.020, "rotate_max_ms": 3.0}
# 86b54d8 WSL2 第 1 輪的實測值（§1.2 判 FAIL；OD-66 門檻內且每項優於同時段 Python 基線）
WSL2_R1 = ({"p50": 0.021592, "p95": 0.030161, "p99": 0.052252}, 48.255355)


def test_host_thresholds_selected_by_host_type():
    """OD-66：WSL2 證據用固定門檻並要求不劣於 Python 基線；§1.2 原門檻保留給原生。expect 與判定器預設一致。"""
    import yaml

    w = judge.wal_s1w_thresholds("wsl2")
    assert {k: w[k] for k in OD66} == OD66 and w["not_worse_than_python_baseline"] is True and "OD-66" in w["source"]
    n = judge.wal_s1w_thresholds("native")
    assert {k: n[k] for k in SEC12} == SEC12 and n["not_worse_than_python_baseline"] is False and "§1.2" in n["source"]
    exp = yaml.safe_load((ROOT / "tests" / "acceptance" / "slice21_gate_expect.yaml").read_text(encoding="utf-8"))
    assert exp["wal_s1w"]["host_thresholds"] == judge.WAL_S1W_HOST_THRESHOLDS


def test_wsl2_evidence_judged_by_od66_not_self_reported_section_1_2_verdict():
    doc = _doc("FAIL", append=WSL2_R1[0], rotate=WSL2_R1[1])          # 證據自報 FAIL（依 §1.2）
    st, reasons = judge.wal_s1w_verdict(None, imported=doc, host_label="owner-wsl2", formal_params=FORMAL, python_baseline=_py())
    assert st == "PASS", reasons
    assert any("OD-66" in r and "0.05" in r and "100" in r for r in reasons), reasons   # 標明所用門檻


@pytest.mark.parametrize("append,rotate,why", [
    ({"p50": 0.06, "p95": 0.08, "p99": 0.15}, 40.0, "append_p50"),
    ({"p50": 0.02, "p95": 0.03, "p99": 0.25}, 40.0, "append_p99"),
    ({"p50": 0.02, "p95": 0.03, "p99": 0.05}, 120.0, "rotate_max"),
])
def test_wsl2_od66_threshold_exceeded_is_fail(append, rotate, why):
    st, reasons = judge.wal_s1w_verdict(None, imported=_doc(append=append, rotate=rotate), host_label="owner-wsl2",
                                        formal_params=FORMAL, python_baseline=_py())
    assert st == "FAIL" and any(why in r for r in reasons), reasons


def test_wsl2_requires_python_baseline():
    st, reasons = judge.wal_s1w_verdict(None, imported=_doc(append=WSL2_R1[0], rotate=WSL2_R1[1]), host_label="owner-wsl2",
                                        formal_params=FORMAL)
    assert st == "EVIDENCE_MISSING" and any("Python 基線" in r for r in reasons), reasons


@pytest.mark.parametrize("py,why", [
    (_py(append_ms=(0.01, 0.02, 0.04)), "append_p99"),   # Rust p99 0.052 > Python 0.04
    (_py(rotate_ms=30.0), "rotate_max"),                  # Rust 48.3 ms > Python 30 ms
])
def test_wsl2_must_not_be_worse_than_python_baseline(py, why):
    st, reasons = judge.wal_s1w_verdict(None, imported=_doc(append=WSL2_R1[0], rotate=WSL2_R1[1]), host_label="owner-wsl2",
                                        formal_params=FORMAL, python_baseline=py)
    assert st == "FAIL" and any(why in r and "Python 基線" in r for r in reasons), reasons


@pytest.mark.parametrize("py,why", [
    (_py(cpu="Other CPU"), "cpu"),
    (_py(os_build="6.6.0"), "os_build"),
    (_py(storage_device="/dev/sde"), "storage_device"),
    (_py(virtualization="native"), "virtualization"),
    ({**_py(), "total_records": 4000}, "total_records"),
    ({**_py(), "rotate_max_ns": None}, "rotate_max_ns"),
])
def test_python_baseline_must_match_host_and_workload(py, why):
    st, reasons = judge.wal_s1w_verdict(None, imported=_doc(append=WSL2_R1[0], rotate=WSL2_R1[1]), host_label="owner-wsl2",
                                        formal_params=FORMAL, python_baseline=py)
    assert st == "EVIDENCE_MISSING" and any(why in r for r in reasons), reasons


def test_native_evidence_keeps_section_1_2_thresholds():
    doc = _doc(virt="native", append=WSL2_R1[0], rotate=WSL2_R1[1])   # 自報 PASS，但數值超 §1.2
    st, reasons = judge.wal_s1w_verdict(None, imported=doc, host_label="owner-native", formal_params=FORMAL)
    assert st == "FAIL" and any("§1.2" in r for r in reasons), reasons


def test_multiple_imported_runs_take_worst_verdict():
    runs = [_doc(append=WSL2_R1[0], rotate=WSL2_R1[1]), _doc(append=WSL2_R1[0], rotate=130.0)]
    st, reasons = judge.wal_s1w_verdict(None, imported=runs, host_label="owner-wsl2", formal_params=FORMAL, python_baseline=_py())
    assert st == "FAIL" and any("第 2 輪" in r for r in reasons), reasons
    st, _ = judge.wal_s1w_verdict(None, imported=runs[:1] * 3, host_label="owner-wsl2", formal_params=FORMAL, python_baseline=_py())
    assert st == "PASS"


def test_od66_rejudge_86b54d8_wsl2_evidence(monkeypatch):
    """OD-66：以 86b54d8 的三輪 WSL2 證據＋同時段 Python 基線重新判定（證據複本見 p5-evidence/od61-wal-s1w-wsl2-86b54d8/）。
    可追溯性（之後 WAL 路徑未變更）另由 gate 實跑核對，此處只驗門檻判定。"""
    monkeypatch.setattr(judge, "_paths_changed_since", lambda rev, paths: [])
    d = ROOT / "docs" / "rust-refactor" / "reviews" / "p5-evidence" / "od61-wal-s1w-wsl2-86b54d8"
    runs = [json.loads((d / f"wal_s1w-{i}.json").read_text(encoding="utf-8")) for i in (1, 2, 3)]
    py = json.loads((d / "python-baseline-wal_microbench.json").read_text(encoding="utf-8"))
    assert all(r["verdict"] == "FAIL" for r in runs)          # 依 §1.2 自報 FAIL
    st, reasons = judge.wal_s1w_verdict(None, imported=runs, host_label="owner-wsl2-ext4-vhdx", formal_params=FORMAL,
                                        python_baseline=py)
    assert st == "PASS", reasons
