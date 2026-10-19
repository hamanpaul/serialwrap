"""Stage 0 實機回歸 case、程序 runbook 與 Spike 3 腳本契約測試（TDD 紅燈）。

依據：`docs/rust-refactor/plans/02-stage0-python-bc.md` Task T21（規格 §6.2、§11.6、§13、§9.1 BC-05；CLAUDE.md #155）。
驗證：
1. regression 新增三個 case（f10-release-drain-receipt、f10-release-aborts-inflight-command、f5-deferred-dropped-after-reboot）註冊與屬性；
2. f10 release receipt oracle 判定邏輯；
3. f10 inflight abort 重送偵測 oracle；
4. f5 reboot 跨開機按鍵不回放 oracle；
5. 三份 runbook 與三支腳本固定首段欄位；
6. Spike 3 四個 case_id 與 P2 一致；
7. Spike 3 證據 JSON schema 拒絕缺 edr；
8. 非隔離實例忽略 SERIALWRAP_TEST_KILL_AFTER_SAVE 並示警；
9. 子行程（見 tests/test_stage0_kill_hook_subprocess.py）：隔離實例建構子不觸發鉤子、release 持久化後以 137 終止，
   情境 (b) commit 前暫停點 arrived 後 kill -9 不留 RELEASED。
"""
from __future__ import annotations

import ast
import base64
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "regression"))


def test_new_cases_registered_with_family_and_flags():
    """驗證三個新實機回歸 case（BC-03、BC-02、BC-04／SC-04）已註冊，且 family 與 flags 正確。"""
    from serialwrap_regression import core

    registry = core.load_registry()
    by_id = {c.id: c for c in registry}

    for case_id in (
        "f10-release-drain-receipt",
        "f10-release-aborts-inflight-command",
        "f5-deferred-dropped-after-reboot",
    ):
        assert case_id in by_id, f"Case {case_id} not found in regression registry"

    c_receipt = by_id["f10-release-drain-receipt"]
    assert c_receipt.family == "F10"
    assert c_receipt.destructive is True

    c_abort = by_id["f10-release-aborts-inflight-command"]
    assert c_abort.family == "F10"
    assert c_abort.destructive is True

    c_f5 = by_id["f5-deferred-dropped-after-reboot"]
    assert c_f5.family == "F5"
    f5_existing = [c for c in registry if c.family == "F5" and c.id != "f5-deferred-dropped-after-reboot"]
    assert f5_existing, "No existing F5 cases found in registry"
    expected_requires = f5_existing[0].requires
    assert all(c.requires == expected_requires for c in f5_existing)
    assert c_f5.requires == expected_requires


def test_f10_release_receipt_oracle_with_fake_ctx():
    """替身 ctx：release 回應時間早於外部 open 成功 → FAIL（category 'test'）；晚於 → PASS。"""
    f10_file = REPO_ROOT / "regression" / "serialwrap_regression" / "cases" / "f10_device_handoff.py"
    assert f10_file.is_file(), f"Expected case file missing: {f10_file}"

    import importlib
    f10 = importlib.import_module("serialwrap_regression.cases.f10_device_handoff")

    oracle = getattr(f10, "evaluate_release_receipt", getattr(f10, "f10_release_drain_receipt", None))
    assert oracle is not None, "f10_device_handoff must provide evaluate_release_receipt or f10_release_drain_receipt"

    # 情境 1：release 回應時間 (10.0) 早於外部 open 成功 (10.5) → 提早回應，FAIL（category 'test'）
    ctx_early = SimpleNamespace(
        release_time=10.0,
        open_success_time=10.5,
        release_ts=10.0,
        open_ts=10.5,
    )
    try:
        res_early = oracle(ctx_early)
    except TypeError:
        res_early = oracle(10.0, 10.5)
    assert res_early.verdict == "FAIL"
    assert res_early.category == "test"

    # 情境 2：release 回應時間 (11.0) 晚於外部 open 成功 (10.5) → handle 已關閉才回應，PASS
    ctx_late = SimpleNamespace(
        release_time=11.0,
        open_success_time=10.5,
        release_ts=11.0,
        open_ts=10.5,
    )
    try:
        res_late = oracle(ctx_late)
    except TypeError:
        res_late = oracle(11.0, 10.5)
    assert res_late.verdict == "PASS"


