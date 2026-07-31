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
# which_all()／run(timeout_s=...)（#154：doctor 多份安裝診斷需要）
# ---------------------------------------------------------------------------

import os
import stat
import tempfile


def _make_executable(path):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho hi\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_system_effects_which_all_finds_all_matches_in_path_order(monkeypatch):
    from sw_core.sysenv import SystemEffects

    with tempfile.TemporaryDirectory() as tmp:
        dir_a = os.path.join(tmp, "a")
        dir_b = os.path.join(tmp, "b")
        os.mkdir(dir_a)
        os.mkdir(dir_b)
        path_a = os.path.join(dir_a, "myserialwrap")
        path_b = os.path.join(dir_b, "myserialwrap")
        _make_executable(path_a)
        _make_executable(path_b)
        monkeypatch.setenv("PATH", os.pathsep.join([dir_a, dir_b]))

        fx = SystemEffects()
        got = fx.which_all("myserialwrap")
        assert got == [path_a, path_b]


def test_system_effects_which_all_excludes_non_executable(monkeypatch):
    from sw_core.sysenv import SystemEffects

    with tempfile.TemporaryDirectory() as tmp:
        non_exec = os.path.join(tmp, "myserialwrap")
        with open(non_exec, "w", encoding="utf-8") as fh:
            fh.write("not executable\n")
        monkeypatch.setenv("PATH", tmp)

        fx = SystemEffects()
        assert fx.which_all("myserialwrap") == []


def test_system_effects_which_all_empty_when_no_match(monkeypatch):
    from sw_core.sysenv import SystemEffects

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("PATH", tmp)
        fx = SystemEffects()
        assert fx.which_all("definitely-not-a-binary-xyz") == []


def test_system_effects_run_default_timeout_none_unchanged_behavior():
    """不帶 timeout_s（既有呼叫）行為與修改前逐位元組相同：正常完成、不逾時。"""
    from sw_core.sysenv import SystemEffects

    fx = SystemEffects()
    rc, out, err = fx.run(["python3", "-c", "print('ok')"])
    assert rc == 0
    assert "ok" in out


def test_system_effects_run_timeout_returns_sentinel_not_raises():
    from sw_core.sysenv import SystemEffects

    fx = SystemEffects()
    rc, out, err = fx.run(["sleep", "5"], timeout_s=0.1)
    assert (rc, out, err) == (-1, "", "TIMEOUT")


def test_fake_effects_which_all_default_empty():
    from sw_core.sysenv import FakeEffects

    fx = FakeEffects()
    assert fx.which_all("serialwrap") == []


def test_fake_effects_which_all_returns_configured_list():
    from sw_core.sysenv import FakeEffects

    fx = FakeEffects(which_all={"serialwrap": ["/a/serialwrap", "/b/serialwrap"]})
    assert fx.which_all("serialwrap") == ["/a/serialwrap", "/b/serialwrap"]
    assert fx.which_all("absent") == []


def test_fake_effects_run_accepts_and_records_timeout_s():
    from sw_core.sysenv import FakeEffects

    fx = FakeEffects(commands={("x",): (0, "out", "")})
    rc, out, _err = fx.run(["x"], timeout_s=2.0)
    assert (rc, out) == (0, "out")
    assert fx.timeouts == [2.0]
    fx.run(["y"])  # 未帶 timeout_s → 記錄 None，介面對齊、行為不變
    assert fx.timeouts == [2.0, None]


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


def test_force_utf8_stdio_tolerates_incompatible_reconfigure_signature(monkeypatch):
    """reconfigure 存在但不吃 encoding=（拋 TypeError）時仍不得崩（#118 review）。"""
    from sw_core.sysenv import force_utf8_stdio

    class _BadSignatureStream:
        def __init__(self):
            self.called = False

        def reconfigure(self, *args, **kwargs):
            self.called = True
            raise TypeError("reconfigure() got an unexpected keyword argument 'encoding'")

    out, err = _BadSignatureStream(), _BadSignatureStream()
    monkeypatch.setattr(_sys, "platform", "win32")
    monkeypatch.setattr(_sys, "stdout", out)
    monkeypatch.setattr(_sys, "stderr", err)

    force_utf8_stdio()  # 不得拋 TypeError

    assert out.called and err.called


def test_force_utf8_stdio_skips_non_callable_reconfigure(monkeypatch):
    """reconfigure 屬性存在但不可呼叫時安全略過。"""
    from sw_core.sysenv import force_utf8_stdio

    class _WeirdStream:
        reconfigure = "not-callable"

    monkeypatch.setattr(_sys, "platform", "win32")
    monkeypatch.setattr(_sys, "stdout", _WeirdStream())
    monkeypatch.setattr(_sys, "stderr", _WeirdStream())

    force_utf8_stdio()  # 不得拋 TypeError（str 不可呼叫）
