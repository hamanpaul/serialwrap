"""P5-T15a：黑箱 FakeTarget 擴充的紅燈測試（測試資產變更，規格 §10.4）。

對應 docs/rust-refactor/plans/06-slice21-daemon-core.md「Task 15a」Step 1。
待擴充的能力（全部 keyword-only、預設值維持舊行為）：
- 建構子 `login`、`boot_banner`、`noise_interval_s`（本檔只驗 login 與 boot_banner）
- 方法 `emit(data)`、`written()`（= `rx_log()` 別名）
- 行內命令 `sleep N`（N 秒後才送 prompt）、`yes | head -c N`（恰送 N bytes 的 `y\\n` 後才送 prompt）

本檔只用 stdlib 與 PTY，不得 import sw_core／serialwrap／serialwrapd。
紅燈階段預期失敗原因：建構子缺 `login`／`boot_banner` 參數（TypeError）、缺 `emit`／`written`
（AttributeError），以及 `sleep`／`yes | head` 尚未實作（prompt 立即送出，時序或位元組斷言失敗）。
"""
from __future__ import annotations

import os
import select
import time

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty]

PROMPT = "root@fake:~# "
PROMPT_B = PROMPT.encode("utf-8")


@pytest.fixture
def make_target(tmp_path):
    """建立 FakeTarget 的工廠；測試結束時統一 stop。"""
    from bb_faketarget import FakeTarget

    made: list = []

    def _make(**kwargs):
        ft = FakeTarget(tmp_path / f"wd-{len(made)}", **kwargs)
        made.append(ft)
        return ft

    yield _make
    for ft in made:
        ft.stop()


class _SlaveReader:
    """以 PTY slave 端讀取 DUT 輸出（與既有 test_bb_harness_selftest 相同做法）。"""

    def __init__(self, path: str) -> None:
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        os.set_blocking(self.fd, False)
        self.buf = bytearray()

    def close(self) -> None:
        os.close(self.fd)

    def wait_for(self, token: bytes, *, start: int = 0, timeout: float = 5.0) -> tuple[int, float]:
        """等待 buf[start:] 出現 token；回傳 (token 起始的絕對索引, 含該 token 的資料抵達時刻)。"""
        deadline = time.monotonic() + timeout
        arrived = time.monotonic()
        while True:
            idx = self.buf.find(token, start)
            if idx >= 0:
                return idx, arrived
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"逾時未見 {token!r}；最後收到 {bytes(self.buf[-200:])!r}")
            rlist, _, _ = select.select([self.fd], [], [], min(0.05, remaining))
            if not rlist:
                continue
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                raise AssertionError("DUT 端 PTY 提前 EOF")
            arrived = time.monotonic()
            self.buf.extend(chunk)


def _open_reader(ft, tmp_path) -> _SlaveReader:
    ft.start(tmp_path / "by-id")
    return _SlaveReader(ft.slave_path)


def _login_default(rd: _SlaveReader) -> int:
    """以 FakeTarget 預設帳密（root／pw）登入，回傳抵達 prompt 後的 buf 長度。"""
    rd.wait_for(b"login: ")
    os.write(rd.fd, b"root\r")
    rd.wait_for(b"Password: ")
    os.write(rd.fd, b"pw\r")
    rd.wait_for(PROMPT_B)
    return len(rd.buf)


# ---------------------------------------------------------------------------
# 新參數 login=False：開機直接送 prompt（不經 login 流程）
# ---------------------------------------------------------------------------


def test_login_false_sends_prompt_at_boot(make_target, tmp_path):
    ft = make_target(login=False)
    rd = _open_reader(ft, tmp_path)
    try:
        rd.wait_for(PROMPT_B)
        assert bytes(rd.buf) == PROMPT_B, "login=False 時開機輸出應僅為 prompt，不得出現 login:"
    finally:
        rd.close()


# ---------------------------------------------------------------------------
# 新參數 boot_banner：在第一個 prompt（login: 或 shell prompt）之前送出
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("login", "first_prompt"),
    [(False, PROMPT_B), (True, b"login: ")],
    ids=["login-false-shell-prompt", "login-true-login-prompt"],
)
def test_boot_banner_precedes_first_prompt(make_target, tmp_path, login, first_prompt):
    banner = b"BOOT: serial console ready\r\n"
    ft = make_target(login=login, boot_banner=banner)
    rd = _open_reader(ft, tmp_path)
    try:
        rd.wait_for(first_prompt)
        assert bytes(rd.buf) == banner + first_prompt, "boot_banner 必須位於第一個 prompt 之前且逐位元組送出"
    finally:
        rd.close()


