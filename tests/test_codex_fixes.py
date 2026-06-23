"""Codex 對抗式審查發現的修正回歸測試（#1a/#1b/#1c/#2/#3/#5；#4 在 test_setup_reconcile.py）。"""
from __future__ import annotations

import argparse
import asyncio
import os
import stat
import threading
import time
from pathlib import Path


# ── #1a：CLI endpoint 解析會讀 config.yaml 的有效 socket ──────────────────────
def test_resolve_endpoint_uses_config_socket_when_socket_not_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(tmp_path))
    import importlib
    import sw_core.constants as c
    import sw_core.cli as cli
    importlib.reload(c)
    importlib.reload(cli)
    from sw_core.runtime_config import RuntimeConfig
    RuntimeConfig(tmp_path / "config.yaml").set_mode("systemd-system", socket_path="/run/serialwrap/serialwrapd.sock")
    args = argparse.Namespace(endpoint=None, socket=c.SOCKET_PATH)  # 未覆寫 --socket
    assert cli._resolve_endpoint(args) == "/run/serialwrap/serialwrapd.sock"
    # 明確 --socket 仍優先
    args2 = argparse.Namespace(endpoint=None, socket="/tmp/explicit.sock")
    assert cli._resolve_endpoint(args2) == "/tmp/explicit.sock"
    importlib.reload(c); importlib.reload(cli)


# ── #1b：system unit ExecStart 帶 --profile-dir，且 install 會把 profiles 放到 /etc ──
def test_system_exec_start_and_install_cover_profiles(tmp_path):
    from sw_core import setup_cmd as sc
    assert "--profile-dir /etc/serialwrap/profiles" in sc._SYSTEM_EXEC_START
    cmds = sc._system_install_cmds(Path(tmp_path), include_start=True)
    flat = [" ".join(c) for c in cmds]
    assert any("/etc/serialwrap/profiles" in f and "cp" in f for f in flat), flat
    # staging 的 profiles 真有內容
    assert (tmp_path / ".local" / "share" / "serialwrap" / "system-profiles" / "default.yaml").is_file()


# ── #2：system unit 設 UMask + socket group；rpc socket chmod 660 ─────────────
def test_system_unit_has_umask_and_socket_group():
    from sw_core.systemd_units import render_system_unit
    text = render_system_unit(exec_start="/usr/local/bin/serialwrapd")
    assert "UMask=0117" in text
    assert "Environment=SERIALWRAP_SOCKET_GROUP=dialout" in text


def test_rpc_socket_is_chmod_660(tmp_path):
    from sw_core.rpc import JsonRpcUnixServer
    sock = str(tmp_path / "t.sock")
    loop = asyncio.new_event_loop()
    srv = JsonRpcUnixServer(sock, lambda m, p: {"ok": True})
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(srv.start())
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(5.0)
    time.sleep(0.1)
    mode = stat.S_IMODE(os.stat(sock).st_mode)
    fut = asyncio.run_coroutine_threadsafe(srv.stop(), loop)
    try:
        fut.result(timeout=5)
    except Exception:
        pass
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()
    assert mode == 0o660, oct(mode)


# ── #3/#5：minicom wrapper 不再硬編 /tmp socket，改走 CLI 解析（throwaway 隔離） ──
def test_minicom_wrapper_no_hardcoded_tmp_socket():
    from importlib import resources
    text = (resources.files("sw_core.assets") / "tools" / "minicom_router.sh").read_text(encoding="utf-8")
    assert "/tmp/serialwrap/serialwrapd.sock" not in text
    assert "SW()" in text  # 改走 SW 包裝（僅在 SERIALWRAP_SOCKET 明設時帶 --socket）
    assert 'daemon start --profile-dir "${PROFILE_DIR}"' not in text


# ── #1c：setup 在 flash 進行中（未 force）須在「物化之前」中止 ───────────────────
def test_setup_aborts_before_materialize_when_flashing(tmp_path, monkeypatch):
    monkeypatch.setenv("SERIALWRAP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path))
    import importlib
    import sw_core.constants as c
    import sw_core.cli as cli
    importlib.reload(c)
    importlib.reload(cli)

    calls = {"materialize": 0}
    monkeypatch.setattr(cli, "materialize_assets", lambda *a, **k: calls.__setitem__("materialize", calls["materialize"] + 1))

    def fake_rpc(sock, method, params, timeout_s=0.5):
        if method == "mcu.status":
            return {"ok": True, "flashing": True}
        return {"ok": True}
    monkeypatch.setattr(cli, "rpc_call", fake_rpc)

    rc = cli.main(["setup", "--on-demand"])  # 未 --force
    assert rc == 2
    assert calls["materialize"] == 0, "flash 進行中不可在中止前先物化資產"
    importlib.reload(c); importlib.reload(cli)


# ── Copilot PR review：loginctl enable-linger 須帶使用者名稱（否則 linger 不生效） ──
def test_enable_linger_includes_user(tmp_path, monkeypatch):
    monkeypatch.setenv("USER", "alice")
    monkeypatch.delenv("LOGNAME", raising=False)
    from sw_core.sysenv import FakeEffects
    from sw_core.setup_cmd import reconcile
    fx = FakeEffects(systemd=True)
    reconcile(old_mode="on-demand", target_mode="systemd-user", fx=fx, home=tmp_path,
              daemon_running=False, config_path=tmp_path / "config.yaml")
    assert ["loginctl", "enable-linger", "alice"] in fx.calls
    # 不可送出沒帶使用者的版本
    assert ["loginctl", "enable-linger"] not in fx.calls


def test_enable_linger_skipped_when_no_user(tmp_path, monkeypatch):
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    from sw_core.sysenv import FakeEffects
    from sw_core.setup_cmd import reconcile
    fx = FakeEffects(systemd=True)
    reconcile(old_mode="on-demand", target_mode="systemd-user", fx=fx, home=tmp_path,
              daemon_running=False, config_path=tmp_path / "config.yaml")
    assert not any(c[:2] == ["loginctl", "enable-linger"] for c in fx.calls)  # 不送空字串
