from __future__ import annotations

import contextlib
import json
import os
import subprocess

import pytest

from sw_core import remote_tunnel as rt


def test_parse_target_user_host_port():
    assert rt.parse_target("tester@relay:7777") == ("tester@relay", 7777)


def test_parse_target_alias_only():
    assert rt.parse_target("myrelay:22000") == ("myrelay", 22000)


@pytest.mark.parametrize("bad", ["nohost", "host:", "host:abc", "host:-1", "", "a@b@c:1"])
def test_parse_target_rejects_bad(bad):
    with pytest.raises(rt.TunnelError) as ei:
        rt.parse_target(bad)
    assert ei.value.code == "INVALID_TARGET"


def test_identity_stable_and_distinguishes_remote_socket():
    a = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/run/s.sock")
    b = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/run/s.sock")
    c = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/run/s.sock",
                      remote_socket="/relay/b.sock")
    assert rt.compute_identity(a) == rt.compute_identity(b)
    assert rt.compute_identity(a) != rt.compute_identity(c)


def test_identity_distinguishes_via_and_ssh_opts():
    base = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/run/s.sock")
    assert rt.compute_identity(base) != rt.compute_identity(
        rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/run/s.sock", via="autossh"))
    assert rt.compute_identity(base) != rt.compute_identity(
        rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/run/s.sock",
                      ssh_opts=("-p", "2222")))


def _cp():
    return "/run/user/1000/serialwrap/remote/cm-abc"


def test_build_argv_expose_unix_socket_loopback_bind():
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777,
                         forward_src="/run/serialwrapd.sock")
    argv = rt.build_argv(spec, _cp())
    assert argv[0] == "ssh"
    assert "-N" in argv and "-T" in argv
    assert "-R" in argv
    assert argv[argv.index("-R") + 1] == "127.0.0.1:7777:/run/serialwrapd.sock"
    joined = " ".join(argv)
    assert "BatchMode=yes" in joined
    assert "ExitOnForwardFailure=yes" in joined
    assert "ControlMaster=auto" in joined
    assert f"ControlPath={_cp()}" in joined
    assert argv[-1] == "u@h"


def test_build_argv_expose_remote_socket_hardened():
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777,
                         forward_src="/run/serialwrapd.sock", remote_socket="/relay/x.sock")
    argv = rt.build_argv(spec, _cp())
    assert argv[argv.index("-R") + 1] == "/relay/x.sock:/run/serialwrapd.sock"


def test_build_argv_expose_tcp_forward_src():
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777,
                         forward_src="127.0.0.1:48700")
    argv = rt.build_argv(spec, _cp())
    assert argv[argv.index("-R") + 1] == "127.0.0.1:7777:127.0.0.1:48700"


def test_build_argv_connect_tcp_loopback():
    spec = rt.TunnelSpec(role="connect", ssh_target="u@relay", port=7777, local=7777)
    argv = rt.build_argv(spec, _cp())
    assert argv[argv.index("-L") + 1] == "127.0.0.1:7777:localhost:7777"


def test_build_argv_connect_remote_socket():
    spec = rt.TunnelSpec(role="connect", ssh_target="u@relay", port=7777, local=7788,
                         remote_socket="/relay/x.sock")
    argv = rt.build_argv(spec, _cp())
    assert argv[argv.index("-L") + 1] == "127.0.0.1:7788:/relay/x.sock"


def test_build_argv_autossh_and_ssh_opts_passthrough():
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777,
                         forward_src="/s.sock", via="autossh", ssh_opts=("-p", "2222"))
    argv = rt.build_argv(spec, _cp())
    assert argv[0] == "autossh"
    assert argv[1:3] == ["-M", "0"]
    assert "-p" in argv and argv[argv.index("-p") + 1] == "2222"


# Task 3: Registry + PID liveness
def test_registry_write_read_remove_atomic(tmp_path):
    reg = rt.Registry(str(tmp_path))
    with reg.lock():
        reg.write({"listen_port": 7777, "status": "active", "role": "expose"})
    assert reg.read(7777)["status"] == "active"
    assert [s["listen_port"] for s in reg.read_all()] == [7777]
    with reg.lock():
        reg.remove(7777)
    assert reg.read(7777) is None


def test_pid_alive_true_for_running_and_reuse_guard(tmp_path):
    proc = subprocess.Popen(["sleep", "30"])
    try:
        ticks = rt.read_pid_start_ticks(proc.pid)
        assert ticks is not None
        assert rt.pid_alive(proc.pid, ticks) is True
        # start-ticks 不符 → 視為非我方（PID reuse 防護）
        assert rt.pid_alive(proc.pid, ticks + 999999) is False
    finally:
        proc.terminate()
        proc.wait()


