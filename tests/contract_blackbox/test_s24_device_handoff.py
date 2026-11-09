"""P8 Task 11（09-slice24-transfer-flash.md）：外部工具裝置交接（`device.release`／`device.attach`）黑箱。

A/B：Python 基線與 Rust artifact（`SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE`／`SERIALWRAP_AB_LABEL=rust-linux`）同檔各跑一次，
期望相同；Rust 若因 P5／P7 尚未對齊 main 而紅，列 SPEC-GAPS，不放寬案例。

凍結案例（tests/acceptance/cases.yaml，Slice 2.4 凍結時登記的節點）：
- `s24-ho-ext-tool`：`test_external_tool_blocks_attach_until_it_exits`；
- `s24-ho-second-daemon`：`test_second_daemon_takes_device_then_returns`；
- `s24-ho-persist-restart`：`test_released_survives_two_restarts_and_replug`（動態 session，DF-04）。
其餘為計畫 Task 11 測試表的補充族（不另登記案例）。Windows 交接（`s24-ho-windows-com`）在
`test_s24_device_handoff_windows.py`。

外部 oracle（Review Focus 6）：「零 TX」一律以 DUT 端 `shell_target.rx_log()` 判定；「未雙持有」以
`HolderSampler`（10 ms 取樣、`/proc/*/fd` 掃描）連續取樣判定，不以單次快照證明；WAL 只作關聯。
`ExternalHolder(flock=False)` 模擬 minicom 類（不取 flock、不設 TIOCEXCL），`flock=True` 模擬遵守 advisory lock 的工具。
stdlib＋pytest＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import concurrent.futures
import errno
import os
import select
import time
from pathlib import Path

import pytest

from bb_harness import is_rust
from p8_fixtures import ExternalHolder, HolderSampler

pytestmark = [pytest.mark.posix_only]

_META_KEYS = ("id", "version", "code_fingerprint")
_RELEASE_ARGS = ("--source", "agent:p8", "--reason", "p8 handoff")


def _body(d: dict | None) -> dict | None:
    if d is None:
        return None
    return {k: v for k, v in d.items() if k not in _META_KEYS}


def _ready(sw_env, shell_target, **kw) -> None:
    sw_env.start(targets={"COM0": shell_target}, **kw)
    sw_env.wait_state("COM0", "READY", timeout=20)


def _ready_dynamic(sw_env, shell_target) -> None:
    """只建 by-id 連結、不寫 `targets`（全走動態偵測）。"""
    sw_env.add_by_id(shell_target.by_id_name, shell_target.slave_path)
    sw_env.start(targets={})
    sw_env.wait_state("COM0", "READY", timeout=20)


def _release(sw_env, reason: str = "p8 handoff") -> dict:
    r = sw_env.cli("device", "release", "--selector", "COM0", "--source", "agent:p8", "--reason", reason)
    assert r.exit == 0 and r.json is not None, (r.exit, r.stdout, r.stderr)
    assert r.json["ok"] is True and r.json["session"]["state"] == "RELEASED", r.json
    return r.json


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


def _wal_max_seq(sw_env) -> int:
    return max((int(r.get("seq") or 0) for r in sw_env.wal_records()), default=0)


def _holders(sw_env, shell_target) -> list[int]:
    return sw_env.holders(shell_target.slave_path, exclude_self=True)


def _wait_no_holder(sw_env, shell_target, timeout: float = 5.0) -> None:
    sw_env.wait_until(lambda: _holders(sw_env, shell_target) == [], timeout=timeout)


def _same_uid_proc_fd_unreadable() -> bool:
    """外部觀測：本機是否有**同 uid** 行程的 `/proc/<pid>/fd` 因權限不可讀（例如 ssh-agent 關閉 dumpable）。

    此時 `/proc` holder 掃描不完整（status=permission），BC-04 規定 `holder_unknown:true`；他 uid 行程不可讀屬常態
    （S1X-a-6），不使 holder 不明，故不列入。
    """
    uid = os.getuid()
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            if os.stat(f"/proc/{name}").st_uid != uid:
                continue
            os.listdir(f"/proc/{name}/fd")
        except PermissionError:
            return True
        except OSError:
            continue
    return False


def _still_held(pid: int) -> dict:
    return {"ok": False, "error_code": "DEVICE_STILL_HELD", "pids": [pid], "holder_unknown": False,
            "selector": "COM0"}


# ---------------------------------------------------------------------------
# 凍結案例
# ---------------------------------------------------------------------------

@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_external_tool_blocks_attach_until_it_exits(sw_env, shell_target):
    """s24-ho-ext-tool：外部工具持有期間 attach 回 DEVICE_STILL_HELD、不清除 released 保護且零 TX；工具退出後才可收回。"""
    _ready(sw_env, shell_target)
    with HolderSampler(shell_target.slave_path, exclude=(os.getpid(),)) as sampler:
        _release(sw_env)
        assert _holders(sw_env, shell_target) == [], "release 回成功前 daemon handle 已關"
        holder = ExternalHolder(shell_target.slave_path, flock=False)
        holder.start()
        try:
            n0 = _settled_rx_len(shell_target)
            r = sw_env.cli("device", "attach", "--selector", "COM0")
            assert r.exit == 2, (r.exit, r.stdout, r.stderr)
            assert _body(r.json) == _still_held(holder.pid)
            assert sw_env.session("COM0")["state"] == "RELEASED", "失敗不得清除 released 保護"
            st = sw_env.rpc("session.self_test", {"selector": "COM0"})
            assert (st["classification"], st["recommended_action"], st["external_holder"], st["reclaimable"]) == \
                   ("RELEASED", "wait_external_flash", [holder.pid], False), st
            time.sleep(0.5)  # 觀察窗（證明無新增），不作同步點
            assert shell_target.rx_log()[n0:] == b"", "外部工具持有期間 UART 零 TX"
        finally:
            holder.stop()
        r = sw_env.cli("device", "attach", "--selector", "COM0")
        assert r.exit == 0 and r.json is not None and r.json["ok"] is True, (r.exit, r.stdout, r.stderr)
        sw_env.wait_state("COM0", "READY", timeout=20)
    assert sampler.max_concurrent() <= 1, f"daemon 與外部工具不得同時持有 tty：{sampler.samples()}"
    seen = sampler.holders_seen()
    assert sw_env.daemon_pid in seen and holder.pid in seen, seen


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_second_daemon_takes_device_then_returns(sw_env, shell_target, throwaway_daemon):
    """s24-ho-second-daemon：daemon A release 後由拋棄式 daemon B 接手；B 持有期間 A attach（含 --force）回
    DEVICE_STILL_HELD{pids:[B]}；B 停止後 A 收回；兩個 daemon 從未同時持有。"""
    _ready(sw_env, shell_target)
    with HolderSampler(shell_target.slave_path, exclude=(os.getpid(),)) as sampler:
        _release(sw_env, reason="f10-like")
        assert _holders(sw_env, shell_target) == []
        b = throwaway_daemon(by_id={"dut": shell_target.slave_path})   # 獨立 RUN/STATE/WAL/BY_ID/BY_PATH/CONFIG
        b.wait_state("COM0", "READY", timeout=20)
        b_pid = b.daemon_pid
        assert b_pid is not None
        for args in (("device", "attach", "--selector", "COM0"),
                     ("device", "attach", "--selector", "COM0", "--force")):
            r = sw_env.cli(*args)
            assert r.exit == 2, (args, r.exit, r.stdout, r.stderr)
            assert _body(r.json) == _still_held(b_pid), args
            assert sw_env.session("COM0")["state"] == "RELEASED"
        assert _holders(sw_env, shell_target) == [b_pid], "A 試開失敗後不得殘留 fd"
        b.stop()
        r = sw_env.cli("device", "attach", "--selector", "COM0")
        assert r.exit == 0 and r.json is not None and r.json["ok"] is True, (r.exit, r.stdout, r.stderr)
        sw_env.wait_state("COM0", "READY", timeout=20)
    assert sampler.max_concurrent() <= 1, f"兩個 daemon 不得同時持有 tty：{sampler.samples()}"
    seen = sampler.holders_seen()
    assert sw_env.daemon_pid in seen and b_pid in seen, seen


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_released_survives_two_restarts_and_replug(sw_env, shell_target):
    """s24-ho-persist-restart（DF-04）：動態 session release 後重啟兩次、unplug／replug 仍 RELEASED、不自動 attach。"""
    _ready_dynamic(sw_env, shell_target)
    _release(sw_env, reason="persist")
    n0 = _settled_rx_len(shell_target)
    for round_no in (1, 2):
        sw_env.restart()
        time.sleep(3.0)
        s = sw_env.session("COM0")
        assert s["state"] == "RELEASED", (round_no, s)
        assert (s["released_by"], s["released_reason"]) == ("agent:p8", "persist"), s
        assert _holders(sw_env, shell_target) == [], f"第 {round_no} 次重啟後不得重開 tty"
    sw_env.unplug(shell_target.by_id_name)
    time.sleep(2.0)
    sw_env.replug(shell_target.by_id_name)
    time.sleep(3.0)
    assert sw_env.session("COM0")["state"] == "RELEASED", "離線 tick 不得把 RELEASED 降為 DETACHED"
    assert _holders(sw_env, shell_target) == []
    assert shell_target.rx_log()[n0:] == b"", "RELEASED 期間（含重啟與重新插拔）UART 零 TX"
    r = sw_env.cli("device", "attach", "--selector", "COM0")
    assert r.exit == 0 and r.json is not None and r.json["ok"] is True, (r.exit, r.stdout, r.stderr)
    sw_env.wait_state("COM0", "READY", timeout=20)


# ---------------------------------------------------------------------------
# 強制收回與外部 holder（規格 §6.2：force 只略過預檢，不略過已識別的外部 holder）
# ---------------------------------------------------------------------------

@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_force_attach_plain_open_tool_still_held(sw_env, shell_target):
    _ready(sw_env, shell_target)
    _release(sw_env)
    before = sw_env.session("COM0")
    holder = ExternalHolder(shell_target.slave_path, flock=False)
    holder.start()
    try:
        n0 = _settled_rx_len(shell_target)
        wal_tx0 = len(sw_env.wal_tx("COM0"))
        r = sw_env.cli("device", "attach", "--selector", "COM0", "--force")
        assert r.exit == 2, (r.exit, r.stdout, r.stderr)
        assert _body(r.json) == _still_held(holder.pid)
        s = sw_env.session("COM0")
        assert s["state"] == "RELEASED"
        assert (s["released_by"], s["released_reason"]) == (before["released_by"], before["released_reason"]), s
        time.sleep(1.0)  # 觀察窗（證明無新增），不作同步點
        assert shell_target.rx_log()[n0:] == b"", "force 被已識別 holder 擋下時 UART 零 TX"
        assert len(sw_env.wal_tx("COM0")) == wal_tx0, "WAL 無新 TX（關聯）"
        assert _holders(sw_env, shell_target) == [holder.pid], "daemon 已關閉試開的 fd，未與外部工具共持"
    finally:
        holder.stop()
    r = sw_env.cli("device", "attach", "--selector", "COM0", "--force")
    assert r.exit == 0 and r.json is not None and r.json["ok"] is True, (r.exit, r.stdout, r.stderr)
    assert r.json["session"]["state"] == "ATTACHING", r.json
    sw_env.wait_state("COM0", "READY", timeout=20)


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_force_attach_flock_tool_still_held(sw_env, shell_target):
    _ready(sw_env, shell_target)
    _release(sw_env)
    holder = ExternalHolder(shell_target.slave_path, flock=True)
    holder.start()
    try:
        n0 = _settled_rx_len(shell_target)
        r = sw_env.cli("device", "attach", "--selector", "COM0", "--force")
        assert r.exit == 2, (r.exit, r.stdout, r.stderr)
        assert _body(r.json) == _still_held(holder.pid)
        assert sw_env.session("COM0")["state"] == "RELEASED"
        time.sleep(0.5)
        assert shell_target.rx_log()[n0:] == b""
        assert _holders(sw_env, shell_target) == [holder.pid]
    finally:
        holder.stop()


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_auto_attach_plain_open_tool_never_starts_io(sw_env, shell_target):
    holder = ExternalHolder(shell_target.slave_path, flock=False)
    holder.start()
    seen: set[str] = set()
    try:
        sw_env.start(targets={"COM0": shell_target})
        deadline = time.monotonic() + 5.0
        last: dict = {}
        while time.monotonic() < deadline:
            last = sw_env.session("COM0")
            seen.add(last["state"])
            time.sleep(0.05)
        assert last["state"] == "DETACHED", last
        assert last["last_error"] == "ATTACH_FAILED:DeviceHeldError", last
        assert not seen & {"ATTACHING", "ATTACHED", "READY"}, seen
        assert shell_target.rx_log() == b"", "外部工具先持有時不得送 probe／登入 TX"
        assert _holders(sw_env, shell_target) == [holder.pid]
    finally:
        holder.stop()
    r = sw_env.cli("device", "attach", "--selector", "COM0")
    assert r.exit == 0 and r.json is not None and r.json["ok"] is True, (r.exit, r.stdout, r.stderr)
    sw_env.wait_state("COM0", "READY", timeout=20)


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_attach_device_absent(sw_env, shell_target):
    _ready(sw_env, shell_target)
    _release(sw_env)
    link = str(sw_env.by_id_dir / shell_target.by_id_name)
    sw_env.unplug(shell_target.by_id_name)
    time.sleep(3.0)  # 等裝置掃描（每 1 s）察覺 by-id 連結消失
    r = sw_env.cli("device", "attach", "--selector", "COM0")
    assert r.exit != 0, (r.exit, r.stdout, r.stderr)
    assert _body(r.json) == {"ok": False, "error_code": "DEVICE_NOT_PRESENT", "selector": "COM0",
                                    "device_by_id": link}
    assert sw_env.session("COM0")["state"] == "RELEASED"


# ---------------------------------------------------------------------------
# release 本身
# ---------------------------------------------------------------------------

@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_release_dedupe_concurrent(sw_env, shell_target):
    _ready(sw_env, shell_target)
    params = {"selector": "COM0", "source": "agent:p8", "reason": "dedupe"}
    futs = [sw_env.rpc_async("device.release", params) for _ in range(2)]
    results = [_body(f.result(timeout=30)) for f in futs]
    for r in results:
        assert r["ok"] is True and r["session"]["state"] == "RELEASED", results
    shapes = sorted(tuple(r) for r in results)
    # 兩者皆 ok：都加入同一個 stop（皆為完整形狀），或第二個晚到時回 already_released
    for keys in shapes:
        assert keys in {("ok", "session", "closed_consoles", "aborted_cmd"), ("ok", "already_released", "session")}, results
    assert _holders(sw_env, shell_target) == []
    later = _body(sw_env.rpc("device.release", params))
    assert later["ok"] is True and later["already_released"] is True, later


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_release_closes_consoles_count(sw_env, shell_target):
    _ready(sw_env, shell_target)
    fds: list[int] = []
    try:
        for label in ("c1", "c2"):
            att = sw_env.rpc("session.console_attach", {"selector": "COM0", "label": label})
            assert att["ok"] is True and att["vtty"], att
            fd = os.open(att["vtty"], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            fds.append(fd)
        listing = sw_env.rpc("session.console_list", {"selector": "COM0"})
        labels = sorted(c["label"] for c in listing["consoles"])
        # Python 基線（A/B 基準）：closed_consoles＝進入 release 時 bridge 上的 console 數，含 attach 時建立的
        # primary vtty（計畫表寫的 2 未計入 primary，以基線實際行為為準）。
        assert labels == ["c1", "c2", "primary"], listing
        rel = _release(sw_env)
        assert rel["closed_consoles"] == 3, rel
        assert rel["aborted_cmd"] is False, rel
        for fd in fds:
            ready, _, _ = select.select([fd], [], [], 3.0)
            assert ready == [fd], "release 後 console PTY 端點應可讀到 EOF／hangup"
            try:
                data = os.read(fd, 4096)
                while data:
                    ready, _, _ = select.select([fd], [], [], 3.0)
                    assert ready == [fd], f"console 端點只剩殘留輸出、未見 EOF：{data!r}"
                    data = os.read(fd, 4096)
            except OSError as exc:
                assert exc.errno == errno.EIO, exc
    finally:
        for fd in fds:
            os.close(fd)


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_release_flashing_busy(sw_env, fake_mcu):
    from p8_fixtures import FakeFlasher

    sw_env.start(targets={"COM0": fake_mcu}, profile="passthrough")
    sw_env.wait_state("COM0", "ATTACHED", timeout=10)
    link = sw_env.run_dir / "dev" / "ttyMCU"
    sw_env.wait_until(link.is_symlink, timeout=10)
    fl = FakeFlasher(link, baud=115200)
    try:
        fl.write(b"\x55\x55")
        assert fl.read_exact(2, timeout=3.0) == b"\x00\xcc"
        sw_env.wait_state("COM0", "FLASHING", timeout=5)
        rel = _body(sw_env.rpc("device.release", {"selector": "COM0", "source": "agent:p8", "reason": "busy"}))
        assert list(rel) == ["ok", "error_code", "selector", "session"], rel
        assert (rel["ok"], rel["error_code"], rel["selector"]) == (False, "FLASHING_BUSY", "COM0"), rel
        assert rel["session"]["state"] == "FLASHING", rel
        time.sleep(1.2)  # holder-probe armed
    finally:
        fl.close()
    sw_env.wait_state("COM0", "ATTACHED", timeout=5)
    rel = _body(sw_env.rpc("device.release", {"selector": "COM0", "source": "agent:p8", "reason": "after"}))
    assert rel["ok"] is True and rel["session"]["state"] == "RELEASED", rel
    assert fake_mcu.rx_log() == b"\x55\x55", "MCU 線只收到 flasher 自己的 sync"


# ---------------------------------------------------------------------------
# RELEASED 期間
# ---------------------------------------------------------------------------

@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_selftest_released_none_holder(sw_env, shell_target):
    _ready(sw_env, shell_target)
    _release(sw_env, reason="selftest")
    n0 = _settled_rx_len(shell_target)
    st = _body(sw_env.rpc("session.self_test", {"selector": "COM0"}))
    assert st["ok"] is True and st["classification"] == "RELEASED", st
    reclaimable = True
    if is_rust() and _same_uid_proc_fd_unreadable():
        # BC-04（OD-04，ab_participation: diff；parity-amendments #70）：/proc holder 掃描不完整 → holder_unknown，
        # 不視為可收回。Python 基線 self-test 尚未跟上 BC-04（只看 pids）；同 test_p7_selftest_recover 的先例。
        reclaimable = False
    want_action = "device_attach" if reclaimable else "wait_external_flash"
    assert (st["external_holder"], st["reclaimable"], st["recommended_action"]) == ("none", reclaimable, want_action), st
    assert (st["released_by"], st["reason"]) == ("agent:p8", "selftest"), st
    assert isinstance(st["released_at"], str) and st["released_at"], st
    assert st["session"]["state"] == "RELEASED", st
    time.sleep(0.5)
    assert shell_target.rx_log()[n0:] == b"", "RELEASED 的 self-test 唯讀，不開 tty、不 TX"
    assert _holders(sw_env, shell_target) == []


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_released_cmd_submit_not_ready(sw_env, shell_target):
    _ready(sw_env, shell_target)
    _release(sw_env)
    n0 = _settled_rx_len(shell_target)
    r = _body(sw_env.rpc("command.submit", {"selector": "COM0", "cmd": "echo released", "timeout_s": 5}))
    assert (r["ok"], r["error_code"]) == (False, "SESSION_NOT_READY"), r
    assert r["session"]["state"] == "RELEASED", r
    time.sleep(0.5)
    assert shell_target.rx_log()[n0:] == b""


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_released_blocks_auto_attach_on_hotplug(sw_env, shell_target):
    _ready(sw_env, shell_target)
    _release(sw_env)
    n0 = _settled_rx_len(shell_target)
    sw_env.unplug(shell_target.by_id_name)
    time.sleep(2.0)
    sw_env.replug(shell_target.by_id_name)
    time.sleep(3.0)
    assert sw_env.session("COM0")["state"] == "RELEASED"
    assert _holders(sw_env, shell_target) == [], "裝置重新出現時不得自動重開"
    assert shell_target.rx_log()[n0:] == b"", "RELEASED 裝置重新出現時零 TX"


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_handoff_never_double_held(sw_env, shell_target):
    _ready(sw_env, shell_target)
    daemon_pid = sw_env.daemon_pid
    with HolderSampler(shell_target.slave_path, exclude=(os.getpid(),)) as sampler:
        _release(sw_env)
        with ExternalHolder(shell_target.slave_path, flock=False) as holder:
            sw_env.wait_until(lambda: _holders(sw_env, shell_target) == [holder.pid], timeout=5)
            time.sleep(0.3)
        _wait_no_holder(sw_env, shell_target)
        r = sw_env.cli("device", "attach", "--selector", "COM0")
        assert r.exit == 0 and r.json is not None and r.json["ok"] is True, (r.exit, r.stdout, r.stderr)
        sw_env.wait_state("COM0", "READY", timeout=20)
    assert sampler.max_concurrent() <= 1, f"含 daemon 與外部工具，任一時刻至多一個持有者：{sampler.samples()}"
    seen = sampler.holders_seen()
    assert {daemon_pid, holder.pid} <= seen, seen


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_release_external_open_not_ebusy_repeated(sw_env, shell_target):
    """R0-F3：daemon 關閉前須清 TIOCEXCL，否則非 root 的外部 `open` 得 EBUSY；重複 3 輪。"""
    if os.geteuid() == 0:
        pytest.skip("root 不受 TIOCEXCL 限制，此案例必須以非 root 執行才有證據力")
    _ready(sw_env, shell_target)
    for round_no in (1, 2, 3):
        _release(sw_env, reason=f"round {round_no}")
        try:
            fd = os.open(shell_target.slave_path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            pytest.fail(f"第 {round_no} 輪 release 後外部 open 失敗（{errno.errorcode.get(exc.errno, exc.errno)}）")
        os.close(fd)
        r = sw_env.cli("device", "attach", "--selector", "COM0")
        assert r.exit == 0 and r.json is not None and r.json["ok"] is True, (round_no, r.exit, r.stdout, r.stderr)
        assert r.json.get("error_code") != "DEVICE_STILL_HELD", r.json
        sw_env.wait_state("COM0", "READY", timeout=20)


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_handoff_wal_continuity(sw_env, shell_target):
    _ready(sw_env, shell_target)
    _release(sw_env)
    seq_released = _wal_max_seq(sw_env)
    n0 = _settled_rx_len(shell_target)
    with ExternalHolder(shell_target.slave_path, flock=False):
        time.sleep(0.5)
    assert shell_target.rx_log()[n0:] == b"", "外部工具期間 UART 零 TX"
    seq_before_attach = _wal_max_seq(sw_env)
    r = sw_env.cli("device", "attach", "--selector", "COM0")
    assert r.exit == 0 and r.json is not None and r.json["ok"] is True, (r.exit, r.stdout, r.stderr)
    sw_env.wait_state("COM0", "READY", timeout=20)
    rows = sw_env.wal_records()
    seqs = [int(r["seq"]) for r in rows]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), "WAL seq 嚴格遞增"
    gap_tx = [r for r in rows if r.get("dir") == "TX" and seq_released < int(r["seq"]) <= seq_before_attach]
    assert gap_tx == [], f"外部工具期間無 daemon TX 紀錄：{gap_tx}"
    assert any(r.get("dir") == "TX" and int(r["seq"]) > seq_before_attach for r in rows), "收回後的登入／probe TX 接續記錄"


# ---------------------------------------------------------------------------
# OD-63：template 已不存在的 RELEASED 紀錄實體化為佔位 session
# ---------------------------------------------------------------------------

_RENAMED_TEMPLATE = """profiles:
  p8-shell-v2:
    platform: shell
    prompt_regex: '(?m)^root@sw-sim:.*# '
    timeout_s: 10
    uart:
      baud: 115200
      data_bits: 8
      parity: N
      stop_bits: 1
      flow_control: none
      xonxoff: false
