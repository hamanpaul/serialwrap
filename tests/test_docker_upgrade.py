"""OD-33 R2(b)(c)：0.3.2 正式版 → 0.4.0 候選的升級驗證（Linux systemd docker 容器）。

判定邏輯（`scripts/docker/upgrade_checks.py`）以純函式測試：daemon 行程辨識（不得把含 `serialwrapd` 字樣的
shell 指令或 launcher→python 父子算成兩個 daemon）、雙 daemon（two-reader）重疊、設定／profiles 逐位元組保留、
alias／binding 語意相容與整體 verdict（缺項 INSUFFICIENT、任一 FAIL 即 FAIL）。
host runner `scripts/docker/run-upgrade.sh` 以 `--dry-run` 檢查不碰 docker 的命令組成；實跑測試在 docker 可用時
於 OD-16 (5) 的 ubuntu:24.04 systemd 容器中，對 systemd-user 與 on-demand 兩種監管模式各跑一次完整流程。

同檔混合平台與資源（純函式為 core_portable／memory_only；bash runner 與 docker 實跑為 posix_only），
依 Task 14 規則 2 模組層不放 marker，逐一標在測試函式上。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
RUN_UPGRADE = REPO / "scripts" / "docker" / "run-upgrade.sh"
DOCKERFILE = REPO / "tools" / "docker" / "upgrade.Dockerfile"
SYSTEMD_DOCKERFILE = REPO / "tools" / "docker" / "systemd.Dockerfile"
UPGRADE_CONTAINER = REPO / "scripts" / "docker" / "run_upgrade_container.py"

sys.path.insert(0, str(REPO))

from scripts.docker import upgrade_checks as uc  # noqa: E402


def _proc(root: Path, pid: int, argv: list[str], ppid: int = 1) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "stat").write_text(f"{pid} (x) S {ppid} {pid} {pid} 0 -1\n", encoding="utf-8")


# ---------- daemon 行程辨識 ----------

@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("argv", [
    ["/opt/u/.local/share/pipx/venvs/serialwrap/bin/python", "/opt/u/.local/bin/serialwrapd"],
    ["/opt/u/.local/share/serialwrap/versions/0.4.0/py/bin/python3", "-I", "-B",
     "/opt/u/.local/share/serialwrap/versions/0.4.0/py/serialwrapd.py"],
    ["/opt/u/.local/share/serialwrap/versions/0.4.0/bin/serialwrapd"],
    ["python3", "-m", "sw_core.daemon", "--socket", "/run/x.sock"],
])
def test_daemon_argv_recognized(argv):
    assert uc.is_daemon_argv(argv), argv


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("argv", [
    ["bash", "-lc", "serialwrap daemon stop; ls ~/.local/bin/serialwrapd"],
    ["runuser", "-u", "swtest", "--", "/opt/u/.local/bin/serialwrapd"],
    ["/opt/u/.local/bin/serialwrap", "daemon", "start"],
    ["python3", "/opt/sw-driver/run_upgrade_container.py", "--mode", "serialwrapd"],
    ["grep", "serialwrapd"],
    [],
])
def test_non_daemon_argv_not_recognized(argv):
    assert not uc.is_daemon_argv(argv), argv


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_scan_counts_launcher_child_as_one_instance(tmp_path):
    """launcher 以子行程啟動 python daemon（父子皆可辨識為 daemon）只算一個實例；兩個無親子關係者算兩個。"""
    _proc(tmp_path, 10, ["/v/0.4.0/bin/serialwrapd"])
    _proc(tmp_path, 11, ["/v/0.4.0/py/bin/python3", "-I", "-B", "/v/0.4.0/py/serialwrapd.py"], ppid=10)
    _proc(tmp_path, 12, ["bash", "-lc", "echo serialwrapd"])
    assert uc.scan_daemon_instances(tmp_path) == [10]
    _proc(tmp_path, 20, ["/pipx/bin/python", "/opt/u/.local/bin/serialwrapd"])
    assert uc.scan_daemon_instances(tmp_path) == [10, 20]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_scan_tolerates_vanished_process(tmp_path):
    _proc(tmp_path, 30, ["/v/bin/serialwrapd"])
    (tmp_path / "31").mkdir()  # 行程在掃描中途結束：cmdline／stat 已不存在
    (tmp_path / "self").mkdir()
    assert uc.scan_daemon_instances(tmp_path) == [30]


# ---------- 停止後等待舊 daemon 行程樹與 socket 消失（Docker on-demand 預熱競態裁決） ----------

def _proc_full(root: Path, pid: int, argv: list[str], *, ppid: int = 1, start: int = 1000, state: str = "S") -> None:
    """含啟動時間（第 22 欄）的假 /proc/<pid>：供 PID 重用與 zombie 判定。"""
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + (b"\0" if argv else b""))
    rest = [state, str(ppid)] + ["0"] * 17 + [str(start), "0", "0"]
    (d / "stat").write_text(f"{pid} (x y) {' '.join(rest)}\n", encoding="utf-8")


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_daemon_process_tree_includes_launcher_child_and_descendants(tmp_path):
    _proc_full(tmp_path, 40, ["/v/0.4.0/bin/serialwrapd"], start=400)
    _proc_full(tmp_path, 41, ["/v/py/bin/python3", "-I", "-B", "/v/py/serialwrapd.py"], ppid=40, start=410)
    _proc_full(tmp_path, 42, ["sh", "-c", "sleep 1"], ppid=41, start=420)
    _proc_full(tmp_path, 43, ["bash"], start=430)
    assert uc.daemon_process_tree(tmp_path) == {40: "400", 41: "410", 42: "420"}


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wait_daemon_gone_waits_for_delayed_exit_of_tree_and_socket(tmp_path):
    """0.3.2 的 daemon.stop 先回應、先刪 socket，行程稍後才結束：須等整棵行程樹與 socket 都消失才算 gone。"""
    proc = tmp_path / "proc"
    _proc_full(proc, 40, ["/pipx/bin/python", "/h/.local/bin/serialwrapd"], start=400)
    _proc_full(proc, 41, ["sh", "-c", "x"], ppid=40, start=410)
    sock = tmp_path / "serialwrapd.sock"
    sock.write_text("", encoding="utf-8")
    before = uc.daemon_process_tree(proc)
    early = uc.wait_daemon_gone(before, [str(sock)], timeout=0.2, proc_root=proc)
    assert early["gone"] is False and early["alive"] == [40, 41] and early["sockets_left"] == [str(sock)], early
    assert early["timeout_s"] == 0.2

    def _exit() -> None:
        sock.unlink()
        shutil.rmtree(proc / "40")
        time.sleep(0.3)
        shutil.rmtree(proc / "41")

    timer = threading.Timer(0.2, _exit)
    timer.start()
    try:
        r = uc.wait_daemon_gone(before, [str(sock)], timeout=10.0, interval=0.02, proc_root=proc)
    finally:
        timer.join()
    assert r["gone"] is True, r
    assert not (proc / "41").exists() and not sock.exists()
    assert r["waited_ms"] >= 400 and r["pids"] == [40, 41] and r["sockets"] == [str(sock)]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wait_daemon_gone_is_bounded_and_reports_leftovers(tmp_path):
    proc = tmp_path / "proc"
    _proc_full(proc, 50, ["/h/.local/bin/serialwrapd"], start=500)
    t0 = time.monotonic()
    r = uc.wait_daemon_gone(uc.daemon_process_tree(proc), [], timeout=0.3, interval=0.05, proc_root=proc)
    assert time.monotonic() - t0 < 2.0
    assert r["gone"] is False and r["alive"] == [50] and r["instances"] == [50], r


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wait_daemon_gone_pid_reuse_zombie_new_instance_and_socket(tmp_path):
    proc = tmp_path / "proc"
    # PID 重用（啟動時間不同、已非 daemon）與 zombie（已釋放所有資源）都算已結束
    _proc_full(proc, 60, ["bash"], start=999)
    _proc_full(proc, 61, [], start=610, state="Z")
    assert uc.wait_daemon_gone({60: "600", 61: "610"}, [], timeout=0.2, proc_root=proc)["gone"] is True
    # 停止期間冒出不在快照內的 daemon 實例：不算 gone
    _proc_full(proc, 62, ["/h/.local/bin/serialwrapd"], start=620)
    r = uc.wait_daemon_gone({60: "600"}, [], timeout=0.2, proc_root=proc)
    assert r["gone"] is False and r["instances"] == [62], r
    shutil.rmtree(proc / "62")
    # 行程都結束但 socket 檔仍在：不算 gone
    left = tmp_path / "left.sock"
    left.write_text("", encoding="utf-8")
    r = uc.wait_daemon_gone({}, [str(left)], timeout=0.2, proc_root=proc)
    assert r["gone"] is False and r["sockets_left"] == [str(left)], r


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_daemon_socket_paths_from_argv_and_config(tmp_path):
    _proc_full(tmp_path, 70, ["python3", "-m", "sw_core.daemon", "--socket", "/run/a.sock"])
    _proc_full(tmp_path, 71, ["/v/bin/serialwrapd", "--socket=/run/b.sock"])
    _proc_full(tmp_path, 72, ["/h/.local/bin/serialwrapd"])
    config = "supervision_mode: on-demand\nsocket_path: '/run/user/1000/serialwrap/serialwrapd.sock'\n"
    got = uc.daemon_socket_paths([70, 71, 72], config, proc_root=tmp_path)
    assert got == ["/run/a.sock", "/run/b.sock", "/run/user/1000/serialwrap/serialwrapd.sock"]
    assert uc.daemon_socket_paths([], "socket_path: tcp://127.0.0.1:48700\n", proc_root=tmp_path) == []


def _load_driver():
    spec = importlib.util.spec_from_file_location("run_upgrade_container_under_test", UPGRADE_CONTAINER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeContainer:
    """容器內 0.3.2／0.4.0 行為的記憶體替身：假 /proc 與 socket；`daemon stop` 先刪 socket 並回應，舊行程延遲
    `exit_delay` 秒才結束（None＝永不結束）。行程未結束前 `daemon start` 回 DAEMON_ALREADY_RUNNING_ELSEWHERE、
    setup／install.sh 回 DEVICE_STILL_HELD，與本機實測的 on-demand 預熱競態相同。"""

    def __init__(self, tmp: Path, drv: Any, *, cli_version: str, exit_delay: float | None) -> None:
        self.drv = drv
        self.proc = tmp / "proc"
        self.proc.mkdir()
        self.sock = tmp / "run" / "serialwrapd.sock"
        self.sock.parent.mkdir()
        self.config = tmp / "config"
        (self.config / "profiles").mkdir(parents=True)
        (self.config / "config.yaml").write_text(f"supervision_mode: on-demand\nsocket_path: {self.sock}\n",
                                                 encoding="utf-8")
        self.cli = cli_version
        self.exit_delay = exit_delay
        self.next_pid = 4242
        self.daemons: dict[int, str] = {}
        self.stopping: set[int] = set()
        self.calls: list[tuple[str, bool]] = []
        self.timers: list[threading.Timer] = []
        self._spawn(cli_version)

    def _spawn(self, version: str) -> int:
        pid, self.next_pid = self.next_pid, self.next_pid + 1
        _proc_full(self.proc, pid, ["/opt/u/.local/bin/serialwrapd"], start=pid * 10)
        self.sock.write_text("", encoding="utf-8")
        self.daemons[pid] = version
        return pid

    def old_alive(self) -> bool:
        return any((self.proc / str(pid)).exists() for pid in self.daemons)

    def _exit(self, pid: int) -> None:
        shutil.rmtree(self.proc / str(pid), ignore_errors=True)
        self.daemons.pop(pid, None)
        self.stopping.discard(pid)

    @staticmethod
    def _done(argv: list[str], rc: int, out: Any = "", err: str = "") -> subprocess.CompletedProcess[str]:
        text = out if isinstance(out, str) else json.dumps(out)
        return subprocess.CompletedProcess(argv, rc, text + "\n", err)

    def _guarded(self, what: str, argv: list[str], ok: Any) -> subprocess.CompletedProcess[str]:
        alive = self.old_alive()
        self.calls.append((what, alive))
        if alive:
            return self._done(argv, 1, {"ok": False, "error_code": "DEVICE_STILL_HELD"})
        return self._done(argv, 0, ok)

    def sw(self, *args: str, label: str = "", timeout: float = 300.0) -> subprocess.CompletedProcess[str]:
        argv = ["serialwrap", *args]
        if args[:2] == ("daemon", "stop"):
            self.sock.unlink(missing_ok=True)
            for pid in list(self.daemons):
                self.stopping.add(pid)
                if self.exit_delay is not None:
                    timer = threading.Timer(self.exit_delay, self._exit, args=(pid,))
                    timer.start()
                    self.timers.append(timer)
            return self._done(argv, 0, {"ok": True, "stopping": True})
        if args[:2] == ("daemon", "start"):
            alive = self.old_alive()
            self.calls.append(("daemon start", alive))
            if alive:
                return self._done(argv, 2, {"ok": False, "error_code": "DAEMON_ALREADY_RUNNING_ELSEWHERE"})
            return self._done(argv, 0, {"ok": True, "pid": self._spawn(self.cli)})
        if args[:2] == ("daemon", "status"):
            live = [pid for pid in self.daemons if pid not in self.stopping]
            if not live:
                return self._done(argv, 1, {"ok": False, "error_code": "DAEMON_NOT_RUNNING"})
            return self._done(argv, 0, {"ok": True, "running": True, "pid": live[0], "version": self.daemons[live[0]],
                                        "multi_open": False})
        if args == ("--version",):
            return self._done(argv, 0, f"serialwrap {self.cli}")
        if args[:1] == ("setup",):
            return self._guarded("setup " + " ".join(args[1:]), argv, {"ok": True})
        if args[:2] == ("alias", "list"):
            return self._done(argv, 0, {"ok": True, "aliases": [
                {"alias": "upgbench", "session_id": "shellboard:COM7"},
                {"alias": "pinned", "device_by_id": "/dev/serial/by-id/usb-UPGRADE_PIN-if00-port0",
                 "profile": "shellboard"}]})
        if args[:2] == ("session", "list"):
            return self._done(argv, 0, {"ok": True, "sessions": [
                {"session_id": "shellboard:COM7", "device_by_id": "/dev/serial/by-id/usb-UPGRADE_REBOUND-if00-port0"}]})
        if args == ("doctor",):
            return self._done(argv, 0, {"ok": True, "checks": []})
        return self._done(argv, 0, {"ok": True})

    def run(self, argv: list[str], *, timeout: float = 300.0, label: str = "") -> subprocess.CompletedProcess[str]:
        if argv[:1] == ["timeout"]:  # 0.3.2 start_daemon 的前景診斷：舊行程在時回 ELSEWHERE（不含 serialwrapd.py）
            return self._done(argv, 2, "", "DAEMON_ALREADY_RUNNING_ELSEWHERE")
        if argv[:1] == ["pipx"]:
            if argv[1:2] == ["install"]:
                self.cli = "0.3.2"
            return self._done(argv, 0)
        if argv and argv[0].endswith("/bin/serialwrap") and argv[1:2] == ["setup"]:
            self.cli = "0.4.0" if not self.old_alive() else self.cli
            return self._guarded("0.4.0 setup", argv, {"ok": True, "install": {
                "op": "install", "from_version": "0.3.2", "to_version": "0.4.0", "steps": []}})
        if argv[:1] == ["bash"]:
            self.cli = "0.4.0" if not self.old_alive() else self.cli
            return self._guarded("install.sh", argv, "可執行 `pipx uninstall serialwrap` 移除舊版\n" + json.dumps(
                {"ok": True, "install": {"op": "install", "to_version": "0.4.0", "steps": []}}))
        return self._done(argv, 0)

    def close(self) -> None:
        for timer in self.timers:
            timer.cancel()


@pytest.fixture
def fake_driver(tmp_path, monkeypatch):
    """回傳 make(mode, cli_version, exit_delay, stop_wait_s) → (driver, fake)；driver 的 run／sw 全導向替身。"""
    drv = _load_driver()
    made: list[_FakeContainer] = []

    def make(*, cli_version: str, exit_delay: float | None = 0.4, stop_wait_s: float = 10.0):
        fake = _FakeContainer(tmp_path, drv, cli_version=cli_version, exit_delay=exit_delay)
        made.append(fake)
        monkeypatch.setattr(drv, "CONFIG", fake.config)
        monkeypatch.setattr(drv, "BIN", tmp_path / "bin")
        # phase_seed 以 subprocess.run 執行 chown：只替換 driver 模組看到的 subprocess，不動全域
        monkeypatch.setattr(drv, "subprocess", types.SimpleNamespace(
            run=lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""), CompletedProcess=subprocess.CompletedProcess,
            TimeoutExpired=subprocess.TimeoutExpired))
        out = tmp_path / "out"
        out.mkdir(exist_ok=True)
        driver = drv.Driver("on-demand", tmp_path / "w.whl", tmp_path / "bundle", out,
                            installer=tmp_path / "install.sh", bundle_tarball=tmp_path / "t.tar.gz")
        driver.proc_root = fake.proc
        driver.stop_wait_s = stop_wait_s
        driver.sw = fake.sw
        driver.run = fake.run
        orig_wait = driver.wait_daemon
        driver.wait_daemon = lambda *, version, timeout=60.0: orig_wait(version=version, timeout=min(timeout, 2.0))
        orig_gone = driver.wait_no_daemon
        driver.wait_no_daemon = lambda timeout=30.0: orig_gone(timeout=min(timeout, 1.0))
        driver.cfg_before = drv.uc.snapshot_files(fake.config)
        driver.baseline = {"aliases": json.loads(fake.sw("alias", "list").stdout)["aliases"],
                           "sessions": json.loads(fake.sw("session", "list").stdout)["sessions"]}
        return driver, fake

    yield make
    for fake in made:
        fake.close()


def _row(driver: Any, check_id: str) -> dict[str, Any]:
    return next(r for r in driver.rows if r["id"] == check_id)


# 替身為 Linux 容器 driver 的行為模型（runuser／pipx／systemd 流程），只在 POSIX 上有意義。
@pytest.mark.posix_only
@pytest.mark.memory_only
@pytest.mark.parametrize("phase,check_id,cli", [
    ("phase_seed", "seed_user_data", "0.3.2"),
    ("phase_upgrade_after_stop", "upgrade_after_stop", "0.3.2"),
    ("phase_install_sh", "old_daemon_stopped", "0.3.2"),
    ("phase_downgrade", "downgrade_manual", "0.4.0"),
    ("phase_reupgrade", "reupgrade", "0.3.2"),
])
def test_driver_waits_for_old_daemon_exit_before_next_start(fake_driver, phase, check_id, cli):
    """Docker on-demand 預熱競態（rulings/s1.md）：`daemon stop` 之後舊 daemon 仍在退出時，driver 不得立即
    start／setup／install.sh；須等舊行程樹與 socket 都消失，該步驟的判定才 PASS，並記錄等待結果。"""
    driver, fake = fake_driver(cli_version=cli)
    method = getattr(driver, phase)
    method(False) if phase == "phase_upgrade_after_stop" else method()
    assert fake.calls, "替身未記錄任何 start／setup 呼叫"
    assert [c for c in fake.calls if c[1]] == [], f"舊 daemon 尚未結束就呼叫：{fake.calls}"
    row = _row(driver, check_id)
    assert row["verdict"] == "PASS", row
    wait = row["detail"]["stop_wait"]
    assert wait["gone"] is True and wait["pids"] and wait["sockets"] == [str(fake.sock)], wait


@pytest.mark.posix_only
@pytest.mark.memory_only
@pytest.mark.parametrize("phase,check_id,cli", [
    ("phase_seed", "seed_user_data", "0.3.2"),
    ("phase_reupgrade", "reupgrade", "0.3.2"),
    ("phase_downgrade", "downgrade_manual", "0.4.0"),
])
def test_driver_stop_wait_timeout_fails_and_is_recorded(fake_driver, phase, check_id, cli):
    """舊 daemon 在有界逾時內未結束：該步驟判 FAIL，detail 記錄殘留行程；seed 不得在逾時後硬起新 daemon。"""
    driver, fake = fake_driver(cli_version=cli, exit_delay=None, stop_wait_s=0.5)
    getattr(driver, phase)()
    row = _row(driver, check_id)
    assert row["verdict"] == "FAIL", row
    wait = row["detail"]["stop_wait"]
    assert wait["gone"] is False and wait["alive"] and wait["timeout_s"] == 0.5, wait
    if phase == "phase_seed":
        assert ("daemon start", True) not in fake.calls and ("daemon start", False) not in fake.calls


# ---------- two-reader 重疊 ----------

@pytest.mark.core_portable
@pytest.mark.memory_only
def test_two_reader_overlap_fails():
    """舊 daemon 未停即起新 daemon：任何一個取樣同時有兩個實例即 FAIL。"""
    samples = [(0.0, [101]), (0.05, [101, 202]), (0.10, [202])]
    row = uc.judge_two_reader(samples, min_samples=3)
    assert row["verdict"] == "FAIL", row
    assert row["detail"]["overlaps"][0]["pids"] == [101, 202]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_two_reader_sequential_handoff_passes():
    samples = [(0.0, [101]), (0.05, []), (0.10, [202]), (0.15, [202])]
    row = uc.judge_two_reader(samples, min_samples=3)
    assert row["verdict"] == "PASS", row
    assert row["detail"]["instances_seen"] == [101, 202]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_two_reader_too_few_samples_insufficient():
    row = uc.judge_two_reader([(0.0, [101])], min_samples=3)
    assert row["verdict"] == "INSUFFICIENT", row


# ---------- 設定與狀態保留 ----------

@pytest.mark.core_portable
@pytest.mark.memory_only
def test_config_lost_after_upgrade_fails(tmp_path):
    """升級後 config 遺失或被改寫：逐位元組比對 FAIL，並指出差異檔。"""
    cfg = tmp_path / "serialwrap"
    (cfg / "profiles").mkdir(parents=True)
    (cfg / "config.yaml").write_text("supervision_mode: systemd-user\n", encoding="utf-8")
    (cfg / "profiles" / "bench.yaml").write_text("targets: []\n", encoding="utf-8")
    before = uc.snapshot_files(cfg)
    (cfg / "config.yaml").unlink()
    row = uc.judge_files_preserved("config_preserved", before, uc.snapshot_files(cfg))
    assert row["verdict"] == "FAIL", row
    assert row["detail"]["missing"] == ["config.yaml"]
    (cfg / "config.yaml").write_text("supervision_mode: on-demand\n", encoding="utf-8")
    row = uc.judge_files_preserved("config_preserved", before, uc.snapshot_files(cfg))
    assert row["verdict"] == "FAIL" and row["detail"]["changed"] == ["config.yaml"], row
    (cfg / "config.yaml").write_text("supervision_mode: systemd-user\n", encoding="utf-8")
    assert uc.judge_files_preserved("config_preserved", before, uc.snapshot_files(cfg))["verdict"] == "PASS"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_alias_binding_lost_fails():
    base = {"aliases": [{"alias": "upgbench", "session_id": "shellboard:COM7", "updated_at": "t1"},
                        {"alias": "pinned", "device_by_id": "/dev/serial/by-id/x", "profile": "shellboard",
                         "updated_at": "t2"}],
            "sessions": [{"session_id": "shellboard:COM7", "device_by_id": "/dev/serial/by-id/rebound"}]}
    same = json.loads(json.dumps(base))
    same["aliases"][0]["updated_at"] = "t9"  # 時間戳不屬語意
    assert uc.judge_state_compatible(base, same)["verdict"] == "PASS"
    lost = json.loads(json.dumps(base))
    del lost["aliases"][1]
    row = uc.judge_state_compatible(base, lost)
    assert row["verdict"] == "FAIL" and row["detail"]["aliases_missing"], row
    rebound = json.loads(json.dumps(base))
    rebound["sessions"][0]["device_by_id"] = "/dev/serial/by-id/x"
    row = uc.judge_state_compatible(base, rebound)
    assert row["verdict"] == "FAIL" and row["detail"]["bindings_changed"], row


# ---------- doctor ----------

@pytest.mark.core_portable
@pytest.mark.memory_only
def test_doctor_green_requires_non_advisory_and_pairing():
    ok = {"ok": True, "checks": [{"check": "dialout", "ok": True, "advisory": False},
                                 {"check": "version_pairing", "ok": True, "advisory": False},
                                 {"check": "devices", "ok": False, "advisory": True}]}
    assert uc.judge_doctor("doctor_green_new", ok, require_pairing=True)["verdict"] == "PASS"
    bad = json.loads(json.dumps(ok))
    bad["checks"][0]["ok"] = False
    assert uc.judge_doctor("doctor_green_new", bad, require_pairing=True)["verdict"] == "FAIL"
    nopair = {"ok": True, "checks": [c for c in ok["checks"] if c["check"] != "version_pairing"]}
    assert uc.judge_doctor("doctor_green_new", nopair, require_pairing=True)["verdict"] == "FAIL"
    assert uc.judge_doctor("doctor_green_old", nopair, require_pairing=False)["verdict"] == "PASS"
    other_adv = json.loads(json.dumps(ok))
    other_adv["checks"].append({"check": "jq_on_path", "ok": False, "advisory": True})
    assert uc.judge_doctor("doctor_green_new", other_adv, require_pairing=True)["verdict"] == "FAIL"


# ---------- 整體 verdict ----------

@pytest.mark.core_portable
@pytest.mark.memory_only
def test_overall_verdict_rules():
    rows = [{"id": cid, "verdict": "PASS"} for cid in uc.REQUIRED_CHECKS]
    assert uc.overall_verdict(rows) == "PASS"
    assert uc.overall_verdict(rows[:-1]) == "INSUFFICIENT"
    rows[0] = {"id": rows[0]["id"], "verdict": "FAIL"}
    assert uc.overall_verdict(rows[:-1]) == "FAIL"
    dup = [{"id": cid, "verdict": "PASS"} for cid in uc.REQUIRED_CHECKS] + [{"id": uc.REQUIRED_CHECKS[0],
                                                                               "verdict": "PASS"}]
    assert uc.overall_verdict(dup) == "FAIL"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_refused_upgrade_must_leave_install_untouched():
    """舊 daemon 執行中時 0.4.0 setup 若拒絕：舊 daemon 須同 PID 存活、入口與設定不變，否則 FAIL。"""
    before = {"daemon_pids": [101], "entries": {"serialwrap": "/pipx/bin/serialwrap"}, "files": {"config.yaml": "a"}}
    after_ok = json.loads(json.dumps(before))
    assert uc.judge_refusal_untouched("upgrade_with_old_daemon_running", before, after_ok)["verdict"] == "PASS"
    killed = dict(after_ok, daemon_pids=[])
    assert uc.judge_refusal_untouched("upgrade_with_old_daemon_running", before, killed)["verdict"] == "FAIL"
    switched = dict(after_ok, entries={"serialwrap": "/v/0.4.0/bin/serialwrap"})
    assert uc.judge_refusal_untouched("upgrade_with_old_daemon_running", before, switched)["verdict"] == "FAIL"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_refusal_with_wrong_reason_fails():
    """現場未改動但拒絕原因與規格不符（例如 manifest 錯誤、DAEMON_BUSY）→ FAIL；原因相符才 PASS（e270e89#2）。"""
    before = {"daemon_pids": [101], "entries": {"serialwrap": "/pipx/bin/serialwrap"}, "files": {"config.yaml": "a"}}
    after = json.loads(json.dumps(before))
    legacy = {"ok": False, "error_code": "METHOD_NOT_FOUND", "probe": "daemon.handoff_stop"}
    row = uc.judge_refusal("upgrade_with_old_daemon_running", before, after, legacy, uc.REFUSAL_LEGACY_HANDOFF)
    assert row["verdict"] == "PASS", row
    busy = {"ok": False, "error_code": "DAEMON_BUSY"}
    row = uc.judge_refusal("upgrade_with_old_daemon_running", before, after, busy, uc.REFUSAL_LEGACY_HANDOFF)
    assert row["verdict"] == "FAIL" and "拒絕原因" in row["reason"], row
    other_probe = dict(legacy, probe="daemon.status")
    assert uc.judge_refusal("upgrade_with_old_daemon_running", before, after, other_probe,
                            uc.REFUSAL_LEGACY_HANDOFF)["verdict"] == "FAIL"
    rollback = {"ok": False, "error_code": "INVALID_ARGS", "reason": "rollback_unavailable"}
    assert uc.judge_refusal("rollback_command", before, after, rollback, uc.REFUSAL_ROLLBACK_UNAVAILABLE)[
        "verdict"] == "PASS"
    wrong = dict(rollback, reason="manifest_invalid")
    assert uc.judge_refusal("rollback_command", before, after, wrong, uc.REFUSAL_ROLLBACK_UNAVAILABLE)[
        "verdict"] == "FAIL"
    # 原因相符但現場被改動仍 FAIL；拒絕前沒有舊 daemon 仍 INSUFFICIENT
    killed = dict(after, daemon_pids=[])
    assert uc.judge_refusal("rollback_command", before, killed, rollback, uc.REFUSAL_ROLLBACK_UNAVAILABLE)[
        "verdict"] == "FAIL"
    nodaemon = dict(before, daemon_pids=[])
    assert uc.judge_refusal("rollback_command", nodaemon, nodaemon, rollback, uc.REFUSAL_ROLLBACK_UNAVAILABLE)[
        "verdict"] == "INSUFFICIENT"


