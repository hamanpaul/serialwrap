"""P7 Task 0（08-slice23-login-events.md）：Slice 2.3 骨架存在性、cfg 邊界、SC-30／DF-18 登記與案例凍結。"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest
import yaml

# repo 規則（tests/test_resource_markers.py）：每個 item 恰一個平台 marker 與資源 marker。
pytestmark = [pytest.mark.core_portable]

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.process_rpc
@pytest.mark.skipif(not (ROOT / "crates").is_dir(), reason="crates/ 尚未建立")
def test_p7_files_exist_and_cfg_boundary_clean():
    must = ["crates/sw-core/src/login_run.rs", "crates/sw-core/src/reprobe.rs", "crates/sw-core/src/recover.rs",
            "crates/sw-core/src/transport_stall.rs", "crates/sw-proto/src/pyre/redos.rs", "crates/sw-core/src/event_engine/dispatcher.rs"]
    for rel in must:
        assert (ROOT / rel).is_file(), rel
    r = subprocess.run([sys.executable, str(ROOT / "scripts/rust/check_cfg_boundary.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.memory_only
def test_behavior_changes_registers_sc30_df18_per_p0_schema():
    data = yaml.safe_load((ROOT / "contracts/behavior_changes.yaml").read_text(encoding="utf-8"))
    rows = {e["id"]: e for e in data["changes"]}
    assert rows["SC-30"]["scope"] == "rust_only" and rows["SC-30"]["applies_to"]["python"] is None
    assert rows["SC-30"]["applies_to"]["rust"] == {"stage": "slice-2.3"}
    assert rows["DF-18"]["scope"] == "both" and rows["DF-18"]["case_ids"] == ["df18-event-replace-overwrite"]
    assert "SC-28" not in rows or "SERIALWRAP_CONSOLE_BACKEND" in rows["SC-28"]["title"] + rows["SC-28"].get("semantics", ""), "SC-28 屬 P6"


@pytest.mark.process_rpc
def test_slice23_cases_frozen_before_implementation():
    lock = json.loads((ROOT / "tests/acceptance/cases.lock.json").read_text(encoding="utf-8"))
    s23 = set(lock["sets"]["slice_2.3"])
    for cid in ["sc30-quiet-mid-login-aborts", "df18-event-replace-overwrite", "p7-login-unresolved-credentials-terminal-zero-empty-secret-tx",
                "brcm-01-login-flow", "ev-01-spawn-handler", "f10-unresolved-creds-terminal", "p1-rst-recover"]:
        assert cid in s23, cid
    r = subprocess.run([sys.executable, str(ROOT / "tests/acceptance/regen_lock.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.memory_only
def test_p7_py_parity_set_registered_add_only():
    """parity-amendments-2026-10-26 #111／#112（08 Task 0 新增 Step）：Python 基線 parity 測試入 `p7_gate:py_parity`。

    Windows 平台需以 Windows 原生收集結果登記（§2.1），由 Windows runner 補；此處只要求 linux 已登記。
    """
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    spec = lock["sets"]["p7_gate:py_parity"]["linux"]
    ids = set(spec["nodeids"])
    required = [
        "tests/test_console_unlock.py::test_echoed_nonce_command_is_not_ready_evidence",
        "tests/test_console_unlock.py::test_cfe_prompt_blocks_all_tx",
        "tests/test_console_unlock.py::test_existing_shell_prompt_skips_unlock",
        "tests/test_console_unlock.py::test_unlock_timing_log_contains_only_safe_metadata",
        "tests/test_event_dispatcher.py::TestDispatcher::test_per_rule_concurrency_drops_oldest",
        "tests/test_event_dispatcher.py::TestDispatcher::test_timeout_kills_handler",
        "tests/test_event_engine.py::TestEventEngine::test_disable_clears_counter",
    ]
    for nid in required:
        assert nid in ids, nid
    behaviour = {n for n in ids if n.startswith("tests/test_console_unlock.py::")}
    assert len({n.split("[")[0] for n in behaviour}) >= 17, "test_console_unlock.py 行為測試應全數入鎖"
