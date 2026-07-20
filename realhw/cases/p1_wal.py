"""P1 WAL 契約（reset 保留 console、fullrun 逐案 seq 遞增與存活）。"""
from __future__ import annotations

import json
import os
import time

from ..drivers import find_marker
from ..harness import Case, CaseResult, register


def _case(id, title, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, tier="p1", title=title, run=fn,
                      destructive=destructive, requires=requires, hints=tuple(hints)))
        return fn
    return deco


def _wal_tail_seq(ctx):
    """讀 live WAL ndjson 檔尾一筆的 seq（wal_path 由 daemon status 取，不硬編 home）。"""
    path = (ctx.sw.run("daemon", "status") or {}).get("wal_path")
    if not path or not os.path.exists(path):
        return None
    last = b""
    with open(path, "rb") as fh:
        for line in fh:
            if line.strip():
                last = line.strip()
    if not last:
        return None
    try:
        return json.loads(last).get("seq")
    except (json.JSONDecodeError, ValueError):
        return None


@_case("p1-wal-reset", "wal reset 契約＋console 不斷線（T1/T2/T3）",
       hints=("console 掛 console-attach 取 vtty、tmux cat 讀 RX（比照 coexist 輕量法）",
              "reset 後 current-seq 應歸零並重新累積，且與 live WAL 檔尾 seq 相等"),
       requires=("tmux", "two_boards"))
def p1_wal_reset(ctx):
    att = ctx.sw.run("session", "console-attach", "--selector", "COM0", "--label", "realhw")
    ctx.note("console-attach.json", str(att))
    vtty, cid = att.get("vtty"), att.get("client_id")
    if not vtty or not cid:
        return CaseResult("FAIL", reason=f"console-attach 未回 vtty/client_id（{att.get('error_code')}）",
                          category="test", reason_code="console_attach_failed")
    ses = ctx.tmux.name("walreset")
    ctx.tmux.new(ses, f"cat {vtty}")
    try:
        time.sleep(1)
        if (ctx.sw.run("wal", "current-seq").get("seq") or 0) == 0:
            ctx.sw.submit_and_wait("COM0", "echo SEED")  # 確保 reset 前 seq>0
        ctx.sw.run("wal", "reset")
        after = ctx.sw.run("wal", "current-seq").get("seq")
        if after != 0:
            return CaseResult("FAIL", reason=f"reset 後 current-seq 非 0（={after}）",
                              category="test", reason_code="wal_reset_seq_nonzero")
        ctx.sw.submit_and_wait("COM0", "echo T1_ALIVE")
        time.sleep(1)
        cl = ctx.sw.run("session", "console-list", "--selector", "COM0")
        ctx.note("console-list.json", str(cl))
        if not any(c.get("client_id") == cid for c in cl.get("consoles") or []):
            return CaseResult("FAIL", reason="reset 後原 console client 掉線",
                              category="test", reason_code="console_dropped")
        pane = ctx.tmux.capture(ses)
        ctx.note("pane.txt", pane)
        if not find_marker(pane, "T1_ALIVE"):
            return CaseResult("FAIL", reason="console 未見 reset 後命令 marker",
                              category="test", reason_code="console_fanout_lost")
        seq_now = ctx.sw.run("wal", "current-seq").get("seq") or 0
        if seq_now <= 0:
            return CaseResult("FAIL", reason="reset 後未重新累積 seq",
                              category="test", reason_code="wal_seq_not_accumulating")
        tail = _wal_tail_seq(ctx)
        if tail is not None and seq_now != tail:
            return CaseResult("FAIL", reason=f"current-seq {seq_now} 與 WAL 檔尾 seq {tail} 不符",
                              category="test", reason_code="wal_integrity")
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses)
        ctx.sw.run("session", "console-detach", "--selector", "COM0", "--client-id", cid)
        time.sleep(1)


@_case("p1-wal-fullrun", "逐案 seq 嚴格遞增＋console 存活（T8）",
       hints=("歷史 flaky（t8 假 PTY ~50%）在實機版應穩；async line race——等待要足",),
       requires=("tmux", "two_boards"))
def p1_wal_fullrun(ctx):
    att = ctx.sw.run("session", "console-attach", "--selector", "COM0", "--label", "realhw")
    ctx.note("console-attach.json", str(att))
    vtty, cid = att.get("vtty"), att.get("client_id")
    if not vtty or not cid:
        return CaseResult("FAIL", reason=f"console-attach 未回 vtty/client_id（{att.get('error_code')}）",
                          category="test", reason_code="console_attach_failed")
    ses = ctx.tmux.name("walfull")
    ctx.tmux.new(ses, f"cat {vtty}")
    try:
        time.sleep(1)
        ctx.sw.run("wal", "reset")
        markers = []
        prev = ctx.sw.run("wal", "current-seq").get("seq") or 0
        for i in range(3):
            marker = f"CASE_{i}_RESULT"
            markers.append(marker)
            ctx.sw.submit_and_wait("COM0", f"echo {marker}")
            time.sleep(1)
            seq = ctx.sw.run("wal", "current-seq").get("seq") or 0
            if seq <= prev:
                return CaseResult("FAIL", reason=f"round {i} seq 未嚴格遞增（{prev}->{seq}）",
                                  category="test", reason_code="wal_seq_not_increasing")
            prev = seq
        exp = ctx.sw.run("wal", "export", "--from-seq", "0")
        if not (exp.get("records") or []):
            return CaseResult("FAIL", reason="wal export 無記錄",
                              category="test", reason_code="wal_export_empty")
        cl = ctx.sw.run("session", "console-list", "--selector", "COM0")
        ctx.note("console-list.json", str(cl))
        if not any(c.get("client_id") == cid for c in cl.get("consoles") or []):
            return CaseResult("FAIL", reason="fullrun 期間 console client 掉線",
                              category="test", reason_code="console_dropped")
        pane = ctx.tmux.capture(ses)
        ctx.note("pane.txt", pane)
        missing = [m for m in markers if not find_marker(pane, m)]
        if missing:
            return CaseResult("FAIL", reason=f"console 缺 marker：{missing}",
                              category="test", reason_code="console_fanout_lost")
        return CaseResult("PASS")
    finally:
        ctx.tmux.kill(ses)
        ctx.sw.run("session", "console-detach", "--selector", "COM0", "--client-id", cid)
        time.sleep(1)