def _legacy_body(*, started=True, method="rpc_daemon_stop", ok=True):
    return {"ok": ok, "install": {"op": "install", "to_version": "0.4.0", "steps": [
        {"step": "stop_supervisor", "ok": True, "detail": {"handoff": "legacy_stopped", "legacy_stop": {
            "method": method, "from_version": "0.3.2", "waited_ms": 7000, "relaunch_count": 0,
            "stability_window_ms": 5000}}},
        {"step": "start_and_verify", "ok": True, "detail": {"started": started, "daemon_version": "0.4.0"}},
    ]}}


def _legacy_judge(**override):
    args = dict(setup_rc=0, body=_legacy_body(), old_pids=[101], old_alive_after=[],
                status_after={"ok": True, "version": "0.4.0", "pid": 202}, instances_after=[202],
                cfg_before={"config.yaml": "a", "profiles/bench.yaml": "b"},
                cfg_after={"config.yaml": "a", "profiles/bench.yaml": "b"}, overlaps=[])
    args.update(override)
    return uc.judge_legacy_direct_setup("legacy_handoff_direct_setup", **args)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_legacy_direct_setup_is_required_check():
    """OD-48：新增檢查 id 列入必要檢查；舊 id 保留（不改語意）。"""
    assert "legacy_handoff_direct_setup" in uc.REQUIRED_CHECKS
    assert "upgrade_with_old_daemon_running" in uc.REQUIRED_CHECKS and "upgrade_after_stop" in uc.REQUIRED_CHECKS


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_legacy_direct_setup_passes_only_when_setup_started_new_daemon():
    """0.3.2 daemon 執行中直接 setup：切換成功、舊行程樹結束、新 daemon 由 setup 啟動、設定保留、無重疊 → PASS。"""
    assert _legacy_judge()["verdict"] == "PASS"
    assert _legacy_judge(body=_legacy_body(method="systemctl_stop"))["verdict"] == "PASS"
    # setup 沒有啟動新 daemon（driver 不得代啟）→ FAIL
    row = _legacy_judge(body=_legacy_body(started=False))
    assert row["verdict"] == "FAIL" and "由 setup 啟動" in row["reason"], row
    row = _legacy_judge(status_after=None, instances_after=[])
    assert row["verdict"] == "FAIL" and "由 setup 啟動" in row["reason"], row
    # 新舊兩個 daemon 同時存在
    assert _legacy_judge(instances_after=[202, 303])["verdict"] == "FAIL"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_legacy_direct_setup_failure_modes():
    """舊行程樹未結束、設定被改寫、升級期間重疊、缺 legacy_stop 或 setup 失敗 → FAIL；沒有舊 daemon → INSUFFICIENT。"""
    row = _legacy_judge(old_alive_after=[101])
    assert row["verdict"] == "FAIL" and "舊 daemon" in row["reason"], row
    row = _legacy_judge(cfg_after={"config.yaml": "changed", "profiles/bench.yaml": "b"})
    assert row["verdict"] == "FAIL" and "設定" in row["reason"], row
    row = _legacy_judge(overlaps=[{"t": 1.0, "pids": [101, 202]}])
    assert row["verdict"] == "FAIL" and "two-reader" in row["reason"], row
    body = _legacy_body()
    body["install"]["steps"][0]["detail"].pop("legacy_stop")
    assert _legacy_judge(body=body)["verdict"] == "FAIL"
    refused = {"ok": False, "error_code": "METHOD_NOT_FOUND", "probe": "daemon.handoff_stop"}
    assert _legacy_judge(setup_rc=2, body=refused)["verdict"] == "FAIL"
    assert _legacy_judge(old_pids=[])["verdict"] == "INSUFFICIENT"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_legacy_refusal_no_longer_accepted_for_0_3_x():
    """REFUSAL_LEGACY_HANDOFF 只用於非 legacy 的不支援交接 daemon；0.3.x daemon 被拒絕即 FAIL（OD-48）。"""
    before = {"daemon_pids": [101], "entries": {"serialwrap": "/pipx/bin/serialwrap"}, "files": {"config.yaml": "a"}}
    after = json.loads(json.dumps(before))
    refused = {"ok": False, "error_code": "METHOD_NOT_FOUND", "probe": "daemon.handoff_stop"}
    row = uc.judge_upgrade_running_refusal("upgrade_with_old_daemon_running", before, after, refused, legacy=True)
    assert row["verdict"] == "FAIL" and "OD-48" in row["reason"], row
    row = uc.judge_upgrade_running_refusal("upgrade_with_old_daemon_running", before, after, refused, legacy=False)
    assert row["verdict"] == "PASS", row


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_seed_requires_binding_effective_in_baseline():
    """基線 session list 沒有 COM7 或 by-id 仍是 YAML 原值 → seed FAIL，避免 binding 相容檢查空轉（e270e89#1）。"""
    aliases = [{"alias": "upgbench", "session_id": "shellboard:COM7"},
               {"alias": "pinned", "device_by_id": "/dev/serial/by-id/usb-UPGRADE_PIN-if00-port0"}]
    bound = [{"session_id": "shellboard:COM7", "device_by_id": "/dev/serial/by-id/usb-UPGRADE_REBOUND-if00-port0"}]
    files = ["config.yaml", "profiles/bench.yaml"]
    ok = uc.judge_seed("seed_user_data", rcs=[0, 0, 0, 0], baseline={"aliases": aliases, "sessions": bound},
                       config_files=files)
    assert ok["verdict"] == "PASS", ok
    assert ok["detail"]["sessions"] == bound
    yaml_value = [{"session_id": "shellboard:COM7", "device_by_id": "/dev/serial/by-id/usb-UPGRADE_TEST-if00-port0"}]
    row = uc.judge_seed("seed_user_data", rcs=[0, 0, 0, 0], baseline={"aliases": aliases, "sessions": yaml_value},
                        config_files=files)
    assert row["verdict"] == "FAIL", row
    row = uc.judge_seed("seed_user_data", rcs=[0, 0, 0, 0], baseline={"aliases": aliases, "sessions": []},
                        config_files=files)
    assert row["verdict"] == "FAIL", row
    row = uc.judge_seed("seed_user_data", rcs=[0, 0, 0, 0], baseline={"aliases": aliases[:1], "sessions": bound},
                        config_files=files)
    assert row["verdict"] == "FAIL", row
    row = uc.judge_seed("seed_user_data", rcs=[0, 1, 0, 0], baseline={"aliases": aliases, "sessions": bound},
                        config_files=files)
    assert row["verdict"] == "FAIL", row
    row = uc.judge_seed("seed_user_data", rcs=[0, 0, 0, 0], baseline={"aliases": aliases, "sessions": bound},
                        config_files=["config.yaml"])
    assert row["verdict"] == "FAIL", row


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_rollback_success_path_requires_old_daemon_and_config():
    """setup --rollback 若成功：CLI、daemon 版本都須為 0.3.2，config 須逐位元組保留（e270e89#3）。"""
    cfg = {"config.yaml": "a"}
    good = uc.judge_rollback_success("rollback_command", cli_version="serialwrap 0.3.2",
                                     daemon_version="0.3.2", cfg_before=cfg, cfg_after=dict(cfg))
    assert good["verdict"] == "PASS", good
    assert uc.judge_rollback_success("rollback_command", cli_version="serialwrap 0.3.2", daemon_version="0.4.0",
                                     cfg_before=cfg, cfg_after=dict(cfg))["verdict"] == "FAIL"
    assert uc.judge_rollback_success("rollback_command", cli_version="serialwrap 0.3.2", daemon_version="0.3.2",
                                     cfg_before=cfg, cfg_after={})["verdict"] == "FAIL"
    assert uc.judge_rollback_success("rollback_command", cli_version="serialwrap 0.4.0", daemon_version="0.3.2",
                                     cfg_before=cfg, cfg_after=dict(cfg))["verdict"] == "FAIL"


