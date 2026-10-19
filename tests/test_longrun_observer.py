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
            # daemon status 的真實形狀：foreign_holders 為 dict，wal 健康度位於 wal.healthy
            return {"ok": True, "pid": 4242, "version": "0.3.2", "multi_open": False,
                    "foreign_holders": {}, "wal": {"healthy": True}}
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


class _StaticSw:
    """回固定 daemon status 與 session list 的唯讀替身；`fail_status` 為 True 時 daemon status 拋例外。"""

    def __init__(self, *, status=None, fail_status: bool = False, fail_session: bool = False):
        self.status = status
        self.fail_status = fail_status
        self.fail_session = fail_session
        self.calls = []

    def run(self, *args):
        self.calls.append(tuple(args))
        if tuple(args) == ("daemon", "status"):
            if self.fail_status:
                raise RuntimeError("RPC timeout")
            return self.status
        if tuple(args) == ("session", "list"):
            if self.fail_session:
                raise RuntimeError("RPC timeout")
            return {"ok": True, "sessions": [{"selector": "COM0", "state": "READY", "attached_real_path": "COM5"}]}
        raise AssertionError(f"observer 使用了非唯讀或未核准命令：{args!r}")


def test_sample_derives_holder_unknown_and_wal_health(monkeypatch):
    observer = _observer()
    monkeypatch.setattr(observer, "_all_serialwrapd_pids", lambda: [])
    holders = {"COM5": {"device": "COM5", "holder_unknown": True}}
    sw = _StaticSw(status={"ok": True, "pid": 4242, "multi_open": False, "foreign_holders": holders,
                           "wal": {"healthy": True}})
    daemon = observer.sample(sw, ["COM0"], procscan=observer._ProcScan(period_s=0.0))["daemon"]
    assert daemon["ok"] is True and daemon["pid"] == 4242 and daemon["pid_alive"] is True
    assert daemon["holder_unknown"] is True
    assert daemon["wal_healthy"] is True
    assert daemon["foreign_holders"] == holders


def test_sample_rpc_failure_records_pid_alive_of_last_good_pid(monkeypatch):
    observer = _observer()
    monkeypatch.setattr(observer, "_all_serialwrapd_pids", lambda: [])
    monkeypatch.setattr(observer.procobs, "pid_alive", lambda pid: pid == 4242)
    procscan = observer._ProcScan(period_s=0.0)
    good = _StaticSw(status={"ok": True, "pid": 4242, "multi_open": False, "foreign_holders": {}})
    assert observer.sample(good, ["COM0"], procscan=procscan)["daemon"]["pid"] == 4242
    failed = observer.sample(_StaticSw(fail_status=True), ["COM0"], procscan=procscan)["daemon"]
    assert failed["ok"] is False and failed["pid"] is None
    assert failed["pid_alive"] is True


def test_procscan_excludes_own_daemon_when_rpc_fails(monkeypatch):
    """RPC 失敗時 pid 為 None；必須沿用最後一次成功的 pid 排除本 daemon，而非把它當成「其他」daemon。"""
    observer = _observer()
    monkeypatch.setattr(observer, "_all_serialwrapd_pids", lambda: [4242, 5555])
    procscan = observer._ProcScan(period_s=0.0)
    ok = _StaticSw(status={"ok": True, "pid": 4242, "multi_open": False, "foreign_holders": {}})
    first = observer.sample(ok, ["COM0"], procscan=procscan)
    assert first["procscan"]["other_serialwrapd"] == [5555]
    second = observer.sample(_StaticSw(fail_status=True), ["COM0"], procscan=procscan)
    assert second["daemon"]["pid"] is None
    assert second["procscan"]["other_serialwrapd"] == [5555]


def test_procscan_unknown_daemon_pid_is_not_claimed_empty(monkeypatch):
    """從未取得過 daemon pid 且有其他 serialwrapd 時，無法判定歸屬：other_serialwrapd 必須為 None。"""
    observer = _observer()
    monkeypatch.setattr(observer, "_all_serialwrapd_pids", lambda: [4242, 5555])
    row = observer.sample(_StaticSw(fail_status=True), ["COM0"], procscan=observer._ProcScan(period_s=0.0))
    assert row["procscan"]["other_serialwrapd"] is None


@pytest.mark.parametrize("tokens, expected", [
    (["serialwrapd", "--socket", "/run/x.sock"], True),
    (["/usr/bin/python3", "/home/u/.local/bin/serialwrapd", "--socket", "/run/x.sock"], True),
    (["python3", "serialwrapd.py", "daemon"], True),
    (["python3", "-m", "sw_core.daemon"], True),
    (["python3", "-m", "serialwrapd"], True),
    (["C:\\Program Files\\serialwrap\\serialwrapd.exe", "--tcp", "48700"], True),
    (["SERIALWRAPD.EXE"], True),
    (["grep", "serialwrapd"], False),
    (["python3", "-c", "-m sw_core.daemon"], False),
    (["python3", "-m", "pytest", "-q", "tests/test_serialwrapd.py"], False),
    (["sleep", "600"], False),
    ([], False),
])
def test_is_serialwrapd_argv_forms(tokens, expected):
    observer = _observer()
    assert observer.is_serialwrapd_argv(tokens) is expected


def test_observer_refuses_to_overwrite_nonempty_output(tmp_path):
    observer = _observer()
    out = tmp_path / "observer.ndjson"
    out.write_text('{"kind":"header"}\n', encoding="utf-8")
    with pytest.raises(FileExistsError):
        _run(observer, StubSwCli(), out)
    assert out.read_text(encoding="utf-8") == '{"kind":"header"}\n'


def test_observer_survives_probe_rpc_error(tmp_path, monkeypatch):
    observer = _observer()
    monkeypatch.setattr(observer.sys, "platform", "win32")
    calls = []

    def flaky_probe(sw_arg, com, *, expected_device, **kwargs):
        calls.append(com)
        if com == "COM0":
            raise RuntimeError("RPC timeout")
        return {"wall_ts": "2026-10-15T00:00:00+00:00", "verdict": "exclusive", "reason": None}

    monkeypatch.setattr(observer, "second_open_probe", flaky_probe, raising=False)
    out = tmp_path / "observer.ndjson"
    rc = _run(observer, StubSwCli(), out, interval_s=0.05, duration_s=0.3,
              excl_dir=tmp_path, excl_probe_interval_s=0.05)
    assert rc == 0
    probes = _read_rows(tmp_path / "probes.ndjson")
    failed = [row for row in probes if row.get("com") == "COM0"]
    assert failed
    assert all(row["verdict"] == "inconclusive" and row["reason"] == "rpc_error" for row in failed)
    samples = [row for row in _read_rows(out) if row.get("kind") == "sample"]
    assert samples  # 迴圈續跑即可；不對取樣次數做時序斷言（全套件併發下會抖動）
    assert "COM1" in calls


def test_observer_binding_rpc_error_does_not_abort(tmp_path):
    observer = _observer()
    sw = _StaticSw(status={"ok": True, "pid": 4242, "multi_open": False, "foreign_holders": {}}, fail_session=True)
    out = tmp_path / "observer.ndjson"
    assert _run(observer, sw, out) == 0
    header = _read_rows(out)[0]
    assert header["device_binding"] == {"COM0": None, "COM1": None}
    assert any(row.get("kind") == "sample" for row in _read_rows(out))