def test_f10_inflight_abort_oracle_detects_resend():
    """替身 WAL 含同一 cmd_id 兩筆相同 TX payload → FAIL resend_detected。"""
    f10_file = REPO_ROOT / "regression" / "serialwrap_regression" / "cases" / "f10_device_handoff.py"
    assert f10_file.is_file(), f"Expected case file missing: {f10_file}"

    import importlib
    f10 = importlib.import_module("serialwrap_regression.cases.f10_device_handoff")

    oracle = getattr(f10, "evaluate_inflight_abort", getattr(f10, "f10_release_aborts_inflight_command", None))
    assert oracle is not None, "f10_device_handoff must provide evaluate_inflight_abort or f10_release_aborts_inflight_command"

    payload_b64 = base64.b64encode(b"sleep 20; echo X\n").decode("ascii")
    wal_with_resend = [
        {"seq": 101, "dir": "TX", "cmd_id": "cmd-test-1", "payload_b64": payload_b64},
        {"seq": 102, "dir": "TX", "cmd_id": "cmd-test-1", "payload_b64": payload_b64},
    ]
    ctx_resend = SimpleNamespace(
        cmd_id="cmd-test-1",
        wal_records=wal_with_resend,
        records=wal_with_resend,
    )
    try:
        res = oracle(ctx_resend)
    except TypeError:
        res = oracle(wal_with_resend, "cmd-test-1")

    assert res.verdict == "FAIL"
    assert res.reason_code == "resend_detected" or "resend_detected" in str(res.reason)


def test_f5_stale_replay_oracle():
    """替身 console 擷取含 STALE → FAIL；不含且對照組含 → PASS；對照組也不含 → INSUFFICIENT_EVIDENCE（不得 PASS）。"""
    f5_file = REPO_ROOT / "regression" / "serialwrap_regression" / "cases" / "f05_console_coexist.py"
    assert f5_file.is_file(), f"Expected case file missing: {f5_file}"

    from serialwrap_regression.cases import f05_console_coexist as f5

    oracle = getattr(f5, "evaluate_stale_replay", getattr(f5, "f5_deferred_dropped_after_reboot", None))
    assert oracle is not None, "f05_console_coexist must provide evaluate_stale_replay or f5_deferred_dropped_after_reboot"

    def _invoke(test_cap: str, ctrl_cap: str):
        ctx = SimpleNamespace(
            test_capture=test_cap,
            control_capture=ctrl_cap,
            target_output=test_cap,
            control_output=ctrl_cap,
        )
        try:
            return oracle(ctx)
        except TypeError:
            return oracle(test_cap, ctrl_cap)

    # 1. 替身 console 擷取含 STALE → FAIL
    res_stale = _invoke("root@dut:~# echo STALE\nSTALE", "root@dut:~# echo STALE\nSTALE")
    assert res_stale.verdict == "FAIL"

    # 2. 不含且對照組含 → PASS
    res_pass = _invoke("root@dut:~# \n", "root@dut:~# echo STALE\nSTALE")
    assert res_pass.verdict == "PASS"

    # 3. 對照組也不含 → INSUFFICIENT_EVIDENCE（不得 PASS）
    res_insuf = _invoke("root@dut:~# \n", "root@dut:~# \n")
    assert res_insuf.verdict != "PASS"
    assert res_insuf.verdict in ("INSUFFICIENT_EVIDENCE", "SKIP")
    if res_insuf.verdict == "SKIP":
        assert res_insuf.reason_code in ("insufficient_evidence", "control_keystroke_missing")


def test_runbooks_have_frozen_header_fields():
    """三份 realhw/procedures/*.md 與三支腳本 docstring 皆含 entry、revision、expected、owner、evidence_dir、rollback。"""
    required_fields = ("entry", "revision", "expected", "owner", "evidence_dir", "rollback")

    procedures = [
        REPO_ROOT / "realhw" / "procedures" / "stage0-release-kill.md",
        REPO_ROOT / "realhw" / "procedures" / "stage0-windows-remote-topologies.md",
        REPO_ROOT / "realhw" / "procedures" / "stage0-spike1-win-vcom.md",
    ]

    for proc in procedures:
        assert proc.is_file(), f"Procedure runbook missing: {proc}"
        content = proc.read_text(encoding="utf-8")
        for field in required_fields:
            assert field in content, f"Procedure {proc.name} missing required header field '{field}'"

    scripts = [
        REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py",
        REPO_ROOT / "scripts" / "stage0" / "drain_timing.py",
        REPO_ROOT / "regression" / "serialwrap_regression" / "cases" / "f10_device_handoff.py",
    ]

    for script in scripts:
        assert script.is_file(), f"Script missing: {script}"
        tree = ast.parse(script.read_text(encoding="utf-8"))
        doc = ast.get_docstring(tree) or ""
        for field in required_fields:
            assert field in doc, f"Script {script.name} docstring missing required header field '{field}'"