# ---------- runner 與映像 ----------

@pytest.mark.core_portable
@pytest.mark.memory_only
def test_systemd_system_mode_required_checks_cover_legacy_direct_setup():
    """OD-48 SPEC-GAP #10：systemd-system 只驗「0.3.2 daemon 執行中直接 setup」成功路徑，另立必要檢查集合；
    完整流程（rollback／降級／重新升級）的必要集合不變。"""
    required = uc.REQUIRED_CHECKS_SYSTEMD_SYSTEM
    for cid in ("old_install_0_3_2", "legacy_handoff_direct_setup", "upgrade_with_old_daemon_running",
                "entries_replaced", "config_preserved_upgrade", "new_daemon_started", "supervision_reconciled",
                "doctor_green_new", "no_two_reader"):
        assert cid in required, cid
    assert set(required) <= set(uc.REQUIRED_CHECKS)
    assert "rollback_command" not in required and "downgrade_manual" not in required
    rows = [{"id": cid, "verdict": "PASS"} for cid in required]
    assert uc.overall_verdict(rows, required=required) == "PASS"
    assert uc.overall_verdict(rows, required=uc.REQUIRED_CHECKS) == "INSUFFICIENT"
    missing = [r for r in rows if r["id"] != "legacy_handoff_direct_setup"]
    assert uc.overall_verdict(missing, required=required) == "INSUFFICIENT"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_managed_profile_overwrite_must_match_bundle_content():
    """S1-f3c66b4-1：套件管理的 profile 只允許被覆寫成新 bundle 的權威內容；遺失或其他內容必須 FAIL，
    其餘檔案維持逐位元組比較。"""
    managed = "/etc/serialwrap/profiles/default.yaml"
    before = {"config.yaml": "c0", "profiles/bench.yaml": "b0", managed: "old", "/etc/serialwrap/profiles/bench.yaml": "b1"}
    expected = {managed: "new"}
    ok = dict(before, **{managed: "new"})
    assert uc.judge_files_preserved("cfg", before, ok, expected_overwrites=expected)["verdict"] == "PASS"
    assert uc.judge_files_preserved("cfg", before, dict(before), expected_overwrites=expected)["verdict"] == "PASS"
    corrupted = dict(before, **{managed: "garbage"})
    r = uc.judge_files_preserved("cfg", before, corrupted, expected_overwrites=expected)
    assert r["verdict"] == "FAIL" and r["detail"]["changed"] == [managed]
    deleted = {k: v for k, v in before.items() if k != managed}
    r = uc.judge_files_preserved("cfg", before, deleted, expected_overwrites=expected)
    assert r["verdict"] == "FAIL" and r["detail"]["missing"] == [managed]
    # 沒有權威內容的檔案（使用者自加的 /etc profile）被改寫仍 FAIL。
    user_changed = dict(ok, **{"/etc/serialwrap/profiles/bench.yaml": "x"})
    assert uc.judge_files_preserved("cfg", before, user_changed, expected_overwrites=expected)["verdict"] == "FAIL"
    # 預設（未給權威內容）語意不變。
    assert uc.judge_files_preserved("cfg", before, ok)["verdict"] == "FAIL"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_system_driver_keeps_managed_profiles_in_snapshot():
    """S1-f3c66b4-1：driver 不得把套件管理檔從快照排除。"""
    text = UPGRADE_CONTAINER.read_text(encoding="utf-8")
    assert "if k not in managed" not in text
    assert "expected_overwrites" in text


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_upgrade_image_grants_swtest_sudo_for_system_mode():
    """systemd-system 模式以 swtest 執行 `setup --system --with-sudo`，映像需提供免密碼 sudo。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert " sudo " in text
    assert "swtest ALL=(ALL) NOPASSWD: ALL" in text


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_upgrade_image_pins_same_base_as_systemd_image():
    """升級容器與 OD-16 (5) systemd 容器使用同一個 ubuntu:24.04 digest，且不放 repo 內容、不連網安裝依賴。"""
    def base(path: Path) -> str:
        return next(line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("FROM "))
    assert base(DOCKERFILE) == base(SYSTEMD_DOCKERFILE)
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY" not in text
    for pkg in ("pipx", "systemd", "dbus-user-session"):
        assert pkg in text, pkg
    assert "python3-yaml" not in text, "升級驗證映像不得以系統 PyYAML 遮住 bundle 缺少執行期依賴"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# POSIX-only：bash runner 需 POSIX bash、git 與 python3。
@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_run_upgrade_dry_run_lists_both_modes_and_isolation(tmp_path):
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--dry-run", "--out", str(tmp_path / "ev")],
                       cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "git archive v0.3.2" in out
    assert "--release-bundle --profile release --version-override 0.4.0" in out
    assert "pip download --only-binary=:all:" in out
    assert "--network none" in out
    for mode in ("systemd-user", "on-demand"):
        assert f"--mode {mode}" in out, out
    assert "/dev/ttyUSB" not in out and "--device" not in out


# POSIX-only：bash runner 需 POSIX bash、git 與 python3。
@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_run_upgrade_dry_run_systemd_system_mode_is_explicit_only(tmp_path):
    """OD-48 SPEC-GAP #10：systemd-system 只能明確指定；預設（all）仍是 systemd-user 與 on-demand 兩種。"""
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--dry-run", "--mode", "systemd-system", "--out", str(tmp_path / "ev")],
                       cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--mode systemd-system" in r.stdout and "--mode systemd-user" not in r.stdout
    assert "--network none" in r.stdout
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--dry-run", "--out", str(tmp_path / "ev2")],
                       cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--mode systemd-system" not in r.stdout


