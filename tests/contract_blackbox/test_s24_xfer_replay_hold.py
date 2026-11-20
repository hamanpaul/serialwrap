"""回放保留期間的 `file.push`／`file.pull`（S2-P8X-df28b；rulings s2「編排者更正（2026-11-08）：OD-82 的前提不成立」）。

agent 命令執行期間 human console 打的字先暫存（deferred），命令結束後才回放到 UART；回放寫完之前前景仍忙
（Python：`session_manager.py` 的 `_finish_foreground_operation` 在鎖外寫回放、寫完才清前景 token；Rust：07 D-22 的
replay hold）。此期間發出的 file.push／file.pull 兩邊都立即回 `{"ok":false,"error_code":"SESSION_BUSY","selector":"COM0"}`、
零傳檔 TX，回放結束後也不補執行（07 D-22(2)、09 EVT-D-F6「file transfer 不排隊」）。DF-28 作廢，本檔不標任何預期差異；
A/B：同檔對 Python 基線與 Rust artifact 各跑一次，期望相同。

確定性窗口（不靠 sleep 賭時序）：
- 命令執行中寫入 console 的 `FILLER`（250 000 bytes，低於 DEFERRED_INPUT_MAX_BYTES 的 256 KiB）由 daemon 依序收進
  deferred：Python 的 bridge select 迴圈在同一執行緒依讀取順序處理 console bytes；Rust 的 console 輸入與 UART RX
  同走 data lane FIFO，命令的 OpFinished 帶 RX watermark，排在先讀到的 console 輸入之後。測試端寫完 `FILLER` 後才放行
  命令，daemon 尚未讀走的至多是 kernel PTY 緩衝的量；
- `ShellTarget.hold_read_on(MARK)`：DUT 讀到回放開頭的 `MARK` 即停止讀取 UART，回放剩餘 bytes 遠大於 kernel PTY 緩衝，
  daemon 的回放寫入在 `release_read()` 前無法完成；
- 發 RPC 前後都確認 DUT 仍停讀、`session.foreground_busy` 為 true，證明兩個 RPC 落在同一個回放保留窗口內。

外部 oracle（Review Focus 6）：DUT 端 `rx_log()`／`commands()`、DUT 端與本機檔案；WAL 不當 oracle。
RPC 回應比對前去掉 framing `id` 與中繼欄位 `version`／`code_fingerprint`。
stdlib＋pytest＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import os
import select
import threading
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.posix_only]

_META_KEYS = ("id", "version", "code_fingerprint")
BUSY = {"ok": False, "error_code": "SESSION_BUSY", "selector": "COM0"}
MARK = b"#sw-replay-hold-mark"
_LINE = 1000
# 每行皆為 shell 註解、以 `\r` 結尾（console 端不經 ONLCR 轉換；DUT shell 端 ICRNL 轉為換行），回放後只產生 prompt。
FILLER = MARK + b"." * (_LINE - len(MARK) - 1) + b"\r" + b"".join(
    b"#%06d" % i + b"x" * (_LINE - 8) + b"\r" for i in range(1, 250)
)
GO = "replay-hold-go"
HOLD_CMD = f"while [ ! -e {GO} ]; do sleep 0.05; done; echo replay-hold-cmd-done"


def _body(d: dict | None) -> dict | None:
    if d is None:
        return None
    return {k: v for k, v in d.items() if k not in _META_KEYS}


class _ConsoleDrain:
    """持續讀走 console vtty 的 RX fan-out，避免 daemon→console 方向的緩衝塞滿。"""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="console-drain", daemon=True)

    def start(self) -> "_ConsoleDrain":
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.1)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                if not os.read(self._fd, 65536):
                    return
            except BlockingIOError:
                continue
            except OSError:
                return

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)


def _write_all(fd: int, data: bytes, timeout_s: float) -> None:
    """非阻塞寫完 `data`（daemon 讀走多少就寫多少）；寫完時 daemon 尚未讀走的至多是 kernel PTY 緩衝的量。"""
    view = memoryview(data)
    sent = 0
    deadline = time.monotonic() + timeout_s
    while sent < len(data):
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"console 寫入未在 {timeout_s}s 內完成：{sent}/{len(data)} bytes")
        _, ready, _ = select.select([], [fd], [], min(left, 0.2))
        if not ready:
            continue
        try:
            sent += os.write(fd, view[sent:])
        except BlockingIOError:
            continue


def _settled_rx_len(shell_target, quiet_s: float = 0.3) -> int:
    """等 DUT 端輸入停止變化（`quiet_s` 內無新增）後回傳長度。"""
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


@pytest.mark.posix_pty
@pytest.mark.process_rpc
def test_file_transfer_busy_during_replay_hold(sw_env, shell_target, tmp_path):
    assert len(FILLER) == 250_000
    sw_env.start(targets={"COM0": shell_target})
    sw_env.wait_state("COM0", "READY", timeout=20)
    src = tmp_path / "push-src.bin"
    src.write_bytes(b"replay-hold-push\n" * 16)
    remote_push = shell_target.root / "pushed.bin"
    remote_pull = shell_target.root / "pull-src.bin"
    remote_pull.write_bytes(b"replay-hold-pull\n" * 16)
    local_pull = tmp_path / "pulled.bin"

    att = sw_env.rpc("session.console_attach", {"selector": "COM0", "label": "h1"})
    assert att.get("ok") is True and att.get("interactive_owner") is True and att.get("vtty"), att
    fd = os.open(att["vtty"], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    drain = _ConsoleDrain(fd).start()
    try:
        sub = sw_env.rpc("command.submit", {"selector": "COM0", "cmd": HOLD_CMD, "timeout_s": 60})
        assert sub.get("ok") is True, sub
        cmd_id = sub["cmd_id"]
        # DUT 已收到整行命令：兩邊皆在命令 TX 之前取得前景並 suspend human raw，之後的 console 輸入進 deferred。
        sw_env.wait_until(lambda: HOLD_CMD in shell_target.commands(), timeout=10)
        cmd_end = shell_target.rx_log().index(HOLD_CMD.encode() + b"\n") + len(HOLD_CMD) + 1
        shell_target.hold_read_on(MARK)
        _write_all(fd, FILLER, timeout_s=30)
        (shell_target.root / GO).touch()

        # 回放已開始寫出（DUT 讀到回放開頭的 MARK 後停讀）：回放剩餘 bytes 留在 kernel 緩衝、寫入無法完成。
        sw_env.wait_until(shell_target.read_held, timeout=20)
        assert sw_env.session("COM0")["foreground_busy"] is True, "回放未寫完：前景仍忙"
        rx_held = len(shell_target.rx_log())
        push = sw_env.rpc(
            "file.push", {"selector": "COM0", "local_path": str(src), "remote_path": str(remote_push)}
        )
        pull = sw_env.rpc(
            "file.pull", {"selector": "COM0", "remote_path": str(remote_pull), "local_path": str(local_pull)}
        )
        assert _body(push) == BUSY, push
        assert _body(pull) == BUSY, pull
        assert shell_target.read_held(), "兩個 RPC 皆在 DUT 停讀期間回覆"
        assert sw_env.session("COM0")["foreground_busy"] is True, "兩個 RPC 皆落在同一回放保留窗口內"
        assert len(shell_target.rx_log()) == rx_held

        shell_target.release_read()
        sw_env.wait_until(lambda: FILLER in shell_target.rx_log(), timeout=30)
        sw_env.wait_until(lambda: sw_env.session("COM0")["foreground_busy"] is False, timeout=20)
        got: dict = {}

        def _done() -> bool:
            got.update(sw_env.rpc("command.get", {"cmd_id": cmd_id})["command"])
            return got.get("status") in {"done", "error", "canceled"}

        sw_env.wait_until(_done, timeout=20)
        assert got["status"] == "done", got
        settled = _settled_rx_len(shell_target)
        time.sleep(1.0)  # 觀察窗（證明回放結束後不補執行傳檔），不作同步點
        rx = shell_target.rx_log()
        assert len(rx) == settled, "回放結束後 DUT 端零新增 bytes"
        assert rx[cmd_end:] == FILLER, "命令之後 DUT 只收到回放（逐位元組等於 FILLER），沒有任何傳檔 TX"
        assert not [c for c in shell_target.commands() if "SW_XFER" in c], "傳檔未啟動"
        assert not remote_push.exists(), "push 未寫出 DUT 端檔案"
        assert not local_pull.exists(), "pull 未寫出本機檔案"
    finally:
        drain.stop()
        os.close(fd)