def test_pid_alive_false_for_dead(tmp_path):
    proc = subprocess.Popen(["true"])
    proc.wait()
    assert rt.pid_alive(proc.pid, 12345) is False


def test_lock_is_exclusive_serialized(tmp_path):
    # 兩次 lock 不會同時持有（LOCK_EX）；此處驗證可重入取得、釋放後再取。
    reg = rt.Registry(str(tmp_path))
    with reg.lock():
        pass
    with reg.lock():
        reg.write({"listen_port": 1, "status": "spawning", "role": "connect"})
    assert reg.read(1)["status"] == "spawning"


# Task 4: readiness 狀態機（注入式 probe）
class _Clock:
    """注入時鐘，用於測試 wait_ready。"""
    def __init__(self):
        self.t = 0.0
    def monotonic(self):
        return self.t
    def sleep(self, dt):
        self.t += dt


def _spec_R():
    """測試用的 expose spec。"""
    return rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock",
                         ready_timeout=5.0)


def test_wait_ready_active_when_check_and_probe_ok():
    clk = _Clock()
    st = rt.wait_ready(_spec_R(), pid=1, start_ticks=1,
                       ssh_check=lambda: True, role_probe=lambda: True,
                       sleep=clk.sleep, monotonic=clk.monotonic,
                       alive=lambda: True, stderr_tail=lambda: "")
    assert st == "active"


def test_wait_ready_starting_on_timeout_process_alive():
    clk = _Clock()
    st = rt.wait_ready(_spec_R(), pid=1, start_ticks=1,
                       ssh_check=lambda: False, role_probe=lambda: False,
                       sleep=clk.sleep, monotonic=clk.monotonic,
                       alive=lambda: True, stderr_tail=lambda: "")
    assert st == "starting"


def test_wait_ready_spawn_failed_when_process_dies():
    clk = _Clock()
    with pytest.raises(rt.TunnelError) as ei:
        rt.wait_ready(_spec_R(), pid=1, start_ticks=1,
                      ssh_check=lambda: False, role_probe=lambda: False,
                      sleep=clk.sleep, monotonic=clk.monotonic,
                      alive=lambda: False, stderr_tail=lambda: "Permission denied (publickey).")
    assert ei.value.code == "TUNNEL_SPAWN_FAILED"
    assert "publickey" in (ei.value.message or "")


def test_wait_ready_remote_bind_unverified_propagates():
    clk = _Clock()
    def probe():
        raise rt.TunnelError("REMOTE_BIND_UNVERIFIED", "0.0.0.0:7777")
    with pytest.raises(rt.TunnelError) as ei:
        rt.wait_ready(_spec_R(), pid=1, start_ticks=1,
                      ssh_check=lambda: True, role_probe=probe,
                      sleep=clk.sleep, monotonic=clk.monotonic,
                      alive=lambda: True, stderr_tail=lambda: "")
    assert ei.value.code == "REMOTE_BIND_UNVERIFIED"


# Task 5: probe 具體實作（ssh -O check / ss 遠端 bind / health.ping）
def test_ssh_check_true_on_rc0():
    calls = []
    def runner(argv):
        calls.append(argv)
        return (0, "")
    chk = rt.make_ssh_check(rt.TunnelSpec(role="expose", ssh_target="u@h", port=1),
                            "/cp", runner=runner)
    assert chk() is True
    assert "-O" in calls[0] and "check" in calls[0]


def test_role_probe_expose_tcp_rejects_wildcard():
    def runner(argv):
        return (0, "LISTEN 0 128 0.0.0.0:7777 0.0.0.0:*")
    probe = rt.make_role_probe(
        rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock"),
        "/cp", "tcp://127.0.0.1:7777", runner=runner, ping=lambda ep: True)
    with pytest.raises(rt.TunnelError) as ei:
        probe()
    assert ei.value.code == "REMOTE_BIND_UNVERIFIED"


def test_role_probe_expose_tcp_accepts_loopback():
    def runner(argv):
        return (0, "LISTEN 0 128 127.0.0.1:7777 0.0.0.0:*")
    probe = rt.make_role_probe(
        rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock"),
        "/cp", "tcp://127.0.0.1:7777", runner=runner, ping=lambda ep: True)
    assert probe() is True