# ---------- release 前置第 2 輪 OWNER-BATCH #2：bundle 比照 release workflow 以 /usr/bin/python3.12 build venv 建立 ----------

def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    """PATH 首位放假 docker：只記錄呼叫並失敗，確保 fail-closed 測試絕不碰真的 docker。"""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "docker-calls.log"
    fake = bindir / "docker"
    fake.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\nexit 1\n', encoding="utf-8")
    fake.chmod(0o755)
    return bindir, log


def _docker_calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []


# POSIX-only：bash runner 需 POSIX bash、git 與 python3。
@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_run_upgrade_dry_run_builds_bundle_with_system_python312_venv(tmp_path):
    """bundle 以 /usr/bin/python3.12 建的 build venv 執行 mkbundle（PATH 首位為 build venv），並列出
    pyvenv.cfg `home = /usr/bin` 檢查；與 release workflow 的 publish-linux-bundle 同一做法與釘版。"""
    out_dir = tmp_path / "ev"
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--dry-run", "--out", str(out_dir)],
                       cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    work = out_dir.resolve() / "work"
    lines = r.stdout.splitlines()
    assert f"+ /usr/bin/python3.12 -m venv {work}/build-venv" in [line.rstrip() for line in lines], r.stdout
    pip = next(line for line in lines if f"{work}/build-venv/bin/python3 -m pip install" in line)
    assert "pyyaml==6.0.3" in pip and "jsonschema==4.10.3" in pip, pip
    mk = next(line for line in lines if "mkbundle.sh" in line)
    assert mk.startswith(f"+ env PATH={work}/build-venv/bin:"), mk
    assert "--release-bundle --profile release --version-override 0.4.0" in mk
    check = next(line for line in lines if "pyvenv.cfg" in line)
    assert "home = /usr/bin" in check and "include-system-site-packages = false" in check, check
    assert lines.index(check) > lines.index(mk)


