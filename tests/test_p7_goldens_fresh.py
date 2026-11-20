"""P7 Task 1（08-slice23-login-events.md）：Slice 2.3 Python 基線 golden 必須與現行基線重新擷取的結果逐位元組相同。"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
from typing import Any

import pytest

# repo 規則（tests/test_resource_markers.py）：每個 item 恰一個平台 marker 與資源 marker。
# 平台 marker 逐支標：新鮮度測試重現的是 Linux Python 基線（Python 在 Windows 走 TCP console 端點等平台分支，
# 擷取結果本來就與 Linux golden 不同），只在 POSIX 有意義（編排者裁決 2026-11-20，PR #278 hosted CI）；其餘為可攜的純記憶體測試。

ROOT = pathlib.Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "contracts/golden/p7"
NAMES = ["env_file", "auth_resolution", "login_fsm_scenarios", "quiet_rx", "gate_responses", "reprobe_decisions",
         "reprobe_valid", "transport_stall", "self_test_responses", "recover_responses", "log_responses", "event_schema",
         "redos", "line_buffer", "event_payload", "event_log_tail", "event_engine_order", "registry_load", "counter_file"]


def _changed_paths(expected: Any, actual: Any, prefix: str = "$") -> list[str]:
    if type(expected) is not type(actual):
        return [prefix]
    if isinstance(expected, dict):
        changed = []
        for key in sorted(set(expected) | set(actual)):
            child = f"{prefix}.{key}"
            if key not in expected or key not in actual:
                changed.append(child)
            else:
                changed.extend(_changed_paths(expected[key], actual[key], child))
        return changed
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [f"{prefix}.length"]
        changed = []
        for index, (left, right) in enumerate(zip(expected, actual)):
            changed.extend(_changed_paths(left, right, f"{prefix}[{index}]"))
        return changed
    return [] if expected == actual else [prefix]


def _golden_differences(name: str, expected: bytes, actual: bytes) -> list[str]:
    if expected == actual:
        return []
    try:
        expected_rows = [json.loads(line) for line in expected.decode("utf-8").splitlines()]
        actual_rows = [json.loads(line) for line in actual.decode("utf-8").splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [f"{name}: NDJSON bytes differ"]

    differences = []
    if len(expected_rows) != len(actual_rows):
        differences.append(f"{name}: row count {len(expected_rows)} != {len(actual_rows)}")
    for index in range(max(len(expected_rows), len(actual_rows))):
        if index >= len(expected_rows) or index >= len(actual_rows):
            row = expected_rows[index] if index < len(expected_rows) else actual_rows[index]
            differences.append(f"{name}:{row.get('id', index + 1)}: row missing")
            continue
        expected_row, actual_row = expected_rows[index], actual_rows[index]
        changed = _changed_paths(expected_row, actual_row)
        if changed:
            row_id = actual_row.get("id", expected_row.get("id", index + 1))
            differences.append(f"{name}:{row_id}: {', '.join(changed)}")
    return differences or [f"{name}: NDJSON bytes differ although parsed rows match"]


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_goldens_exist_and_are_fresh():
    # 擷取前置（capture_p7_goldens.py 第 12 行）：EVT-D-S15 golden 以「無 google-re2」的 Python 為基準，偵測到 re2 即拒絕擷取。
    # tests.yml 的第二輪刻意安裝 re2 覆蓋 #91 線性引擎路徑；該輪不具備此前置，新鮮度由無 re2 的第一輪覆蓋。
    try:
        import re2  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("cap_present:google-re2：EVT-D-S15 golden 以無 re2 的 Python 為基準，由無 re2 的測試輪覆蓋（非 PASS）")
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([sys.executable, str(ROOT / "scripts/rust/capture_p7_goldens.py"), "--out", td],
                           capture_output=True, text=True, cwd=ROOT)
        assert r.returncode == 0, r.stderr
        capture_differences = []
        for name in NAMES:
            fresh = (pathlib.Path(td) / f"{name}.ndjson").read_bytes()
            capture_differences.extend(_golden_differences(
                name, (GOLDEN / f"{name}.ndjson").read_bytes(), fresh))
        assert not capture_differences, "擷取結果與 Linux golden 不一致：\n" + "\n".join(capture_differences)

    # 以 ntpath 模擬 Windows 的路徑與家目錄規則，逐一比較全部 golden generator。
    import importlib
    import ntpath
    from types import SimpleNamespace
    from unittest import mock

    import sw_core.config as config
    from sw_core import auth as auth_mod

    rust_scripts = str(ROOT / "scripts/rust")
    if rust_scripts not in sys.path:
        sys.path.insert(0, rust_scripts)
    capture = importlib.import_module("capture_p7_goldens")
    p7_sm_goldens = importlib.import_module("p7_sm_goldens")
    windows_differences = []
    if set(NAMES) != set(capture.NAMES):
        windows_differences.append("測試情境清單與 capture_p7_goldens.NAMES 不一致")
    normalize_temp_paths = getattr(capture, "normalize_temporary_paths", None)
    windows_temp_root = ntpath.join(r"C:\Users\fixture\AppData\Local\Temp", "p7-golden")
    real_open = open

    def windows_open(path, *args, **kwargs):
        try:
            path_text = os.fspath(path)
        except TypeError:
            return real_open(path, *args, **kwargs)
        if path_text.endswith(".log") and os.path.isdir(path_text):
            raise PermissionError(13, "Permission denied", path_text)
        return real_open(path, *args, **kwargs)

    def simulate_windows_temp_paths(value, temp_dir):
        def as_windows(item):
            if isinstance(item, str):
                if temp_dir not in item:
                    return item
                prefix, suffix = item.split(temp_dir, 1)
                return prefix + windows_temp_root + suffix.replace("/", "\\")
            if isinstance(item, dict):
                return {key: as_windows(child) for key, child in item.items()}
            if isinstance(item, list):
                return [as_windows(child) for child in item]
            if isinstance(item, tuple):
                return tuple(as_windows(child) for child in item)
            return item

        return normalize_temp_paths(as_windows(value), windows_temp_root)

    with mock.patch.object(config, "os", SimpleNamespace(path=ntpath)), \
         mock.patch.object(auth_mod.os.path, "expanduser", ntpath.expanduser):
        if normalize_temp_paths is None:
            windows_differences.append("log_responses: temporary Windows path normalizer is missing")
            for name in NAMES:
                windows_fresh = capture.render(name)
                windows_differences.extend(_golden_differences(
                    name, (GOLDEN / f"{name}.ndjson").read_bytes(), windows_fresh))
        else:
            with mock.patch("builtins.open", side_effect=windows_open), \
                 mock.patch.object(capture, "normalize_temporary_paths", simulate_windows_temp_paths), \
                 mock.patch.object(p7_sm_goldens, "normalize_temporary_paths", simulate_windows_temp_paths):
                for name in NAMES:
                    windows_fresh = capture.render(name)
                    windows_differences.extend(_golden_differences(
                        name, (GOLDEN / f"{name}.ndjson").read_bytes(), windows_fresh))

    # Windows tempfile 會以反斜線分隔；以合成 temp root 驗證 capture 的共用正規化器。
    windows_log_path = ntpath.join(windows_temp_root, "blog", "COM0_261026-000000.log")
    if normalize_temp_paths is not None:
        normalized = normalize_temp_paths({"log_path": windows_log_path}, windows_temp_root)
        expected_path = {"log_path": "/golden/blog/COM0_261026-000000.log"}
        if normalized != expected_path:
            windows_differences.append(f"log_responses: Windows temp path {normalized!r} != {expected_path!r}")
    assert not windows_differences, "Windows ntpath 模擬與 Linux golden 不一致：\n" + "\n".join(windows_differences)


@pytest.mark.core_portable
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


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_event_engine_order_golden_distinguishes_start_and_rule_set():
    rows = {r["id"]: r for r in map(json.loads, (GOLDEN / "event_engine_order.ndjson").read_text(encoding="utf-8").splitlines())}
    assert rows["event_engine_order:rule_set"]["types"][:2] == ["com_enabled", "rule_loaded"]   # engine.py:107-114
    assert rows["event_engine_order:start"]["types"][:2] == ["rule_loaded", "com_enabled"]


@pytest.mark.core_portable
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
