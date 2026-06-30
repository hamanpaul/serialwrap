from __future__ import annotations
import importlib

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