def test_role_probe_expose_tcp_rejects_interface_ip():
    def runner(argv):
        return (0, "LISTEN 0 128 10.0.0.5:7777 0.0.0.0:*")
    probe = rt.make_role_probe(
        rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock"),
        "/cp", "tcp://127.0.0.1:7777", runner=runner, ping=lambda ep: True)
    with pytest.raises(rt.TunnelError) as ei:
        probe()
    assert ei.value.code == "REMOTE_BIND_UNVERIFIED"


def test_role_probe_expose_tcp_accepts_ipv6_loopback():
    def runner(argv):
        return (0, "LISTEN 0 128 [::1]:7777 [::]:*")
    probe = rt.make_role_probe(
        rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock"),
        "/cp", "tcp://127.0.0.1:7777", runner=runner, ping=lambda ep: True)
    assert probe() is True


def test_role_probe_expose_remote_socket_skips_bind_check():
    def runner(argv):
        raise AssertionError("unix 模式不應跑 ss")
    probe = rt.make_role_probe(
        rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock",
                      remote_socket="/relay/x.sock"),
        "/cp", "", runner=runner, ping=lambda ep: True)
    assert probe() is True


def test_role_probe_connect_uses_health_ping():
    seen = {}
    probe = rt.make_role_probe(
        rt.TunnelSpec(role="connect", ssh_target="u@relay", port=7777, local=7777),
        "/cp", "tcp://127.0.0.1:7777",
        runner=lambda argv: (0, ""), ping=lambda ep: seen.setdefault("ep", ep) or True)
    assert probe() is True
    assert seen["ep"] == "tcp://127.0.0.1:7777"


# Task 6: open_tunnel 編排（spawn → durable state → readiness → active/starting）
class _FakeProc:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None


def _fake_spawner(alive=True, stderr=""):
    procs = {}

    def spawn(argv, control_path, log_path):
        # `start_new_session=True`：讓存活分支的子行程落在獨立 pgid，
        # 不與目前跑 pytest 的行程共用 process group——否則
        # `_terminate_pgid` 的 fail-closed teardown 對 pgid 送
        # `os.killpg(SIGTERM)` 時會連 pytest 本身一併殺死（已實測驗證）。
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True) if alive \
            else subprocess.Popen(["true"])
        if not alive:
            proc.wait()
        procs["p"] = proc
        return proc.pid, os.getpgid(proc.pid) if alive else proc.pid, \
            rt.read_pid_start_ticks(proc.pid), (lambda: stderr)
    spawn.procs = procs
    return spawn


def test_open_tunnel_expose_active(tmp_path):
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock")
    spawn = _fake_spawner(alive=True)
    try:
        res = rt.open_tunnel(spec, str(tmp_path), spawner=spawn,
                             runner=lambda a: (0, "LISTEN 0 128 127.0.0.1:7777 *:*"),
                             ping=lambda ep: True)
        assert res["ok"] and res["status"] == "active" and res["role"] == "expose"
        reg = rt.Registry(str(tmp_path))
        assert reg.read(7777)["status"] == "active"
    finally:
        spawn.procs["p"].terminate(); spawn.procs["p"].wait()


def test_open_tunnel_conflict_same_port_diff_identity(tmp_path):
    reg = rt.Registry(str(tmp_path))
    with reg.lock():
        reg.write({"listen_port": 7777, "status": "active", "role": "expose",
                   "identity": "OTHER", "pid": os.getpid(),
                   "pid_start_ticks": rt.read_pid_start_ticks(os.getpid())})
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock")
    with pytest.raises(rt.TunnelError) as ei:
        rt.open_tunnel(spec, str(tmp_path), spawner=_fake_spawner(),
                       runner=lambda a: (0, ""), ping=lambda ep: True)
    assert ei.value.code == "TUNNEL_CONFLICT"


def test_open_tunnel_fail_closed_removes_state_on_bind_unverified(tmp_path):
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock")
    spawn = _fake_spawner(alive=True)
    try:
        with pytest.raises(rt.TunnelError) as ei:
            rt.open_tunnel(spec, str(tmp_path), spawner=spawn,
                           runner=lambda a: (0, "LISTEN 0 128 0.0.0.0:7777 *:*"),  # wildcard
                           ping=lambda ep: True)
        assert ei.value.code == "REMOTE_BIND_UNVERIFIED"
        assert rt.Registry(str(tmp_path)).read(7777) is None  # 拆除、不留暴露隧道
    finally:
        with contextlib.suppress(Exception):
            spawn.procs["p"].wait(timeout=2)
