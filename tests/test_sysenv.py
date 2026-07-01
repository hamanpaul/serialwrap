"""sw_core.sysenv 的單元測試。"""


def test_fake_effects_run_snapshots_cmd():
    """run() 須以 list(cmd) 快照記錄，呼叫端事後 mutate 原 list 不可污染 calls。"""
    from sw_core.sysenv import FakeEffects
    fx = FakeEffects()
    cmd = ["x"]
    fx.run(cmd)
    cmd.append("y")
    assert fx.calls[0] == ["x"]


def test_fake_effects_records_calls_and_answers_probes():
    from sw_core.sysenv import FakeEffects

    fx = FakeEffects(
        systemd=True,
        in_groups={"dialout"},
        wsl=False,
        which={"minicom": "/usr/bin/minicom"},
        commands={("systemctl", "--user", "is-active", "serialwrap"): (0, "active", "")},
    )
    assert fx.has_systemd() is True
    assert fx.user_in_group("dialout") is True
    assert fx.user_in_group("nope") is False
    assert fx.is_wsl() is False
    assert fx.which("minicom") == "/usr/bin/minicom"
    assert fx.which("absent") is None
    rc, out, err = fx.run(["systemctl", "--user", "is-active", "serialwrap"])
    assert (rc, out) == (0, "active")
    assert ["systemctl", "--user", "is-active", "serialwrap"] in fx.calls
    # unknown command → default (0,"","") and still recorded
    rc2, _, _ = fx.run(["echo", "hi"])
    assert rc2 == 0
    assert ["echo", "hi"] in fx.calls


def test_system_effects_run_captures_returncode():
    from sw_core.sysenv import SystemEffects

    fx = SystemEffects()
    rc, out, err = fx.run(["python3", "-c", "import sys;print('x');sys.exit(3)"])
    assert rc == 3
    assert "x" in out


def test_system_effects_which_and_groups_do_not_raise():
    from sw_core.sysenv import SystemEffects

    fx = SystemEffects()
    assert fx.which("definitely-not-a-binary-xyz") is None
    # should return a bool without raising even for a bogus group
    assert isinstance(fx.user_in_group("definitely-not-a-group-xyz"), bool)
    assert isinstance(fx.is_wsl(), bool)
    assert isinstance(fx.has_systemd(), bool)


# ---------------------------------------------------------------------------
# force_utf8_stdio（#118）：Windows console 預設 cp1252 印繁中 help 會 UnicodeEncodeError，
# CLI 進入點在 win32 需把 stdout/stderr 重設為 UTF-8；非 Windows 為 no-op。
# ---------------------------------------------------------------------------

import sys as _sys


class _FakeStream:
    """記錄 reconfigure 呼叫的假 stream；可選擇拋錯以驗證容錯。"""

    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises


def test_force_utf8_stdio_reconfigures_stdout_stderr_on_win32(monkeypatch):
    from sw_core.sysenv import force_utf8_stdio

    out, err = _FakeStream(), _FakeStream()
    monkeypatch.setattr(_sys, "platform", "win32")
    monkeypatch.setattr(_sys, "stdout", out)
    monkeypatch.setattr(_sys, "stderr", err)

    force_utf8_stdio()

    assert out.calls == [{"encoding": "utf-8"}]
    assert err.calls == [{"encoding": "utf-8"}]


def test_force_utf8_stdio_is_noop_off_win32(monkeypatch):
    from sw_core.sysenv import force_utf8_stdio

    out, err = _FakeStream(), _FakeStream()
    monkeypatch.setattr(_sys, "platform", "linux")
    monkeypatch.setattr(_sys, "stdout", out)
    monkeypatch.setattr(_sys, "stderr", err)

    force_utf8_stdio()

    assert out.calls == []
    assert err.calls == []


def test_force_utf8_stdio_tolerates_stream_without_reconfigure(monkeypatch):
    from sw_core.sysenv import force_utf8_stdio

    class _NoReconfigure:
        pass

    monkeypatch.setattr(_sys, "platform", "win32")
    monkeypatch.setattr(_sys, "stdout", _NoReconfigure())
    monkeypatch.setattr(_sys, "stderr", _NoReconfigure())

    force_utf8_stdio()  # 不得拋錯


def test_force_utf8_stdio_tolerates_reconfigure_error(monkeypatch):
    from sw_core.sysenv import force_utf8_stdio

    monkeypatch.setattr(_sys, "platform", "win32")
    monkeypatch.setattr(_sys, "stdout", _FakeStream(raises=ValueError("locked")))
    monkeypatch.setattr(_sys, "stderr", _FakeStream(raises=OSError("io")))

    force_utf8_stdio()  # 不得拋錯（吞 ValueError/OSError）
