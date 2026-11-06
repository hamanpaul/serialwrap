"""Slice 2.2 BC-02 黑箱契約（07 Task 11）。"""
from __future__ import annotations

import os
import re

import pytest

from .console_helpers import b64_payload, open_vtty, tx_payload, wait_for, wait_rx_contains

pytestmark = pytest.mark.cap_posix_pty


def test_normal_suspend_loses_no_keystroke(console_daemon) -> None:
    import time

    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    fd1 = open_vtty(a1["vtty"])
    human_src = f"human:{a1['client_id']}"
    cmd_ids: list[str] = []
    try:
        for n in range(10):
            sub = d.sw("cmd", "submit", "--selector", "COM0", "--cmd", "sleep 0.6", "--cmd-timeout", "5")
            assert sub["ok"] is True, sub
            cid = sub["cmd_id"]
            cmd_ids.append(cid)
            # 命令已開始（WAL 出現該 cmd_id 的 TX）後才於命令期間打字
            assert wait_for(
                lambda: any(r.get("dir") == "TX" and r.get("cmd_id") == cid for r in d.wal_records()), 3.0, 0.02
            ), f"命令 {cid} 未開始送出"
            os.write(fd1, str(n).encode())
            d.wait_command_done(cid)
            time.sleep(0.05)
        assert wait_rx_contains(target, b"9", 3.0)
        recs = sorted((r for r in d.wal_records() if r["dir"] == "TX"), key=lambda r: r["seq"])
        human = b"".join(b64_payload(r) for r in recs if r["source"] == human_src)
        assert human == b"0123456789", human
        # 每筆 human TX 的 seq 都不落在任何 agent 命令 TX 的 (min_seq, max_seq) 開區間內（逐筆 WAL TX 不交錯）
        spans = []
        for cid in cmd_ids:
            seqs = [r["seq"] for r in recs if r.get("cmd_id") == cid and r["source"] != human_src]
            assert seqs, f"命令 {cid} 無 TX 記錄"
            spans.append((min(seqs), max(seqs)))
        for r in recs:
            if r["source"] == human_src:
                for lo, hi in spans:
                    assert not (lo < r["seq"] < hi), (r, lo, hi)
        # 審查 3317987_d6e3b3c-opus Medium（c6578a2 #1）：每個 agent 命令只有一筆 TX，上面的開區間恆為空；
        # 另以「命令完成」為界：第 n 輪命令 TX 之後第一筆含 prompt（`#`）的 RX 為 s_done[n]，第 n 個 human TX 必須晚於它
        # （命令期間的按鍵 defer 到命令結束後才回放；直接送出時 human seq 會落在 s_done 之前）。
        allrecs = sorted((r for r in d.wal_records() if r["dir"] in ("TX", "RX")), key=lambda r: r["seq"])
        done: list[int] = []
        for cid in cmd_ids:
            s_cmd = min(r["seq"] for r in allrecs if r["dir"] == "TX" and r.get("cmd_id") == cid)
            s_done = next(
                (r["seq"] for r in allrecs if r["dir"] == "RX" and r["seq"] > s_cmd and b"#" in b64_payload(r)), None
            )
            assert s_done is not None, f"命令 {cid} 之後找不到 prompt RX"
            done.append(s_done)
        human_seqs = [r["seq"] for r in recs if r["source"] == human_src]
        assert len(human_seqs) == len(done), (human_seqs, done)
        for n, (h_seq, s_done) in enumerate(zip(human_seqs, done)):
            assert h_seq > s_done, f"第 {n} 個按鍵（seq {h_seq}）早於第 {n} 輪命令完成（prompt RX seq {s_done}）"
    finally:
        os.close(fd1)


def test_banner_during_suspend_drops_deferred(console_daemon) -> None:
    import time

    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    fd1 = open_vtty(a1["vtty"])
    try:
        # console_daemon 腳本：`bootcmd` 0.5 s 後回 U-Boot banner、之後不回 prompt
        sub = d.sw("cmd", "submit", "--selector", "COM0", "--cmd", "bootcmd", "--cmd-timeout", "3")
        assert sub["ok"] is True, sub
        cid = sub["cmd_id"]
        assert wait_for(
            lambda: any(r.get("dir") == "TX" and r.get("cmd_id") == cid for r in d.wal_records()), 3.0, 0.02
        ), "命令已開始送出"
        os.write(fd1, b"Z")  # banner 之前、agent 命令期間 → deferred
        d.wait_command_done(cid, timeout=30.0)
        time.sleep(3.0)
        assert b"Z" not in target.written(0.2), "boot banner 使 deferred 失效，不得回放"
        assert not any(r["dir"] == "TX" and b"Z" in b64_payload(r) for r in d.wal_records()), "WAL 無含 Z 的 TX"
    finally:
        os.close(fd1)


@pytest.mark.rust_only
def test_input_during_recovery_not_replayed(console_daemon) -> None:
    import time

    d, target = console_daemon
    a1 = d.sw("session", "console-attach", "--selector", "COM0", "--label", "h1")
    fd1 = open_vtty(a1["vtty"])
    try:
        import threading

        target.hold_reply(True)
        rec = threading.Thread(
            target=lambda: d.sw("session", "recover", "--selector", "COM0", "--force"), daemon=True
        )
        rec.start()
        assert wait_for(
            lambda: d._session("COM0")["state"] in ("RECOVERING", "ATTACHING"), 5.0, 0.02
        ), "進入恢復窗"
        os.write(fd1, b"R")
        time.sleep(0.3)
        target.hold_reply(False)
        rec.join(timeout=30.0)
        d.wait_state("COM0", "READY", timeout=30.0)
        time.sleep(3.0)
        assert tx_payload(d.wal_records(), f"human:{a1['client_id']}") == b"", "SC-31(a)：恢復窗內的 human 輸入不回放"
        # DUT 端去掉 ready probe 行（`echo __READY__<nonce>`）後不得出現 `R`
        dut = re.sub(rb"echo __READY__[0-9a-f]+", b"", target.written(0.2))
        assert b"R" not in dut, dut
    finally:
        target.hold_reply(False)
        os.close(fd1)
