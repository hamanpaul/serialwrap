"""P2-T14 red：realhw/procobs.py（跨平台行程／RSS 觀測）與 preflight 行程掃描的可攜化。

對應 docs/rust-refactor/plans/03-stage0-test-infra.md Task 14 T14-1。
"""
from __future__ import annotations

import ctypes
import os
import types

import pytest

pytestmark = [pytest.mark.core_portable]


@pytest.mark.memory_only
def test_rss_kb_self():
    from realhw import procobs

    assert procobs.rss_kb(os.getpid()) > 0
    assert procobs.rss_kb(0) == 0
    assert procobs.rss_kb(2**22 + 12345) == 0


@pytest.mark.memory_only
def test_rss_kb_windows_branch(monkeypatch):
    from realhw import procobs

    calls = []

    class _Kernel32:
        def OpenProcess(self, access, inherit, pid):
            return 55

        def K32GetProcessMemoryInfo(self, handle, counters_ref, cb):
            calls.append("kernel32")
            counters_ref.WorkingSetSize = 10485760  # 10 MiB
            return 1

        def CloseHandle(self, handle):
            return 1

    class _Psapi:
        pass

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _Kernel32(), raising=False)
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(
        kernel32=_Kernel32(), psapi=_Psapi()), raising=False)
    monkeypatch.setattr(ctypes, "byref", lambda obj, *a, **k: obj, raising=False)
    assert procobs.rss_kb(1234) == 10240
    assert calls == ["kernel32"]


@pytest.mark.memory_only
def test_pid_alive_windows_queries_exit_code_without_os_kill(monkeypatch):
    from realhw import procobs

    state = {"exit_code": 259, "last_error": 0, "closed": []}

    class _Kernel32:
        def OpenProcess(self, access, inherit, pid):
            assert access == 0x1000
            return 91

        def GetExitCodeProcess(self, handle, code):
            code.value = state["exit_code"]
            return 1

        def CloseHandle(self, handle):
            state["closed"].append(handle)
            return 1

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _Kernel32(), raising=False)
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(
        kernel32=_Kernel32(), psapi=object()), raising=False)
    monkeypatch.setattr(ctypes, "byref", lambda obj, *a, **k: obj, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: state["last_error"], raising=False)
    monkeypatch.setattr(os, "kill", lambda *a: (_ for _ in ()).throw(
        AssertionError("Windows PID probe must not call os.kill")))

    assert procobs.pid_alive(1234) is True
    state["exit_code"] = 0
    assert procobs.pid_alive(1234) is False
    assert state["closed"] == [91, 91]


@pytest.mark.memory_only
def test_pid_alive_windows_access_denied_is_conservatively_alive(monkeypatch):
    from realhw import procobs

    state = {"last_error": 5}

    class _Kernel32:
        def OpenProcess(self, access, inherit, pid):
            return None

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _Kernel32(), raising=False)
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(
        kernel32=_Kernel32(), psapi=object()), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: state["last_error"], raising=False)
    monkeypatch.setattr(os, "kill", lambda *a: (_ for _ in ()).throw(
        AssertionError("Windows PID probe must not call os.kill")))

    assert procobs.pid_alive(1234) is True
    state["last_error"] = 87
    assert procobs.pid_alive(1234) is False


@pytest.mark.memory_only
def test_list_processes_windows_branch(monkeypatch):
    from realhw import procobs

    entries = [(4, "System"), (1234, "serialwrapd.exe")]
    state = {"i": -1}

    def _fill(entry, item):
        entry.th32ProcessID = item[0]
        entry.szExeFile = item[1]

    class _K32:
        def CreateToolhelp32Snapshot(self, flags, pid):
            return 77

        def Process32FirstW(self, handle, entry_ref):
            state["i"] = 0
            _fill(entry_ref, entries[0])
            return 1

        def Process32NextW(self, handle, entry_ref):
            state["i"] += 1
            if state["i"] >= len(entries):
                return 0
            _fill(entry_ref, entries[state["i"]])
            return 1

        def CloseHandle(self, handle):
            return 1

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _K32(), raising=False)
    monkeypatch.setattr(ctypes, "windll",
                        types.SimpleNamespace(kernel32=_K32(), psapi=_K32()), raising=False)
    monkeypatch.setattr(ctypes, "byref", lambda obj, *a, **k: obj, raising=False)
    monkeypatch.setattr(procobs, "_windows_cmdlines",
                        lambda: {4: "System", 1234: "serialwrapd.exe --sw-coexist-x"})

    rows = procobs.list_processes()
    assert rows is not None
    assert {"pid": 4, "name": "System", "cmdline": "System"} in rows
    assert {"pid": 1234, "name": "serialwrapd.exe",
            "cmdline": "serialwrapd.exe --sw-coexist-x"} in rows


