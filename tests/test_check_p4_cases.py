"""`scripts/rust/check_p4_cases.py` 完成度 gate 的自測（P4-T1，審查修正回歸）。

每個測試以暫存映射表、lock 與證據目錄替換模組常數，不碰真實 `target/` 與 `tests/acceptance/`。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = REPO_ROOT / "scripts" / "rust" / "check_p4_cases.py"
HEAD = "a" * 40
OLD = "b" * 40


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_p4_cases_under_test", GATE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _case(case_id: str, **over) -> dict:
    base = {
        "case_id": case_id,
        "invariants": ["zero_tx_on_reject"],
        "layer": "L1",
        "platforms": ["linux"],
        "capabilities": [],
        "artifact": "rust-cli-bundle",
        "tests": [f"sw-cli::mgmt_remote::{case_id.replace('-', '_')}"],
        "evidence": f"target/p4-evidence/{case_id}.json",
        "necessity": "required",
    }
    base.update(over)
    return base


@pytest.fixture
def gate(tmp_path, monkeypatch):
    mod = _load_gate()
    acceptance = tmp_path / "acceptance"
    acceptance.mkdir()
    ev = tmp_path / "evidence"
    ev.mkdir()
    codes = tmp_path / "error_codes.yaml"
    codes.write_text("codes:\n- code: TIMEOUT\n", encoding="utf-8")
    artifact = tmp_path / "serialwrap"
    artifact.write_bytes(b"\x7fELF-test-binary")
    monkeypatch.setattr(mod, "CASE_MAP", acceptance / "p4_case_map.yaml")
    monkeypatch.setattr(mod, "LOCK", acceptance / "p4_required.lock.json")
    monkeypatch.setattr(mod, "ERROR_CODES", codes)
    monkeypatch.setattr(mod, "EVIDENCE_DIR", ev)
    # P4-T16 新增 L1 完整性規則；本檔只測案例證據邏輯，L1 規則由
    # tests/test_rust_bench_tools.py::test_check_l1_completeness_rules 以合成報告獨立覆蓋。
    monkeypatch.setattr(mod, "P4_L1_SETS", {})
    monkeypatch.setattr(mod, "head_commit", lambda: HEAD)
    mod.artifact = artifact  # type: ignore[attr-defined]
    mod.tmp = tmp_path  # type: ignore[attr-defined]
    return mod


def _write_map(mod, cases: list[dict]) -> None:
    mod.CASE_MAP.write_text(yaml.safe_dump({"schema_version": 1, "cases": cases}, sort_keys=False), encoding="utf-8")


def _pass_record(mod, case_id: str, platform: str = "linux", **over) -> dict:
    sha = hashlib.sha256(mod.artifact.read_bytes()).hexdigest()
    rec = {
        "case_id": case_id,
        "round": "rust",
        "platform": platform,
        "commit": HEAD,
        "artifact_path": str(mod.artifact),
        "artifact_sha256": sha,
        "result": "PASS",
    }
    rec.update(over)
    return rec


def _write_evidence(mod, case_id: str, records: list[dict]) -> None:
    (mod.EVIDENCE_DIR / f"{case_id}.json").write_text(json.dumps({"records": records}), encoding="utf-8")


def test_freeze_then_check_passes_with_valid_evidence(gate) -> None:
    _write_map(gate, [_case("sc-a")])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a")])
    assert gate.check() == 0


def test_freeze_refuses_to_overwrite_existing_lock(gate) -> None:
    _write_map(gate, [_case("sc-a")])
    assert gate.freeze(add_only=False) == 0
    assert gate.freeze(add_only=False) == 1


def test_rust_round_skip_is_rejected_even_when_registered(gate) -> None:
    _write_map(gate, [_case("sc-a", skip_registered={"python": "rust-only"})])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a", result="SKIP", reason="x", artifact_sha256=None)])
    assert gate.check() == 1


def _py_skip(case_id: str) -> dict:
    return {
        "case_id": case_id,
        "round": "python",
        "platform": "linux",
        "commit": HEAD,
        "artifact_sha256": None,
        "result": "SKIP",
        "reason": "rust-only",
    }


def test_python_skip_is_accepted_when_registered(gate) -> None:
    _write_map(gate, [_case("sc-a", skip_registered={"python": "rust-only"})])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_py_skip("sc-a"), _pass_record(gate, "sc-a")])
    assert gate.check() == 0


def test_python_skip_is_rejected_when_not_registered(gate) -> None:
    _write_map(gate, [_case("sc-b")])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-b", [_py_skip("sc-b"), _pass_record(gate, "sc-b")])
    assert gate.check() == 1


def test_stale_commit_does_not_satisfy_required_pass(gate) -> None:
    _write_map(gate, [_case("sc-a")])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a", commit=OLD)])
    assert gate.check() == 1


def test_pass_requires_matching_artifact_digest(gate) -> None:
    _write_map(gate, [_case("sc-a")])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a", artifact_sha256="0" * 64)])
    assert gate.check() == 1


def test_pass_record_for_every_platform_is_required(gate) -> None:
    _write_map(gate, [_case("sc-a", platforms=["linux", "windows"])])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a", platform="linux")])
    assert gate.check() == 1


def test_unfrozen_required_case_fails_check(gate) -> None:
    _write_map(gate, [_case("sc-a")])
    assert gate.freeze(add_only=False) == 0
    _write_map(gate, [_case("sc-a"), _case("sc-new")])
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a")])
    _write_evidence(gate, "sc-new", [_pass_record(gate, "sc-new")])
    assert gate.check() == 1
    assert gate.freeze(add_only=True) == 0
    assert gate.check() == 0


def test_add_only_rejects_removed_or_downgraded_case(gate) -> None:
    _write_map(gate, [_case("sc-a"), _case("sc-b")])
    assert gate.freeze(add_only=False) == 0
    _write_map(gate, [_case("sc-a")])
    assert gate.freeze(add_only=True) == 1
    _write_map(gate, [_case("sc-a"), _case("sc-b", necessity="optional")])
    assert gate.freeze(add_only=True) == 1


def test_placeholder_only_case_is_not_evidence(gate) -> None:
    _write_map(gate, [_case("sc-a", tests=["sw-cli::p4_case_placeholders::sc_a"])])
    assert gate.freeze(add_only=False) == 0
    _write_evidence(gate, "sc-a", [_pass_record(gate, "sc-a")])
    assert gate.check() == 1
