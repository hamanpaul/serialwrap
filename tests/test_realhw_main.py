"""realhw CLI 入口單測（不碰 live）。"""
from __future__ import annotations

import inspect
import json
import os
import re
import sys
from types import SimpleNamespace

from realhw import __main__ as realhw_main
from realhw import preflight
import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


def test_main_closes_benchlock_fd_on_success(monkeypatch, tmp_path):
    calls: list[object] = []

    monkeypatch.setattr(sys, "argv", ["realhw", "--report-dir", str(tmp_path)])
    monkeypatch.setattr(realhw_main.harness, "load_cfg", lambda *a, **k: {
        "boards": [{"com": "COM0"}],
        "tmux_prefix": "realhw",
        "usbipd_exe": "/tmp/usbipd.exe",
    })
    monkeypatch.setattr(realhw_main.harness, "select_cases", lambda registry, **kwargs: [])
    monkeypatch.setattr(realhw_main.harness, "run_cases", lambda *args, **kwargs: [])
    monkeypatch.setattr(realhw_main.harness, "write_reports",
                        lambda report_dir, meta, results, hints: calls.append(("write_reports", report_dir, meta)))
    monkeypatch.setattr(realhw_main.preflight, "bench_lock_path", lambda *a, **k: tmp_path / "bench.lock")
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
                return {"pid": 77, "version": "serialwrap 0.2.3"}
            return {}

    monkeypatch.setattr(realhw_main.drivers, "SwCli", lambda *a, **k: _Sw())
    monkeypatch.setattr(realhw_main.drivers, "WinSwCli", lambda exe: SimpleNamespace())
    monkeypatch.setattr(realhw_main.drivers, "TmuxCtl", lambda prefix: SimpleNamespace())
    monkeypatch.setattr(realhw_main.drivers, "Usbipd", lambda exe: SimpleNamespace())
    monkeypatch.setattr(realhw_main.drivers, "Systemd", lambda: SimpleNamespace())

    rc = realhw_main.main()

    assert rc == 0
    assert ("close", 123) in calls


# --------------------------------------------------------------------------- #
# P2-T14 red：pinned CLI、版本 gate、meta 欄位、argv 介面
# --------------------------------------------------------------------------- #
def _wire_main(monkeypatch, tmp_path, *, cli_version: str, daemon_version: str, pid: int = 77):
    """以替身跑完整 main 的 preflight＋報告路徑（真的 collect／evaluate，I/O 全部替身）。"""
    exe = tmp_path / "bin" / "serialwrap"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"cli-binary")
    exe.chmod(0o755)
    cfg = {"boards": [{"com": "COM0", "serial": "S0"}], "tmux_prefix": "realhw",
           "usbipd_exe": "/tmp/usbipd.exe", "win_serialwrap_exe": "",
           "serialwrap_exe": str(exe), "human_frontend": "auto", "bench_lock_path": ""}

    class _Sw:
        def __init__(self, *a, **k):
            pass

        def run(self, *args, **kwargs):
            if args == ("--version",):
                return {"_raw": cli_version}
            if args == ("daemon", "status"):
                return {"ok": True, "running": True, "pid": pid, "version": daemon_version,
                        "wal": {"healthy": True}}
            if args == ("doctor",):
                return {"ok": True, "checks": [{"name": "x", "ok": True}]}
            return {}

        def sessions(self):
            return [{"com": "COM0", "state": "READY"}]

        def session(self, com):
            return {"state": "READY"}

        def wait_state(self, com, want, timeout_s, poll_s=2.0):
            return True

    lock_fd = os.open(os.devnull, os.O_RDONLY)
    monkeypatch.setattr(realhw_main.harness, "load_cfg", lambda *a, **k: dict(cfg))
    monkeypatch.setattr(realhw_main.harness, "select_cases", lambda registry, **kwargs: [])
    monkeypatch.setattr(realhw_main.harness, "run_cases", lambda *a, **k: [])
    monkeypatch.setattr(realhw_main.preflight, "bench_lock_path", lambda *a, **k: tmp_path / "bench.lock")
    monkeypatch.setattr(realhw_main.preflight, "acquire_benchlock", lambda path: lock_fd)
    monkeypatch.setattr(preflight, "_git_behind", lambda repo_root: 0)
    monkeypatch.setattr(preflight, "_tools_missing", lambda cfg: [])
    monkeypatch.setattr(preflight, "_leaked_daemons", lambda: [])
    monkeypatch.setattr(preflight, "_other_pytest", lambda: False)
    monkeypatch.setattr(preflight, "_state_polluted", lambda *a, **k: False)
    monkeypatch.setattr(preflight, "_external_testpilot", lambda: [])
    monkeypatch.setattr(preflight, "collect_capabilities",
                        lambda sw: preflight.Capabilities(True, cli_version, True))
    monkeypatch.setattr(realhw_main.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout="", returncode=0, stderr=""))
    monkeypatch.setattr(realhw_main.drivers, "SwCli", _Sw)
    monkeypatch.setattr(realhw_main.drivers, "WinSwCli",
                        lambda exe: SimpleNamespace(available=lambda: False, held_devices=lambda: []))
    monkeypatch.setattr(realhw_main.drivers, "TmuxCtl", lambda prefix: SimpleNamespace())
    monkeypatch.setattr(realhw_main.drivers, "Usbipd", lambda exe: SimpleNamespace())
    monkeypatch.setattr(realhw_main.drivers, "Systemd", lambda: SimpleNamespace(main_pid=lambda: 0))
    return exe, cfg