"""


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_released_placeholder_blocks_command_then_attach_retires_it(sw_env, shell_target):
    _ready_dynamic(sw_env, shell_target)
    _release(sw_env, reason="placeholder")
    sw_env.stop()
    (Path(sw_env.profile_dir) / "p8.yaml").write_text(_RENAMED_TEMPLATE, encoding="utf-8")
    sw_env._start_daemon()
    time.sleep(3.0)
    n0 = _settled_rx_len(shell_target)
    s = sw_env.session("COM0")
    assert s["state"] == "RELEASED", s
    assert (s["profile"], s["platform"], s["command_capable"]) == ("p8-shell", "passthrough", False), s
    r = _body(sw_env.rpc("command.submit", {"selector": "COM0", "cmd": "echo placeholder", "timeout_s": 5}))
    assert (r["ok"], r["error_code"]) == (False, "SESSION_NOT_READY"), r
    assert _holders(sw_env, shell_target) == [], "佔位 session 不得開 tty"
    time.sleep(0.5)
    assert shell_target.rx_log()[n0:] == b"", "佔位 session 期間零 TX"
    r = sw_env.cli("device", "attach", "--selector", "COM0")
    assert r.exit == 0 and r.json is not None and r.json["ok"] is True, (r.exit, r.stdout, r.stderr)
    sw_env.wait_state("COM0", "READY", timeout=20)
    s = sw_env.session("COM0")
    assert (s["profile"], s["platform"], s["command_capable"]) == ("p8-shell-v2", "shell", True), s
    listing = sw_env.rpc("session.list", {})
    coms = [row["com"] for row in listing["sessions"]]
    assert coms == ["COM0"], f"佔位已移除自身、交 template 偵測沿用原 COM：{listing}"
