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
import errno
import socket
import sys
from types import SimpleNamespace
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


class _BareProbe:
    def __init__(self, **values: Any) -> None:
        self.values = values

    def pid_alive(self, pid: int) -> bool | None:
        return self.values.get("pid_alive", {}).get(pid, False)

    def descendants(self, pid: int) -> tuple[int, ...] | None:
        return self.values.get("descendants", ())

    def pids_by_exe(self, paths: Any) -> Any:
        return self.values.get("by_exe", ())

    def endpoint_connectable(self, endpoint: str) -> bool | None:
        return self.values.get("endpoint", False)

    def lock_acquirable(self, path: str) -> bool | None:
        return True

    def device_holders(self, path: str) -> Any:
        return self.values.get("holders", ((), 0))

    def device_exclusive_open(self, path: str) -> bool | None:
        return True


def _bare_spec(**overrides: Any) -> Any:
    values = dict(
        timeout_s=0.0,
        poll_s=0.2,
        endpoints=("tcp://127.0.0.1:48700",),
        device_paths=(),
        lock_paths=(),
        device_open_probe=False,
        accept_unreadable_holders=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _cmd_result(rc: int, stdout: str = "", *, timed_out: bool = False) -> Any:
    return SimpleNamespace(rc=rc, stdout=stdout, stderr="", timed_out=timed_out)


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


def test_posix_cmdline_permission_error_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rc = _rc()
    proc_root = tmp_path / "proc"
    process = proc_root / "123"
    process.mkdir(parents=True)
    (process / "exe").symlink_to(sys.executable)
    (process / "cmdline").write_bytes(b"python\0/tmp/serialwrapd.py\0")
    probe = rc.PosixProbe()
    probe._PROC_ROOT = str(proc_root)
    original_read_bytes = Path.read_bytes

    def denied_cmdline(path: Path) -> bytes:
        if path == process / "cmdline":
            raise PermissionError(errno.EACCES, "permission denied", str(path))
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", denied_cmdline)
    assert probe.pids_by_exe(("/tmp/serialwrapd.py",)) is None


def test_posix_pids_by_exe_resolves_relative_script_from_process_cwd(tmp_path: Path):
    rc = _rc()
    script = tmp_path / "serialwrapd.py"
    script.write_text("pass\n", encoding="utf-8")
    proc_root = tmp_path / "proc"
    process = proc_root / "123"
    process.mkdir(parents=True)
    (process / "exe").symlink_to(sys.executable)
    (process / "cwd").symlink_to(tmp_path, target_is_directory=True)
    (process / "cmdline").write_bytes(f"{sys.executable}\0./serialwrapd.py\0".encode())
    probe = rc.PosixProbe()
    probe._PROC_ROOT = str(proc_root)
    assert probe.pids_by_exe((str(script),)) == (123,)


def test_windows_pids_by_exe_ignores_unrelated_unreadable_process():
    rc = _rc()
    probe = rc.WindowsProbe.__new__(rc.WindowsProbe)
    probe._process_rows = lambda: [(4, 0, "System"), (55, 0, "serialwrapd.exe")]
    probe._image_path = lambda pid: (None, None) if pid == 4 else (True, "/candidate/serialwrapd.exe")
    assert probe.pids_by_exe(("/candidate/serialwrapd.exe",)) == (55,)


def test_windows_invalid_pid_is_unknown():
    rc = _rc()
    probe = rc.WindowsProbe.__new__(rc.WindowsProbe)
    assert probe.pid_alive(0) is None


def test_posix_invalid_pid_is_unknown():
    assert _rc().PosixProbe().pid_alive(0) is None


def test_windows_closed_endpoint_probe_uses_refusal_timeout(monkeypatch: pytest.MonkeyPatch):
    rc = _rc()
    probe = rc.WindowsProbe.__new__(rc.WindowsProbe)
    seen: list[float] = []

    def refused(address: Any, *, timeout: float) -> Any:
        seen.append(timeout)
        raise ConnectionRefusedError(errno.ECONNREFUSED, "connection refused")

    monkeypatch.setattr(socket, "create_connection", refused)
    assert probe.endpoint_connectable("tcp://127.0.0.1:48700") is False
    assert seen == [5.0]


def test_capture_pre_stop_keeps_pid_when_running_is_false():
    rc = _rc()
    runner_calls: list[dict[str, Any]] = []

    def runner(argv: Any, **kwargs: Any) -> Any:
        runner_calls.append({"argv": list(argv), **kwargs})
        return _cmd_result(0, '{"ok":true,"running":false,"pid":777}')

    snap = rc.capture_pre_stop(
        Path("/bundle/bin/serialwrap"),
        {},
        _bare_spec(),
        daemon_exe=Path("/bundle/bin/serialwrapd"),
        probe=_BareProbe(),
        run=runner,
    )
    assert snap.daemon_pid == 777
    assert snap.descendant_pids == ()


def test_capture_pre_stop_rejects_live_daemon_error_response():
    rc = _rc()

    def runner(argv: Any, **kwargs: Any) -> Any:
        return _cmd_result(2, '{"ok":false,"error_code":"TIMEOUT","daemon_reachable":true}')

    with pytest.raises(rc.HarnessError, match="daemon status"):
        rc.capture_pre_stop(
            Path("/bundle/bin/serialwrap"), {}, _bare_spec(),
            daemon_exe=Path("/bundle/bin/serialwrapd"), probe=_BareProbe(), run=runner,
        )


def test_capture_pre_stop_requires_all_endpoints_to_be_closed():
    rc = _rc()

    def runner(argv: Any, **kwargs: Any) -> Any:
        return _cmd_result(2)

    with pytest.raises(rc.HarnessError, match="端點"):
        rc.capture_pre_stop(
            Path("/bundle/bin/serialwrap"), {}, _bare_spec(),
            daemon_exe=Path("/bundle/bin/serialwrapd"),
            probe=_BareProbe(endpoint=True), run=runner,
        )


def test_capture_pre_stop_uses_explicit_endpoint_and_extended_timeout():
    rc = _rc()
    calls: list[dict[str, Any]] = []

    def runner(argv: Any, **kwargs: Any) -> Any:
        calls.append({"argv": list(argv), **kwargs})
        return _cmd_result(0, '{"ok":true,"running":true,"pid":55}')

    rc.capture_pre_stop(
        Path("/bundle/bin/serialwrap"), {}, _bare_spec(),
        daemon_exe=Path("/bundle/bin/serialwrapd"), endpoint="tcp://127.0.0.1:48700",
        probe=_BareProbe(), run=runner,
    )
    assert calls[0]["argv"][:3] == ["/bundle/bin/serialwrap", "--endpoint", "tcp://127.0.0.1:48700"]
    assert calls[0]["timeout"] == 15.0


def test_capture_pre_stop_requires_evidence_for_daemon_down():
    rc = _rc()

    def runner(argv: Any, **kwargs: Any) -> Any:
        return _cmd_result(2)

    with pytest.raises(rc.HarnessError, match="端點"):
        rc.capture_pre_stop(
            Path("/bundle/bin/serialwrap"), {}, _bare_spec(endpoints=()),
            daemon_exe=Path("/bundle/bin/serialwrapd"), probe=_BareProbe(), run=runner,
        )


def test_capture_pre_stop_accepts_socket_error_only_after_endpoint_closes():
    rc = _rc()

    def runner(argv: Any, **kwargs: Any) -> Any:
        return _cmd_result(2, '{"ok":false,"error_code":"SOCKET_ERROR"}')

    snap = rc.capture_pre_stop(
        Path("/bundle/bin/serialwrap"), {}, _bare_spec(),
        daemon_exe=Path("/bundle/bin/serialwrapd"), probe=_BareProbe(), run=runner,
    )
    assert snap.daemon_pid is None


def test_safe_probe_exception_produces_unknown():
    rc = _rc()
    probe = _BareProbe()

    def fail(endpoint: str) -> bool | None:
        raise OSError("probe failed")

    probe.endpoint_connectable = fail
    result = rc.verify_released(
        rc.PreStopSnapshot(None, (), ("/bundle/bin/serialwrapd",), ("broken",), (), (), 0.0),
        _bare_spec(endpoints=()), probe=probe,
    )
    assert not result.released
    assert _status(result, "endpoint") == ["unknown"]


@pytest.mark.parametrize(
    ("result", "stdout"),
    [
        (_cmd_result(-1, timed_out=True), ""),
        (_cmd_result(1), ""),
        (_cmd_result(0), '{"ok":true,"running":true}'),
    ],
)
def test_capture_pre_stop_rejects_status_timeout_failure_or_missing_pid(result: Any, stdout: str):
    rc = _rc()
    if stdout:
        result = _cmd_result(result.rc, stdout, timed_out=result.timed_out)

    def runner(argv: Any, **kwargs: Any) -> Any:
        return result

    with pytest.raises(rc.HarnessError):
        rc.capture_pre_stop(
            Path("/bundle/bin/serialwrap"), {}, _bare_spec(),
            daemon_exe=Path("/bundle/bin/serialwrapd"), probe=_BareProbe(), run=runner,
        )


def test_snapshot_from_missing_endpoints_uses_manifest_paths(tmp_path: Path):
    rc = _rc()
    snap = rc.snapshot_from_endpoints(
        tmp_path / "missing-endpoints.json",
        _bare_spec(endpoints=("tcp://127.0.0.1:4000",), device_paths=("device-a",), lock_paths=("lock-a",)),
        match_paths=("/bundle/bin/serialwrapd",),
    )
    assert snap.endpoints == ("tcp://127.0.0.1:4000",)
    assert snap.device_paths == ("device-a",)
    assert snap.lock_paths == ("lock-a",)


def test_snapshot_from_malformed_endpoints_is_unconfirmed(tmp_path: Path):
    rc = _rc()
    path = tmp_path / "endpoints.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(rc.HarnessError, match="endpoints.json"):
        rc.snapshot_from_endpoints(path, _bare_spec(), match_paths=("/bundle/bin/serialwrapd",))


@pytest.mark.parametrize("pid", [0, -1])
def test_invalid_snapshot_pid_is_unknown(pid: int):
    rc = _rc()
    result = rc.verify_released(
        rc.PreStopSnapshot(pid, (), ("/bundle/bin/serialwrapd",), (), (), (), 0.0),
        _bare_spec(endpoints=()), probe=_BareProbe(),
    )
    assert not result.released
    assert _status(result, "daemon_pid") == ["unknown"]


def test_invalid_descendant_pid_is_unknown():
    rc = _rc()
    result = rc.verify_released(
        rc.PreStopSnapshot(None, (0,), ("/bundle/bin/serialwrapd",), (), (), (), 0.0),
        _bare_spec(endpoints=()), probe=_BareProbe(),
    )
    assert not result.released
    assert _status(result, "descendant") == ["unknown"]


@pytest.mark.parametrize(
    ("values", "name", "spec_overrides"),
    [
        ({"by_exe": ("bad-pid",)}, "leaked_daemon", {}),
        ({"holders": (("bad-pid",), 0)}, "device_holders", {"device_paths": ("device-a",)}),
    ],
)
def test_malformed_probe_pid_results_are_unknown(values: dict[str, Any], name: str, spec_overrides: dict[str, Any]):
    rc = _rc()
    spec = _bare_spec(**spec_overrides)
    snap = rc.PreStopSnapshot(None, (), ("/bundle/bin/serialwrapd",), (), spec.device_paths, (), 0.0)
    result = rc.verify_released(snap, spec, probe=_BareProbe(**values))
    assert not result.released
    assert _status(result, name) == ["unknown"]