# POSIX-only：bash runner 需 POSIX bash、git 與 python3。
@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.parametrize("kind", ["missing", "not_usr_bin"])
def test_run_upgrade_fails_closed_without_system_python312(tmp_path, kind):
    """主機沒有 /usr/bin/python3.12（或指定的直譯器不在 /usr/bin）→ 建任何東西之前 rc=3 fail-closed，
    不回退 PATH 上的其他 python，也不碰 docker。"""
    bindir, log = _fake_docker(tmp_path)
    if kind == "missing":
        python = tmp_path / "missing" / "python3.12"
    else:
        # 真的可執行的 3.12 直譯器，但實體不在 /usr/bin（如 toolcache／自編 Python）：不得接受
        python = tmp_path / "other" / "python3.12"
        python.parent.mkdir()
        shutil.copy2(os.path.realpath(sys.executable), python)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", SW_UPGRADE_SYSTEM_PYTHON=str(python))
    out_dir = tmp_path / "ev"
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--out", str(out_dir),
                        "--wheel", str(tmp_path / "serialwrap-0.3.2-py3-none-any.whl")],
                       cwd=REPO, capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "/usr/bin/python3.12" in r.stderr and "不回退" in r.stderr, r.stderr
    work = out_dir / "work"
    assert not (work / "build-venv").exists() and not (work / "bundle-0.4.0").exists()
    assert not list(work.glob("wheels/*.whl"))
    assert _docker_calls(log) == []


