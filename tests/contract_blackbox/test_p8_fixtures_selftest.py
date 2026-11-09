"""P8 Task 0（09-slice24-transfer-flash.md）：黑箱共用 fixture 自測（測試工具變更，規格 §10.4）。

(a) `sw_env.start`＋`shell_target` 到 READY，DUT 端 `rx_log()` 含 ready probe，READY 期間 slave 只有 daemon 持有；
    `sw_env.stop()` 後 daemon 行程、socket 與 tty 持有皆消失（對 Python 基線；Rust artifact 以同一檔再跑）；
(b) ShellTarget 單獨運作：HOME／TMPDIR／cwd 指向案例暫存目錄、`hide_tools`、`echo_stall_after`、`line_limit`、
    `silent_after`、`half_prompt`、`slow_reader_bps`、`hold_read_on`（讀到 marker 停讀／`release_read`）；
(c) `HolderSampler` 在 `ExternalHolder` 存活期間 `max_concurrent()==1`、結束後為 0，且只有該單一 pid 持有；
    flock 衝突、TIOCEXCL（EBUSY）與不存在路徑一律 `ExternalHolderOpenError`；
(d) `fake_mcu` 收到 `55 55` 回 `00 cc`、之後每個封包回 `00 cc`；`FakeFlasher` 的 baud 反映在 `current_speed()`。
stdlib＋pytest＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import errno
import os
import select
import time
from pathlib import Path

import pytest

from p8_fixtures import (
    ANY,
    CliRun,
    ExternalHolder,
    ExternalHolderOpenError,
    FakeFlasher,
    HolderSampler,
    ShellTarget,
    drop_version,
    scan_holders,
)

# 模組層只放平台 marker；資源 marker 由各函式自帶（PTY／子行程案例 posix_pty＋process_rpc，純物件案例 memory_only）。
pytestmark = [pytest.mark.posix_only]


class _DutPeer:
    """以 PTY slave 端扮演 daemon（UART 端）：寫命令、收 ShellTarget 的回應。"""

    def __init__(self, slave_path: str) -> None:
        import termios
        import tty

        self.fd = os.open(slave_path, os.O_RDWR | os.O_NOCTTY)
        attrs = termios.tcgetattr(self.fd)
        tty.cfmakeraw(attrs)
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.buf = b""

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def read_for(self, seconds: float) -> bytes:
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return self.buf
            ready, _, _ = select.select([self.fd], [], [], left)
            if ready:
                self.buf += os.read(self.fd, 4096)

    def read_until(self, needle: bytes, timeout: float = 10.0) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise AssertionError(f"未在 {timeout}s 內收到 {needle!r}；已收到 {self.buf!r}")
            ready, _, _ = select.select([self.fd], [], [], left)
            if ready:
                self.buf += os.read(self.fd, 4096)
        return self.buf

    def drain(self) -> None:
        self.read_for(0.3)
        self.buf = b""

    def close(self) -> None:
        os.close(self.fd)


@pytest.fixture
def make_shell(tmp_path):
    made: list[ShellTarget] = []
    peers: list[_DutPeer] = []

    def _make(**kwargs) -> tuple[ShellTarget, _DutPeer]:
        st = ShellTarget(tmp_path / f"root{len(made)}", **kwargs).start()
        made.append(st)
        peer = _DutPeer(st.slave_path)
        peers.append(peer)
        peer.send(b"\n")
        peer.read_until(b"root@sw-sim:/# ")
        peer.drain()
        return st, peer

    yield _make
    for p in peers:
        p.close()
    for st in made:
        st.stop()


def _proc_cmdlines_containing(needle: str) -> list[int]:
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            raw = Path(f"/proc/{name}/cmdline").read_bytes()
        except OSError:
            continue
        if needle.encode() in raw:
            out.append(int(name))
    return out


# ---------------------------------------------------------------- (a) daemon＋ShellTarget
@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_sw_env_shell_target_ready_then_clean_stop(sw_env, shell_target):
    sw_env.start(targets={"COM0": shell_target})
    sw_env.wait_state("COM0", "READY", timeout=20)
    assert b"__READY__" in shell_target.rx_log(), "DUT 端必須實際收到 ready probe（外部 oracle）"
    assert any("__READY__" in c for c in shell_target.commands())
    pid = sw_env.daemon_pid
    assert pid is not None
    assert sw_env.holders(shell_target.slave_path, exclude_self=True) == [pid], "READY 期間 slave 只能由 daemon 持有"
    assert sw_env.holders(shell_target.slave_path, exclude_self=False) == [pid], "測試行程本身不得持有 slave"
    r = sw_env.cli("session", "list")
    assert isinstance(r, CliRun) and r.exit == 0 and r.json is not None and r.json.get("ok") is True
    assert any(row.get("com") == "COM0" and row.get("state") == "READY" for row in r.json["sessions"])
    assert sw_env.rpc_ok("health.ping")

    sw_env.stop()
    assert not Path(f"/proc/{pid}").exists(), "daemon 主行程於 stop 後不得殘留"
    assert not sw_env.socket.exists(), "socket 於 stop 後不得殘留"
    assert sw_env.holders(shell_target.slave_path, exclude_self=True) == [], "stop 後不得有 tty 持有者"
    assert _proc_cmdlines_containing(str(sw_env.socket)) == [], "不得殘留以本 socket 啟動的行程"
    assert not sw_env.rpc_ok("health.ping")


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_throwaway_daemon_isolated_and_target_teardown_stops_it(throwaway_daemon, sw_env, tmp_path):
    from p8_fixtures import _stop_envs_using

    st = ShellTarget(tmp_path / "tw-root").start()
    try:
        b = throwaway_daemon(targets={"COM0": st})
        for attr in ("run_dir", "state_dir", "wal_dir", "by_id_dir", "by_path_dir", "profile_dir", "socket"):
            assert getattr(b, attr) != getattr(sw_env, attr), f"throwaway 的 {attr} 必須獨立"
        assert b.isolated_env()["SERIALWRAP_CONFIG_DIR"] != sw_env.isolated_env()["SERIALWRAP_CONFIG_DIR"]
        b.wait_state("COM0", "READY", timeout=20)
        link = b.by_id_dir / st.by_id_name
        b.unplug(st.by_id_name)
        assert not link.is_symlink()
        b.replug(st.by_id_name)
        assert os.readlink(link) == st.slave_path
        pid = b.daemon_pid
        _stop_envs_using(st.slave_path)          # target fixture 收尾時先停掉仍開著該 tty 的 daemon
        assert b.daemon_pid is None and not Path(f"/proc/{pid}").exists()
        assert scan_holders(st.slave_path) == []
    finally:
        st.stop()


# ---------------------------------------------------------------- (b) ShellTarget 單獨運作
@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_shell_target_runs_in_case_tmpdir(make_shell):
    st, peer = make_shell()
    peer.send(b'echo "H=$HOME T=$TMPDIR P=$(pwd)"; echo ok > probe.txt\n')
    out = peer.read_until(b"root@sw-sim:/# ").decode("utf-8", errors="replace")
    root = str(st.root)
    assert f"H={root} T={root}/tmp P={root}" in out
    assert (st.root / "probe.txt").read_text() == "ok\n"
    assert out.startswith('echo "H=$HOME'), "relay 必須回顯輸入"
    assert st.commands()[-1] == 'echo "H=$HOME T=$TMPDIR P=$(pwd)"; echo ok > probe.txt'


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_shell_target_hide_tools(make_shell):
    _st, peer = make_shell(hide_tools=("base64", "openssl"))
    peer.send(b"base64 --version; echo rc=$?; openssl version; echo rc2=$?\n")
    out = peer.read_until(b"rc2=").decode("utf-8", errors="replace")
    out += peer.read_until(b"root@sw-sim:/# ").decode("utf-8", errors="replace")
    assert "sh: base64: not found" in out and "rc=127" in out
    assert "sh: openssl: not found" in out and "rc2=127" in out


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_shell_target_echo_stall_after(make_shell):
    st, peer = make_shell()
    st.arm(echo_stall_after=3)
    peer.send(b"echo XYZ\n")
    out = peer.read_until(b"root@sw-sim:/# ")
    # 只回顯前 3 個輸入 byte（"ech"），之後是 shell 照常執行的輸出 "XYZ\r\n" 與 prompt
    assert out == b"echXYZ\r\nroot@sw-sim:/# ", out
    assert st.rx_log().endswith(b"echo XYZ\n")


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_shell_target_line_limit(make_shell):
    _st, peer = make_shell(line_limit=8)
    peer.send(b"echo 0123456789\n")
    out = peer.read_until(b"root@sw-sim:/# ")
    assert b"echo 012\r\n012\r\n" in out, out
    assert b"0123" not in out


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_shell_target_silent_after(make_shell):
    st, peer = make_shell()
    st.arm(silent_after=len(b"echo a\n"))
    peer.send(b"echo a\n")
    peer.read_until(b"root@sw-sim:/# ")
    peer.drain()
    peer.send(b"echo b\n")
    assert peer.read_for(0.5) == b"", "超過 silent_after 後 DUT 必須完全靜默"
    assert st.rx_log().endswith(b"echo b\n"), "靜默期間 rx_log 照記"


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_shell_target_half_prompt(make_shell):
    st, peer = make_shell()
    st.arm(half_prompt="MARK")
    peer.send(b"echo MARK\n")
    out = peer.read_until(b"MARK\r\nroot@sw")
    assert out.endswith(b"MARK\r\nroot@sw"), out
    assert peer.read_for(0.5) == out, "半個 prompt 之後不得再有任何輸出"
    peer.send(b"echo z\n")
    assert peer.read_for(0.3) == out


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_shell_target_slow_reader(make_shell):
    st, peer = make_shell(slow_reader_bps=2400)
    base = len(st.rx_log())
    payload = b"#" + b"x" * 199 + b"\n"            # shell 註解行：不產生命令輸出
    peer.send(payload)
    time.sleep(0.3)
    assert len(st.rx_log()) - base < len(payload), "2400 bps（240 B/s）下 0.3 s 內不得讀完 201 bytes"
    deadline = time.monotonic() + 10
    while len(st.rx_log()) - base < len(payload) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert st.rx_log()[base:] == payload


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_shell_target_hold_read_on_marker(make_shell):
    """`hold_read_on`：讀到 marker 後停讀，寫入方塞滿 kernel 緩衝即 EAGAIN、DUT 端不再增加；`release_read` 後全數送達。"""
    st, peer = make_shell()
    base = len(st.rx_log())
    st.hold_read_on(b"#hold-mark")
    assert not st.read_held()
    payload = b"#hold-mark" + b"." * 89 + b"\n" + (b"#" + b"y" * 98 + b"\n") * 2000  # shell 註解行
    os.set_blocking(peer.fd, False)
    sent = 0
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            sent += os.write(peer.fd, payload[sent:])
        except BlockingIOError:
            if st.read_held():
                break
            time.sleep(0.01)
    assert st.read_held(), "讀到 marker 後應停讀"
    assert sent < len(payload), "停讀後寫入方無法寫完（kernel 緩衝有限）"
    held_rx = len(st.rx_log())
    assert held_rx - base < len(payload)
    time.sleep(0.3)  # 觀察窗（證明停讀期間不再讀取），不作同步點
    assert len(st.rx_log()) == held_rx
    with pytest.raises(BlockingIOError):
        os.write(peer.fd, payload[sent:])
    st.release_read()
    assert not st.read_held()
    deadline = time.monotonic() + 10
    while sent < len(payload) and time.monotonic() < deadline:
        try:
            sent += os.write(peer.fd, payload[sent:])
        except BlockingIOError:
            time.sleep(0.01)
    while len(st.rx_log()) - base < len(payload) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert st.rx_log()[base:] == payload


# ---------------------------------------------------------------- (c) 持有者
@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_holder_sampler_sees_single_external_holder(tmp_path):
    from shelltarget import open_dut_pty

    master, slave = open_dut_pty()
    try:
        with HolderSampler(slave) as s:
            h = ExternalHolder(slave, flock=False)
            h.start()
            try:
                deadline = time.monotonic() + 5
                while scan_holders(slave) != [h.pid] and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert scan_holders(slave) == [h.pid], "ExternalHolder 必須以單一 pid 持有"
                time.sleep(0.1)
            finally:
                h.stop()
            time.sleep(0.1)
        assert s.max_concurrent() == 1
        assert s.holders_seen() == {h.pid}
        assert s.samples()[-1][1] == [], "持有者結束後的樣本必須為空"
        with HolderSampler(slave) as s2:
            time.sleep(0.05)
        assert s2.max_concurrent() == 0
    finally:
        os.close(master)


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_external_holder_open_errors(tmp_path):
    from shelltarget import open_dut_pty

    with pytest.raises(ExternalHolderOpenError) as e:
        ExternalHolder(str(tmp_path / "no-such-tty"), flock=False).start()
    assert e.value.errno == errno.ENOENT

    master, slave = open_dut_pty()
    try:
        with ExternalHolder(slave, flock=True) as first:
            with pytest.raises(ExternalHolderOpenError) as e:
                ExternalHolder(slave, flock=True).start()
            assert e.value.errno in (errno.EWOULDBLOCK, errno.EAGAIN), "flock 被占用必須回報，不得靜默持有"
            assert scan_holders(slave) == [first.pid]
        if os.geteuid() != 0:   # root 不受 TIOCEXCL 限制
            with FakeFlasher(slave, baud=None, excl=True):
                with pytest.raises(ExternalHolderOpenError) as e:
                    ExternalHolder(slave, flock=False).start()
                assert e.value.errno == errno.EBUSY, "TIOCEXCL 期間外部 open 必須回 EBUSY（R0-F3）"
    finally:
        os.close(master)


# ---------------------------------------------------------------- (d) FakeMcu／FakeFlasher
@pytest.mark.posix_pty
def test_fake_mcu_sync_ack_and_packets(fake_mcu):
    import termios

    with FakeFlasher(fake_mcu.slave_path, baud=57600) as fl:
        assert fake_mcu.current_speed() == termios.B57600
        fl.write(b"\x55\x55")
        assert fl.read_exact(2, 3.0) == b"\x00\xcc"
        fl.write(b"\x03\x10\x20")
        assert fl.read_exact(2, 3.0) == b"\x00\xcc"
        fl.write(b"\x55\x55")                         # 封包邊界重新同步（同一 daemon 第二次 flash）
        assert fl.read_exact(2, 3.0) == b"\x00\xcc"
        fl.set_baud(115200)
        assert fake_mcu.current_speed() == termios.B115200
    assert fake_mcu.rx_log() == b"\x55\x55\x03\x10\x20\x55\x55"
    with FakeFlasher(fake_mcu.slave_path, baud=None):
        assert fake_mcu.current_speed() == termios.B115200, "baud=None 不得改動 speed"


@pytest.mark.memory_only
def test_any_and_drop_version():
    assert ANY == 1 and ANY == "x" and ANY == {"a": 1}
    assert {"ok": True, "version": "0.4.0"} == {"ok": True, "version": ANY}
    assert drop_version({"ok": False, "error_code": "X", "version": "0.4.0"}) == {"ok": False, "error_code": "X"}
    assert drop_version(None) is None
