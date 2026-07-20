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