# POSIX-only：bash runner 需 POSIX bash、git 與 python3。
@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.parametrize("cfg", [
    None,
    "home = /opt/hostedtoolcache/Python/3.12.3/x64/bin\ninclude-system-site-packages = false\nversion = 3.12.3\n",
    "home = /usr/local/bin\ninclude-system-site-packages = false\nversion = 3.12.3\n",
    "home = /usr/bin\ninclude-system-site-packages = true\nversion = 3.12.3\n",
], ids=["missing_cfg", "toolcache_home", "usr_local_home", "system_site_packages"])
def test_run_upgrade_rejects_bundle_runtime_not_bound_to_usr_bin(tmp_path, cfg):
    """`--bundle` 提供的（或自建的）bundle runtime 的 pyvenv.cfg 不是 `home = /usr/bin`＋
    `include-system-site-packages = false`（或缺檔）→ 建映像之前 rc=3 fail-closed。"""
    bindir, log = _fake_docker(tmp_path)
    vdir = tmp_path / "bundle" / "versions" / "0.4.0"
    (vdir / "py").mkdir(parents=True)
    (vdir / "release-manifest.json").write_text(json.dumps({"bundle_version": "0.4.0"}), encoding="utf-8")
    if cfg is not None:
        (vdir / "py" / "pyvenv.cfg").write_text(cfg, encoding="utf-8")
    wheel = tmp_path / "serialwrap-0.3.2-py3-none-any.whl"
    wheel.write_bytes(b"")
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--out", str(tmp_path / "ev"), "--wheel", str(wheel),
                        "--bundle", str(tmp_path / "bundle")],
                       cwd=REPO, capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 3, r.stdout + r.stderr
    assert "pyvenv.cfg" in r.stderr, r.stderr
    calls = _docker_calls(log)
    assert not [c for c in calls if c.split()[:1] in (["build"], ["run"], ["exec"])], calls


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_upgrade_environment_does_not_hide_missing_pyyaml():
    """升級容器不得用系統 PyYAML 或共享 site-packages 遮住一般 pipx 使用者缺依賴。"""
    container = UPGRADE_CONTAINER.read_text(encoding="utf-8")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "--system-site-packages" not in container
    assert "python3-yaml" not in dockerfile
    assert "--find-links=/opt/sw-old" in container
    assert "pip download" in (REPO / "scripts" / "docker" / "run-upgrade.sh").read_text(encoding="utf-8")


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_release_plan_accepts_only_real_stage1_gate_outcomes():
    plan = (REPO / "docs" / "rust-refactor" / "release-0.4.0-plan.md").read_text(encoding="utf-8")
    assert "`stage1a_gate.py` overall PASS" not in plan
    assert "VERIFIED" in plan and "IMPLEMENTED" in plan
    assert "沒有 FAIL／INSUFFICIENT" in plan


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_run_upgrade_refuses_existing_evidence(tmp_path):
    out = tmp_path / "ev"
    (out / "systemd-user").mkdir(parents=True)
    (out / "systemd-user" / "upgrade-report.json").write_text("{}", encoding="utf-8")
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--out", str(out), "--mode", "systemd-user"],
                       cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 2, r.stdout + r.stderr


