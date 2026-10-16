from __future__ import annotations

import json
import os

import pytest

from sw_core import cli
try:  # remote_tunnel.py:10 頂層 import fcntl；原生 Windows 無此模組，rt 僅供 POSIX 測試使用
    from sw_core import remote_tunnel as rt
except ImportError:  # pragma: no cover -- 原生 Windows
    rt = None

# 模組層只放資源 marker；平台 marker 逐函式標註（Windows 行為測試為 core_portable，其餘依賴 POSIX fcntl 的為 posix_only）
pytestmark = [pytest.mark.memory_only]


def _run(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip() else None


@pytest.mark.posix_only
def test_remote_bare_is_status(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    rc, obj = _run(["remote"], capsys)
    assert rc == 0 and obj["ok"] and obj["tunnels"] == []


@pytest.mark.posix_only
def test_remote_mutually_exclusive_R_L(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    rc, obj = _run(["remote", "-R", "-L", "u@h:7777"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_ARGS"


@pytest.mark.posix_only
def test_remote_invalid_target(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    rc, obj = _run(["remote", "nohost"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_TARGET"


@pytest.mark.posix_only
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


@pytest.mark.posix_only
def test_remote_local_with_default_role_is_invalid_args(tmp_path, monkeypatch, capsys):
    """--local 僅用於 -L（connect）；預設（-R/expose）帶入須擋在 CLI 層，不靜默略過。"""
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")
    rc, obj = _run(["remote", "--local", "7000", "tester@relay:7777"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_ARGS"


@pytest.mark.posix_only
def test_remote_local_with_R_is_invalid_args(tmp_path, monkeypatch, capsys):
    """--local 僅用於 -L（connect）；明確帶 -R 仍須擋。"""
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")
    rc, obj = _run(["remote", "-R", "--local", "7000", "tester@relay:7777"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_ARGS"


@pytest.mark.posix_only
def test_remote_local_zero_is_invalid_args(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")
    rc, obj = _run(["remote", "-L", "--local", "0", "tester@relay:7777"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_ARGS"


@pytest.mark.posix_only
def test_remote_local_too_large_is_invalid_args(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")
    rc, obj = _run(["remote", "-L", "--local", "99999", "tester@relay:7777"], capsys)
    assert rc == 1 and obj["error_code"] == "INVALID_ARGS"


@pytest.mark.posix_only
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


@pytest.mark.posix_only
def test_remote_malformed_endpoint_returns_json_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "/usr/bin/ssh")
    rc, obj = _run(["--endpoint", "http://badscheme:1", "remote", "tester@relay:7777"], capsys)
    assert rc == 1 and obj is not None
    assert obj["ok"] is False
    assert obj["error_code"] == "INVALID_ENDPOINT"


@pytest.mark.core_portable
def test_remote_windows_supported(tmp_path, monkeypatch, capsys):
    """BC-05：native Windows 不再回 REMOTE_NOT_SUPPORTED。status 與 open 兩條路徑皆進入
    正常分派（以替身 status／open_tunnel 回 ok:true），不得回 REMOTE_NOT_SUPPORTED。
    """
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(rt, "status", lambda run_dir: {"ok": True, "tunnels": []})
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "ssh")
    monkeypatch.setattr(
        rt,
        "open_tunnel",
        lambda spec, run_dir, **kw: {"ok": True, "status": "active", "role": spec.role, "listen_port": spec.port},
    )

    rc, obj = _run(["remote"], capsys)
    assert rc == 0 and obj is not None
    assert obj["ok"] is True
    assert obj.get("error_code") != "REMOTE_NOT_SUPPORTED"

    rc, obj = _run(["remote", "u@h:7777"], capsys)
    assert rc == 0 and obj is not None
    assert obj["ok"] is True
    assert obj.get("error_code") != "REMOTE_NOT_SUPPORTED"


@pytest.mark.posix_only
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
