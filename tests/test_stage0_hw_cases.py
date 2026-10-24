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
RESOURCE_AUDIT_WAIVERS = {
    "interactive_desktop": "只驗證案例 capability 字串，未啟動 tmux 或桌面資源",
}

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
    ported = {
        "f5-raw-ownership-survives-agent-rounds",
        "f5-deferred-bytes-flushed",
        "f5-console-peer-gone-recycled",
        "f5-second-console-linebuffer",
    }
    for case in f5_existing:
        if case.id == "f5-console-peer-gone-recycled":
            assert {"human_frontend", "posix_orphan_semantics"} <= set(case.requires)
        elif case.id in ported:
            assert case.requires == ("human_frontend",)
        else:
            assert case.requires == ("tmux",)
    assert {"tmux", "posix_throwaway_daemon"} <= set(c_f5.requires)


@pytest.mark.parametrize("case_name", ["p1_con_liveness", "p1_con_orphan"])
def test_realhw_orphan_cases_refuse_vacuous_frontend(monkeypatch, case_name):
    from realhw.cases import p1_console

    monkeypatch.setattr(p1_console.time, "sleep", lambda _seconds: None)

    class Human:
        def __init__(self):
            self.crashed = False

        def open(self, _com):
            return None

        def alive(self):
            return False

        def crash(self):
            self.crashed = True
            raise AssertionError("未 attach 的 frontend 不應被當成孤兒測試輸入")

        def close(self):
            return None

    human = Human()
    notes = []

    class Sw:
        def run(self, *args):
            if args[:2] == ("session", "console-list"):
                return {"consoles": []}
            return {"human_attached": False}

    ctx = SimpleNamespace(
        human=lambda *_args: human,
        sw=Sw(),
        note=lambda *args: notes.append(args),
    )
    result = getattr(p1_console, case_name)(ctx)
    assert result.verdict == "FAIL"
    assert result.category == "environment"
    assert result.reason_code == "minicom_spawn_failed"
    assert not human.crashed


def test_regression_orphan_case_refuses_empty_console_list(monkeypatch):
    from serialwrap_regression.cases import f05_console_coexist

    monkeypatch.setattr(f05_console_coexist.time, "sleep", lambda _seconds: None)

    class Human:
        def open(self, _com):
            return None

        def alive(self):
            return True

        def crash(self):
            raise AssertionError("沒有 console attach，不應呼叫 crash")

        def close(self):
            return None

    class Sw:
        def run(self, *_args):
            return {"consoles": []}

    ctx = SimpleNamespace(
        cfg={"boards": [{"com": "COM0"}]},
        human=lambda *_args: Human(),
        sw=Sw(),
        note=lambda *_args: None,
    )
    result = f05_console_coexist.f5_console_peer_gone_recycled(ctx)
    assert result.verdict == "FAIL"
    assert result.category == "environment"
    assert result.reason_code == "minicom_spawn_failed"


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