@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.supervisor_runtime
def test_docker_upgrade_0_3_2_to_0_4_0(tmp_path):
    """實跑：兩種監管模式各自完成 0.3.2 安裝→0.4.0 升級→降級回 0.3.2→重新升級，全部檢查 PASS。"""
    if not _docker_available():
        pytest.skip("cap_missing:docker：本機無可用 docker daemon（能力式 SKIP，證據不足，非 PASS）")
    if subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/tags/v0.3.2"], cwd=REPO,
                      capture_output=True).returncode != 0:
        pytest.skip("cap_missing:tag_v0.3.2：版本庫沒有 v0.3.2 tag（證據不足，非 PASS）")
    out = tmp_path / "ev"
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--out", str(out)], cwd=REPO, capture_output=True,
                       text=True, timeout=1800, env=dict(os.environ))
    reports = {}
    for mode in ("systemd-user", "on-demand"):
        path = out / mode / "upgrade-report.json"
        reports[mode] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    diag = json.dumps({m: None if rep is None else [c for c in rep.get("checks", []) if c.get("verdict") != "PASS"]
                       for m, rep in reports.items()}, ensure_ascii=False)[:4000]
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:] + diag
    for mode, rep in reports.items():
        assert rep is not None, f"{mode} 未產生 upgrade-report.json"
        assert rep["verdict"] == "PASS", diag
        assert rep["evidence_layer"] == "L1.5-docker"
        assert rep["old_version"] == "0.3.2" and rep["new_version"] == "0.4.0"


