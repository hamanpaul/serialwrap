"""realhw case 薄層單測（monkeypatch，不碰 docker/真板）。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from realhw import harness
from realhw.cases import p1_hotplug


class _Ctx:
    def __init__(self, tmp_path):
        self.cfg = {"boards": [{"com": "COM1", "serial": "AQ00OAQ7", "busid": "8-2"}]}
        self.report_dir = tmp_path
        self.case_dir = tmp_path
        self.sw = None
        self.tmux = None
        self.systemd = None
        self.usbipd = None
        self.win = None

    def note(self, name: str, content: str) -> str:
        p = self.case_dir / name
        p.write_text(content, encoding="utf-8")
        return p.name


def test_attach_with_rescue_releases_windows_holder_then_succeeds(tmp_path):
    ctx = _Ctx(tmp_path)
    calls = []

    class _Usbipd:
        def __init__(self):
            self.rcs = [1, 0]

        def attach(self, busid):
            calls.append(("attach", busid))
            return self.rcs.pop(0)

    class _Win:
        def available(self):
            return True

        def held_devices(self):
            calls.append(("held",))
            return [{"com": "COM3", "state": "READY", "device_by_id": "AQ00OAQ7"}]

        def release(self, com):
            calls.append(("release", com))
            return {"ok": True, "_rc": 0}

    ctx.usbipd = _Usbipd()
    ctx.win = _Win()
    ok, code = p1_hotplug._attach_with_rescue(ctx, ctx.cfg["boards"][0], [])
    assert (ok, code) == (True, "")
    assert ("release", "COM3") in calls


def test_attach_with_rescue_exhaustion_reports_windows_holder_reason(tmp_path):
    ctx = _Ctx(tmp_path)

    class _Usbipd:
        def attach(self, busid):
            return 1

    class _Win:
        def available(self):
            return True

        def held_devices(self):
            return [{"com": "COM3", "state": "READY", "device_by_id": "AQ00OAQ7"}]

        def release(self, com):
            return {"ok": True, "_rc": 0}

    ctx.usbipd = _Usbipd()
    ctx.win = _Win()
    ok, code = p1_hotplug._attach_with_rescue(ctx, ctx.cfg["boards"][0], [])
    assert (ok, code) == (False, "windows_daemon_holds_device")


def test_remote_cases_register_and_rm_live_e2e_is_monkeypatchable(tmp_path, monkeypatch):
    from realhw.cases import remote

    ids = {c.id for c in harness.REGISTRY if c.tier == "remote"}
    assert {"rm-topo-direct", "rm-topo-nat-host", "rm-topo-dual-nat", "rm-topo-gwports",
            "rm-live-e2e", "rm-live-orphan", "rm-live-cycle"} <= ids

    monkeypatch.setattr(remote, "_ensure_image", lambda ctx: "docker build 失敗 rc=1")
    ctx = _Ctx(tmp_path)
    ctx.sw = SimpleNamespace(run=lambda *args, **kwargs: {})
    result = remote.rm_live_e2e(ctx)
    assert result.verdict == "FAIL"
    assert result.category == "environment"
    assert result.reason_code == "docker_build_failed"


def test_start_ssh_peer_exports_key_without_absolute_home_literal(tmp_path, monkeypatch):
    from realhw.cases import remote

    ctx = _Ctx(tmp_path)
    calls = []

    def fake_run(argv, capture_output=True, text=True, timeout=None, env=None):
        calls.append(tuple(str(x) for x in argv))
        if argv[:3] == ["docker", "rm", "-f"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:5] == ["docker", "run", "-d", "--init", "--name"]:
            return SimpleNamespace(returncode=0, stdout="cid\n", stderr="")
        if argv[:4] == ["docker", "exec", argv[2], "bash"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:3] == ["docker", "exec", argv[2]] and argv[3:] == ["pgrep", "-x", "sshd"]:
            return SimpleNamespace(returncode=0, stdout="123\n", stderr="")
        if argv[:3] == ["docker", "inspect", "-f"]:
            return SimpleNamespace(returncode=0, stdout="172.18.0.3\n", stderr="")
        if argv[:4] == ["docker", "exec", "-u", "tester"] and argv[5:7] == ["bash", "-lc"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:2] == ["docker", "cp"]:
            Path(argv[-1]).write_text("KEY", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:4] == ["docker", "exec", "-u", "tester"] and argv[5:7] == ["rm", "-f"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(remote.subprocess, "run", fake_run)

    name, ip, key = remote._start_ssh_peer(ctx, "unit")

    assert name.startswith("rhwlive-unit-")
    assert ip == "172.18.0.3"
    assert key == tmp_path / "id_ed25519"
    assert key.read_text(encoding="utf-8") == "KEY"
    banned = "/" + "home" + "/tester/"
    assert all(banned not in " ".join(cmd) for cmd in calls)
