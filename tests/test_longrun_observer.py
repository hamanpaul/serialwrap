"""唯讀長跑 observer 與 Windows 獨占探測的契約測試。"""

from __future__ import annotations

import json
from pathlib import Path
import threading

import pytest


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def _observer():
    import importlib

    return importlib.import_module("realhw.acceptance.observer")


class StubSwCli:
    def __init__(self, *, fail_on_daemon_call: int | None = None):
        self.calls = []
        self.daemon_calls = 0
        self.fail_on_daemon_call = fail_on_daemon_call

    def run(self, *args):
        self.calls.append(tuple(args))
        if tuple(args) == ("daemon", "status"):
            self.daemon_calls += 1
            if self.daemon_calls == self.fail_on_daemon_call:
                raise RuntimeError("RPC timeout")
            return {"ok": True, "pid": 4242, "version": "0.3.2", "multi_open": False,
                    "foreign_holders": [], "holder_unknown": False, "wal_healthy": True}
        if tuple(args) == ("session", "list"):
            return {"ok": True, "sessions": [
                {"selector": "COM0", "state": "READY", "attached_real_path": "COM5", "bridge_generation": 3},
                {"selector": "COM1", "state": "READY", "attached_real_path": "COM6", "bridge_generation": 3},
            ]}
        raise AssertionError(f"observer 使用了非唯讀或未核准命令：{args!r}")


def _run(observer, sw, out: Path, *, interval_s: float = 0.05, duration_s: float = 0.3, **kwargs):
    return observer.run_observer(sw, ["COM0", "COM1"], interval_s=interval_s, out=out,
                                 stop=threading.Event(), duration_s=duration_s, **kwargs)


def _read_rows(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_observer_readonly_calls(tmp_path):
    observer = _observer()
    sw = StubSwCli()
    _run(observer, sw, tmp_path / "observer.ndjson")
    assert set(sw.calls) == {("daemon", "status"), ("session", "list")}
    source = Path(observer.__file__).read_text(encoding="utf-8")
    assert "interactive-status" not in source


def test_observer_writes_samples(tmp_path):
    observer = _observer()
    out = tmp_path / "observer.ndjson"
    _run(observer, StubSwCli(), out)
    rows = _read_rows(out)
    samples = [row for row in rows if row.get("kind") == "sample"]
    assert len(samples) >= 4
    assert all("wall_ts" in row and "pid" in row["daemon"] and "sessions" in row for row in samples)


def test_observer_survives_rpc_error(tmp_path):
    observer = _observer()
    out = tmp_path / "observer.ndjson"
    _run(observer, StubSwCli(fail_on_daemon_call=2), out)
    rows = _read_rows(out)
    failed = [row for row in rows if row.get("kind") == "sample" and row["daemon"].get("ok") is False]
    assert failed
    assert len([row for row in rows if row.get("kind") == "sample"]) >= 4


def test_observer_header_supervision():
    observer = _observer()
    attestation = {"mode": "systemd-user", "auto_restart_disabled": True, "evidence": "Restart=no"}
    header = observer.header_record(supervision=attestation, platform="linux")
    assert header["kind"] == "header"
    assert header["supervision"] == attestation

    missing = observer.header_record(
        supervision={"mode": "unknown", "auto_restart_disabled": None, "evidence": ""}, platform="linux"
    )
    assert missing["supervision"]["auto_restart_disabled"] is None


def test_observer_windows_excl_probes(tmp_path, monkeypatch):
    observer = _observer()
    monkeypatch.setattr(observer.sys, "platform", "win32")
    sw = StubSwCli()
    calls = []

    def fake_probe(sw_arg, com, *, expected_device, **kwargs):
        calls.append((com, expected_device))
        return {"kind": "excl_probe", "com": com, "expected_device": expected_device,
                "wall_ts": "2026-10-15T00:00:00+00:00", "verdict": "exclusive"}

    monkeypatch.setattr(observer, "second_open_probe", fake_probe, raising=False)
    out = tmp_path / "observer.ndjson"
    _run(observer, sw, out, interval_s=0.05, duration_s=0.3,
         excl_dir=tmp_path, excl_probe_interval_s=0.05)
    probes = _read_rows(tmp_path / "probes.ndjson")
    assert all(sum(row.get("com") == com and row.get("kind") == "excl_probe" for row in probes) >= 4
               for com in ("COM0", "COM1"))
    header = _read_rows(out)[0]
    assert header["device_binding"] == {"COM0": "COM5", "COM1": "COM6"}
    assert set(sw.calls) == {("daemon", "status"), ("session", "list")}
    assert set(calls) == {("COM0", "COM5"), ("COM1", "COM6")}


def test_observer_linux_no_excl_probes(tmp_path, monkeypatch):
    observer = _observer()
    monkeypatch.setattr(observer.sys, "platform", "linux")
    sw = StubSwCli()
    opened = []
    monkeypatch.setattr(observer, "second_open_probe", lambda *args, **kwargs: opened.append(args), raising=False)
    _run(observer, sw, tmp_path / "observer.ndjson", excl_dir=tmp_path, excl_probe_interval_s=0.05)
    assert not (tmp_path / "probes.ndjson").exists()
    assert opened == []
