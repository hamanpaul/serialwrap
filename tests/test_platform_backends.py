from __future__ import annotations

from sw_core import platform_backends as pb


def test_explicit_posix():
    assert pb.select_rpc_backend("posix") == "posix"

def test_explicit_win_aliases():
    for v in ("win", "windows", "win32"):
        assert pb.select_rpc_backend(v) == "win"

def test_auto_follows_platform(monkeypatch):
    monkeypatch.setattr(pb.sys, "platform", "linux")
    monkeypatch.setattr(pb.os, "name", "posix")
    assert pb.select_rpc_backend("auto") == "posix"
    monkeypatch.setattr(pb.sys, "platform", "win32")
    assert pb.select_rpc_backend("auto") == "win"

def test_env_override(monkeypatch):
    monkeypatch.setenv("SERIALWRAP_RPC_BACKEND", "win")
    assert pb.select_rpc_backend() == "win"

def test_unknown_backend_raises():
    """未知後端值應 raise ValueError，不靜默退化（Copilot review fix）。"""
    import pytest
    with pytest.raises(ValueError, match="unsupported backend"):
        pb.select_rpc_backend("bogus")
