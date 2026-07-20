from __future__ import annotations

import json
import os

import pytest

from sw_core import cli
from sw_core import remote_tunnel as rt


def _run(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip() else None


def test_remote_bare_is_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    rc, obj = _run(["remote"], capsys)
    assert rc == 0 and obj["ok"] and obj["tunnels"] == []


def test_remote_mutually_exclusive_R_L(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    rc, obj = _run(["remote", "-R", "-L", "u@h:7777"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_ARGS"


def test_remote_invalid_target(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    rc, obj = _run(["remote", "nohost"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_TARGET"


def test_remote_open_dispatches_to_open_tunnel(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    captured = {}
    def fake_open(spec, run_dir, **kw):
        captured["spec"] = spec
        return {"ok": True, "status": "active", "role": "expose", "listen_port": spec.port}
    monkeypatch.setattr(rt, "open_tunnel", fake_open)
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")
    rc, obj = _run(["remote", "tester@relay:7777"], capsys)
    assert rc == 0 and obj["status"] == "active"
    assert captured["spec"].role == "expose"  # -R 預設
    assert captured["spec"].ssh_target == "tester@relay"


def test_remote_local_with_default_role_is_invalid_args(tmp_path, monkeypatch, capsys):
    """--local 僅用於 -L（connect）；預設（-R/expose）帶入須擋在 CLI 層，不靜默略過。"""
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")
    rc, obj = _run(["remote", "--local", "7000", "tester@relay:7777"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_ARGS"


def test_remote_local_with_R_is_invalid_args(tmp_path, monkeypatch, capsys):
    """--local 僅用於 -L（connect）；明確帶 -R 仍須擋。"""
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")
    rc, obj = _run(["remote", "-R", "--local", "7000", "tester@relay:7777"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_ARGS"


def test_remote_local_zero_is_invalid_args(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")
    rc, obj = _run(["remote", "-L", "--local", "0", "tester@relay:7777"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_ARGS"


def test_remote_local_too_large_is_invalid_args(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")
    rc, obj = _run(["remote", "-L", "--local", "99999", "tester@relay:7777"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_ARGS"


def test_remote_local_valid_with_L_dispatches(tmp_path, monkeypatch, capsys):
    """有效範圍內的 --local 搭 -L 仍照常派送到 open_tunnel。"""
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    captured = {}

    def fake_open(spec, run_dir, **kw):
        captured["spec"] = spec
        return {"ok": True, "status": "active", "role": "connect", "listen_port": spec.local}

    monkeypatch.setattr(rt, "open_tunnel", fake_open)
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")
    rc, obj = _run(["remote", "-L", "--local", "7000", "tester@relay:7777"], capsys)
    assert rc == 0 and obj["status"] == "active"
    assert captured["spec"].role == "connect"
    assert captured["spec"].local == 7000


def test_remote_malformed_endpoint_returns_json_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")
    rc, obj = _run(["--endpoint", "http://badscheme:1", "remote", "tester@relay:7777"], capsys)
    assert rc == 1 and obj is not None
    assert obj["ok"] is False
    assert obj["error_code"] == "INVALID_ENDPOINT"


def test_remote_windows_returns_not_supported(tmp_path, monkeypatch, capsys):
    """native Windows：guard 需在 `import remote_tunnel`（頂層 import fcntl）之前
    就攔截，status／open 兩條路徑皆須回 JSON REMOTE_NOT_SUPPORTED，不得拋例外。
    """
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(cli.os, "name", "nt")

    rc, obj = _run(["remote"], capsys)
    assert rc == 1 and obj is not None
    assert obj["ok"] is False
    assert obj["error_code"] == "REMOTE_NOT_SUPPORTED"

    rc, obj = _run(["remote", "u@h:7777"], capsys)
    assert rc == 1 and obj is not None
    assert obj["ok"] is False
    assert obj["error_code"] == "REMOTE_NOT_SUPPORTED"


def test_remote_internal_error_is_json_not_traceback(tmp_path, monkeypatch, capsys):
    """非 TunnelError 例外（如壞掉的 SERIALWRAP_RUN_DIR）不得穿越 CLI 邊界，
    須經 catch-all 轉為 JSON INTERNAL_ERROR。"""
    bad_parent = tmp_path / "not_a_dir"
    bad_parent.write_text("i am a file, not a directory")
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(bad_parent / "sub"))
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")

    rc, obj = _run(["remote", "tester@relay:7777"], capsys)
    assert rc == 1 and obj is not None
    assert obj["ok"] is False
    assert "error_code" in obj
