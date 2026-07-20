"""realhw CLI 入口單測（不碰 live）。"""
from __future__ import annotations

import sys
from types import SimpleNamespace

from realhw import __main__ as realhw_main
from realhw import preflight


def test_main_closes_benchlock_fd_on_success(monkeypatch, tmp_path):
    calls: list[object] = []

    monkeypatch.setattr(sys, "argv", ["realhw", "--report-dir", str(tmp_path)])
    monkeypatch.setattr(realhw_main.harness, "load_cfg", lambda: {
        "boards": [{"com": "COM0"}],
        "tmux_prefix": "realhw",
        "usbipd_exe": "/tmp/usbipd.exe",
    })
    monkeypatch.setattr(realhw_main.harness, "select_cases", lambda registry, **kwargs: [])
    monkeypatch.setattr(realhw_main.harness, "run_cases", lambda *args, **kwargs: [])
    monkeypatch.setattr(realhw_main.harness, "write_reports",
                        lambda report_dir, meta, results, hints: calls.append(("write_reports", report_dir, meta)))
    monkeypatch.setattr(realhw_main.preflight, "bench_lock_path", lambda: tmp_path / "bench.lock")
    monkeypatch.setattr(realhw_main.preflight, "acquire_benchlock", lambda path: 123)
    monkeypatch.setattr(realhw_main.preflight, "collect",
                        lambda cfg, sw, repo_root, **kwargs: preflight.Checks(
                            git_behind=0, doctor_ok=True, boards_ready=["COM0"],
                            boards_expected=["COM0"], tools_missing=[], leaked_daemons=[],
                            other_pytest=False, state_polluted=False, benchlock_ok=True,
                            external_testpilot=(), win_daemon_present=False, win_daemon_holds=()))
    monkeypatch.setattr(realhw_main.preflight, "evaluate", lambda checks: (True, []))
    monkeypatch.setattr(realhw_main.preflight, "collect_capabilities",
                        lambda sw: preflight.Capabilities(True, "serialwrap 0.2.3", True))
    monkeypatch.setattr(realhw_main.subprocess, "run",
                        lambda argv, capture_output=True, text=True: SimpleNamespace(stdout="abc123\n"))
    monkeypatch.setattr(realhw_main, "os", SimpleNamespace(close=lambda fd: calls.append(("close", fd))),
                        raising=False)

    class _Sw:
        def run(self, *args, **kwargs):
            if args == ("--version",):
                return {"_raw": "serialwrap 0.2.3"}
            if args == ("daemon", "status"):
                return {"pid": 77}
            return {}

    monkeypatch.setattr(realhw_main.drivers, "SwCli", lambda: _Sw())
    monkeypatch.setattr(realhw_main.drivers, "WinSwCli", lambda exe: SimpleNamespace())
    monkeypatch.setattr(realhw_main.drivers, "TmuxCtl", lambda prefix: SimpleNamespace())
    monkeypatch.setattr(realhw_main.drivers, "Usbipd", lambda exe: SimpleNamespace())
    monkeypatch.setattr(realhw_main.drivers, "Systemd", lambda: SimpleNamespace())

    rc = realhw_main.main()

    assert rc == 0
    assert ("close", 123) in calls
