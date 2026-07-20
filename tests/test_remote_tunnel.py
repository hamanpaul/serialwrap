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


def test_registry_remove_also_deletes_log(tmp_path):
    """`.log`（ssh stdout/stderr）須與 state/control socket 同生命週期清除，
    否則重試/失敗會在 `<run_dir>/remote/` 下累積孤兒 `.log`。"""
    reg = rt.Registry(str(tmp_path))
    with reg.lock():
        reg.write({"listen_port": 7777, "status": "active", "role": "expose"})
    log_path = reg.log_path(7777)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("ssh spawn log\n")
    assert os.path.exists(log_path)
    with reg.lock():
        reg.remove(7777)
    assert reg.read(7777) is None
    assert not os.path.exists(log_path)


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


def test_open_tunnel_already_running_noop(tmp_path):
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock")
    identity = rt.compute_identity(spec)
    reg = rt.Registry(str(tmp_path))
    with reg.lock():
        reg.write({
            "listen_port": 7777, "status": "active", "role": "expose",
            "identity": identity, "pid": os.getpid(),
            "pid_start_ticks": rt.read_pid_start_ticks(os.getpid()),
        })

    def _spawner_should_not_be_called(argv, control_path, log_path):
        raise AssertionError("已存活且 identity 相同時不應呼叫 spawner")

    res = rt.open_tunnel(spec, str(tmp_path), spawner=_spawner_should_not_be_called,
                         runner=lambda a: (0, ""), ping=lambda ep: True)
    assert res["ok"] is True
    assert res["already_running"] is True


def test_terminate_pgid_reaps_and_returns_promptly():
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    rt._terminate_pgid(pgid)
    assert proc.poll() is not None
    try:
        result = os.waitpid(proc.pid, os.WNOHANG)
    except ChildProcessError:
        result = None
    assert result is None or result[0] == proc.pid


# Task 7: status（prune 死 state + orphan scan）與 close（killpg 整組 + verify + error-state）
def test_status_prunes_dead_and_lists_alive(tmp_path):
    reg = rt.Registry(str(tmp_path))
    # `start_new_session=True`：獨立 pgid，避免與目前跑 pytest 的行程共用
    # process group（見上方 `_fake_spawner`／`test_terminate_pgid_reaps_and_returns_promptly`
    # 的既有慣例與理由；本測試雖不對 live 呼叫 killpg，仍統一套用避免僥倖依賴）。
    live = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        with reg.lock():
            reg.write({"listen_port": 7777, "status": "active", "role": "expose",
                       "pid": live.pid, "pgid": os.getpgid(live.pid),
                       "pid_start_ticks": rt.read_pid_start_ticks(live.pid),
                       "target": "u@h"})
            reg.write({"listen_port": 7778, "status": "active", "role": "connect",
                       "pid": 999999, "pgid": 999999, "pid_start_ticks": 1, "target": "u@r"})
        res = rt.status(str(tmp_path))
        ports = {t["listen_port"] for t in res["tunnels"]}
        assert 7777 in ports and 7778 not in ports  # 死的被 prune
    finally:
        live.terminate(); live.wait()


def test_close_terminates_and_removes(tmp_path):
    reg = rt.Registry(str(tmp_path))
    # `start_new_session=True`：務必獨立 pgid——`close()` 會對 pgid 送
    # `os.killpg(SIGTERM)`；若沿用 pytest 本身的 pgid，會連 pytest 行程一併
    # 殺死（已實測驗證：無此旗標時子行程與呼叫者同 pgid，killpg 會連呼叫者
    # 一起中止，整個測試行程被 SIGTERM 中斷而非乾淨地跑完斷言）。
    live = subprocess.Popen(["sleep", "30"], start_new_session=True)
    with reg.lock():
        reg.write({"listen_port": 7777, "status": "active", "role": "expose",
                   "pid": live.pid, "pgid": os.getpgid(live.pid),
                   "pid_start_ticks": rt.read_pid_start_ticks(live.pid), "target": "u@h"})
    res = rt.close(str(tmp_path), 7777)
    assert 7777 in res["closed"]
    assert reg.read(7777) is None
    assert live.poll() is not None  # 已被終止


def test_close_missing_is_idempotent(tmp_path):
    assert rt.close(str(tmp_path), 12345) == {"ok": True, "closed": []}


def test_resolve_ssh_bin_missing(monkeypatch):
    monkeypatch.setattr(rt.shutil, "which", lambda name: None)
    with pytest.raises(rt.TunnelError) as ei:
        rt.resolve_ssh_bin("ssh")
    assert ei.value.code == "SSH_NOT_FOUND"


def test_guard_platform_rejects_windows(monkeypatch):
    monkeypatch.setattr(rt.os, "name", "nt")
    with pytest.raises(rt.TunnelError) as ei:
        rt.guard_platform()
    assert ei.value.code == "REMOTE_NOT_SUPPORTED"


def test_real_ping_uses_client(monkeypatch):
    # 注意：不可寫成 `seen.setdefault("ep", ep) or {"ok": True}`——setdefault
    # 首次呼叫回傳剛設定的 ep（truthy 字串），`or` 短路後整條表達式會變成該字串
    # 而非 dict，導致 real_ping 對字串呼叫 .get() 拋例外、被吞成 False。
    seen = {}

    def _fake_rpc_call(ep, m, p, timeout_s):
        seen["ep"] = ep
        return {"ok": True}

    monkeypatch.setattr(rt, "_rpc_call", _fake_rpc_call)
    assert rt.real_ping("tcp://127.0.0.1:7777") is True
    assert seen["ep"] == "tcp://127.0.0.1:7777"


# Task 8 review fix：fail-closed 涵蓋 probe 逾時與任意 readiness 例外
def test_make_runner_timeout_returns_nonzero(monkeypatch):
    """`subprocess.run` 逾時拋 `TimeoutExpired` 時，`make_runner()` 的 `_run` 需吞下並
    回傳非 0 rc（非崩潰），讓逾時留在 readiness 狀態機內（probe 回 False 續 poll）。"""
    def _raise_timeout(argv, stdout=None, stderr=None, text=None, timeout=None, check=None):
        raise subprocess.TimeoutExpired(cmd=["x"], timeout=10.0)

    monkeypatch.setattr(rt.subprocess, "run", _raise_timeout)
    rc, msg = rt.make_runner()(["x"])
    assert rc != 0
    assert isinstance(msg, str)


def test_open_tunnel_teardown_on_nontunnel_exception(tmp_path):
    """readiness 階段丟出非 `TunnelError`（例如 `RuntimeError`）時，`open_tunnel` 仍需
    fail-closed：拆除已 spawn 的行程群組、移除 durable state，再原樣 re-raise。"""
    spec = rt.TunnelSpec(role="expose", ssh_target="u@h", port=7777, forward_src="/s.sock")
    spawn = _fake_spawner(alive=True)

    def _raising_role_probe_factory(spec, control_path, endpoint, *, runner, ping):
        def _probe():
            raise RuntimeError("probe 爆炸（非 TunnelError）")
        return _probe

    try:
        with pytest.raises(RuntimeError):
            rt.open_tunnel(
                spec, str(tmp_path), spawner=spawn,
                runner=lambda a: (0, ""), ping=lambda ep: True,
                role_probe_factory=_raising_role_probe_factory,
            )
        assert spawn.procs["p"].poll() is not None  # 已被 teardown 終止
        assert rt.Registry(str(tmp_path)).read(7777) is None  # state 已移除，fail-closed
    finally:
        with contextlib.suppress(Exception):
            spawn.procs["p"].wait(timeout=2)
