"""Windows 原生 COM 獨占證據判定測試；所有 COM 行為皆由記憶體假物件提供。"""

from __future__ import annotations

import ast
from contextlib import contextmanager
from pathlib import Path

import pytest


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = Path(__file__).resolve().parents[1]
WINEXCL_SOURCE = ROOT / "realhw" / "acceptance" / "winexcl.py"


def _winexcl():
    import importlib

    return importlib.import_module("realhw.acceptance.winexcl")


class FakeLock:
    def __init__(self, available: bool = True):
        self.available = available
        self.held = False

    def try_acquire(self):
        if not self.available or self.held:
            return False
        self.held = True
        return True

    def acquire(self):
        self.held = True
        return True

    def release(self):
        self.held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release()
        return False


class FakeSwCli:
    def __init__(self, *, initial_state="READY", device="COM5", generation=3,
                 attach_while_held_ok=False, attach_ready=True, advance_generation=True,
                 identity_sequence=None):
        self.state = initial_state
        self.device = device
        self.generation = generation
        self.attach_while_held_ok = attach_while_held_ok
        self.attach_ready = attach_ready
        self.advance_generation = advance_generation
        self.external_open = False
        self.calls = []
        self.identity_sequence = list(identity_sequence or [])

    def run(self, *args):
        args = tuple(args)
        self.calls.append(args)
        if args[:2] == ("daemon", "status"):
            return {"ok": True, "pid": 4242}
        if args[:2] == ("session", "list"):
            return {"ok": True, "sessions": [{
                "selector": "COM0", "com": "COM0", "state": self.state,
                "attached_real_path": self.device, "bridge_generation": self.generation,
            }]}
        if args[:2] == ("device", "release"):
            self.state = "RELEASED"
            return {"ok": True}
        if args[:2] == ("device", "attach"):
            if self.external_open:
                if self.attach_while_held_ok:
                    self.state = "READY"
                    return {"ok": True}
                return {"ok": False, "error_code": "DEVICE_STILL_HELD", "pids": [],
                        "holder_unknown": True, "selector": "COM0"}
            if self.attach_ready:
                self.state = "READY"
                if self.advance_generation:
                    self.generation += 1
            else:
                self.state = "ATTACHED"
            return {"ok": True}
        raise AssertionError(f"winexcl 發出未預期命令：{args!r}")


def _binding_sw(*, state="READY", device="COM5", generation=3, states=None):
    sw = FakeSwCli(initial_state=state, device=device, generation=generation)
    if states is not None:
        iterator = iter(states)

        def run(*args):
            args = tuple(args)
            sw.calls.append(args)
            if args[:2] == ("daemon", "status"):
                return {"ok": True, "pid": 4242}
            if args[:2] == ("session", "list"):
                row = next(iterator)
                return {"ok": True, "sessions": [{
                    "selector": "COM0", "com": "COM0", "state": row["state"],
                    "attached_real_path": row["device"], "bridge_generation": row["generation"],
                }]}
            raise AssertionError(args)

        sw.run = run
    return sw


def _exclusive_opener(module, attempts):
    def opener(device, share_mode):
        attempt = module.OpenAttempt(share_mode=share_mode, opened=False, winerror=5)
        attempts.append(attempt)
        return attempt
    return opener


def test_winexcl_second_open_exclusive():
    winexcl = _winexcl()
    sw = _binding_sw()
    attempts = []
    result = winexcl.second_open_probe(
        sw, "COM0", expected_device="COM5", opener=_exclusive_opener(winexcl, attempts),
        identity=lambda pid: 133000000000000000, lock=FakeLock(),
    )
    assert result["verdict"] == "exclusive"
    assert [attempt.share_mode for attempt in attempts] == [0, 3]
    assert result["before"] == result["after"]


def test_winexcl_second_open_opened_is_violated():
    winexcl = _winexcl()
    sw = _binding_sw()
    attempts = []

    def opener(device, share_mode):
        opened = share_mode == 0
        attempt = winexcl.OpenAttempt(share_mode=share_mode, opened=opened, winerror=None if opened else 5)
        attempts.append(attempt)
        return attempt

    result = winexcl.second_open_probe(
        sw, "COM0", expected_device="COM5", opener=opener,
        identity=lambda pid: 133000000000000000, lock=FakeLock(),
    )
    assert result["verdict"] == "violated"
    assert len(attempts) == 2


