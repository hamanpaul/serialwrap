"""tests/test_stage0_handoff_stop.py — SC-11 交接停止 daemon.handoff_stop 原子交接與 admission 邊界測試。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from stage0_fakes import FakePort, make_service, wal_records
from sw_core.daemon import HANDOFF_DRAIN_GRACE_S

def _rpc(svc: Any, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return svc.daemon_dispatch_for_test({"id": 1, "method": method, "params": params or {}})


def _mutating_methods() -> list[str]:
    schema_path = Path(__file__).resolve().parents[1] / "contracts" / "rpc_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    out = [
        m for m, s in schema.get("x-methods", {}).items()
        if s.get("side_effects") and not s.get("retryable_readonly", False)
        and m not in {"daemon.stop", "daemon.handoff_stop"}
    ]
    return sorted(out) or ["command.submit", "file.push", "session.recover", "device.release"]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_handoff_file_push_after_last_probe(tmp_path):
    gate = threading.Event()
    port = FakePort(block=gate)
    svc = make_service(tmp_path, ready=["COM0"], port=port)
    payload = b"x" * 16
    local_path = tmp_path / "upload.bin"
    local_path.write_bytes(payload)
    expected_md5 = hashlib.md5(payload).hexdigest()
    bridge = svc._sessions.get_session("COM0").bridge

    def target_reply(raw: bytes) -> None:
        command = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if "SW_XFER_DECODER_B64_" in command:
            output = "SW_XFER_DECODER_B64_42"
        elif "SW_XFER_CHUNK_" in command:
            output = "SW_XFER_CHUNK_42"
        elif command.startswith("md5sum "):
            output = f"{expected_md5}  /tmp/.sw_upload_test\nSW_XFER_MD5_42"
        elif command.startswith("mv "):
            output = "SW_XFER_MV_42"
        else:
            output = "SW_XFER_CLEANUP_42"
        bridge._handle_serial_rx(f"{command}\r\n{output}\r\nroot@prplOS:/# ".encode())

    port._on_write = target_reply
    assert all(not s["foreground_busy"] for s in _rpc(svc, "session.list")["sessions"])   # CLI 預檢看到閒置
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(_rpc(svc, "file.push", {
        "selector": "COM0", "local_path": str(local_path), "remote_path": "/tmp/result.bin",
        "ack_mode": "none", "chunk_size": 16, "chunk_timeout_s": 2.0,
    })))
    t.start()
    svc.wait_until_admitted("file.push", "COM0", timeout=1.0)              # 預檢之後才開始的 push
    r = _rpc(svc, "daemon.handoff_stop", {"reason": "update"})
    assert (r["ok"], r["error_code"]) == (False, "SESSION_BUSY")
    assert any(row["com"] == "COM0" and row["ops"] == ["file.push"] for row in r["busy"])
    assert svc.stop_requested is False and svc.admission.frozen is False
    gate.set()
    t.join(10.0)
    assert not t.is_alive(), f"production file.push 未在測試期限內返回：writes={len(port.writes)} rx={bridge.rx_tail()!r}"
    assert out.get("ok") is True and out.get("bytes") == len(payload), out              # production file.push 完整完成
    assert base64.b64encode(payload) in b"".join(port.writes)
    r2 = _rpc(svc, "daemon.handoff_stop", {"reason": "update"})
    assert r2["ok"] is True and r2["stopping"] is True and re.fullmatch(r"[0-9a-f]{32}", r2["handoff_token"])
    assert "version" not in r2 and svc.stop_requested is True
    svc.close_for_test()


# POSIX-only（§10.4）：tests/stage0_fakes.py:327 用 os.O_NOCTTY、:348 啟動 flash PTY；ttyMCU PTY-bridge 僅 POSIX（CLAUDE.md）。
@pytest.mark.posix_only
@pytest.mark.posix_pty
@pytest.mark.parametrize("first", ["probe", "handoff", "race"])
def test_handoff_flash_races_prepare(tmp_path, first):
    for _ in range(200 if first == "race" else 1):
        svc, mcu = make_service(tmp_path, ready=["COM0"], with_fake_mcu=True)
        res: dict = {}
        probe = lambda: res.__setitem__("probe", mcu.write_sync(b"\x55\x55"))   # 透過真實 PTY 寫入 flasher sync
        handoff = lambda: res.__setitem__("handoff", _rpc(svc, "daemon.handoff_stop", {"reason": "rollback"}))
        if first == "probe":
            probe()
            handoff()
            assert res["handoff"]["error_code"] == "FLASHING_BUSY"                      # 預檢後才開始的 flash 必擋下交接
        elif first == "handoff":
            handoff()
            probe()
            assert res["handoff"]["ok"] is True
        else:
            barrier = threading.Barrier(2)
            ts = [threading.Thread(target=lambda f=f: (barrier.wait(), f())) for f in (probe, handoff)]
            [x.start() for x in ts]
            [x.join(5.0) for x in ts]
        if res["handoff"]["ok"]:                                                       # 交接成立 ⇒ probe 必被拒、MCU 零 byte、未進 FLASHING
            assert res["probe"] == "refused_frozen" and mcu.received == b""
            assert svc.session_state("COM0") != "FLASHING"
        else:                                                                          # probe 先 admit ⇒ 交接必為 FLASHING_BUSY、daemon 不停止
            assert res["handoff"]["error_code"] == "FLASHING_BUSY" and res["probe"] == "admitted"
            assert svc.stop_requested is False and svc.admission.frozen is False
        mcu.release.set()
        svc.close_for_test()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_push_after_freeze_rejected_zero_tx(tmp_path):
    port = FakePort()
    svc = make_service(tmp_path, ready=["COM0"], port=port)
    assert _rpc(svc, "daemon.handoff_stop")["ok"] is True
    before = len([r for r in wal_records(svc.wal) if r["dir"] == "TX"])
    r = _rpc(svc, "file.push", {"selector": "COM0", "data_b64": "eA=="})
    assert (r["ok"], r["error_code"], r["reason"]) == (False, "FLUSHED_BY_SHUTDOWN", "handoff_frozen")
    assert port.writes == [] and len([x for x in wal_records(svc.wal) if x["dir"] == "TX"]) == before
    assert svc.session_public("COM0")["foreground_busy"] is False


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_handoff_drain_window_background_engines_zero_tx(tmp_path):
    """交接凍結後的排空窗口（HANDOFF_DRAIN_GRACE_S）內，背景引擎不得 TX，session 狀態不得被改寫。

    窗口內仍可能運作的來源（凍結只檢查 RPC admission，這兩條路徑原本不經 admission）：
    - DeviceWatcher 重探 tick（`reconcile_readiness`）對 ATTACHED session 排 readiness probe；
    - 人工 console 行經 arbiter（`admission_exempt=True`）送出命令。
    兩者都必須零 TX、session 狀態與 state.json 位元組不變；`health.ping` 仍須回應（監聽仍在）。
    """
    from sw_core.device_watcher import DeviceInfo

    port = FakePort()
    svc = make_service(tmp_path, ready=["COM0", "COM1", "COM2"], port=port)
    mgr = svc._sessions
    reprobe = mgr.get_session("COM0")
    console = mgr.get_session("COM1")
    detached = mgr.get_session("COM2")
    for session in (reprobe, console, detached):
        by_id = session.profile.device_by_id
        mgr._devices[by_id] = DeviceInfo(by_id=by_id, real_path=str(tmp_path / f"fake-{session.profile.com}"))
    with mgr._lock:
        # 名義 ATTACHED 且為 prompt 類錯誤：DeviceWatcher tick 會排 readiness probe（ATTACHED 分支）。
        reprobe.state = "ATTACHED"
        reprobe.last_error = "PROMPT_UNAVAILABLE"
        # DETACHED 且已有裝置：DeviceWatcher tick 會排重新 attach（登入路徑）。
        detached.state = "DETACHED"
        detached.bridge = None
    assert _rpc(svc, "daemon.handoff_stop", {"reason": "update"})["ok"] is True

    state_file = Path(mgr._state_path)

    def fingerprint() -> tuple:
        with mgr._lock:
            row = reprobe.to_public_dict()
            return (
                reprobe.state, reprobe.last_error, reprobe.reprobe_attempts,
                row.get("state"), row.get("last_error"),
                console.state, detached.state, detached.last_error,
                state_file.read_bytes() if state_file.exists() else None,
            )

    before_state = fingerprint()
    tx_before = list(port.writes)
    mgr.reconcile_readiness()                                              # DeviceWatcher 背景 tick（重探＋重 attach）
    mgr.join_reprobe_workers(timeout=2.0)
    svc._on_console_line(console.session_id, "console-1", "echo window-zero-tx")  # 人工 console 行
    time.sleep(0.5)                                                        # 給 arbiter worker 取件的時間
    assert port.writes == tx_before, f"窗口內背景引擎產生 TX：{port.writes[len(tx_before):]!r}"
    assert fingerprint() == before_state, "窗口內 session 狀態或 state.json 被改寫"
    assert not any(t.is_alive() for t in mgr._attach_threads.values()), "窗口內不得拉起新的 attach 執行緒"
    pong = _rpc(svc, "health.ping")
    assert pong.get("ok") is True and pong.get("pong") is True, pong
    assert _rpc(svc, "session.list").get("ok") is True
    svc.close_for_test()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_command_submit_races_handoff(tmp_path):
    for _ in range(10):
        port = FakePort()
        svc = make_service(tmp_path, ready=["COM0"], port=port)
        barrier = threading.Barrier(2)
        res: dict = {}
        t1 = threading.Thread(target=lambda: (barrier.wait(), res.update({"submit": _rpc(svc, "command.submit", {"selector": "COM0", "command": "echo 1"})})))
        t2 = threading.Thread(target=lambda: (barrier.wait(), res.update({"handoff": _rpc(svc, "daemon.handoff_stop", {"reason": "update"})})))
        t1.start()
        t2.start()
        t1.join(5.0)
        t2.join(5.0)
        if res.get("handoff", {}).get("ok"):
            assert res.get("submit", {}).get("error_code") == "FLUSHED_BY_SHUTDOWN"
            assert res.get("submit", {}).get("reason") == "handoff_frozen"
            assert port.writes == []
        else:
            assert res.get("handoff", {}).get("error_code") == "SESSION_BUSY"
            assert "command.submit" in res.get("handoff", {}).get("busy", [{}])[0].get("ops", [])
            assert svc.stop_requested is False


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_rpc_human_source_cannot_bypass_handoff_admission(tmp_path):
    port = FakePort()
    svc = make_service(tmp_path, ready=["COM0"], port=port)
    assert _rpc(svc, "daemon.handoff_stop")["ok"] is True
    result = _rpc(svc, "command.submit", {
        "selector": "COM0", "command": "echo bypass", "source": "human:rpc",
    })
    assert (result.get("ok"), result.get("error_code")) == (False, "FLUSHED_BY_SHUTDOWN")
    assert port.writes == []
    svc.close_for_test()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_rpc_human_source_in_flight_blocks_handoff(tmp_path):
    release_write = threading.Event()
    port = FakePort(block=release_write)
    svc = make_service(tmp_path, ready=["COM0"], port=port)
    accepted: dict[str, Any] = {}
    request = threading.Thread(target=lambda: accepted.update(_rpc(svc, "command.submit", {
        "selector": "COM0", "command": "echo in flight", "source": "human:rpc", "timeout_s": 0.05,
    })))
    request.start()
    svc.wait_until_admitted("command.submit", "COM0", timeout=1.0)
    result = _rpc(svc, "daemon.handoff_stop")
    assert result.get("error_code") == "SESSION_BUSY"
    assert "command.submit" in result.get("busy", [{}])[0].get("ops", [])
    assert svc.admission.frozen is False and svc.stop_requested is False
    release_write.set()
    request.join(2.0)
    assert accepted.get("ok") is True
    svc.close_for_test()


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_cancelled_queued_command_does_not_leave_handoff_ticket(tmp_path):
    worker_started = threading.Event()
    release_worker = threading.Event()
    svc = make_service(tmp_path, ready=["COM0"])

    def blocked_send(_session_id, command, *_args, **_kwargs):
        if command == "first":
            worker_started.set()
            assert release_worker.wait(3.0), "測試未能解除第一筆命令"
        return {"ok": True}

    svc._arbiter._send_cb = blocked_send
    first = _rpc(svc, "command.submit", {"selector": "COM0", "command": "first"})
    assert first["ok"] is True
    assert worker_started.wait(1.0), "第一筆命令未進入 worker"

    second = _rpc(svc, "command.submit", {"selector": "COM0", "command": "second"})
    assert second["ok"] is True and second["status"] == "accepted"
    canceled = _rpc(svc, "command.cancel", {"cmd_id": second["cmd_id"]})
    assert (canceled.get("ok"), canceled.get("cmd_id"), canceled.get("status")) == (
        True, second["cmd_id"], "canceled",
    )

    release_worker.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        current = _rpc(svc, "command.get", {"cmd_id": first["cmd_id"]})
        if current.get("command", {}).get("done_at"):
            break
        time.sleep(0.01)
    else:
        pytest.fail("第一筆命令未在時限內完成")

    handoff = _rpc(svc, "daemon.handoff_stop")
    assert handoff.get("ok") is True, handoff
    svc.close_for_test()


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("method", _mutating_methods())
def test_every_mutating_method_rejected_after_freeze(tmp_path, method):
    port = FakePort()
    svc = make_service(tmp_path, ready=["COM0"], port=port)
    handoff_res = _rpc(svc, "daemon.handoff_stop")
    assert handoff_res.get("ok") is True
    port.writes.clear()
    state_mtime = os.path.getmtime(svc._sessions._state_path)
    r = _rpc(svc, method, {"selector": "COM0", "command": "echo 1", "data_b64": "eA=="})
    assert (r.get("ok"), r.get("error_code"), r.get("reason")) == (False, "FLUSHED_BY_SHUTDOWN", "handoff_frozen")
    assert port.writes == []
    assert os.path.getmtime(svc._sessions._state_path) == state_mtime
    assert _rpc(svc, "health.ping").get("ok") is True
    assert _rpc(svc, "session.list").get("ok") is True


@pytest.mark.core_portable
@pytest.mark.memory_only
@pytest.mark.parametrize("reason", ["update", "rollback", "revert", "supervision_set"])
def test_handoff_reason_supervision_set_same_semantics(tmp_path, reason):
    port = FakePort()
    svc = make_service(tmp_path, ready=["COM0"], port=port)
    r = _rpc(svc, "daemon.handoff_stop", {"reason": reason})
    assert r.get("ok") is True and r.get("stopping") is True
    assert re.fullmatch(r"[0-9a-f]{32}", r.get("handoff_token", ""))
    r_push = _rpc(svc, "file.push", {"selector": "COM0", "data_b64": "eA=="})
    assert (r_push.get("ok"), r_push.get("error_code"), r_push.get("reason")) == (False, "FLUSHED_BY_SHUTDOWN", "handoff_frozen")
    assert port.writes == []


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_handoff_busy_does_not_freeze(tmp_path):
    svc = make_service(tmp_path, ready=["COM0"])
    s = svc._sessions.get_session("COM0")
    with svc._sessions._lock:
        svc._sessions._open_interactive_locked(s, owner="agent:test", timeout_s=60.0)
    r = _rpc(svc, "daemon.handoff_stop")
    assert r.get("ok") is False and r.get("error_code") == "SESSION_BUSY"
    assert "session.interactive_open" in r.get("busy", [{}])[0].get("ops", [])
    sub = _rpc(svc, "command.submit", {"selector": "COM0", "command": "echo 1"})
    assert sub.get("error_code") != "FLUSHED_BY_SHUTDOWN"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_handoff_recovering_flag_is_busy(tmp_path):
    svc = make_service(tmp_path, ready=["COM0"])
    s = svc._sessions.get_session("COM0")
    s.state = "RECOVERING"
    r = _rpc(svc, "daemon.handoff_stop")
    assert r.get("ok") is False and r.get("error_code") == "SESSION_BUSY"
    assert r.get("busy", [{}])[0].get("recovering") is True


def _spawn_isolated_daemon(tmp_path: Path) -> tuple[subprocess.Popen, Path]:
    """以隔離目錄啟動真實 sw_core.daemon 子行程，等待 Unix socket 出現；回傳 (proc, socket_path)。"""
    isolated = tmp_path / "daemon"
    dirs = {
        "SERIALWRAP_CONFIG_DIR": isolated / "config",
        "SERIALWRAP_STATE_DIR": isolated / "state",
        "SERIALWRAP_RUN_DIR": isolated / "run",
        "SERIALWRAP_DATA_DIR": isolated / "data",
        "SERIALWRAP_PROFILE_DIR": isolated / "profiles",
        "SERIALWRAP_WAL_DIR": isolated / "wal",
        "SERIALWRAP_BY_ID_DIR": isolated / "by-id",
        "SERIALWRAP_BY_PATH_DIR": isolated / "by-path",
        "SERIALWRAP_LOG_DIR": isolated / "logs",
        "SERIALWRAP_EVENTS_DIR": isolated / "events-config",
        "SERIALWRAP_EVENTS_RUNTIME_DIR": isolated / "events-runtime",
        "SERIALWRAP_TTYMCU_PATH": isolated / "dev" / "ttyMCU",
    }
    for key, path in dirs.items():
        (path.parent if key == "SERIALWRAP_TTYMCU_PATH" else path).mkdir(
            parents=True, exist_ok=True
        )
    socket_path = isolated / "run" / "serialwrapd.sock"
    lock_path = isolated / "run" / "serialwrapd.lock"
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("SERIALWRAP_"):
            env.pop(key)
    env.update({key: str(path) for key, path in dirs.items()})
    env.update({
        "SERIALWRAP_ENDPOINT": str(socket_path),
        "SERIALWRAP_EVENTS_LOG_PATH": str(isolated / "events-runtime" / "events.ndjson"),
        "SERIALWRAP_RPC_BACKEND": "posix",
        "SERIALWRAP_LOCK_BACKEND": "posix",
        "SERIALWRAP_DEVICE_BACKEND": "posix",
        "XDG_CONFIG_HOME": str(isolated / "xdg-config"),
        "XDG_STATE_HOME": str(isolated / "xdg-state"),
        "XDG_DATA_HOME": str(isolated / "xdg-data"),
        "XDG_RUNTIME_DIR": str(isolated / "xdg-runtime"),
    })
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "sw_core.daemon",
            "--profile-dir", str(dirs["SERIALWRAP_PROFILE_DIR"]),
            "--socket", str(socket_path),
            "--lock", str(lock_path),
        ],
        cwd=repo,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10.0
        while not socket_path.exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr is not None else ""
                raise AssertionError(f"隔離 daemon 提前退出（{proc.returncode}）：{stderr[-1000:]}")
            time.sleep(0.02)
        assert socket_path.exists(), "隔離 daemon 未在期限內建立 Unix socket"
    except BaseException:
        _terminate_daemon(proc)
        raise
    return proc, socket_path


def _terminate_daemon(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3.0)
    if proc.stderr is not None:
        proc.stderr.close()


def _rpc_over_socket(socket_path: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """以獨立連線送一則 JSON-RPC 請求並讀回一行回應（每次新連線，避免共用連線掩蓋 listener 關閉）。"""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5.0)
        client.connect(str(socket_path))
        request = json.dumps({"id": 1, "method": method, "params": params}, separators=(",", ":")).encode() + b"\n"
        client.sendall(request)
        return json.loads(client.makefile("rb").readline())


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_handoff_ok_then_sc11_stop_order(tmp_path):
    proc, socket_path = _spawn_isolated_daemon(tmp_path)
    try:
        response = _rpc_over_socket(socket_path, "daemon.handoff_stop", {"reason": "update"})
        assert response.get("ok") is True and response.get("stopping") is True
        assert re.fullmatch(r"[0-9a-f]{32}", response.get("handoff_token", ""))
        assert "version" not in response
        assert proc.wait(timeout=HANDOFF_DRAIN_GRACE_S + 10.0) == 0
    finally:
        _terminate_daemon(proc)


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_handoff_drain_window_flushes_mutating_answers_ping_and_exits_bounded(tmp_path):
    """交接停止成功回覆後的有界排空窗口（HANDOFF_DRAIN_GRACE_S）：真實行程內驗證

    - 窗口內有副作用的請求（file.push）回 `FLUSHED_BY_SHUTDOWN`＋`reason:"handoff_frozen"`，而非 socket 錯誤；
    - `health.ping` 仍可回應（監聽仍在）；
    - 行程於窗口結束後才退出，且在 窗口＋餘裕 內結束（不殘留）。
    """
    proc, socket_path = _spawn_isolated_daemon(tmp_path)
    try:
        response = _rpc_over_socket(socket_path, "daemon.handoff_stop", {"reason": "update"})
        assert response.get("ok") is True and response.get("stopping") is True
        reply_seen = time.monotonic()

        flushed = _rpc_over_socket(socket_path, "file.push", {
            "selector": "COM0", "local_path": str(tmp_path / "absent.bin"), "remote_path": "/tmp/absent.bin",
        })
        assert flushed.get("ok") is False, flushed
        assert flushed.get("error_code") == "FLUSHED_BY_SHUTDOWN", flushed
        assert flushed.get("reason") == "handoff_frozen", flushed

        pong = _rpc_over_socket(socket_path, "health.ping", {})
        assert pong.get("ok") is True and pong.get("pong") is True, pong

        exit_code = proc.wait(timeout=HANDOFF_DRAIN_GRACE_S + 10.0)
        exited_after = time.monotonic() - reply_seen
        assert exit_code == 0
        # 窗口結束（call_later）前不得退出；允許 0.5 秒量測餘裕。
        assert exited_after >= HANDOFF_DRAIN_GRACE_S - 0.5, exited_after
        assert exited_after <= HANDOFF_DRAIN_GRACE_S + 10.0, exited_after
    finally:
        _terminate_daemon(proc)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_handoff_stop_contract_merged():
    schema_path = Path(__file__).resolve().parents[1] / "contracts" / "rpc_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert "daemon.handoff_stop" in schema["x-methods"]
    assert schema["x-methods"]["daemon.handoff_stop"]["status"] == "NEW"
    assert not any(p.get("adds_method") == "daemon.handoff_stop" for p in schema.get("x-pending", []))
    assert "daemon.handoff_stop" in schema.get("x-wire", {}).get("version_absent_for", [])

    error_codes_path = Path(__file__).resolve().parents[1] / "contracts" / "error_codes.yaml"
    import yaml
    codes_doc = yaml.safe_load(error_codes_path.read_text(encoding="utf-8"))
    flushed = next(e for e in codes_doc["codes"] if e.get("code") == "FLUSHED_BY_SHUTDOWN")
    assert "rpc" in flushed.get("scope", {}).get("interfaces", [])
