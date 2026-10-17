# tests/test_stage0_remote_windows.py
"""P1 Stage 0 Task T14 的 A1 紅燈測試：BC-05 Windows remote -R/-L、SC-27 connect／benches、
SC-21 remote stop、跨行程檔案鎖（sw_core.filelock）。

依 02-stage0-python-bc.md Global Constraints：本模組不在頂層 import 本 Phase 才新增的
模組或名稱（`sw_core.filelock`、`TunnelSpec` 之外的新函式等），一律於測試函式內以
`_load()` 系列輔助函式延遲 import，使 `python3 -m pytest -q --collect-only tests/` 在任何階段都能完成收集。

marker 規則（P0 T14）：本檔不設模組層資源 marker（部分測試需子行程，模組層 memory_only 會與
process_rpc 混合，違反 `test_module_resource_marker_not_mixed`），每個測試函式各自標註
「恰一個平台 marker ＋ 至少一個資源 marker」。windows_native 測試於非 nt 平台 skipif，
不計為 PASS（00-master-plan §3：證據不足）。
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# 子行程以 raw fcntl 持有 flock（不依賴本 Phase 新增的 sw_core.filelock）
_POSIX_HOLD_CHILD = (
    "import fcntl, os, sys, time\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('locked', flush=True)\n"
    "time.sleep(60)\n"
)

# 原生 Windows 子行程以 sw_core.filelock 持鎖（LockFileEx）
_NT_HOLD_CHILD = (
    "import sys, time\n"
    "from sw_core.filelock import exclusive_file_lock\n"
    "cm = exclusive_file_lock(sys.argv[1])\n"
    "cm.__enter__()\n"
    "print('locked', flush=True)\n"
    "time.sleep(60)\n"
)


def _rt():
    """延遲載入 sw_core.remote_tunnel（原生 Windows 於 B1 前仍會因頂層 import fcntl 失敗，屬預期紅燈）。"""
    from sw_core import remote_tunnel

    return remote_tunnel


def _cli():
    from sw_core import cli

    return cli


def _filelock():
    """延遲載入 sw_core.filelock（T14 B1 才建立，A1 階段此處拋 ModuleNotFoundError）。"""
    from sw_core import filelock

    return filelock


def _connect_spec(rt):
    return rt.TunnelSpec(role="connect", ssh_target="u@h", port=48701, remote_socket="/run/s.sock")


def _expose_tcp_spec(rt):
    return rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="127.0.0.1:7777")


def _run_cli(cli, argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip() else None


def _bench_env(monkeypatch, tmp_path: Path) -> None:
    """設定 bench 相關環境：benches.yaml（含 eit-test）、state 目錄、run 目錄，全部指向 tmp_path。"""
    benches = tmp_path / "benches.yaml"
    benches.write_text(
        "benches:\n"
        "  eit-test:\n"
        "    target: eit@eit-test.hamanpaul.cc\n"
        "    remote_socket: /run/serialwrap/serialwrapd.sock\n"
        "    local_port: 7777\n"
        "    ssh_opts: []\n"
        "    autossh: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SERIALWRAP_BENCHES_FILE", str(benches))
    monkeypatch.setenv("SERIALWRAP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path / "run"))


# ---------------------------------------------------------------------------
# argv／control master（BC-05：Windows 不帶 ControlMaster 三組 -o）
# ---------------------------------------------------------------------------


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_argv_without_control_master_drops_three_options():
    rt = _rt()
    a = rt.build_argv(_connect_spec(rt), "cm-48701", control_master=False)
    joined = " ".join(a)
    assert "ControlMaster" not in joined and "ControlPath" not in joined and "ControlPersist" not in joined
    assert a[:3] == ["ssh", "-N", "-T"] and a[-1] == "u@h"
    assert "-L" in a and "127.0.0.1:48701:/run/s.sock" in a


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_argv_with_control_master_unchanged_from_baseline():
    rt = _rt()
    cp = "cm-48701"
    assert rt.build_argv(_connect_spec(rt), cp) == (
        ["ssh", "-N", "-T"] + rt.default_ssh_opts(cp) + ["-L", "127.0.0.1:48701:/run/s.sock", "u@h"]
    )


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_supports_control_master_by_platform():
    rt = _rt()
    assert rt.supports_control_master("posix") is True
    assert rt.supports_control_master("nt") is False


# ---------------------------------------------------------------------------
# 行程存活判定（Windows 不得呼叫 os.kill(pid, 0)）
# ---------------------------------------------------------------------------


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_windows_pid_alive_never_calls_os_kill(monkeypatch):
    rt = _rt()

    def _no_kill(*args, **kwargs):
        raise AssertionError("os.kill used")

    monkeypatch.setattr(os, "kill", _no_kill)
    monkeypatch.setattr(rt, "_win_process_state", lambda pid: (True, 1234))
    assert rt.pid_alive(42, 1234, os_name="nt") is True
    assert rt.pid_alive(42, 999, os_name="nt") is False


# ---------------------------------------------------------------------------
# readiness（無 ControlMaster 時 role probe／ssh check 的替代路徑）
# ---------------------------------------------------------------------------


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_role_probe_connect_without_cm_uses_ping():
    rt = _rt()
    probe = rt.make_role_probe(
        _connect_spec(rt),
        "cm-48701",
        "tcp://127.0.0.1:48701",
        runner=None,
        ping=lambda ep: ep == "tcp://127.0.0.1:48701",
        control_master=False,
    )
    assert probe() is True


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wait_ready_expose_tcp_without_cm_uses_separate_ssh(tmp_path):
    rt = _rt()
    spec = rt.TunnelSpec(
        role="expose", ssh_target="u@h", port=7777,
        forward_src="127.0.0.1:7777", ssh_opts=("-o", "BatchMode=no"),
    )
    control_path = str(tmp_path / "cm-7777")
    endpoint = "tcp://127.0.0.1:7777"
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return 0, "LISTEN 0 128 127.0.0.1:7777 0.0.0.0:*\n"

    status = rt.wait_ready(
        spec,
        1,
        None,
        ssh_check=rt.make_ssh_check(spec, control_path, runner=runner, control_master=False),
        role_probe=rt.make_role_probe(
            spec, control_path, endpoint, runner=runner, ping=lambda ep: True, control_master=False
        ),
        sleep=lambda seconds: None,
        monotonic=lambda: 0.0,
        alive=lambda: True,
        stderr_tail=lambda: "",
    )
    assert status == "active"
    assert calls, "expose tcp 無 ControlMaster 時應以獨立 ssh 執行 ss 驗證"
    for argv in calls:
        assert not any("ControlPath" in part for part in argv)
        assert argv[0] == "ssh" and "BatchMode=yes" in argv and "ss" in argv
        assert argv.index("BatchMode=yes") < argv.index("BatchMode=no")


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_role_probe_expose_tcp_without_cm_retries_empty_listener_result():
    rt = _rt()
    spec = _expose_tcp_spec(rt)
    outputs = iter([
        (0, ""),
        (0, "LISTEN 0 128 127.0.0.1:7777 0.0.0.0:*\n"),
    ])
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return next(outputs)

    probe = rt.make_role_probe(
        spec, "cm-7777", "tcp://127.0.0.1:7777", runner=runner,
        ping=lambda ep: False, control_master=False,
    )

    assert probe() is False
    assert probe() is True
    assert len(calls) == 2


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_role_probe_expose_tcp_without_cm_retries_ssh_failure():
    rt = _rt()
    probe = rt.make_role_probe(
        _expose_tcp_spec(rt), "cm-7777", "tcp://127.0.0.1:7777",
        runner=lambda argv: (255, "Could not resolve hostname\n"),
        ping=lambda ep: False, control_master=False,
    )

    assert probe() is False


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_role_probe_remote_socket_without_cm_requires_socket_evidence():
    rt = _rt()
    spec = rt.TunnelSpec(
        role="expose", ssh_target="u@h", port=7777,
        forward_src="/run/serialwrapd.sock", remote_socket="/relay/x.sock",
        ssh_opts=("-o", "BatchMode=no"),
    )
    replies = iter([(1, ""), (0, "")])
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return next(replies)

    probe = rt.make_role_probe(
        spec, "cm-7777", "", runner=runner, ping=lambda ep: False,
        control_master=False,
    )

    assert probe() is False
    assert probe() is True
    assert calls[0][0:3] == ["ssh", "-o", "BatchMode=yes"]
    assert calls[0].index("BatchMode=yes") < calls[0].index("BatchMode=no")
    assert calls[0][-3:] == ["test", "-S", "/relay/x.sock"]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_open_tunnel_expose_tcp_timeout_fails_closed(tmp_path, monkeypatch):
    rt = _rt()
    monkeypatch.setattr(rt, "supports_control_master", lambda: False)
    monkeypatch.setattr(rt, "pid_alive", lambda pid, start_ticks, **kwargs: True)
    terminate_calls: list[tuple] = []

    def terminate_tree(pid, pgid, **kwargs):
        terminate_calls.append((pid, pgid, kwargs.get("start_ticks")))

    monkeypatch.setattr(rt, "terminate_tree", terminate_tree)

    class _Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clk = _Clock()
    spec = rt.TunnelSpec(
        role="expose", ssh_target="u@h", port=7777,
        forward_src="127.0.0.1:7777", ready_timeout=0.25,
    )
    with pytest.raises(rt.TunnelError) as ei:
        rt.open_tunnel(
            spec, str(tmp_path),
            spawner=lambda argv, cp, log: (4242, None, 99, lambda: ""),
            runner=lambda argv: (0, ""), ping=lambda ep: False, clock=clk,
        )

    assert ei.value.code == "REMOTE_BIND_UNVERIFIED"
    assert terminate_calls == [(4242, None, 99)]
    assert rt.Registry(str(tmp_path)).read(7777) is None


# ---------------------------------------------------------------------------
# SC-21 remote stop 別名、SC-27 connect／benches 與 remote status 不再受 nt 擋板
# ---------------------------------------------------------------------------


@pytest.mark.core_portable
@pytest.mark.process_rpc
def test_remote_stop_is_alias_of_close(tmp_path):
    env = {**os.environ, "SERIALWRAP_RUN_DIR": str(tmp_path)}
    a = subprocess.run(
        [sys.executable, "-m", "sw_core.cli", "remote", "stop", "all"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    b = subprocess.run(
        [sys.executable, "-m", "sw_core.cli", "remote", "close", "all"],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )
    assert a.returncode == b.returncode == 0
    assert a.stdout == b.stdout == '{"closed":[],"ok":true}\n'


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_cli_remote_not_blocked_on_simulated_nt(tmp_path, monkeypatch, capsys):
    cli, rt = _cli(), _rt()
    monkeypatch.setenv("SERIALWRAP_RUN_DIR", str(tmp_path))
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(rt, "status", lambda run_dir: {"ok": True, "tunnels": []})
    rc, obj = _run_cli(cli, ["remote", "status"], capsys)
    assert rc == 0 and obj == {"ok": True, "tunnels": []}


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_connect_not_blocked_on_simulated_nt(tmp_path, monkeypatch, capsys):
    cli, rt = _cli(), _rt()
    _bench_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(rt, "resolve_ssh_bin", lambda via: "ssh")
    captured: dict = {}

    def fake_open(spec, run_dir, **kwargs):
        captured["spec"] = spec
        return {"ok": True, "status": "active", "role": "connect", "listen_port": spec.port}

    monkeypatch.setattr(rt, "open_tunnel", fake_open)
    # endpoint 記憶寫檔的鎖由 filelock 負責（另有 test_bench_state_lock_uses_filelock）；此處只驗派送路徑
    monkeypatch.setattr(cli, "_remember_bench_endpoint", lambda code, endpoint: None)
    rc, obj = _run_cli(cli, ["connect", "eit-test"], capsys)
    assert rc == 0 and obj["ok"] is True
    assert captured["spec"].role == "connect"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_benches_not_blocked_on_simulated_nt(tmp_path, monkeypatch, capsys):
    cli, rt = _cli(), _rt()
    _bench_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.os, "name", "nt")
    monkeypatch.setattr(rt, "status", lambda run_dir: {"ok": True, "tunnels": []})
    rc, obj = _run_cli(cli, ["benches"], capsys)
    assert rc == 0 and obj["ok"] is True
    assert [b["code"] for b in obj["benches"]] == ["eit-test"]


# ---------------------------------------------------------------------------
# spawn／registry／停止（Windows 語意以替身與 nt 分支驗證）
# ---------------------------------------------------------------------------


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_registry_windows_fields(tmp_path, monkeypatch):
    rt = _rt()

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            self.pid = 4321

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    # 以下為 Windows 專屬常數與 API 替身：Linux 上 subprocess 無此屬性，raising=False
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(rt, "_win_creation_time", lambda pid: 133456789012345678)
    monkeypatch.setattr(rt, "pid_alive", lambda pid, start_ticks, **kwargs: True)
    monkeypatch.setattr(rt.Registry, "lock", lambda self: contextlib.nullcontext())

    res = rt.open_tunnel(
        _expose_tcp_spec(rt),
        str(tmp_path),
        spawner=rt.real_spawner,
        runner=lambda argv: (0, ""),
        ping=lambda ep: True,
        ssh_check_factory=lambda *args, **kwargs: (lambda: True),
        role_probe_factory=lambda *args, **kwargs: (lambda: True),
    )
    assert res["ok"] is True
    state = rt.Registry(str(tmp_path)).read(7777)
    assert state is not None
    assert state["pgid"] is None
    assert isinstance(state["pid_start_ticks"], int)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_close_windows_uses_terminate_tree(tmp_path, monkeypatch):
    rt = _rt()
    reg = rt.Registry(str(tmp_path))
    reg.write({
        "listen_port": 7777, "status": "active", "role": "expose",
        "pid": 4242, "pgid": None, "pid_start_ticks": 99, "target": "u@h",
    })
    tree_calls: list[tuple] = []
    killpg_calls: list[int] = []

    def spy_terminate_tree(pid, pgid, **kwargs):
        tree_calls.append((pid, pgid, kwargs.get("start_ticks")))

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(rt, "pid_alive", lambda pid, start_ticks, **kwargs: True)
    monkeypatch.setattr(rt, "terminate_tree", spy_terminate_tree)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killpg_calls.append(pgid), raising=False)
    monkeypatch.setattr(rt.Registry, "lock", lambda self: contextlib.nullcontext())

    res = rt.close(str(tmp_path), "7777", sleep=lambda seconds: None)
    assert res == {"ok": True, "closed": [7777]}
    assert tree_calls == [(4242, None, 99)]
    assert killpg_calls == []


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_windows_terminate_tree_checks_identity_and_taskkill_exit(monkeypatch):
    rt = _rt()
    alive_checks: list[tuple[int, int | None]] = []
    taskkill: dict = {}

    def alive(pid, start_ticks, **kwargs):
        alive_checks.append((pid, start_ticks))
        return True

    def run(argv, **kwargs):
        taskkill["argv"] = list(argv)
        taskkill.update(kwargs)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Access denied")

    def unexpected_kill(*args, **kwargs):
        raise AssertionError("Windows termination must not send a console signal")

    monkeypatch.setattr(rt, "pid_alive", alive)
    monkeypatch.setattr(rt.subprocess, "run", run)
    monkeypatch.setattr(os, "kill", unexpected_kill)

    with pytest.raises(OSError, match="taskkill"):
        rt.terminate_tree(4242, None, timeout=1.0, sleep=lambda seconds: None,
                          os_name="nt", start_ticks=9876)

    assert taskkill["argv"] == ["taskkill", "/PID", "4242", "/T", "/F"]
    assert taskkill["timeout"] == 10.0
    assert alive_checks and all(check == (4242, 9876) for check in alive_checks)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_windows_terminate_tree_waits_until_same_process_identity_exits(monkeypatch):
    rt = _rt()
    alive_checks: list[tuple[int, int | None]] = []
    states = iter([True, True, False, False])
    taskkill_calls: list[list[str]] = []

    def alive(pid, start_ticks, **kwargs):
        alive_checks.append((pid, start_ticks))
        return next(states)

    def run(argv, **kwargs):
        taskkill_calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="SUCCESS", stderr="")

    monkeypatch.setattr(rt, "pid_alive", alive)
    monkeypatch.setattr(rt.subprocess, "run", run)
    monkeypatch.setattr(os, "kill", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("Windows termination must not send a console signal")
    ))

    rt.terminate_tree(4242, None, timeout=1.0, sleep=lambda seconds: None,
                      os_name="nt", start_ticks=9876)

    assert alive_checks == [(4242, 9876)] * 4
    assert taskkill_calls == [["taskkill", "/PID", "4242", "/T", "/F"]]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_close_windows_keeps_error_state_when_tree_termination_fails(tmp_path, monkeypatch):
    rt = _rt()
    reg = rt.Registry(str(tmp_path))
    reg.write({
        "listen_port": 7777, "status": "active", "role": "expose",
        "pid": 4242, "pgid": None, "pid_start_ticks": 99, "target": "u@h",
    })

    def fail_terminate(pid, pgid, **kwargs):
        raise OSError("taskkill failed")

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(rt, "pid_alive", lambda pid, start_ticks, **kwargs: True)
    monkeypatch.setattr(rt, "terminate_tree", fail_terminate)
    monkeypatch.setattr(rt.Registry, "lock", lambda self: contextlib.nullcontext())

    res = rt.close(str(tmp_path), "7777", sleep=lambda seconds: None)

    assert res == {"ok": True, "closed": []}
    assert reg.read(7777)["status"] == "error"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_open_tunnel_retains_error_state_when_teardown_fails(tmp_path, monkeypatch):
    rt = _rt()
    monkeypatch.setattr(rt, "pid_alive", lambda pid, start_ticks, **kwargs: True)

    def fail_terminate(pid, pgid, **kwargs):
        raise OSError("taskkill failed")

    monkeypatch.setattr(rt, "terminate_tree", fail_terminate)

    def raising_probe_factory(*args, **kwargs):
        def probe():
            raise rt.TunnelError("REMOTE_BIND_UNVERIFIED", "未驗證的遠端 bind")
        return probe

    with pytest.raises(rt.TunnelError) as ei:
        rt.open_tunnel(
            _expose_tcp_spec(rt), str(tmp_path),
            spawner=lambda argv, cp, log: (4242, None, 99, lambda: ""),
            runner=lambda argv: (0, ""), ping=lambda ep: False,
            ssh_check_factory=lambda *args, **kwargs: (lambda: True),
            role_probe_factory=raising_probe_factory,
        )

    assert ei.value.code == "REMOTE_BIND_UNVERIFIED"
    state = rt.Registry(str(tmp_path)).read(7777)
    assert state is not None and state["status"] == "error"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_ssh_child_has_no_pipes(tmp_path, monkeypatch):
    rt = _rt()
    captured: dict = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            self.pid = os.getpid()

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    rt.real_spawner(["ssh", "-N"], str(tmp_path / "cm-7777"), str(tmp_path / "7777.log"))
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is not subprocess.PIPE and hasattr(captured["stdout"], "fileno")
    assert captured["stderr"] is not subprocess.PIPE and hasattr(captured["stderr"], "fileno")


@pytest.mark.windows_native
@pytest.mark.process_rpc
@pytest.mark.skipif(os.name != "nt", reason="windows_native：僅原生 Windows 執行（CreateProcess／taskkill）")
def test_native_spawn_alive_terminate_tree(tmp_path):
    rt = _rt()
    child_pid_path = tmp_path / "child.pid"
    child_code = "import time; time.sleep(60)"
    code = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c',sys.argv[1]]); "
        "open(sys.argv[2],'w',encoding='utf-8').write(str(child.pid)); "
        "time.sleep(60)"
    )
    log = tmp_path / "t.log"
    pid, pgid, ticks, _tail = rt.real_spawner(
        [sys.executable, "-c", code, child_code, str(child_pid_path)],
        str(tmp_path / "cm"), str(log),
    )
    assert pgid is None and isinstance(ticks, int)
    assert rt.pid_alive(pid, ticks) is True
    deadline = time.monotonic() + 5.0
    while not child_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    child_ticks = rt._win_creation_time(child_pid)
    assert child_ticks is not None and rt.pid_alive(child_pid, child_ticks) is True
    rt.terminate_tree(pid, None, timeout=2.0, start_ticks=ticks)
    time.sleep(0.5)
    assert rt.pid_alive(pid, ticks) is False
    assert rt.pid_alive(child_pid, child_ticks) is False
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
    assert str(pid) not in out
    child_out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {child_pid}"], capture_output=True, text=True
    ).stdout
    assert str(child_pid) not in child_out


# ---------------------------------------------------------------------------
# 跨行程檔案鎖（sw_core.filelock）
# ---------------------------------------------------------------------------


@pytest.mark.posix_only
@pytest.mark.process_rpc
@pytest.mark.skipif(os.name != "posix", reason="posix_only：僅 POSIX 執行（flock）")
def test_filelock_posix_blocks_second_holder(tmp_path):
    filelock = _filelock()
    lock_path = str(tmp_path / "state.lock")
    holder = subprocess.Popen(
        [sys.executable, "-c", _POSIX_HOLD_CHILD, lock_path],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(BlockingIOError):
            with filelock.exclusive_file_lock(lock_path, blocking=False):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=10)
        holder.stdout.close()


@pytest.mark.windows_native
@pytest.mark.process_rpc
@pytest.mark.skipif(os.name != "nt", reason="windows_native：僅原生 Windows 執行（LockFileEx）")
def test_native_filelock_windows_blocks_second_holder(tmp_path):
    filelock = _filelock()
    lock_path = str(tmp_path / "state.lock")
    holder = subprocess.Popen(
        [sys.executable, "-c", _NT_HOLD_CHILD, lock_path],
        cwd=str(REPO_ROOT), stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(BlockingIOError):
            with filelock.exclusive_file_lock(lock_path, blocking=False):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=10)
        holder.stdout.close()


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("simulated_os", [None, "nt"])
def test_bench_state_lock_uses_filelock(tmp_path, monkeypatch, simulated_os):
    cli = _cli()
    filelock = _filelock()
    monkeypatch.setenv("SERIALWRAP_STATE_DIR", str(tmp_path))
    lock_calls: list[str] = []

    @contextlib.contextmanager
    def spy_exclusive_file_lock(path, *, blocking=True):
        lock_calls.append(path)
        yield

    monkeypatch.setattr(filelock, "exclusive_file_lock", spy_exclusive_file_lock)
    if simulated_os is not None:
        monkeypatch.setattr(cli.os, "name", simulated_os)

    cli._remember_bench_endpoint("eit-test", "tcp://127.0.0.1:7777")
    assert lock_calls == [str(tmp_path / "benches.state.json") + ".lock"]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_readme_mentions_remote_stop():
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    idx = text.index("\n## 繁體中文")
    english, chinese = text[:idx], text[idx:]
    assert "## English" in english
    assert "remote stop" in english
    assert "remote stop" in chinese
