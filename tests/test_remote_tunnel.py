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