def _load_spike3_module():
    """以檔案路徑載入 spike3_win_wal.py（頂層只定義函式與常數，不執行 Windows 探測）。"""
    import importlib.util

    path = REPO_ROOT / "scripts" / "stage0" / "spike3_win_wal.py"
    spec = importlib.util.spec_from_file_location("spike3_win_wal_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_run(stdout: str = "", returncode: int = 0):
    import subprocess

    def runner(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")

    return runner


def test_spike3_query_edr_reports_product_and_status_as_info_only():
    """OD-19 (3)：唯讀查詢解析防毒名稱與狀態；成功即寫入資訊欄，失敗一律 unknown。"""
    mod = _load_spike3_module()
    rows = json.dumps([
        {"displayName": "Vendor EDR", "productState": 397568},
        {"displayName": "Vendor EDR", "productState": 397568},
    ])
    info = mod.query_edr(runner=_fake_run(rows))
    assert info["product"] == "Vendor EDR" and info["status"] == "enabled"
    assert info["interceptions"] == "unknown"

    single = mod.query_edr(runner=_fake_run(json.dumps({"displayName": "Other AV", "productState": 262144})))
    assert single["product"] == "Other AV" and single["status"] == "disabled"

    for failing in (_fake_run("", returncode=1), _fake_run("not json"), _fake_run("[]")):
        degraded = mod.query_edr(runner=failing)
        assert degraded["product"] == "unknown" and degraded["status"] == "unknown"

    def missing_powershell(*_args, **_kwargs):
        raise FileNotFoundError("powershell.exe")

    assert mod.query_edr(runner=missing_powershell)["product"] == "unknown"


def test_spike3_decode_product_state_is_info_only_mapping():
    mod = _load_spike3_module()
    assert mod.decode_product_state(397568) == "enabled"      # 0x61100，第二位元組 0x11
    assert mod.decode_product_state(262144) == "disabled"     # 0x40000，第二位元組 0x00
    assert mod.decode_product_state(0xFF00) == "unknown"      # 第二位元組 0xFF，不在已知版面內
    assert mod.decode_product_state("bad") == "unknown"


def test_spike3_binary_integrity_report_detects_change_and_refusal():
    mod = _load_spike3_module()
    before = {"x.exe": {"exists": True, "sha256": "a" * 64}}
    same = mod.binary_integrity_report(before, {"x.exe": {"exists": True, "sha256": "a" * 64}}, "none")
    assert same["unchanged"] is True and same["launch_refused"] is False

    changed = mod.binary_integrity_report(before, {"x.exe": {"exists": True, "sha256": "b" * 64}}, "none")
    assert changed["unchanged"] is False

    removed = mod.binary_integrity_report(before, {"x.exe": {"exists": False, "sha256": None}}, "none")
    assert removed["unchanged"] is False

    refused = mod.binary_integrity_report(before, {"x.exe": {"exists": True, "sha256": "a" * 64}}, "helper 就緒逾時")
    assert refused["launch_refused"] is True and refused["unchanged"] is True


def test_spike3_cli_takes_no_edr_flags():
    """OD-19：--edr-* 自填參數已移除，argparse 必須拒絕。"""
    mod = _load_spike3_module()
    parser = mod.build_parser()
    for flag in ("--edr-product", "--edr-status", "--edr-interceptions"):
        with pytest.raises(SystemExit):
            parser.parse_args([flag, "x", "--case", "win-wal-av-rotate", "--evidence-dir", "d"])


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
    """OD-19（oracle 變更，見 commit 的兩位審查者註記）：av_sim 與 binary_integrity 必填；edr 為選填資訊欄。

    注意：函式名稱沿用舊名 ``test_spike3_evidence_schema_rejects_missing_edr``，因為 L1 凍結鎖
    （``tests/acceptance/l1_required.lock.json``，``s0_win_pytest.windows``）以此 nodeid 登記；
    改名會使凍結必要集合出現懸空 nodeid。函式本體的斷言已依 OD-19 改為「edr 缺漏可接受」。
    改名須依 §11.3 以 ``regen_lock --l1-add-only --allow-removal`` 與擁有者核准一併處理。
    """
    schema_path = REPO_ROOT / "scripts" / "stage0" / "spike3_evidence.schema.json"
    assert schema_path.is_file(), f"Spike 3 evidence schema missing: {schema_path}"

    import jsonschema

    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    base = {
        "case_id": "win-wal-av-rotate",
        "run_at": "2026-10-16T12:00:00Z",
        "env": {"os": "windows"},
        "artifact": {"binary": "sw-core.exe"},
        "steps": [{"op": "rotate", "ts": "2026-10-16T12:00:01Z", "result": "ok"}],
        "observations": {"append_count": 1, "append_latency_ms": [0.1], "rotated": True},
        "av_sim": {"mode": "share_no_delete", "share_mode": "FILE_SHARE_READ|FILE_SHARE_WRITE",
                   "hold_ms_range": [10, 500], "seed": 1, "oplock": "unavailable",
                   "launch_error": "none", "grabs": 1, "helper_exit": 0},
        "binary_integrity": {
            "subjects": [{"path": "sw-core.exe", "exists_before": True, "exists_after": True,
                          "sha256_before": "c" * 64, "sha256_after": "c" * 64, "unchanged": True}],
            "unchanged": True, "launch_refused": False, "launch_error": "none",
        },
        "verdict": "PASS",
        "fallback": "none",
    }

    jsonschema.validate(instance=base, schema=schema)  # edr 缺漏不再不合格
    jsonschema.validate(
        instance=dict(base, edr={"product": "unknown", "status": "unknown", "interceptions": "unknown"}),
        schema=schema,
    )
    without_av = {k: v for k, v in base.items() if k != "av_sim"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=without_av, schema=schema)
    without_integrity = {k: v for k, v in base.items() if k != "binary_integrity"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=without_integrity, schema=schema)


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

