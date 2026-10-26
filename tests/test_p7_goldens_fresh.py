"""P7 Task 1（08-slice23-login-events.md）：Slice 2.3 Python 基線 golden 必須與現行基線重新擷取的結果逐位元組相同。"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

import pytest

# repo 規則（tests/test_resource_markers.py）：每個 item 恰一個平台 marker 與資源 marker。
pytestmark = [pytest.mark.core_portable]

ROOT = pathlib.Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "contracts/golden/p7"
NAMES = ["env_file", "auth_resolution", "login_fsm_scenarios", "quiet_rx", "gate_responses", "reprobe_decisions",
         "reprobe_valid", "transport_stall", "self_test_responses", "recover_responses", "log_responses", "event_schema",
         "redos", "line_buffer", "event_payload", "event_log_tail", "event_engine_order", "registry_load", "counter_file"]


@pytest.mark.process_rpc
def test_goldens_exist_and_are_fresh():
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([sys.executable, str(ROOT / "scripts/rust/capture_p7_goldens.py"), "--out", td],
                           capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0, r.stderr
        for name in NAMES:
            fresh = (pathlib.Path(td) / f"{name}.ndjson").read_bytes()
            assert (GOLDEN / f"{name}.ndjson").read_bytes() == fresh, f"{name} 與現行 Python 基線不一致，請重新擷取並審閱差異"


@pytest.mark.memory_only
def test_login_fsm_golden_covers_every_platform_and_error():
    rows = [json.loads(l) for l in (GOLDEN / "login_fsm_scenarios.ndjson").read_text(encoding="utf-8").splitlines()]
    platforms = {r["case"]["profile"]["platform"] for r in rows}
    assert {"shell", "bcm", "prpl", "passthrough"} <= platforms
    errs = {r["result"][1] for r in rows}
    assert {None, "LOGIN_REQUIRED", "PROMPT_UNAVAILABLE", "LOGIN_PROMPT_TIMEOUT", "LOGIN_USER_REQUIRED", "PASS_ENV_REQUIRED",
            "PASS_ENV_MISSING", "BCM_PROMPT_TIMEOUT", "SHELL_PROMPT_TIMEOUT", "PRPL_PROMPT_TIMEOUT", "POST_LOGIN_CMD_TIMEOUT",
            "READY_NONCE_TIMEOUT", "READY_PROMPT_TIMEOUT", "RX_FLOOD", "CREDENTIALS_UNRESOLVED",
            "CONSOLE_UNLOCK_IDLE_TIMEOUT", "CONSOLE_UNLOCK_BOOTLOADER", "CONSOLE_UNLOCK_PROMPT_TIMEOUT"} <= errs
    assert "USER_ENV_MISSING" not in errs   # X3：不可達
    assert any(r.get("raised") == "error" for r in rows), "無效 regex 須照 Python 拋 re.error"


@pytest.mark.memory_only
def test_event_engine_order_golden_distinguishes_start_and_rule_set():
    rows = {r["id"]: r for r in map(json.loads, (GOLDEN / "event_engine_order.ndjson").read_text(encoding="utf-8").splitlines())}
    assert rows["event_engine_order:rule_set"]["types"][:2] == ["com_enabled", "rule_loaded"]   # engine.py:107-114
    assert rows["event_engine_order:start"]["types"][:2] == ["rule_loaded", "com_enabled"]


@pytest.mark.memory_only
@pytest.mark.parametrize("version", [(3, 11, 9), (3, 13, 0), (3, 14, 1)])
def test_capture_refuses_non_312_python(version, monkeypatch, capsys):
    """審查 L5-4：re 錯誤訊息／sre AST／int() 位數檢查順序依版本而異，golden 只能以 CPython 3.12 擷取（exit 2）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("capture_p7_goldens_under_test", ROOT / "scripts/rust/capture_p7_goldens.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_python_version", lambda: version[:2])
    assert mod.main(check=True) == 2
    assert "CPython 3.12" in capsys.readouterr().err