# ---------- OD-50：install.sh 升級路徑（0.3.2 pipx、daemon 已停止 → install.sh --bundle） ----------

@pytest.mark.core_portable
@pytest.mark.memory_only
def test_install_sh_scenario_required_checks_and_verdict():
    """install-sh 情境另列必要檢查；升級情境的 REQUIRED_CHECKS 不因 install-sh 而變（OD-48 合入後為下列 19 項，
    含 legacy_handoff_direct_setup；逐項釘住，不只比長度）。"""
    assert uc.REQUIRED_CHECKS == (
        "old_install_0_3_2", "seed_user_data", "doctor_green_old", "upgrade_with_old_daemon_running",
        "legacy_handoff_direct_setup", "upgrade_after_stop", "entries_replaced", "config_preserved_upgrade",
        "new_daemon_started", "supervision_reconciled", "state_compatible_upgrade", "doctor_green_new",
        "rollback_command", "downgrade_manual", "config_preserved_downgrade", "state_compatible_downgrade",
        "doctor_green_downgrade", "reupgrade", "no_two_reader")
    assert "install_sh_upgrade" not in uc.REQUIRED_CHECKS
    assert "old_daemon_stopped" not in uc.REQUIRED_CHECKS
    required = uc.REQUIRED_INSTALL_SH_CHECKS
    assert {"old_install_0_3_2", "old_daemon_stopped", "install_sh_upgrade", "entries_replaced",
            "config_preserved_upgrade", "new_daemon_started", "doctor_green_new", "no_two_reader"} <= set(required)
    assert "upgrade_with_old_daemon_running" not in required
    rows = [{"id": cid, "verdict": "PASS"} for cid in required]
    assert uc.overall_verdict(rows, required=required) == "PASS"
    assert uc.overall_verdict(rows[:-1], required=required) == "INSUFFICIENT"
    # 預設仍以升級情境的完整集合判定：install-sh 的檢查集合不足以讓升級情境 PASS
    assert uc.overall_verdict(rows) == "INSUFFICIENT"
    # 亦不足以讓 systemd-system（OD-48）情境 PASS：缺 legacy_handoff_direct_setup 等
    assert uc.overall_verdict(rows, required=uc.REQUIRED_CHECKS_SYSTEMD_SYSTEM) == "INSUFFICIENT"


def _install_body(**install):
    base = {"op": "install", "to_version": "0.4.0"}
    base.update(install)
    return {"ok": True, "install": base}


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("kwargs,verdict", [
    ({}, "PASS"),
    ({"rc": 1}, "FAIL"),
    ({"body": {"ok": False, "error_code": "METHOD_NOT_FOUND"}}, "FAIL"),
    ({"body": _install_body(to_version="0.3.2")}, "FAIL"),
    ({"output": "[serialwrap] SHA256 驗證通過"}, "FAIL"),  # 缺 pipx uninstall 提示
    ({"pipx_venv_left": False}, "FAIL"),  # install.sh 不得自動移除舊 pipx
    ({"leftover_tmp": ["/tmp/serialwrap-install.abc"]}, "FAIL"),
])
def test_judge_install_sh(kwargs, verdict):
    args = {"rc": 0, "body": _install_body(), "output": "可執行 `pipx uninstall serialwrap` 移除舊版",
            "pipx_venv_left": True, "leftover_tmp": []}
    args.update(kwargs)
    r = uc.judge_install_sh("install_sh_upgrade", **args)
    assert r["verdict"] == verdict, r


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_run_upgrade_dry_run_install_sh_scenario(tmp_path):
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--dry-run", "--scenario", "install-sh", "--out", str(tmp_path / "ev")],
                       cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "--network none" in out
    assert "install.sh:/opt/sw-installer/install.sh:ro" in out
    assert "serialwrap-0.4.0-linux-x86_64.tar.gz" in out
    for mode in ("systemd-user", "on-demand"):
        assert f"--mode {mode}" in out, out
    assert out.count("--scenario install-sh") == 2, out
    assert "/dev/ttyUSB" not in out and "--device" not in out


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_run_upgrade_rejects_unknown_scenario(tmp_path):
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--dry-run", "--scenario", "bogus", "--out", str(tmp_path / "ev")],
                       cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 2, r.stdout + r.stderr


# POSIX-only：bash runner 需 POSIX bash、git 與 python3。
@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_install_sh_scenario_rejects_systemd_system_mode(tmp_path):
    """OD-48 合入後 --mode 多了 systemd-system；install-sh 情境的 driver 沒有該模式的 phase（setup 需 --with-sudo、
    監管檢查走 system unit），組合須在 runner 與 driver 兩端都以 rc=2 拒絕，不得跑出錯置判定的報告。"""
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--dry-run", "--scenario", "install-sh", "--mode", "systemd-system",
                        "--out", str(tmp_path / "ev")], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "systemd-system" in r.stderr
    assert "docker run" not in r.stdout
    r = subprocess.run([sys.executable, str(UPGRADE_CONTAINER), "--mode", "systemd-system", "--wheel", "w.whl",
                        "--bundle", str(tmp_path / "b"), "--out", str(tmp_path / "out"), "--scenario", "install-sh",
                        "--installer", "/x/install.sh", "--bundle-tarball", "/x/t.tar.gz"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "systemd-system" in r.stderr
    assert not (tmp_path / "out").exists()


@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.supervisor_runtime
def test_docker_install_sh_upgrade_from_0_3_2(tmp_path):
    """實跑（OD-50）：兩種監管模式各自 0.3.2 pipx 安裝→停 daemon→`install.sh --bundle <0.4.0 tarball>`→升級成功。"""
    if not _docker_available():
        pytest.skip("cap_missing:docker：本機無可用 docker daemon（能力式 SKIP，證據不足，非 PASS）")
    if subprocess.run(["git", "rev-parse", "-q", "--verify", "refs/tags/v0.3.2"], cwd=REPO,
                      capture_output=True).returncode != 0:
        pytest.skip("cap_missing:tag_v0.3.2：版本庫沒有 v0.3.2 tag（證據不足，非 PASS）")
    out = tmp_path / "ev"
    r = subprocess.run(["bash", str(RUN_UPGRADE), "--scenario", "install-sh", "--out", str(out)], cwd=REPO,
                       capture_output=True, text=True, timeout=1800, env=dict(os.environ))
    reports = {}
    for mode in ("systemd-user", "on-demand"):
        path = out / mode / "upgrade-report.json"
        reports[mode] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    diag = json.dumps({m: None if rep is None else [c for c in rep.get("checks", []) if c.get("verdict") != "PASS"]
                       for m, rep in reports.items()}, ensure_ascii=False)[:4000]
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:] + diag
    for mode, rep in reports.items():
        assert rep is not None, f"{mode} 未產生 upgrade-report.json"
        assert rep["scenario"] == "install-sh"
        assert rep["verdict"] == "PASS", diag
        assert {c["id"] for c in rep["checks"]} >= set(uc.REQUIRED_INSTALL_SH_CHECKS)
