"""#122 preflight 判定邏輯單測——吃注入的檢查結果，不碰 live。"""
from __future__ import annotations

import os
from types import SimpleNamespace

from realhw import preflight


def _checks(**over):
    base = dict(git_behind=0, doctor_ok=True, boards_ready=["COM0", "COM1"],
                boards_expected=["COM0", "COM1"], tools_missing=[],
                leaked_daemons=[], other_pytest=False, state_polluted=False,
                benchlock_ok=True, external_testpilot=(),
                win_daemon_present=False, win_daemon_holds=())
    base.update(over)
    return preflight.Checks(**base)


def test_all_green_passes():
    ok, problems = preflight.evaluate(_checks())
    assert ok and problems == []


def test_missing_tool_fails():
    ok, problems = preflight.evaluate(_checks(tools_missing=["tmux"]))
    assert not ok and any("tmux" in p for p in problems)


def test_other_pytest_fails_with_mutex_reason():
    ok, problems = preflight.evaluate(_checks(other_pytest=True))
    assert not ok and any("live guard" in p for p in problems)


def test_boards_not_ready_fails():
    ok, problems = preflight.evaluate(_checks(boards_ready=["COM0"]))
    assert not ok and any("COM1" in p for p in problems)


def test_git_behind_warns_but_passes():
    ok, problems = preflight.evaluate(_checks(git_behind=3))
    assert ok and any("落後" in p for p in problems)  # 警告仍列出但不擋


def test_parse_version():
    assert preflight.parse_version("serialwrap 0.2.3") == (0, 2, 3)
    assert preflight.parse_version("0.10.1") == (0, 10, 1)
    assert preflight.parse_version("") is None
    assert preflight.parse_version("usage: serialwrap ...") is None


def test_missing_capabilities_all_present():
    caps = preflight.Capabilities(remote_capability=True,
                                  deployed_version="serialwrap 0.2.3", docker=True)
    assert preflight.missing_capabilities(caps) == {}


def test_missing_capabilities_maps_reason_codes():
    caps = preflight.Capabilities(remote_capability=False,
                                  deployed_version="serialwrap 0.2.2", docker=False)
    assert preflight.missing_capabilities(caps) == {
        "remote_capability": "remote_capability_missing",
        "deployed_recent": "deployed_daemon_stale",
        "docker": "docker_unavailable",
    }


def test_missing_capabilities_unparseable_version_is_stale():
    caps = preflight.Capabilities(remote_capability=True, deployed_version="", docker=True)
    assert preflight.missing_capabilities(caps) == {"deployed_recent": "deployed_daemon_stale"}


def test_collect_capabilities_reads_remote_version_and_docker(monkeypatch):
    class _Sw:
        def run(self, *args):
            if args == ("remote", "status"):
                return {"ok": True}
            if args == ("--version",):
                return {"_raw": "serialwrap 0.2.3"}
            raise AssertionError(args)

    monkeypatch.setattr(preflight.shutil, "which",
                        lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(preflight, "_run",
                        lambda argv, timeout=30.0: SimpleNamespace(returncode=0, stdout="", stderr=""))
    caps = preflight.collect_capabilities(_Sw())
    assert caps == preflight.Capabilities(True, "serialwrap 0.2.3", True)


def test_benchlock_mutual_exclusion(tmp_path):
    lock = tmp_path / "bench.lock"
    fd1 = preflight.acquire_benchlock(lock)
    assert fd1 is not None
    assert preflight.acquire_benchlock(lock) is None
    os.close(fd1)
    fd2 = preflight.acquire_benchlock(lock)
    assert fd2 is not None
    os.close(fd2)


def test_benchlock_failure_refuses_suite():
    ok, problems = preflight.evaluate(_checks(benchlock_ok=False))
    assert not ok and any("bench.lock" in p for p in problems)


def test_external_testpilot_refuses_suite():
    ok, problems = preflight.evaluate(_checks(external_testpilot=("1234 testpilot run wifi_llapi",)))
    assert not ok and any("testpilot" in p for p in problems)


def test_external_testpilot_ignores_own_pid(monkeypatch):
    me = os.getpid()
    monkeypatch.setattr(
        preflight,
        "_run",
        lambda argv, timeout=30.0: SimpleNamespace(
            returncode=0,
            stdout=(
                f"{me} testpilot run serialwrap_reliability --case p0-doctor\n"
                "4321 testpilot run wifi_llapi --case smoke\n"
            ),
            stderr="",
        ),
    )
    assert preflight._external_testpilot() == ["4321 testpilot run wifi_llapi --case smoke"]


def test_boards_missing_attributed_to_windows_daemon():
    ok, problems = preflight.evaluate(_checks(
        boards_ready=["COM0"], win_daemon_present=True, win_daemon_holds=("COM1",)))
    assert not ok
    joined = "\n".join(problems)
    assert "windows_daemon_holds_device" in joined
    assert "COM1" in joined


def test_boards_missing_without_windows_attribution_unchanged():
    ok, problems = preflight.evaluate(_checks(boards_ready=["COM0"]))
    assert not ok and any("COM1" in p and "windows_daemon" not in p for p in problems)
