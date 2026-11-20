"""F139：gate identity 的 daemon_version 取自 daemon RPC 回應的 `version`（serialwrapd 依 Python 1:1 不提供 --version）。"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("probe_daemon_version", ROOT / "scripts" / "rust" / "probe_daemon_version.py")
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_version_from_rpc_result():
    assert probe.version_from_result({"ok": True, "version": "0.3.2"}) == "0.3.2"
    for bad in ({"ok": True}, {"ok": True, "version": ""}, {"ok": True, "version": 3}, None):
        assert probe.version_from_result(bad) is None


@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_probe_python_daemon_reports_package_version(tmp_path):
    """實際啟動 Python 基線 daemon（throwaway 目錄）並以 daemon.status RPC 取 version；與 VERSION 檔一致。"""
    daemon = str(ROOT / "tests" / "contract_blackbox" / "bin" / "py-serialwrapd")
    got = probe.probe(daemon, work=tmp_path / "w")
    assert got == (ROOT / "VERSION").read_text(encoding="utf-8").strip()