# ---------------------------------------------------------------------------
# 方法 emit(data)：立即從 DUT 端送出，master 端（test 的 slave 讀取端）可讀到
# ---------------------------------------------------------------------------


def test_emit_delivers_bytes_to_master_side(make_target, tmp_path):
    ft = make_target()
    rd = _open_reader(ft, tmp_path)
    try:
        mark = _login_default(rd)
        ft.emit(b"ASYNC-EVT 42\r\n")
        rd.wait_for(b"ASYNC-EVT 42\r\n", start=mark)
        assert bytes(rd.buf[mark:]) == b"ASYNC-EVT 42\r\n", "emit 應只送出傳入的 bytes，不得附加任何內容"
    finally:
        rd.close()


# ---------------------------------------------------------------------------
# 行內命令 sleep N：N 秒後才送 prompt
# ---------------------------------------------------------------------------


def test_sleep_command_sends_prompt_after_n_seconds(make_target, tmp_path):
    ft = make_target()
    rd = _open_reader(ft, tmp_path)
    try:
        mark = _login_default(rd)
        t0 = time.monotonic()
        os.write(rd.fd, b"sleep 1\r")
        _, arrived = rd.wait_for(PROMPT_B, start=mark, timeout=5.0)
        elapsed = arrived - t0
        assert 0.9 <= elapsed <= 1.5, f"sleep 1 應於 0.9～1.5 s 後才送 prompt，實測 {elapsed:.3f} s"
    finally:
        rd.close()


# ---------------------------------------------------------------------------
# 行內命令 yes | head -c N：恰送 N bytes 的 `y\n` 後才送 prompt
# ---------------------------------------------------------------------------


def test_yes_head_emits_exact_bytes_then_prompt(make_target, tmp_path):
    ft = make_target()
    rd = _open_reader(ft, tmp_path)
    try:
        mark = _login_default(rd)
        os.write(rd.fd, b"yes | head -c 200000\r")
        idx, _ = rd.wait_for(PROMPT_B, start=mark, timeout=10.0)
        region = bytes(rd.buf[mark:idx])
        echo, sep, payload = region.partition(b"\n")
        assert sep == b"\n", f"命令回顯後應有換行，實得 {region[:120]!r}"
        assert b"yes | head -c 200000" in echo, f"回顯行異常：{echo!r}"
        assert len(payload) == 200000, f"應恰送出 200000 bytes，實得 {len(payload)}"
        assert payload == b"y\n" * 100000, "輸出內容應為重複的 y\\n"
    finally:
        rd.close()


# ---------------------------------------------------------------------------
# 方法 written()：DUT 已收到的全部 bytes（rx_log 的別名）
# ---------------------------------------------------------------------------


def test_written_returns_all_bytes_received_by_dut(make_target, tmp_path):
    ft = make_target()
    rd = _open_reader(ft, tmp_path)
    try:
        mark = _login_default(rd)
        os.write(rd.fd, b"echo abc\r")
        rd.wait_for(PROMPT_B, start=mark)
        expected = b"root\rpw\recho abc\r"
        assert ft.written() == expected, "written() 應為 DUT 收到的全部 bytes（含登入輸入）"
        assert ft.written() == ft.rx_log(), "written() 必須等於 rx_log()"
    finally:
        rd.close()


# ---------------------------------------------------------------------------
# 舊建構子呼叫（P3 自測形式）行為不變：只給 workdir 時仍為 login 流程，開機輸出僅 "login: "
# ---------------------------------------------------------------------------


def test_legacy_constructor_keeps_login_boot_output(make_target, tmp_path):
    ft = make_target()
    assert ft.prompt == PROMPT, "預設 prompt 不得改變"
    assert ft.user == "root" and ft.password == "pw", "預設帳密不得改變"
    rd = _open_reader(ft, tmp_path)
    try:
        rd.wait_for(b"login: ")
        assert bytes(rd.buf) == b"login: ", "舊呼叫形式開機輸出必須僅為 login: "
        _login_default(rd)
        assert bytes(rd.buf).endswith(PROMPT_B), "舊呼叫形式登入後應進入預設 prompt"
    finally:
        rd.close()
