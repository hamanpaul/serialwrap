"""P2-T14 red：realhw pin 與版本 gate、bench lock、跨 OS 守衛、部署內容報告（R5-F4）。

對應 docs/rust-refactor/plans/03-stage0-test-infra.md Task 14 T14-1。
本檔無模組層平台 marker：每個測試函式各自帶一個平台 marker 與一個資源 marker（P0 T14 稽核規則）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_install(root: Path) -> Path:
    """最小安裝：bin/serialwrap（內容 b"cli"）、旁載 bin/version.dll、release-manifest.json。"""
    bindir = root / "bin"
    bindir.mkdir(parents=True)
    exe = bindir / "serialwrap"
    exe.write_bytes(b"cli")
    exe.chmod(0o755)
    (bindir / "version.dll").write_bytes(b"dll")
    manifest = {"schema_version": "release-manifest.v1",
                "components": {"cli": {"sha256": hashlib.sha256(b"cli").hexdigest()}}}
    (root / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _wire_main(monkeypatch, tmp_path: Path, *, cli_version: str, daemon_version: str,
               pid: int = 77):
    """把 realhw.__main__.main 的所有 I/O 換成替身（不碰 live）。回傳 (exe, cfg)。"""
    from realhw import __main__ as realhw_main
    from realhw import preflight

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
    monkeypatch.setattr(realhw_main.preflight, "bench_lock_path",
                        lambda *a, **k: tmp_path / "bench.lock")
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
                        lambda exe: SimpleNamespace(available=lambda: False,
                                                    held_devices=lambda: []))
    monkeypatch.setattr(realhw_main.drivers, "TmuxCtl", lambda prefix: SimpleNamespace())
    monkeypatch.setattr(realhw_main.drivers, "Usbipd", lambda exe: SimpleNamespace())
    monkeypatch.setattr(realhw_main.drivers, "Systemd",
                        lambda: SimpleNamespace(main_pid=lambda: 0))
    return exe, cfg


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_version_gate_moved_message_identical():
    from realhw import preflight as rp

    assert rp.version_gate("serialwrap 0.3.2", "0.3.3") == (
        "client↔daemon 版本不齊：cli=0.3.2 daemon=0.3.3（#154 gate，suite-refuse）")
    sys.path.insert(0, str(REPO_ROOT / "regression"))
    from serialwrap_regression import preflight as sp

    assert sp.version_gate is rp.version_gate


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_resolve_exe_pinned_and_unpinned(tmp_path, monkeypatch):
    from realhw import preflight

    exe = tmp_path / "serialwrap"
    exe.write_bytes(b"x")
    exe.chmod(0o755)
    digest = hashlib.sha256(b"x").hexdigest()

    path, info = preflight.resolve_serialwrap_exe({"serialwrap_exe": str(exe)})
    assert info["pinned"] is True
    assert info["sha256"] == digest
    assert info["resolved"] == os.path.realpath(exe)
    assert path == os.path.realpath(exe)

    monkeypatch.setattr(preflight.shutil, "which", lambda name: str(exe))
    path2, info2 = preflight.resolve_serialwrap_exe({})
    assert info2["pinned"] is False
    assert info2["configured"] is None
    assert info2["sha256"] == digest


@pytest.mark.core_portable
@pytest.mark.process_rpc
def test_realhw_report_deployed_content(tmp_path, monkeypatch):
    import subprocess

    from realhw import __main__ as realhw_main
    from realhw import deployed_content as dc
    from realhw import harness

    inst = _make_install(tmp_path / "v1")
    # daemon 行程由本測試自行 spawn（sys.executable 子行程）；realhw 只能量測「實際在服務」的映像
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    order: list[str] = []
    try:
        _wire_main(monkeypatch, tmp_path, cli_version="serialwrap 0.3.2",
                   daemon_version="0.3.2", pid=child.pid)
        # 把 measure 與 case／write_reports 的呼叫序記下來
        real_measure = dc.measure

        def _measure(*args, **kwargs):
            order.append("measure")
            return real_measure(*args, **kwargs)

        monkeypatch.setattr(dc, "measure", _measure)
        monkeypatch.setattr(harness, "select_cases", lambda registry, **kwargs: [
            SimpleNamespace(id="fake-case", destructive=False, requires=(), tier="p0",
                            hints=(), title="fake")])

        def _run_cases(cases, ctx, *, boards, missing_caps=None):
            order.append("case")
            return []

        monkeypatch.setattr(harness, "run_cases", _run_cases)
        real_write = harness.write_reports

        def _write(*args, **kwargs):
            order.append("write")
            return real_write(*args, **kwargs)

        monkeypatch.setattr(harness, "write_reports", _write)

        report_dir = tmp_path / "rep"
        rc = realhw_main.main(["--tier", "p0", "--report-dir", str(report_dir)])
        assert rc == 0

        meta = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))["meta"]
        dcn = meta["deployed_content"]
        assert dcn["start"]["bundle_tree_sha256"] == dc.tree_digests(inst)[0]
        assert dcn["end"]["bundle_tree_sha256"] == dc.tree_digests(inst)[0]
        assert dcn["start"]["cli_resolved"] == meta["cli_identity"]["resolved"]
        assert dcn["start"]["cli_sha256"] == meta["cli_identity"]["sha256"]
        assert dcn["daemon_live"][0]["in_install"] is False

        # 呼叫序：start 量測早於第一個 case；end 量測晚於最後一個 case 且早於 write_reports
        assert order == ["measure", "case", "measure", "write"]
    finally:
        child.kill()
        child.wait()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_bench_lock_path_windows_requires_key():
    from pathlib import Path as _Path

    from realhw import preflight as rp

    with pytest.raises(rp.BenchLockConfigError):
        rp.bench_lock_path({}, os_name="nt")
    got = rp.bench_lock_path({"bench_lock_path": "C:/bench/bench.lock"}, os_name="nt")
    assert str(got).replace("\\", "/") == "C:/bench/bench.lock"
    assert isinstance(got, _Path)


@pytest.mark.posix_only
@pytest.mark.memory_only
def test_acquire_benchlock_exclusive_posix(tmp_path):
    from realhw import preflight as rp

    lock = rp.bench_lock_path({"bench_lock_path": str(tmp_path / "bench.lock")})
    fd1 = rp.acquire_benchlock(lock)
    try:
        assert fd1 is not None
        assert rp.acquire_benchlock(lock) is None  # 同一路徑第二次取鎖必須失敗
    finally:
        if fd1 is not None:
            os.close(fd1)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_state_dir_windows(tmp_path, monkeypatch):
    from realhw import preflight as rp

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert rp.resolve_state_dir({}, os_name="nt") == tmp_path / "serialwrap"

    # _state_polluted 讀 resolve_state_dir(cfg) 指向的 state.json（污染哨兵：bindings 值含 /tmp/）
    state_dir = tmp_path / "serialwrap"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps({"bindings": {"COM0": "/tmp/sw-leftover"}}), encoding="utf-8")
    assert rp._state_polluted({"state_dir": str(state_dir)}) is True


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_cross_os_holder_check():
    import subprocess

    from realhw import preflight as rp

    cfg = {"boards": [{"com": "COM0", "serial": "AC01QZT0"}],
           "cross_os_probe_argv": ["fake-other-os", "serialwrap", "daemon", "status"]}
    payload = {"sessions": [{"com": "COM0", "state": "READY",
                             "device_by_id": "USB\\VID_0403+PID_6001+AC01QZT0A"}]}

    def _ok_run(argv, *args, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    def _down_run(argv, *args, **kwargs):
        raise OSError("probe down")

    status, detail = rp.cross_os_holder_check(cfg, ["COM0"], run=_ok_run)
    assert status == "held" and detail

    status, detail = rp.cross_os_holder_check(cfg, ["COM0"], run=_down_run)
    assert status == "unknown" and detail

    status, detail = rp.cross_os_holder_check({"boards": cfg["boards"]}, ["COM0"], run=_ok_run)
    assert status == "unconfigured" and detail is None
