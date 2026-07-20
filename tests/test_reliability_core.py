"""Phase 2 plugin core（serialwrap_reliability.core）純邏輯單測——不 import testpilot、不碰 live。"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "reliability"))

from serialwrap_reliability import core  # noqa: E402


CFG = {
    "boards": [
        {"com": "COM0", "alias": "dut-prpl", "serial": "S0", "busid": "8-1", "platform": "prpl"},
        {"com": "COM1", "alias": "sta-prpl", "serial": "S1", "busid": "8-2", "platform": "brcm"},
    ],
    "tmux_prefix": "realhw",
    "usbipd_exe": "/mnt/c/x/usbipd.exe",
    "timeouts": {"ready_wait_s": 180, "reboot_wait_s": 300, "human_active_window_s": 60},
    "longrun": {"snapshot_interval_s": 300, "agent_workers": 4},
    "duration_s": 900,
}


def _mk_case(id: str, tier: str = "p0", destructive: bool = False,
             requires: tuple[str, ...] = (), hints: tuple[str, ...] = ()):
    core.ensure_realhw_importable()
    from realhw import harness

    return harness.Case(
        id=id,
        tier=tier,
        title=f"title-{id}",
        run=lambda ctx: harness.CaseResult("PASS"),
        destructive=destructive,
        requires=tuple(requires),
        hints=tuple(hints),
    )


def test_repo_root_locates_worktree():
    assert core.REPO_ROOT == REPO_ROOT
    assert (core.REPO_ROOT / "realhw" / "harness.py").is_file()


def test_resolve_repo_root_rejects_non_editable_layout(tmp_path):
    fake = tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "serialwrap_reliability" / "core.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="editable 安裝"):
        core.resolve_repo_root(fake)


def test_ensure_realhw_importable_idempotent():
    got = core.ensure_realhw_importable()
    assert got == REPO_ROOT
    import realhw  # noqa: F401

    before = list(sys.path)
    core.ensure_realhw_importable()
    assert sys.path.count(str(REPO_ROOT)) == before.count(str(REPO_ROOT))


def test_load_registry_populated_unique():
    registry = core.load_registry()
    ids = [case.id for case in registry]
    assert len(ids) == len(set(ids))
    assert len(ids) >= 29
    assert any(case.id == "p0-doctor" for case in registry)


def test_core_modules_do_not_import_testpilot():
    pkg = REPO_ROOT / "reliability" / "serialwrap_reliability"
    for name in ("__init__.py", "core.py"):
        text = (pkg / name).read_text(encoding="utf-8")
        bad = [line for line in text.splitlines() if re.match(r"\s*(import|from)\s+testpilot", line)]
        assert not bad, f"{name} 不得 import testpilot：{bad}"


@pytest.mark.parametrize("duration_s,interval_s,n", [(900, 300, 3), (60, 300, 1), (0, 300, 1)])
def test_synth_longrun_steps_count(duration_s, interval_s, n):
    steps = core.synth_longrun_steps(duration_s, interval_s)
    assert len(steps) == n
    assert steps[0]["id"] == "checkpoint-001"
    for step in steps:
        assert {"id", "action", "target"} <= set(step)
        assert step["action"] == "longrun_checkpoint"
        assert step["target"] == "bench"


def test_case_to_dict_single_step_schema():
    got = core.case_to_dict(_mk_case("p0-x", requires=("two_boards",), hints=("h1",)), CFG)
    assert {"id", "name", "topology", "steps", "pass_criteria", "metadata"} <= set(got)
    assert set(got["topology"]["devices"]) == {"COM0", "COM1"}
    assert got["steps"] == [{"id": "exec", "action": "run_case", "target": "bench"}]
    assert got["pass_criteria"] == ["realhw_case_verdict"]
    assert got["metadata"] == {
        "tier": "p0",
        "destructive": False,
        "requires": ["two_boards"],
        "hints": ["h1"],
    }


def test_case_to_dict_longrun_synthesizes_checkpoints():
    got = core.case_to_dict(_mk_case("lr-mixed", tier="longrun"), CFG)
    assert [step["id"] for step in got["steps"]] == [
        "checkpoint-001",
        "checkpoint-002",
        "checkpoint-003",
    ]


def test_case_to_dict_missing_com_fails_loud():
    bad_cfg = dict(CFG)
    bad_cfg["boards"] = [{"alias": "dut-only"}]
    with pytest.raises(ValueError, match=r"boards\[0\].*com"):
        core.case_to_dict(_mk_case("bad"), bad_cfg)


def test_case_to_dict_duplicate_com_fails_loud():
    bad_cfg = dict(CFG)
    bad_cfg["boards"] = [
        {"com": "COM0", "alias": "dut"},
        {"com": "COM0", "alias": "sta"},
    ]
    with pytest.raises(ValueError, match=r"COM0.*重複|COM0.*碰撞"):
        core.case_to_dict(_mk_case("dup"), bad_cfg)


def test_build_case_dicts_preserves_registry_order():
    got = core.build_case_dicts([_mk_case("a"), _mk_case("b"), _mk_case("c")], CFG)
    assert [case["id"] for case in got] == ["a", "b", "c"]


def test_filter_for_run_default_excludes_destructive():
    cases = core.build_case_dicts(
        [_mk_case("a"), _mk_case("b", destructive=True), _mk_case("c")], CFG
    )
    got = core.filter_for_run(cases, set())
    assert [case["id"] for case in got] == ["a", "c"]


def test_filter_for_run_explicit_id_includes_destructive():
    cases = core.build_case_dicts([_mk_case("a"), _mk_case("b", destructive=True)], CFG)
    got = core.filter_for_run(cases, {"b"})
    assert [case["id"] for case in got] == ["b"]


# ---------------------------------------------------------------- Task 4：判決抄寫與執行編排
def test_result_to_dict_copies_case_result_shape():
    core.ensure_realhw_importable()
    from realhw import harness

    result = harness.CaseResult(
        "FAIL",
        reason="oops",
        evidence={"a": "x.txt"},
        duration_s=1.25,
        category="test",
        reason_code="boom",
    )
    assert core.result_to_dict(result) == {
        "verdict": "FAIL",
        "reason": "oops",
        "category": "test",
        "reason_code": "boom",
        "evidence": {"a": "x.txt"},
        "duration_s": 1.25,
    }


def test_failure_payload_pass_is_none():
    assert core.failure_payload({"verdict": "PASS"}) is None


def test_failure_payload_fail_copies_classification():
    payload = core.failure_payload({
        "verdict": "FAIL", "reason": "fan-out 斷線", "category": "test",
        "reason_code": "console_fanout_lost",
        "evidence": {"pane": "p1-con/pane.txt"}, "duration_s": 1.0,
    })
    assert payload == {
        "category": "test",
        "reason_code": "console_fanout_lost",
        "comment": "fan-out 斷線",
        "evidence": ["p1-con/pane.txt"],
        "metadata": {"realhw_verdict": "FAIL"},
    }


def test_failure_payload_runtime_skip_defaults_environment():
    payload = core.failure_payload({"verdict": "SKIP", "reason": "base64 缺", "category": "",
                                    "reason_code": "base64_missing", "evidence": {}})
    assert payload["category"] == "environment"


def test_failure_payload_fail_without_category_stays_empty():
    payload = core.failure_payload({"verdict": "FAIL", "reason": "boom", "category": "",
                                    "reason_code": "uncaught_exception", "evidence": {}})
    assert payload["category"] == ""


def test_runtime_skip_broken_by_and_missing_caps():
    meta_dep = {"requires": ["two_boards"], "destructive": False}
    assert core.runtime_skip(meta_dep, {}, "p1-hp-cycle") == (
        "broken_by:p1-hp-cycle", "前置不滿足（p1-hp-cycle 後板卡未恢復）")
    meta_rm = {"requires": ["docker"], "destructive": False}
    assert core.runtime_skip(meta_rm, {"docker": "docker_unavailable"}, None) == (
        "docker_unavailable", "能力缺項：docker")
    assert core.runtime_skip(meta_rm, {}, None) is None
    assert core.runtime_skip({"requires": [], "destructive": False}, {}, "x") is None


def test_runtime_skip_consumes_missing_capabilities_output():
    """整合斷言（防靜默失效）：Phase 1 missing_capabilities 的實際輸出必須能直接餵 runtime_skip。"""
    core.ensure_realhw_importable()
    from realhw import preflight

    caps = preflight.Capabilities(remote_capability=False, deployed_version="", docker=False)
    missing = preflight.missing_capabilities(caps)
    assert core.runtime_skip({"requires": ["remote_capability"], "destructive": False},
                             missing, None) == (
        "remote_capability_missing", "能力缺項：remote_capability")
    assert core.runtime_skip({"requires": ["docker"], "destructive": False},
                             missing, None) == (
        "docker_unavailable", "能力缺項：docker")


def test_make_skip_result_shape():
    r = core.make_skip_result("docker_unavailable", "能力缺項：docker")
    assert (r.verdict, r.category, r.reason_code) == ("SKIP", "environment", "docker_unavailable")


def test_build_ctx_injects_win_cli(monkeypatch, tmp_path):
    core.ensure_realhw_importable()
    from realhw import drivers

    class FakeSwCli:
        pass

    class FakeTmuxCtl:
        def __init__(self, prefix: str) -> None:
            self.prefix = prefix

    class FakeUsbipd:
        def __init__(self, exe: str) -> None:
            self.exe = exe

    class FakeSystemd:
        pass

    class FakeWinSwCli:
        def __init__(self, exe: str) -> None:
            self.exe = exe

    monkeypatch.setattr(drivers, "SwCli", FakeSwCli)
    monkeypatch.setattr(drivers, "TmuxCtl", FakeTmuxCtl)
    monkeypatch.setattr(drivers, "Usbipd", FakeUsbipd)
    monkeypatch.setattr(drivers, "Systemd", FakeSystemd)
    monkeypatch.setattr(drivers, "WinSwCli", FakeWinSwCli)
    ctx = core.build_ctx({
        "tmux_prefix": "realhw",
        "usbipd_exe": "/x/usbipd.exe",
        "win_serialwrap_exe": "/mnt/c/serialwrap.exe",
    }, tmp_path)
    assert isinstance(ctx.sw, FakeSwCli)
    assert isinstance(ctx.tmux, FakeTmuxCtl) and ctx.tmux.prefix == "realhw"
    assert isinstance(ctx.usbipd, FakeUsbipd) and ctx.usbipd.exe == "/x/usbipd.exe"
    assert isinstance(ctx.systemd, FakeSystemd)
    assert isinstance(ctx.win, FakeWinSwCli) and ctx.win.exe == "/mnt/c/serialwrap.exe"
    assert ctx.report_dir == tmp_path and ctx.case_dir == tmp_path


def test_run_case_blackbox_pass_and_uncaught(monkeypatch, tmp_path):
    core.ensure_realhw_importable()
    from realhw import harness

    ok = harness.Case(id="fake-ok", tier="p0", title="ok",
                      run=lambda ctx: harness.CaseResult("PASS"))

    def _boom(ctx):
        raise RuntimeError("爆")

    bad = harness.Case(id="fake-bad", tier="p0", title="bad", run=_boom)
    monkeypatch.setattr(harness, "REGISTRY", [ok, bad])
    ctx = SimpleNamespace(report_dir=tmp_path, case_dir=tmp_path)

    r1 = core.run_case_blackbox("fake-ok", ctx)
    assert r1.verdict == "PASS" and r1.duration_s >= 0.0
    assert ctx.case_dir == tmp_path / "fake-ok"

    r2 = core.run_case_blackbox("fake-bad", ctx)
    assert (r2.verdict, r2.category, r2.reason_code) == ("FAIL", "", "uncaught_exception")

    r3 = core.run_case_blackbox("no-such", ctx)
    assert (r3.verdict, r3.category, r3.reason_code) == (
        "FAIL", "configuration", "invalid_case_config")


class _FakeSw:
    """恢復流程 fake：第一輪回報非 READY、恢復後 READY。"""

    def __init__(self, initial_state: str) -> None:
        self.state = initial_state
        self.calls: list[tuple[str, ...]] = []

    def session(self, com: str) -> dict:
        return {"com": com, "state": self.state}

    def run(self, *args: str, **kw) -> dict:
        self.calls.append(args)
        self.state = "READY"
        return {"ok": True}

    def wait_state(self, com: str, want: str, *, timeout_s: float, poll_s: float = 2.0) -> bool:
        return self.state == want


def test_recover_boards_dispatches_state_aware_verb(monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda s: None)
    sw = _FakeSw("ATTACHED")
    ctx = SimpleNamespace(sw=sw)
    left = core.recover_boards(ctx, ["COM0"])
    assert left == []
    assert sw.calls == [("session", "recover", "--selector", "COM0")]


def test_recover_boards_ready_is_noop():
    sw = _FakeSw("READY")
    assert core.recover_boards(SimpleNamespace(sw=sw), ["COM0", "COM1"]) == []
    assert sw.calls == []


def test_sweep_tmux_kills_only_prefix(monkeypatch):
    ran: list[list[str]] = []

    def fake_run(argv, capture_output=True, text=True):
        ran.append(list(argv))
        if argv[:2] == ["tmux", "ls"]:
            return SimpleNamespace(stdout="realhw-p0con-1\nother-sess\nrealhw-lrhuman-2\n",
                                   returncode=0)
        return SimpleNamespace(stdout="", returncode=0)

    monkeypatch.setattr(core.subprocess, "run", fake_run)
    killed = core.sweep_tmux("realhw")
    assert killed == ["realhw-p0con-1", "realhw-lrhuman-2"]
    assert ["tmux", "kill-session", "-t", "other-sess"] not in ran


def test_checkpoint_index():
    assert core.checkpoint_index("checkpoint-007", fallback=9) == 7
    assert core.checkpoint_index("weird", fallback=9) == 9


def test_longrun_runner_join_and_snapshots(tmp_path):
    snaps = tmp_path / "snapshots.ndjson"
    snaps.write_text('{"t":0}\n{"t":300}\n', encoding="utf-8")
    sentinel = object()
    runner = core.LongrunRunner(run_fn=lambda: sentinel, snapshots_path=snaps, duration_s=0)
    runner.start()
    progress = runner.wait_checkpoint(1, 1)
    assert progress == {"checkpoint": 1, "total": 1, "snapshots_seen": 2, "finished": True}
    assert runner.result() is sentinel


def test_longrun_runner_skipped_mode():
    r = core.make_skip_result("docker_unavailable", "能力缺項：docker")
    runner = core.LongrunRunner.skipped(r)
    progress = runner.wait_checkpoint(1, 3)
    assert progress["finished"] is True and progress["snapshots_seen"] == 0
    assert runner.result() is r


def test_longrun_runner_result_joins_even_if_thread_already_finished():
    class FakeThread:
        def __init__(self) -> None:
            self.joined = False

        def is_alive(self) -> bool:
            return False

        def join(self) -> None:
            self.joined = True

    runner = core.LongrunRunner.skipped("done")
    runner._thread = FakeThread()
    assert runner.result() == "done"
    assert runner._thread.joined is True


def test_run_preflight_acquires_benchlock_and_collects_missing(monkeypatch, tmp_path):
    """防靜默失效雙斷言：benchlock 有被嘗試取鎖並注入 collect；missing_caps 有被收集。"""
    core.ensure_realhw_importable()
    from realhw import preflight

    lockfile = tmp_path / "bench.lock"
    seen: dict = {}
    monkeypatch.setattr(preflight, "bench_lock_path", lambda: lockfile)
    real_acquire = preflight.acquire_benchlock

    def spy_acquire(path):
        seen["acquire_path"] = Path(path)
        return real_acquire(path)

    monkeypatch.setattr(preflight, "acquire_benchlock", spy_acquire)

    def fake_collect(cfg, sw, root, *, benchlock_ok=True, win=None):
        seen["benchlock_ok"] = benchlock_ok
        seen["win_passed"] = win is not None
        return "CHECKS"

    monkeypatch.setattr(preflight, "collect", fake_collect)
    monkeypatch.setattr(preflight, "evaluate", lambda c: (True, []))
    caps = preflight.Capabilities(remote_capability=True,
                                  deployed_version="serialwrap 0.2.3", docker=False)
    monkeypatch.setattr(preflight, "collect_capabilities", lambda sw: caps)

    out = core.run_preflight({"boards": [], "win_serialwrap_exe": ""})
    assert seen["acquire_path"] == lockfile
    assert seen["benchlock_ok"] is True
    assert seen["win_passed"] is True
    assert out["ok"] is True
    assert out["missing_caps"] == {"docker": "docker_unavailable"}
    assert out["deployed_version"] == "serialwrap 0.2.3"
    assert out["benchlock_fd"] is not None


def test_run_preflight_closes_benchlock_on_collect_error(monkeypatch, tmp_path):
    core.ensure_realhw_importable()
    from realhw import preflight

    lockfile = tmp_path / "bench.lock"
    monkeypatch.setattr(preflight, "bench_lock_path", lambda: lockfile)

    def boom(*args, **kwargs):
        raise RuntimeError("collect boom")

    monkeypatch.setattr(preflight, "collect", boom)
    with pytest.raises(RuntimeError, match="collect boom"):
        core.run_preflight({"boards": [], "win_serialwrap_exe": ""})
    fd = preflight.acquire_benchlock(lockfile)
    assert fd is not None
    os.close(fd)


def test_run_preflight_refuses_when_benchlock_held(monkeypatch, tmp_path):
    core.ensure_realhw_importable()
    from realhw import preflight

    lockfile = tmp_path / "bench.lock"
    monkeypatch.setattr(preflight, "bench_lock_path", lambda: lockfile)
    held_fd = preflight.acquire_benchlock(lockfile)
    assert held_fd is not None

    def fake_collect(cfg, sw, root, *, benchlock_ok=True, win=None):
        return {"benchlock_ok": benchlock_ok}

    monkeypatch.setattr(preflight, "collect", fake_collect)
    monkeypatch.setattr(
        preflight, "evaluate",
        lambda c: (c["benchlock_ok"],
                   [] if c["benchlock_ok"] else ["bench 互斥：benchlock 已被持有（拒跑）"]))
    out = core.run_preflight({"boards": [], "win_serialwrap_exe": ""})
    assert out["ok"] is False
    assert out["benchlock_fd"] is None
    assert any("benchlock" in p for p in out["problems"])
    os.close(held_fd)
    assert out["missing_caps"] == {} and out["deployed_version"] == ""