def test_winexcl_probe_not_ready_does_not_open():
    winexcl = _winexcl()
    for state in ("RELEASED", "ATTACHED"):
        calls = []
        result = winexcl.second_open_probe(
            _binding_sw(state=state), "COM0", expected_device="COM5",
            opener=lambda *args: calls.append(args), identity=lambda pid: 133000000000000000, lock=FakeLock(),
        )
        assert calls == []
        assert result["verdict"] == "inconclusive"
        assert result["reason"] == "not_ready"

    calls = []
    unbound = winexcl.second_open_probe(
        _binding_sw(device="COM9"), "COM0", expected_device="COM5",
        opener=lambda *args: calls.append(args), identity=lambda pid: 133000000000000000, lock=FakeLock(),
    )
    assert calls == []
    assert unbound["reason"] == "device_unbound"

    calls = []
    unknown = winexcl.second_open_probe(
        _binding_sw(), "COM0", expected_device="COM5", opener=lambda *args: calls.append(args),
        identity=lambda pid: None, lock=FakeLock(),
    )
    assert calls == []
    assert unknown["reason"] == "process_identity_unknown"


def test_winexcl_binding_changed_inconclusive():
    winexcl = _winexcl()
    for opened in (True, False):
        states = [
            {"state": "READY", "device": "COM5", "generation": 3},
            {"state": "READY", "device": "COM5", "generation": 4},
        ]
        sw = _binding_sw(states=states)

        def opener(device, share_mode):
            return winexcl.OpenAttempt(share_mode, opened, None if opened else 5)

        result = winexcl.second_open_probe(
            sw, "COM0", expected_device="COM5", opener=opener,
            identity=lambda pid: 133000000000000000, lock=FakeLock(),
        )
        assert result["verdict"] == "inconclusive"
        assert result["reason"] == "binding_changed"


def test_winexcl_unexpected_winerror_inconclusive():
    winexcl = _winexcl()
    result = winexcl.second_open_probe(
        _binding_sw(), "COM0", expected_device="COM5",
        opener=lambda device, share: winexcl.OpenAttempt(share, False, 2),
        identity=lambda pid: 133000000000000000, lock=FakeLock(),
    )
    assert result["verdict"] == "inconclusive"
    assert result["reason"] == "unexpected_winerror"


def test_winexcl_probe_skips_when_lock_busy():
    winexcl = _winexcl()
    opened = []
    result = winexcl.second_open_probe(
        _binding_sw(), "COM0", expected_device="COM5", opener=lambda *args: opened.append(args),
        identity=lambda pid: 133000000000000000, lock=FakeLock(available=False),
    )
    assert opened == []
    assert result["verdict"] == "skipped"
    assert result["reason"] == "handoff_in_progress"


def _handoff(winexcl, sw, *, holder_attempt=None, identity=None, ready_timeout_s=0.2):
    attempts = []
    identity = identity or (lambda pid: 133000000000000000)

    def holder(device):
        @contextmanager
        def held():
            sw.external_open = True
            attempt = holder_attempt or winexcl.OpenAttempt(share_mode=0, opened=True, winerror=None)
            try:
                yield attempt
            finally:
                sw.external_open = False
        return held()

    result = winexcl.handoff_probe(
        sw, "COM0", phase="pre", expected_device="COM5", opener=_exclusive_opener(winexcl, attempts),
        holder=holder, identity=identity, lock=FakeLock(), ready_timeout_s=ready_timeout_s,
    )
    return result, attempts


def test_winexcl_handoff_full_cycle_exclusive():
    winexcl = _winexcl()
    sw = FakeSwCli()
    result, _ = _handoff(winexcl, sw)
    steps = result["steps"]
    assert result["verdict"] == "exclusive"
    assert [step["name"] for step in steps] == [
        "probe_before", "release", "hold", "attach_held", "unhold", "attach", "probe_after"
    ]
    assert steps[3]["detail"]["holder_unknown"] is True
    assert steps[3]["detail"]["pids"] == []
    assert sum(call[:2] == ("device", "attach") for call in sw.calls) == 2
    assert sum(call[:2] == ("device", "release") for call in sw.calls) == 1