def test_spike3_cases_match_p2_case_ids(capsys, monkeypatch):
    """spike3_win_wal.py --list 輸出恰為 P2 指派給本計畫的四個 case_id。"""
    script_path = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    assert script_path.is_file(), f"Spike 3 script missing: {script_path}"

    expected_ids = {
        "win-wal-av-rotate",
        "win-wal-av-unlink",
        "win-wal-av-reader-handle",
        "win-wal-f12-progress",
    }

    import runpy
    monkeypatch.setattr(sys, "argv", [str(script_path), "--list"])
    try:
        runpy.run_path(str(script_path), run_name="__main__")
    except SystemExit as exc:
        assert exc.code in (0, None), f"spike3_win_wal.py --list exited with {exc.code}"

    captured = capsys.readouterr().out
    try:
        cases = json.loads(captured)
        if isinstance(cases, list):
            found_ids = set(cases)
        elif isinstance(cases, dict):
            found_ids = set(cases.keys())
        else:
            found_ids = set(captured.split())
    except json.JSONDecodeError:
        found_ids = set(captured.split())

    assert found_ids == expected_ids


def test_spike3_evidence_schema_rejects_missing_edr():
    """缺 edr 的證據 JSON 不通過 schema。"""
    schema_path = REPO_ROOT / "scripts" / "stage0" / "spike3_evidence.schema.json"
    assert schema_path.is_file(), f"Spike 3 evidence schema missing: {schema_path}"

    import jsonschema

    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    sample_without_edr = {
        "case_id": "win-wal-av-rotate",
        "run_at": "2026-10-16T12:00:00Z",
        "env": {"os": "windows"},
        "artifact": {"binary": "sw-core.exe"},
        "steps": [{"op": "rotate", "ts": "2026-10-16T12:00:01Z", "result": "ok"}],
        "observations": {"rotated": True},
        "verdict": "PASS",
        "fallback": "none",
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=sample_without_edr, schema=schema)

    sample_with_edr = dict(
        sample_without_edr,
        edr={"product": "Defender", "status": "active", "interceptions": "unknown"},
    )
    jsonschema.validate(instance=sample_with_edr, schema=schema)


def test_kill_hook_ignored_on_non_isolated_instance(tmp_path, monkeypatch, caplog):
    """非隔離實例設 SERIALWRAP_TEST_KILL_AFTER_SAVE=1 → _save_state 後不 kill、log WARNING。"""
    from stage0_fakes import make_manager, make_profile

    monkeypatch.setenv("SERIALWRAP_TEST_KILL_AFTER_SAVE", "1")
    monkeypatch.delenv("SERIALWRAP_STATE_DIR", raising=False)
    monkeypatch.delenv("SERIALWRAP_RUN_DIR", raising=False)
    monkeypatch.delenv("SERIALWRAP_CONFIG_DIR", raising=False)

    def _fail_kill(*_a, **_kw):
        raise AssertionError("Non-isolated instance should not kill process")

    monkeypatch.setattr(os, "_exit", _fail_kill)
    monkeypatch.setattr(os, "kill", _fail_kill)
    monkeypatch.setattr(sys, "exit", _fail_kill)

    mgr = make_manager(tmp_path, [make_profile()])

    with caplog.at_level(logging.WARNING):
        mgr._save_state()

    assert any(
        record.levelno >= logging.WARNING
        and ("SERIALWRAP_TEST_KILL_AFTER_SAVE" in record.message or "kill" in record.message.lower())
        for record in caplog.records
    ), "Expected WARNING logged when SERIALWRAP_TEST_KILL_AFTER_SAVE set on non-isolated instance"