@pytest.mark.memory_only
def test_list_processes_windows_returns_unavailable_without_rows_or_cmdlines(monkeypatch):
    from realhw import procobs

    state = {"first": False}

    class _K32:
        def CreateToolhelp32Snapshot(self, flags, pid):
            return 77

        def Process32FirstW(self, handle, entry_ref):
            return state["first"]

        def Process32NextW(self, handle, entry_ref):
            return 0

        def CloseHandle(self, handle):
            return 1

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _K32(), raising=False)
    monkeypatch.setattr(ctypes, "windll",
                        types.SimpleNamespace(kernel32=_K32(), psapi=_K32()), raising=False)
    monkeypatch.setattr(ctypes, "byref", lambda obj, *a, **k: obj, raising=False)
    monkeypatch.setattr(procobs, "_windows_cmdlines", lambda: {})

    assert procobs.list_processes() is None
    state["first"] = True
    assert procobs.list_processes() is None


@pytest.mark.memory_only
def test_preflight_process_scans_portable(monkeypatch):
    from realhw import preflight, procobs

    def _no_pgrep(argv, *args, **kwargs):
        raise AssertionError(f"不得再以 pgrep 掃描行程：{argv}")

    monkeypatch.setattr(preflight, "_run", _no_pgrep)
    listing = [
        {"pid": 1001, "name": "python3", "cmdline": "python3 -m pytest -q tests/"},
        {"pid": 1002, "name": "serialwrapd", "cmdline": "serialwrapd --sw-coexist-x"},
        {"pid": 1003, "name": "testpilot.exe",
         "cmdline": '"C:/tools/testpilot.exe" run serialwrap_regression'},
    ]
    monkeypatch.setattr(procobs, "list_processes", lambda: listing)
    assert preflight._other_pytest() is True
    assert any("sw-coexist" in row for row in preflight._leaked_daemons())
    assert any("testpilot.exe" in row for row in preflight._external_testpilot())

    # 無法列舉行程 → 三者都必須回報「證據不足」，不得視為乾淨
    monkeypatch.setattr(procobs, "list_processes", lambda: None)

    class _Sw:
        def run(self, *args, **kwargs):
            return {"checks": [{"ok": True}]}

        def sessions(self):
            return [{"com": "COM0", "state": "READY"}]

    monkeypatch.setattr(preflight, "_git_behind", lambda repo_root: 0)
    monkeypatch.setattr(preflight, "_tools_missing", lambda cfg: [])
    monkeypatch.setattr(preflight, "_state_polluted", lambda *a, **k: False)
    cfg = {"boards": [{"com": "COM0", "serial": "S0"}], "usbipd_exe": "/x/usbipd.exe"}
    checks = preflight.collect(cfg, _Sw(), "/nonexistent-repo", benchlock_ok=True, win=None)
    ok, problems = preflight.evaluate(checks)
    assert ok is False
    assert any("process_scan_unavailable" in p for p in problems)


@pytest.mark.memory_only
def test_process_scans_do_not_treat_unknown_relevant_processes_as_clean(monkeypatch):
    from realhw import preflight, procobs

    monkeypatch.setattr(procobs, "list_processes", lambda: [
        {"pid": 1001, "name": "testpilot.exe", "cmdline": None},
        {"pid": 1002, "name": "pytest.exe", "cmdline": None},
    ])
    assert "process_scan_unavailable" in preflight._external_testpilot()
    assert preflight._other_pytest() is True
