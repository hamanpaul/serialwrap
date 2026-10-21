# tests/test_stage3_release_check.py
"""P9-T4 A→B 資源釋放驗證（tools/stage3/release_check.py）的替身驅動紅燈測試。

判定規則見 plans/10-stage3-canary.md Task 4 判定表：每輪全部重測、任一 unknown 即非 released、
逾時以最後一次結果為準。本模組不在頂層 import 本 Phase 才新增的模組或名稱（stage3_fakes、tools.stage3.*），
一律於測試函式內延遲 import，使 `pytest --collect-only tests/` 在任何階段都能完成收集（同 test_stage0_fakes.py 慣例）。
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

PROBE_METHODS = {
    "pid_alive",
    "descendants",
    "pids_by_exe",
    "endpoint_connectable",
    "lock_acquirable",
    "device_holders",
    "device_exclusive_open",
}


def _rc() -> Any:
    return importlib.import_module("tools.stage3.release_check")


def _fakes() -> Any:
    return importlib.import_module("stage3_fakes")


def _spec(**kw: Any) -> Any:
    spec_cls = importlib.import_module("tools.stage3.ab_manifest").ReleaseSpec
    base: dict[str, Any] = dict(
        timeout_s=1.0,
        poll_s=0.2,
        endpoints=("/tmp/sw-x.sock",),
        device_paths=(),
        lock_paths=(),
        device_open_probe=False,
        accept_unreadable_holders=False,
    )
    base.update(kw)
    return spec_cls(**base)


def _snap(pid: int | None = 4242, **kw: Any) -> Any:
    snap_cls = _rc().PreStopSnapshot
    base: dict[str, Any] = dict(
        daemon_pid=pid,
        descendant_pids=(),
        match_paths=("/b/bin/serialwrapd",),
        endpoints=("/tmp/sw-x.sock",),
        device_paths=(),
        lock_paths=(),
        captured_at=0.0,
    )
    base.update(kw)
    return snap_cls(**base)


def _status(result: Any, name: str) -> list[str]:
    return [c.status for c in result.checks if c.name == name]


def test_fake_probe_conforms_to_protocol():
    names = {n for n in dir(_rc().ReleaseProbe) if not n.startswith("_")}
    assert names == PROBE_METHODS
    assert PROBE_METHODS <= set(dir(_fakes().FakeProbe))


def test_unreadable_holders_are_unknown():
    rc = _rc()
    clock = _fakes().FakeClock()
    probe = _fakes().FakeProbe(holders={"/dev/ttyUSB0": [((), 2)]})
    snap = _snap(device_paths=("/dev/ttyUSB0",))
    r = rc.verify_released(snap, _spec(device_paths=("/dev/ttyUSB0",)), probe=probe, clock=clock.now, sleep=clock.sleep)
    assert r.released is False
    assert _status(r, "device_holders") == ["unknown"]
    r2 = rc.verify_released(
        snap,
        _spec(device_paths=("/dev/ttyUSB0",), accept_unreadable_holders=True),
        probe=probe,
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert r2.released is True


def test_unknown_is_never_released():
    rc = _rc()
    clock = _fakes().FakeClock()
    probe = _fakes().FakeProbe(pid_alive={4242: [None]})
    r = rc.verify_released(_snap(), _spec(), probe=probe, clock=clock.now, sleep=clock.sleep)
    assert r.released is False
    assert {c.name: c.status for c in r.checks}["daemon_pid"] == "unknown"


def test_polls_until_released():
    rc = _rc()
    clock = _fakes().FakeClock()
    probe = _fakes().FakeProbe(pid_alive={4242: [True, True, False]}, endpoint={"/tmp/sw-x.sock": [True, False]})
    r = rc.verify_released(_snap(), _spec(timeout_s=5.0), probe=probe, clock=clock.now, sleep=clock.sleep)
    assert r.released is True
    assert 0.4 <= r.elapsed_s <= 0.6


def test_timeout_reports_held_lock():
    rc = _rc()
    clock = _fakes().FakeClock()
    probe = _fakes().FakeProbe(lock={"/r/serialwrapd.lock": [False]})
    r = rc.verify_released(
        _snap(lock_paths=("/r/serialwrapd.lock",)),
        _spec(lock_paths=("/r/serialwrapd.lock",)),
        probe=probe,
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert r.released is False
    assert _status(r, "lock") == ["held"]
    assert r.elapsed_s >= 1.0


def test_device_open_probe_not_called_when_disabled():
    rc = _rc()
    clock = _fakes().FakeClock()
    probe = _fakes().FakeProbe()
    rc.verify_released(
        _snap(device_paths=("COM7",)),
        _spec(device_paths=("COM7",), device_open_probe=False),
        probe=probe,
        clock=clock.now,
        sleep=clock.sleep,
    )
    assert not any(name == "device_exclusive_open" for name, _ in probe.calls)


def test_descendant_alive_blocks():
    rc = _rc()
    clock = _fakes().FakeClock()
    probe = _fakes().FakeProbe(pid_alive={9: [True]})
    r = rc.verify_released(_snap(descendant_pids=(9,)), _spec(), probe=probe, clock=clock.now, sleep=clock.sleep)
    assert r.released is False
    assert _status(r, "descendant") == ["held"]


def test_leaked_daemon_blocks():
    rc = _rc()
    clock = _fakes().FakeClock()
    probe = _fakes().FakeProbe(by_exe={"/b/bin/serialwrapd": [(77,)]})
    r = rc.verify_released(_snap(), _spec(), probe=probe, clock=clock.now, sleep=clock.sleep)
    assert r.released is False
    assert _status(r, "leaked_daemon") == ["held"]


def test_endpoint_timeout_is_unknown():
    rc = _rc()
    clock = _fakes().FakeClock()
    probe = _fakes().FakeProbe(endpoint={"/tmp/sw-x.sock": [None]})
    r = rc.verify_released(_snap(), _spec(), probe=probe, clock=clock.now, sleep=clock.sleep)
    assert r.released is False
    assert _status(r, "endpoint") == ["unknown"]


def test_capture_pre_stop_reads_daemon_pid():
    rc = _rc()
    fakes = _fakes()
    clock = fakes.FakeClock()
    runner = fakes.RecordingRunner(
        {"cli:daemon status": [{"rc": 0, "stdout": '{"ok":true,"running":true,"pid":55}', "stderr": ""}]}
    )
    snap = rc.capture_pre_stop(
        Path("/b/bin/serialwrap"),
        {},
        _spec(),
        daemon_exe=Path("/b/bin/serialwrapd"),
        run=runner,
        probe=fakes.FakeProbe(),
        clock=clock.now,
    )
    assert snap.daemon_pid == 55
    assert runner.calls[0]["kind"] == "cli"
    assert runner.calls[0]["sub"] == "daemon status"


def test_capture_pre_stop_daemon_down():
    rc = _rc()
    fakes = _fakes()
    clock = fakes.FakeClock()
    runner = fakes.RecordingRunner({"cli:daemon status": [{"rc": 2, "stdout": "", "stderr": "not running"}]})
    snap = rc.capture_pre_stop(
        Path("/b/bin/serialwrap"),
        {},
        _spec(),
        daemon_exe=Path("/b/bin/serialwrapd"),
        run=runner,
        probe=fakes.FakeProbe(),
        clock=clock.now,
    )
    assert snap.daemon_pid is None
    assert os.path.realpath("/b/bin/serialwrapd") in snap.match_paths


def test_snapshot_from_endpoints_merges(tmp_path: Path):
    rc = _rc()
    clock = _fakes().FakeClock()
    endpoints_json = tmp_path / "endpoints.json"
    endpoints_json.write_text(
        json.dumps(
            {"endpoints": ["/tmp/a.sock"], "device_paths": ["/dev/ttyX"], "lock_paths": ["/r/l.lock"]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    snap = rc.snapshot_from_endpoints(
        endpoints_json,
        _spec(endpoints=("/tmp/b.sock",)),
        match_paths=("/b/bin/serialwrapd",),
        clock=clock.now,
    )
    assert snap.endpoints == ("/tmp/b.sock", "/tmp/a.sock")
    assert snap.device_paths == ("/dev/ttyX",)
    assert snap.lock_paths == ("/r/l.lock",)
    assert snap.daemon_pid is None
