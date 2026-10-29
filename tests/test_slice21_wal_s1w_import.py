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


def _doc(verdict="PASS", virt="wsl2", rev=HEAD, **params):
    return {"schema": "wal_s1w.v1", "verdict": verdict, "failures": [] if verdict == "PASS" else ["append_p99 0.05 ms > 0.02 ms"],
            "params": {**FORMAL, **params}, "thresholds": {"platform": "linux"},
            "append_ms": {"p50": 0.003, "p95": 0.008, "p99": 0.015}, "rotate_max_ms": 2.1,
            "env": {"virtualization": virt, "git_revision": rev, "fs_type": "ext4", "os": "posix"}}


def test_imported_wsl2_evidence_is_formal():
    st, reasons = judge.wal_s1w_verdict(None, imported=_doc(), host_label="owner-wsl2-ext4-vhdx", formal_params=FORMAL)
    assert st == "PASS", reasons
    st, reasons = judge.wal_s1w_verdict(None, imported=_doc("FAIL"), host_label="owner-wsl2-ext4-vhdx", formal_params=FORMAL)
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
    assert judge.wal_s1w_verdict(None, formal_params=FORMAL)[0] == "EVIDENCE_MISSING"


def test_stale_import_rejected_when_wal_code_changed_since(monkeypatch):
    """匯入證據的 commit 之後若 WAL 寫入相關路徑有變更，證據不再代表受測版本 → EVIDENCE_MISSING。"""
    monkeypatch.setattr(judge, "_paths_changed_since", lambda rev, paths: ["crates/sw-core/src/wal.rs"])
    st, reasons = judge.wal_s1w_verdict(None, imported=_doc(), host_label="owner-wsl2", formal_params=FORMAL)
    assert st == "EVIDENCE_MISSING" and any("wal.rs" in r for r in reasons)


def test_cli_and_gate_wiring(tmp_path):
    imp = tmp_path / "imp.json"
    imp.write_text(json.dumps(_doc()), encoding="utf-8")
    out = tmp_path / "v.json"
    assert judge.main(["wal-s1w-verdict", "--import", str(imp), "--host-label", "owner-wsl2", "--out", str(out)]) == 0
    v = json.loads(out.read_text(encoding="utf-8"))
    assert v["verdict"] == "PASS" and v["source"] == "import" and v["host_label"] == "owner-wsl2"
    text = (ROOT / "scripts" / "rust" / "slice21_gate.sh").read_text(encoding="utf-8")
    assert "SW_GATE_WAL_S1W_IMPORT" in text and "SW_GATE_WAL_S1W_HOST" in text and "wal-s1w-verdict" in text
    import yaml

    exp = yaml.safe_load((ROOT / "tests" / "acceptance" / "slice21_gate_expect.yaml").read_text(encoding="utf-8"))
    assert exp["wal_s1w"]["formal_params"] == FORMAL and exp["wal_s1w"]["relevant_paths"]
