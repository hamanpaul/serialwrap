"""#155 preflight 版本 gate 純函式單測（#154 防線）。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "regression"))

from serialwrap_regression import core, preflight  # noqa: E402
import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def test_version_gate_pass_on_match():
    assert preflight.version_gate("serialwrap 0.2.4", "0.2.4") is None


def test_version_gate_refuses_on_mismatch():
    got = preflight.version_gate("serialwrap 0.2.4", "0.2.3")
    assert got is not None and "0.2.4" in got and "0.2.3" in got and "#154" in got


def test_version_gate_refuses_on_unparsable():
    assert preflight.version_gate("", "0.2.4") is not None
    assert preflight.version_gate("serialwrap 0.2.4", "") is not None


def test_stale_client_note():
    assert preflight.stale_client_note("serialwrap 0.2.4", "serialwrap 0.2.4") is None
    note = preflight.stale_client_note("serialwrap 0.2.1", "serialwrap 0.2.4")
    assert note is not None and "0.2.1" in note and "#154" in note
    assert preflight.stale_client_note("", "serialwrap 0.2.4") is None  # 解析不出→不加註


class _StubSw:
    """daemon_version_probe() 的最小 sw 替身：只需 `.run("daemon", "status")`。"""

    def __init__(self, status: dict) -> None:
        self._status = status

    def run(self, *_args):
        return self._status


def test_daemon_version_probe_fast_path_uses_status_version_field():
    """#154 快路徑：daemon status 已帶 version 欄位時直接採用，不繞 /proc。"""
    sw = _StubSw({"ok": True, "pid": 99999, "version": "0.2.4"})
    assert preflight.daemon_version_probe(sw) == "0.2.4"


def test_daemon_version_probe_falls_back_when_version_field_missing():
    """向下相容：舊 daemon（version 欄位加入前部署）缺席時落回 pid 缺失早退（無 /proc 可讀）。"""
    sw = _StubSw({"ok": True})  # 無 pid、無 version
    assert preflight.daemon_version_probe(sw) == ""


def test_daemon_version_probe_ignores_blank_version_field():
    """version 欄位存在但為空字串／全空白 → 視為缺席，落回 fallback（pid 缺失時回空字串）。"""
    sw = _StubSw({"ok": True, "version": "   "})
    assert preflight.daemon_version_probe(sw) == ""


def test_load_testbed_honors_stage3_path_override(tmp_path, monkeypatch):
    override = tmp_path / "stage3-testbed.yaml"
    override.write_text("serialwrap_exe: /stage3/bin/serialwrap\nallow_destructive: true\n", encoding="utf-8")
    monkeypatch.setenv("SERIALWRAP_REGRESSION_TESTBED", str(override))

    loaded = core.load_testbed(Path("/plugin/testbed.yaml.example"))

    assert loaded["serialwrap_exe"] == "/stage3/bin/serialwrap"
    assert loaded["allow_destructive"] is True
    assert loaded["_testbed_source"] == str(override)
