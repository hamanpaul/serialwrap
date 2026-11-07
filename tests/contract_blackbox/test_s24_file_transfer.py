"""P8 Task 6（09-slice24-transfer-flash.md）：`file push`／`file pull` 經部署 artifact 的黑箱（A/B：Python 基線與
Rust artifact 同檔各跑一次，期望相同）。

案例（tests/acceptance/cases.yaml，Slice 2.4 凍結時登記的節點）：
- `s24-xfer-push-roundtrip`：`test_push_then_pull_roundtrip_md5`；
- `s24-xfer-pull-roundtrip`：`test_pull_roundtrip_md5`（含未給 local_path 時寫入 daemon cwd 的 basename）；
- `s24-xfer-echo-stall`：`test_echo_stall_blackbox`；
- `s24-xfer-session-gates`：`test_gate_resolve_quiet`（依編排者裁決 2026-11-08 以 parametrize 涵蓋全部閘門情境）；
- `s24-xfer-abort-on-release`：`test_release_during_push_aborts_without_resend`（OD-62：比照 BC-03）。

外部 oracle（Review Focus 6）：「零 TX」「不重送」一律以 DUT 端 `shell_target.rx_log()`／`commands()` 判定，
WAL 只用於 META 的關聯。RPC 回應比對前去掉 framing `id` 與中繼欄位 `version`／`code_fingerprint`。
stdlib＋pytest＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.posix_only]

_META_KEYS = ("id", "version", "code_fingerprint")
QUIET_HINT = "boot quiet window 進行中（疑似板卡自發重開機）；等 session 重新確認 READY 後重送"
PASSTHROUGH_HINT = "passthrough profile 不支援 file.push／file.pull；請使用互動 console。"
# 背景 job 於命令完成後才印出 boot banner（`\125` 為 `U`，使命令回顯本身不含 banner 字樣）；之後無 prompt，quiet 持續。
# decoder probe 命令（`_decoder_probe_command()`，282 字元）＋換行：echo-ACK 只作用於其後的 chunk 命令。
PROBE_LINE_BYTES = 283
BANNER_JOB = "(sleep 0.5; printf '\\125-Boot 2024.04 (Jan 01 2024 - 00:00:00 +0000)\\r\\n') &"


def _body(d: dict | None) -> dict | None:
    if d is None:
        return None
    return {k: v for k, v in d.items() if k not in _META_KEYS}


def _ready(sw_env, shell_target, **kw) -> None:
    sw_env.start(targets={"COM0": shell_target}, **kw)
    sw_env.wait_state("COM0", "READY", timeout=20)


def _chunk_lines(shell_target) -> list[str]:
    return [c for c in shell_target.commands() if "SW_XFER_CHUNK" in c]


def _settled_rx_len(shell_target, quiet_s: float = 0.3) -> int:
    """等 DUT 端輸入停止變化（`quiet_s` 內無新增）後回傳長度，作為零 TX 判定的起點。"""
    last = len(shell_target.rx_log())
    deadline = time.monotonic() + 5.0
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.05)
        now = len(shell_target.rx_log())
        if now != last:
            last, stable_since = now, time.monotonic()
        elif time.monotonic() - stable_since >= quiet_s:
            break
    return last


def _assert_zero_tx(shell_target, before: int, what: str) -> None:
    time.sleep(0.5)  # 觀察窗（證明無新增），不作同步點
    assert len(shell_target.rx_log()) == before, f"{what}：被拒時 DUT 端零新增 bytes（zero_tx_on_reject）"


def _run_command(sw_env, cmd: str, timeout_s: float = 10.0) -> dict:
    r = sw_env.rpc("command.submit", {"selector": "COM0", "cmd": cmd, "timeout_s": timeout_s})
    assert r.get("ok") is True, r
    cmd_id = r["cmd_id"]
    got: dict = {}

    def _done() -> bool:
        got.update(sw_env.rpc("command.get", {"cmd_id": cmd_id}))
        return got.get("command", {}).get("status") in {"done", "error", "canceled"}

    sw_env.wait_until(_done, timeout=timeout_s + 10)
    return got["command"]


def _arm_quiet(sw_env) -> None:
    _run_command(sw_env, BANNER_JOB)
    sw_env.wait_until(lambda: sw_env.session("COM0").get("boot_quiet_remaining_s") is not None, timeout=10)


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_push_then_pull_roundtrip_md5(sw_env, shell_target, tmp_path):
    _ready(sw_env, shell_target)
    src = tmp_path / "blob.bin"
    data = bytes(range(256)) * 20
    src.write_bytes(data)
    remote = str(shell_target.root / "dst.bin")
    r = sw_env.cli("--timeout", "120", "file", "push", "--selector", "COM0", "--local", str(src), "--remote", remote)
    assert r.exit == 0, r.stderr
    assert _body(r.json) == {"ok": True, "bytes": len(data), "chunks": 10, "md5": hashlib.md5(data).hexdigest(),
                             "remote_path": remote}
    assert (shell_target.root / "dst.bin").read_bytes() == data, "外部 oracle：DUT 端檔案"
    assert len(_chunk_lines(shell_target)) == 10, "DUT 端收到的 chunk 命令恰為 10 行（不重送）"
    out = tmp_path / "back.bin"
    r = sw_env.cli("--timeout", "120", "file", "pull", "--selector", "COM0", "--remote", remote, "--local", str(out))
    assert r.exit == 0, r.stderr
    assert _body(r.json) == {"ok": True, "bytes": len(data), "md5": hashlib.md5(data).hexdigest(), "local_path": str(out)}
    assert out.read_bytes() == data


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_pull_roundtrip_md5(sw_env, shell_target, tmp_path):
    _ready(sw_env, shell_target)
    data = os.urandom(3000)
    remote = shell_target.root / "src.bin"
    remote.write_bytes(data)
    out = tmp_path / "pulled.bin"
    r = sw_env.cli("--timeout", "120", "file", "pull", "--selector", "COM0", "--remote", str(remote), "--local", str(out))
    assert r.exit == 0, r.stderr
    assert _body(r.json) == {"ok": True, "bytes": len(data), "md5": hashlib.md5(data).hexdigest(), "local_path": str(out)}
    assert out.read_bytes() == remote.read_bytes() == data, "外部 oracle：取回檔與 DUT 端檔案逐位元組相同"

    # 未給 local_path：寫入 daemon cwd 的 basename（EVT-D-S7：本地檔由 daemon 讀寫）。
    name = f"sw-p8t6-pull-{uuid.uuid4().hex}.bin"
    (shell_target.root / name).write_bytes(data)
    cwd = Path(os.readlink(f"/proc/{sw_env.daemon_pid}/cwd"))
    landed = cwd / name
    try:
        r = sw_env.rpc("file.pull", {"selector": "COM0", "remote_path": str(shell_target.root / name)}, timeout_s=120)
        assert _body(r) == {"ok": True, "bytes": len(data), "md5": hashlib.md5(data).hexdigest(), "local_path": name}
        assert landed.read_bytes() == data
    finally:
        landed.unlink(missing_ok=True)


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_echo_stall_blackbox(sw_env, shell_target, tmp_path):
    _ready(sw_env, shell_target)
    src = tmp_path / "stall.bin"
    src.write_bytes(os.urandom(2048))
    # decoder probe 行（282 字元＋換行）照常回顯；之後 chunk 命令的第 100 個 echo 字元起停止回顯。
    shell_target.arm(echo_stall_after=PROBE_LINE_BYTES + 100)
    before = shell_target.rx_log()
    r = sw_env.cli("--timeout", "120", "file", "push", "--selector", "COM0", "--local", str(src),
                   "--remote", str(shell_target.root / "stall.bin"))
    assert r.exit != 0
    body = _body(r.json)
    assert body is not None and body.get("ok") is False and body.get("error_code") == "TRANSFER_ECHO_STALL", r.json
    # CLI stdout 以 sort_keys=True 輸出（CLI-11.16），只比對鍵集合。
    assert sorted(body) == sorted(["ok", "error_code", "chunks_sent", "chunks_total", "acked_chars", "sent_chars"]), body
    assert body["chunks_sent"] == 0 and body["chunks_total"] == 4 and body["acked_chars"] < body["sent_chars"], body
    after = shell_target.rx_log()[len(before):]
    assert after.count(b"\x15\n") == 1, "echo 停滯只送一次 Ctrl-U"
    lines = _chunk_lines(shell_target)
    assert len(lines) == len(set(lines)), "已送出的 chunk 不重送（no_auto_resend）"
    assert not (shell_target.root / "stall.bin").exists(), "停滯的傳輸不得產生目的檔"


GATE_SCENARIOS = [
    "quiet", "quiet_human_explicit", "quiet_human_bare", "rpc_args", "not_found", "passthrough",
    "agent_lease", "foreground_busy",
]


@pytest.mark.posix_pty
@pytest.mark.process_rpc
@pytest.mark.parametrize("scenario", GATE_SCENARIOS)
def test_gate_resolve_quiet(sw_env, shell_target, tmp_path, scenario):
    """file.* 的 resolve／manager 閘門（s24-xfer-session-gates）：每列被拒時 DUT 端零新增 bytes。"""
    src = tmp_path / "g.bin"
    src.write_bytes(b"gate")
    push = {"selector": "COM0", "local_path": str(src), "remote_path": str(shell_target.root / "g.bin")}
    pull = {"selector": "COM0", "remote_path": str(shell_target.root / "g.bin")}

    if scenario == "passthrough":
        # SC-35：passthrough 任何 state 皆回 PROFILE_NOT_COMMAND_CAPABLE（explicit target attach 零 TX、停在 ATTACHED）。
        sw_env.start(targets={"COM0": shell_target}, profile="passthrough")
        sw_env.wait_state("COM0", "ATTACHED", timeout=20)
        before = _settled_rx_len(shell_target)
        for method, params in (("file.push", push), ("file.pull", pull)):
            r = _body(sw_env.rpc(method, params))
            assert list(r) == ["ok", "error_code", "hint", "session"], r
            assert (r["ok"], r["error_code"], r["hint"]) == (False, "PROFILE_NOT_COMMAND_CAPABLE", PASSTHROUGH_HINT), r
            assert r["session"]["com"] == "COM0" and r["session"]["state"] == "ATTACHED", r
        assert sw_env.session("COM0")["foreground_busy"] is False, "不占前景槽"
        _assert_zero_tx(shell_target, before, scenario)
        return

    _ready(sw_env, shell_target)

    if scenario.startswith("quiet"):
        _arm_quiet(sw_env)
        before = _settled_rx_len(shell_target)
        source = {"quiet": None, "quiet_human_explicit": "human:x", "quiet_human_bare": "human"}[scenario]
        for method, params in (("file.push", push), ("file.pull", pull)):
            p = dict(params)
            if source is not None:
                p["source"] = source
            r = _body(sw_env.rpc(method, p))
            # DF-02／OD-74：RPC 路徑 trusted_human=False，明寫 human:<x> 與 bare human 都比照 agent 被 gate。
            assert list(r) == ["ok", "error_code", "retry_after_s", "hint", "session"], r
            assert (r["ok"], r["error_code"], r["hint"]) == (False, "AUTOBOOT_QUIET", QUIET_HINT), r
            assert isinstance(r["retry_after_s"], float) and 0.0 < r["retry_after_s"] <= 180.0, r
            assert r["session"]["com"] == "COM0" and r["session"]["state"] == "READY", r
        _assert_zero_tx(shell_target, before, scenario)
        return

    if scenario == "rpc_args":
        before = _settled_rx_len(shell_target)
        invalid = {"ok": False, "error_code": "INVALID_ARGS"}
        for method, params in (
            ("file.push", {}),
            ("file.pull", {}),
            ("file.push", {**push, "chunk_size": "abc"}),
            ("file.push", {**push, "chunk_timeout_s": "x"}),
            ("file.pull", {**pull, "chunk_timeout_s": ""}),
            ("file.push", {**push, "ack_mode": "ehco"}),
            ("file.pull", {**pull, "ack_mode": "ehco"}),
            ("file.push", {"selector": "COM0", "remote_path": "/r"}),
        ):
            assert _body(sw_env.rpc(method, params)) == invalid, (method, params)
        for method, params in (("file.push", push), ("file.pull", pull)):
            r = _body(sw_env.rpc(method, {**params, "source": "system:recover"}))
            assert list(r) == ["ok", "error_code", "field", "value", "reserved_prefix", "hint"], r
            assert (r["error_code"], r["field"], r["value"], r["reserved_prefix"]) == (
                "SOURCE_RESERVED", "source", "system:recover", "system"), r
        _assert_zero_tx(shell_target, before, scenario)
        return

    if scenario == "not_found":
        before = _settled_rx_len(shell_target)
        for method, params in (("file.push", push), ("file.pull", pull)):
            r = _body(sw_env.rpc(method, {**params, "selector": "COM9"}))
            assert r == {"ok": False, "error_code": "SESSION_NOT_FOUND", "selector": "COM9"}, r
        _assert_zero_tx(shell_target, before, scenario)
        return

    if scenario == "agent_lease":
        lease = sw_env.rpc("session.interactive_open", {"selector": "COM0", "owner": "agent:t6", "timeout_s": 60})
        assert lease.get("ok") is True, lease
        before = _settled_rx_len(shell_target)
        for method, params in (("file.push", push), ("file.pull", pull)):
            r = _body(sw_env.rpc(method, params))
            assert r == {"ok": False, "error_code": "SESSION_INTERACTIVE_BUSY"}, r
        _assert_zero_tx(shell_target, before, scenario)
        return

    if scenario == "foreground_busy":
        sub = sw_env.rpc("command.submit", {"selector": "COM0", "cmd": "sleep 4", "timeout_s": 20})
        assert sub.get("ok") is True, sub
        sw_env.wait_until(lambda: sw_env.session("COM0")["foreground_busy"] is True, timeout=10)
        before = _settled_rx_len(shell_target)
        for method, params in (("file.push", push), ("file.pull", pull)):
            r = _body(sw_env.rpc(method, params))
            assert r == {"ok": False, "error_code": "SESSION_BUSY", "selector": "COM0"}, r
        assert len(shell_target.rx_log()) == before, "SESSION_BUSY 零 TX"
        sw_env.wait_until(lambda: sw_env.session("COM0")["foreground_busy"] is False, timeout=20)
        return

    raise AssertionError(f"未知情境：{scenario}")


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_release_during_push_aborts_without_resend(sw_env, shell_target, tmp_path):
    _ready(sw_env, shell_target)
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(128 * 1024))
    push = sw_env.rpc_async("file.push", {"selector": "COM0", "local_path": str(src),
                                          "remote_path": str(shell_target.root / "x"), "chunk_size": 512})
    sw_env.wait_until(lambda: sw_env.session("COM0")["foreground_busy"] is True, timeout=10)
    # 已有 chunk 命令到 DUT（已確認寫出 ≥1 byte）後，令 DUT 自此再轉送 200 bytes 即停止回顯：傳輸停在某個 chunk 的
    # echo-ACK 等待中（Python 等 echo 逾時、Rust 由 drain 取消等待，兩邊都在 chunk 的送出段被拒），再 release。
    sw_env.wait_until(lambda: len(_chunk_lines(shell_target)) >= 2, timeout=30)
    shell_target.arm(echo_stall_after=200)
    _settled_rx_len(shell_target, quiet_s=0.5)
    rel = sw_env.rpc("device.release", {"selector": "COM0", "source": "agent:p8", "reason": "p8"})
    assert rel["ok"] is True and rel["aborted_cmd"] is True and rel["session"]["state"] == "RELEASED", rel
    res = _body(push.result(timeout=60))
    assert res.get("ok") is False, res
    assert res["error_code"] == "COMMAND_ABORTED_IN_FLIGHT", res
    assert list(res) == ["ok", "error_code", "chunks_sent", "chunks_total"], res
    assert res["chunks_total"] == 256 and 2 <= res["chunks_sent"] < 256, res
    metas = [r for r in sw_env.wal_records()
             if r.get("dir") == "META" and (r.get("meta") or {}).get("event") == "command_aborted_in_flight"]
    assert len(metas) == 1, metas
    assert re.fullmatch(r"ft-[0-9a-f]{8}", metas[0].get("cmd_id") or ""), metas[0]
    confirmed = metas[0]["meta"]["confirmed_tx_bytes"]
    assert isinstance(confirmed, int) and 0 < confirmed <= len(shell_target.rx_log()), metas[0]
    lines = _chunk_lines(shell_target)
    assert len(lines) == len(set(lines)), "外部 oracle：中止前後同一 chunk 命令不得出現兩次"
    n_after = len(shell_target.rx_log())
    time.sleep(1.0)  # 觀察窗（證明無新增），不作同步點
    assert len(shell_target.rx_log()) == n_after, "RELEASED 後 DUT 端零新增 bytes"
    _remove_upload_tmp(shell_target)


def _remove_upload_tmp(shell_target) -> None:
    """中止的 push 不送 cleanup（OD-62）：測試自行移除 shelltarget（本機 /bin/sh）留下的上傳暫存檔。"""
    for name in set(re.findall(rb"/tmp/\.sw_upload_[0-9a-f]{12}", shell_target.rx_log())):
        Path(name.decode("ascii")).unlink(missing_ok=True)
