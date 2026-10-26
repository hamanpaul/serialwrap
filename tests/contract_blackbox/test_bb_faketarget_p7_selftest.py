"""P7 Task 18A（08-slice23-login-events.md）：黑箱 FakeTarget 的 P7 擴充自測（測試工具變更，規格 §10.4）。

(a) 不帶新參數時行為與 P3 逐位元組相同；(b) login_steps 依序回應；(c) freeze_rx 後 target 輸出 0 byte、TX 照收；
(d) boot 腳本時間誤差 ≤ 100 ms；(e) noise 間隔誤差 ≤ 20%；(f) console_lock 未見 secret 前不輸出 prompt。
另驗 delay_ms、commands.overrides、boot_on_start、rx_mark／rx_log_since 與 bb_p7.p7_profile_dir 代換。
stdlib＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import os
import select
import time

import pytest

# 模組層只放平台 marker；資源 marker 由各函式自帶（PTY 案例 posix_pty、純檔案代換 memory_only）。
pytestmark = [pytest.mark.posix_only]


@pytest.fixture
def make_target(tmp_path):
    from bb_faketarget import FakeTarget

    made: list = []

    def _make(**kwargs):
        ft = FakeTarget(tmp_path / f"wd-{len(made)}", **kwargs)
        made.append(ft)
        return ft

    yield _make
    for ft in made:
        ft.stop()


class _Slave:
    """以 PTY slave 端扮演 daemon：讀 target 輸出並記錄每段抵達時刻。"""

    def __init__(self, link) -> None:
        self.fd = os.open(os.path.realpath(link), os.O_RDWR | os.O_NOCTTY)
        os.set_blocking(self.fd, False)
        self.buf = bytearray()
        self.arrivals: list[tuple[float, int]] = []   # (時刻, 該段資料後的 buf 長度)

    def close(self) -> None:
        os.close(self.fd)

    def pump(self, duration: float) -> None:
        end = time.monotonic() + duration
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            r, _, _ = select.select([self.fd], [], [], min(0.02, remaining))
            if r:
                try:
                    chunk = os.read(self.fd, 65536)
                except BlockingIOError:
                    continue
                self.buf.extend(chunk)
                self.arrivals.append((time.monotonic(), len(self.buf)))

    def wait_for(self, token: bytes, timeout: float = 5.0) -> float:
        end = time.monotonic() + timeout
        while token not in self.buf:
            if time.monotonic() > end:
                raise AssertionError(f"逾時未見 {token!r}；最後收到 {bytes(self.buf[-200:])!r}")
            self.pump(0.02)
        return time.monotonic()

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)


@pytest.mark.posix_pty
def test_a_defaults_keep_p3_behaviour(make_target, tmp_path):
    ft = make_target()
    s = _Slave(ft.start(tmp_path / "by-id-a"))
    try:
        s.wait_for(b"login: ")
        s.write(b"root\r"); s.wait_for(b"Password: ")
        s.write(b"pw\r"); s.wait_for(b"root@fake:~# ")
        s.write(b"echo hi\r"); s.wait_for(b"hi\r\nroot@fake:~# ")
        s.write(b"\x03"); s.wait_for(b"^C\r\nroot@fake:~# ")
        assert bytes(s.buf) == (b"login: \r\nPassword: \r\nroot@fake:~# echo hi\r\nhi\r\nroot@fake:~# ^C\r\nroot@fake:~# ")
        assert ft.rx_log() == b"root\rpw\recho hi\r\x03"
    finally:
        s.close()


@pytest.mark.posix_pty
def test_b_login_steps_in_order(make_target, tmp_path):
    ft = make_target(prompt="# ", login_steps=[{"expect": "", "respond": "\r\nLogin: "}, {"expect": "admin", "respond": "\r\nPassword: "},
                                                {"expect": "pw", "respond": "\r\nCM> "}],
                     commands={"overrides": {"sh": {"response": "\r\n# "}}})
    s = _Slave(ft.start(tmp_path / "by-id-b"))
    try:
        s.pump(0.3)
        assert bytes(s.buf) == b"", "login_steps 模式啟動時不主動輸出"
        s.write(b"\n"); s.wait_for(b"\r\nLogin: ")
        s.write(b"nobody\n"); s.pump(0.3)
        assert bytes(s.buf).count(b"Login: ") == 2, "不符 expect 時重印目前提示"
        s.write(b"admin\n"); s.wait_for(b"\r\nPassword: ")
        s.write(b"pw\n"); s.wait_for(b"\r\nCM> ")
        s.write(b"sh\n"); s.wait_for(b"CM> \r\n# ")
        s.write(b"echo __READY__ab\n"); s.wait_for(b"__READY__ab\r\n# ")
    finally:
        s.close()


@pytest.mark.posix_pty
def test_c_freeze_rx_outputs_nothing_but_swallows_tx(make_target, tmp_path):
    ft = make_target()
    s = _Slave(ft.start(tmp_path / "by-id-c"))
    try:
        s.wait_for(b"login: ")
        mark = ft.rx_mark()
        ft.freeze_rx()
        before = len(s.buf)
        s.write(b"root\r")
        ft.emit("should-not-appear")
        s.pump(2.0)
        assert len(s.buf) == before, f"freeze 後 target 不得輸出：{bytes(s.buf[before:])!r}"
        assert ft.rx_log_since(mark) == b"root\r"
        ft.thaw_rx()
        ft.emit("back")
        s.wait_for(b"back")
    finally:
        s.close()


@pytest.mark.posix_pty
def test_d_boot_script_timing_within_100ms(make_target, tmp_path):
    ft = make_target(login_steps=[])
    s = _Slave(ft.start(tmp_path / "by-id-d"))
    try:
        t0 = time.monotonic()
        ft.boot([(0.2, "U-Boot 2024.04\r\n"), (0.6, "Hit any key to stop autoboot:  3"), (1.0, "\r\nlogin: ")])
        got = {}
        for token in (b"U-Boot 2024.04", b"autoboot:  3", b"\r\nlogin: "):
            got[token] = s.wait_for(token, timeout=3.0) - t0
        for token, want in ((b"U-Boot 2024.04", 0.2), (b"autoboot:  3", 0.6), (b"\r\nlogin: ", 1.0)):
            assert abs(got[token] - want) <= 0.1, (token, got[token], want)
    finally:
        s.close()


@pytest.mark.posix_pty
def test_e_noise_interval_within_20_percent(make_target, tmp_path):
    ft = make_target(login_steps=[], noise={"interval_ms": 100, "pattern": "kernel: noise"})
    s = _Slave(ft.start(tmp_path / "by-id-e"))
    try:
        s.pump(1.6)
        text = bytes(s.buf)
        n = text.count(b"kernel: noise\r\n")
        assert 12 <= n <= 19, f"1.6 s 內應約 16 行雜訊（±20%），實得 {n}"
        times = [t for t, _ in s.arrivals]
        gaps = [b - a for a, b in zip(times, times[1:])]
        mean = sum(gaps) / len(gaps)
        assert 0.08 <= mean <= 0.12, mean
    finally:
        s.close()


@pytest.mark.posix_pty
def test_f_console_lock_no_prompt_until_secret(make_target, tmp_path):
    ft = make_target(prompt="root@smart4r:~# ", console_lock={"secret": "k3y", "idle_s": 0.2})
    s = _Slave(ft.start(tmp_path / "by-id-f"))
    try:
        s.write(b"\n"); s.write(b"root\n"); s.write(b"wrong\n")
        s.pump(0.6)
        assert b"root@smart4r" not in bytes(s.buf), bytes(s.buf)
        s.write(b"k3y\n")
        s.wait_for(b"root@smart4r:~# ")
        s.write(b"echo __READY__z\n"); s.wait_for(b"__READY__z\r\nroot@smart4r:~# ")
        assert b"k3y" in ft.rx_log()
    finally:
        s.close()


@pytest.mark.posix_pty
def test_delay_ms_delays_each_response(make_target, tmp_path):
    ft = make_target(delay_ms=300)
    s = _Slave(ft.start(tmp_path / "by-id-g"))
    try:
        s.wait_for(b"login: ")
        t0 = time.monotonic()
        s.write(b"root\r")
        dt = s.wait_for(b"Password: ") - t0
        assert 0.25 <= dt <= 0.6, dt
    finally:
        s.close()


@pytest.mark.posix_pty
def test_boot_on_start_replaces_initial_login(make_target, tmp_path):
    ft = make_target(boot_on_start=[(0.0, "CFE version 1.0\r\n"), (0.3, "login: ")])
    s = _Slave(ft.start(tmp_path / "by-id-h"))
    try:
        s.wait_for(b"login: ")
        assert bytes(s.buf) == b"CFE version 1.0\r\nlogin: "
        s.write(b"root\r"); s.wait_for(b"Password: ")
    finally:
        s.close()


@pytest.mark.memory_only
def test_p7_profile_dir_substitutes_placeholders(tmp_path):
    from bb_p7 import p7_profile_dir

    d = p7_profile_dir(tmp_path / "prof", "shell", BY_ID="/dev/serial/by-id/usb-X", ENV_FILE="/tmp/OPI.env")
    text = (d / "shell.yaml").read_text(encoding="utf-8")
    assert "${" not in text
    assert "/dev/serial/by-id/usb-X" in text and "/tmp/OPI.env" in text
    with pytest.raises(KeyError):
        p7_profile_dir(tmp_path / "prof2", "shell", BY_ID="x")   # 缺 ENV_FILE 代換值