def _call_main(monkeypatch, argv: list[str]) -> int:
    """優先以新簽章 main(argv) 呼叫（P2-T14 介面）；舊簽章時退回 sys.argv，讓語意斷言仍會執行並顯示真正缺口。"""
    if "argv" in inspect.signature(realhw_main.main).parameters:
        return realhw_main.main(argv)
    monkeypatch.setattr(sys, "argv", ["realhw", *argv])
    return realhw_main.main()


def _read_report_meta(report_dir) -> dict:
    return json.loads((report_dir / "report.json").read_text(encoding="utf-8"))["meta"]


def test_realhw_main_refuses_version_mismatch(monkeypatch, tmp_path):
    _wire_main(monkeypatch, tmp_path, cli_version="serialwrap 0.3.2", daemon_version="0.3.1")
    report_dir = tmp_path / "rep"

    rc = _call_main(monkeypatch, ["--tier", "p0", "--report-dir", str(report_dir)])

    assert rc == 2
    meta = _read_report_meta(report_dir)
    assert any("#154 gate" in p for p in meta["preflight_notes"])


def test_meta_started_at_utc(monkeypatch, tmp_path):
    _wire_main(monkeypatch, tmp_path, cli_version="serialwrap 0.3.2", daemon_version="0.3.2",
               pid=os.getpid())
    report_dir = tmp_path / "rep"

    rc = _call_main(monkeypatch, ["--tier", "p0", "--report-dir", str(report_dir)])

    assert rc == 0
    meta = _read_report_meta(report_dir)
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?\+00:00$", meta["started_at_utc"])
    assert meta["cli_identity"]["pinned"] is True
    assert meta["contract_revision"] is None or re.fullmatch(r"[0-9a-f]{40}", meta["contract_revision"])
    assert "cases_yaml_sha256" in meta
    daemon = meta["daemon_identity"]
    assert daemon["pid"] == os.getpid()
    assert {"version", "image_path", "image_sha256"} <= set(daemon)


def test_main_argv_default_uses_sys_argv(monkeypatch, tmp_path):
    params = inspect.signature(realhw_main.main).parameters
    assert "argv" in params and params["argv"].default is None

    _wire_main(monkeypatch, tmp_path, cli_version="serialwrap 0.3.2", daemon_version="0.3.2")
    report_dir = tmp_path / "rep"
    monkeypatch.setattr(sys, "argv", ["realhw", "--tier", "p0", "--report-dir", str(report_dir)])

    assert realhw_main.main() == 0
    assert (report_dir / "report.json").is_file()


def test_main_loads_explicit_config_argument(monkeypatch, tmp_path):
    config = tmp_path / "docker-config.json"
    config.write_text("{}\n", encoding="utf-8")
    loaded = []

    def load_cfg(path=None, **kwargs):
        loaded.append(path)
        return {}

    monkeypatch.setattr(realhw_main.harness, "load_cfg", load_cfg)
    assert realhw_main.main(["--config", str(config), "--tier", "p0", "--list"]) == 0
    assert loaded == [config]
