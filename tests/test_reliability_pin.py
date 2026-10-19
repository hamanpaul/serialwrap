"""P2-T14 red：reliability plugin 的 serialwrap_exe pin、版本 gate 與 testbed 鍵。

對應 docs/rust-refactor/plans/03-stage0-test-infra.md Task 14 T14-1。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "reliability"))

pytestmark = [pytest.mark.core_portable]


@pytest.mark.memory_only
def test_reliability_pin_and_gate(tmp_path, monkeypatch):
    from realhw import drivers, preflight
    from serialwrap_reliability import core

    exe = tmp_path / "serialwrap"
    exe.write_bytes(b"cli")
    exe.chmod(0o755)
    cfg = {
        "boards": [{"com": "COM0", "serial": "S0"}, {"com": "COM1", "serial": "S1"}],
        "serialwrap_exe": str(exe),
        "bench_lock_path": str(tmp_path / "bench.lock"),
        "human_frontend": "auto",
        "tmux_prefix": "realhw",
        "usbipd_exe": "/x/usbipd.exe",
        "win_serialwrap_exe": "",
        "timeouts": {"ready_wait_s": 180, "reboot_wait_s": 300, "human_active_window_s": 60},
        "longrun": {"snapshot_interval_s": 300, "agent_workers": 4},
    }

    # (1) build_ctx 的 sw 必須綁定 pinned 執行檔（不得走 PATH）
    ctx = core.build_ctx(cfg, tmp_path / "rep")
    bound = (getattr(ctx.sw, "exe", None), getattr(ctx.sw, "_exe", None))
    assert str(exe) in bound

    # (2) 版本不齊（CLI 0.3.2／daemon 0.3.1）→ run_preflight ok False 且 problems 含 #154 gate
    def _dispatch(self, *args, timeout=30.0):
        if args == ("--version",):
            return {"_raw": "serialwrap 0.3.2", "_rc": 0, "_stderr": ""}
        if args == ("daemon", "status"):
            return {"ok": True, "running": True, "pid": 77, "version": "0.3.1",
                    "wal": {"healthy": True}, "_rc": 0, "_stderr": ""}
        if args == ("doctor",):
            return {"ok": True, "checks": [{"name": "x", "ok": True}], "_rc": 0, "_stderr": ""}
        if args[:2] == ("session", "list"):
            return {"sessions": [{"com": "COM0", "state": "READY"}, {"com": "COM1", "state": "READY"}],
                    "_rc": 0, "_stderr": ""}
        return {"_rc": 0, "_stderr": ""}

    monkeypatch.setattr(drivers.SwCli, "run", _dispatch)
    monkeypatch.setattr(preflight, "acquire_benchlock", lambda path: 9)
    monkeypatch.setattr(preflight, "bench_lock_path", lambda *a, **k: tmp_path / "bench.lock")
    # 全部 I/O 替身：不碰 git、docker、pgrep、live state
    monkeypatch.setattr(preflight, "_run",
                        lambda argv, timeout=30.0: SimpleNamespace(returncode=1, stdout="", stderr=""))
    monkeypatch.setattr(preflight, "_tools_missing", lambda cfg: [])
    monkeypatch.setattr(preflight, "_leaked_daemons", lambda: [])
    monkeypatch.setattr(preflight, "_other_pytest", lambda: False)
    monkeypatch.setattr(preflight, "_external_testpilot", lambda: [])
    monkeypatch.setattr(preflight, "_state_polluted", lambda *a, **k: False)
    monkeypatch.setattr(preflight, "_git_behind", lambda repo_root: 0)
    monkeypatch.setattr(preflight, "collect_capabilities",
                        lambda sw: preflight.Capabilities(True, "serialwrap 0.3.1", True))

    result = core.run_preflight(cfg)
    assert result["ok"] is False
    assert any("#154 gate" in p for p in result["problems"])


@pytest.mark.memory_only
def test_reliability_testbed_keys():
    from serialwrap_reliability import testbed_loader

    raw = {"testbed": {
        "serialwrap_reliability": {
            "serialwrap_exe": "/opt/sw/bin/serialwrap",
            "bench_lock_path": "/var/lock/bench.lock",
            "human_frontend": "telnet",
            "tmux_prefix": "realhw",
            "usbipd_exe": "/x/usbipd.exe",
            "win_serialwrap_exe": "",
        },
        "devices": {"d1": {"selector": "COM0", "serial": "S0", "platform": "prpl"}},
    }}
    cfg = testbed_loader.testbed_to_cfg(raw)
    assert cfg["serialwrap_exe"] == "/opt/sw/bin/serialwrap"
    assert cfg["bench_lock_path"] == "/var/lock/bench.lock"
    assert cfg["human_frontend"] == "telnet"