def test_winexcl_handoff_attach_ok_while_held_violated():
    winexcl = _winexcl()
    result, _ = _handoff(winexcl, FakeSwCli(attach_while_held_ok=True))
    assert result["verdict"] == "violated"
    assert result["reason"] == "attach_held"


def test_winexcl_handoff_hold_denied_after_release_violated():
    winexcl = _winexcl()
    denied = winexcl.OpenAttempt(share_mode=0, opened=False, winerror=5)
    violated, _ = _handoff(winexcl, FakeSwCli(), holder_attempt=denied)
    assert violated["verdict"] == "violated"
    assert violated["reason"] == "hold"

    unexpected = winexcl.OpenAttempt(share_mode=0, opened=False, winerror=2)
    inconclusive, _ = _handoff(winexcl, FakeSwCli(), holder_attempt=unexpected)
    assert inconclusive["verdict"] == "inconclusive"
    assert inconclusive["reason"] == "hold"


def test_winexcl_handoff_identity_or_generation_inconclusive():
    winexcl = _winexcl()
    identity_values = iter([133000000000000000] * 4 + [133000000000000001] * 2)
    identity_changed, _ = _handoff(winexcl, FakeSwCli(), identity=lambda pid: next(identity_values))
    assert identity_changed["verdict"] == "inconclusive"
    assert identity_changed["reason"] == "probe_after"

    generation, _ = _handoff(winexcl, FakeSwCli(advance_generation=False))
    assert generation["verdict"] == "inconclusive"

    timeout, _ = _handoff(winexcl, FakeSwCli(attach_ready=False), ready_timeout_s=0.2)
    assert timeout["verdict"] == "inconclusive"
    assert timeout["reason"] == "attach"


def test_winexcl_no_io_calls(monkeypatch):
    winexcl = _winexcl()
    source = WINEXCL_SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(WINEXCL_SOURCE))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    forbidden = {"ReadFile", "WriteFile", "SetCommState", "SetCommMask", "PurgeComm",
                 "EscapeCommFunction", "TransmitCommChar"}
    assert names.isdisjoint(forbidden)

    class FakeKernel32:
        def __init__(self):
            self.opens = []
            self.closed = []

        def CreateFileW(self, *args):
            self.opens.append(args)
            return 123

        def CloseHandle(self, handle):
            self.closed.append(handle)
            return True

        def GetLastError(self):
            return 5

    kernel = FakeKernel32()
    monkeypatch.setattr(winexcl, "kernel32", kernel, raising=False)
    monkeypatch.setattr(winexcl, "_kernel32", lambda: kernel, raising=False)
    monkeypatch.setattr(winexcl.ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)

    winexcl.win_open("COM5", 0)
    assert kernel.opens[-1][1] == winexcl.GENERIC_READ | winexcl.GENERIC_WRITE
    assert kernel.opens[-1][4] == winexcl.OPEN_EXISTING
    assert len(kernel.closed) == 1

    with winexcl.win_hold("COM5") as attempt:
        assert attempt.opened is True
        assert kernel.opens[-1][2] == 0
        assert kernel.opens[-1][1] == winexcl.GENERIC_READ | winexcl.GENERIC_WRITE
        assert kernel.opens[-1][4] == winexcl.OPEN_EXISTING
    assert len(kernel.closed) == 2


def test_winexcl_cli_non_windows_exit2(capsys, monkeypatch):
    winexcl = _winexcl()
    calls = []
    monkeypatch.setattr(winexcl, "SwCli", lambda *args, **kwargs: calls.append((args, kwargs)), raising=False)
    code = winexcl.main(["handoff", "--phase", "pre", "--serialwrap-exe", "serialwrap",
                         "--boards", "COM0", "--observer-header", "observer.ndjson", "--excl-dir", "."])
    captured = capsys.readouterr()
    assert code == 2
    assert "windows only" in captured.err.lower()
    assert calls == []
