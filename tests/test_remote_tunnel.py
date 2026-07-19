from __future__ import annotations

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
