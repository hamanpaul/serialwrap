"""P1 命令執行（line／background／interactive 三模式、並發序列化、UART 檔案傳輸）。"""
from __future__ import annotations

import concurrent.futures
import hashlib
import os
import random
import tempfile
import threading
import time

from ..harness import Case, CaseResult, register


def _case(id, title, hints=(), requires=(), destructive=False):
    def deco(fn):
        register(Case(id=id, tier="p1", title=title, run=fn,
                      destructive=destructive, requires=requires, hints=tuple(hints)))
        return fn
    return deco


def _submit_source_wait(ctx, com, cmd, source, *, cmd_timeout=12.0, retries=20):
    """帶 --source 的 submit→輪詢 status；佇列滿（SESSION_QUEUE_FULL）等短暫重試。

    註（#122）：arbiter 對同 session 的並發 submit 走 per-session PriorityQueue 序列化，
    一般不會退 busy；只在 pending 達硬上限退 SESSION_QUEUE_FULL——故此處重試涵蓋
    backpressure 而非 foreground busy。
    """
    for _ in range(retries):
        sub = ctx.sw.run("cmd", "submit", "--selector", com, "--cmd", cmd,
                         "--source", source, "--cmd-timeout", str(cmd_timeout))
        cmd_id = sub.get("cmd_id")
        if not cmd_id:
            if sub.get("error_code") in ("SESSION_QUEUE_FULL", "FOREGROUND_BUSY"):
                time.sleep(0.5)
                continue
            return {"_error": "submit 未回 cmd_id", **sub}
        deadline = time.monotonic() + cmd_timeout + 10
        time.sleep(1.0)
        while time.monotonic() < deadline:
            st = ctx.sw.run("cmd", "status", "--cmd-id", str(cmd_id))
            command = st.get("command") or {}
            if command.get("status") in ("done", "error", "timeout"):
                return command
            time.sleep(0.5)
        return {"_error": "cmd status 輪詢逾時", "cmd_id": cmd_id}
    return {"_error": "SESSION_QUEUE_FULL 重試耗盡"}


@_case("p1-cmd-modes", "三模式（line／background／interactive）＋錯誤碼",
       hints=("interactive-send 有效 encoding＝plain/base64/key（無 text）；文字用 plain、按鍵用 key",
              "background 輸出來源：執行至 prompt 即結束者由 command.stdout 承載，"
              "result-tail chunks 承載 prompt 後非同步輸出——marker 檢查須併看兩者（#122 實機）"),
       requires=("two_boards",))
def p1_cmd_modes(ctx):
    # (1) line
    line = ctx.sw.submit_and_wait("COM0", "echo MODE_LINE")
    ctx.note("line.json", str(line))
    if "MODE_LINE" not in (line.get("stdout") or ""):
        return CaseResult("FAIL", reason=f"line 模式 stdout 未含 marker（status={line.get('status')}）")

    # (2) background：輪詢 result-tail 累積 chunk 收齊 BG_1..3
    sub = ctx.sw.run("cmd", "submit", "--selector", "COM0", "--mode", "background",
                     "--cmd", "for i in 1 2 3; do echo BG_$i; sleep 1; done", "--cmd-timeout", "15")
    cmd_id = sub.get("cmd_id")
    if not cmd_id:
        return CaseResult("FAIL", reason=f"background submit 未回 cmd_id（{sub.get('error_code')}）")
    need = [f"BG_{i}" for i in (1, 2, 3)]
    # 部署 daemon（0.2.2）對「執行至 prompt 即結束」的 background 命令，會把輸出
    # 放進 command.stdout（result-tail chunks 只承載 prompt 之後的非同步輸出）；
    # 故 marker 需同時檢查 result-tail chunks 與 cmd status stdout 兩處來源，
    # 兼容 streaming 與 run-to-prompt 兩種 background 行為（#122 實機）。
    tail_text = ""
    status_stdout = ""
    from_chunk = 0
    deadline = time.monotonic() + 30
    ended = False
    while time.monotonic() < deadline:
        tail = ctx.sw.run("cmd", "result-tail", "--cmd-id", str(cmd_id), "--from-chunk", str(from_chunk))
        tail_text += "".join(tail.get("chunks") or [])
        from_chunk = tail.get("next_chunk", from_chunk)
        st = ctx.sw.run("cmd", "status", "--cmd-id", str(cmd_id)).get("command") or {}
        status_stdout = st.get("stdout") or status_stdout
        combined = tail_text + status_stdout
        if all(m in combined for m in need):
            break
        if tail.get("status") in ("done", "error", "timeout") or st.get("status") in ("done", "error", "timeout"):
            if ended:
                break
            ended = True  # 結束後再讀一輪抓尾段 chunk / 最終 stdout
        time.sleep(1)
    combined = tail_text + status_stdout
    ctx.note("background.txt", f"tail={tail_text!r}\nstatus_stdout={status_stdout!r}")
    missing = [m for m in need if m not in combined]
    if missing:
        return CaseResult("FAIL", reason=f"background 輸出缺 marker（chunks+stdout 皆無）：{missing}")

    # (3) interactive
    op = ctx.sw.run("session", "interactive-open", "--selector", "COM0",
                    "--owner", "agent:realhw", "--timeout", "20")
    iid = op.get("interactive_id") or ""
    if not iid:
        return CaseResult("FAIL", reason=f"interactive-open 未回 interactive_id（{op.get('error_code')}）")
    try:
        ctx.sw.run("session", "interactive-send", "--interactive-id", iid,
                   "--data", "echo IA_OK", "--encoding", "plain")
        ctx.sw.run("session", "interactive-send", "--interactive-id", iid,
                   "--data", "enter", "--encoding", "key")
        time.sleep(2)
        stt = ctx.sw.run("session", "interactive-status", "--interactive-id", iid)
        ctx.note("interactive-status.json", str(stt))
        if "IA_OK" not in (stt.get("screen") or ""):
            return CaseResult("FAIL", reason="interactive 畫面未見 IA_OK")
    finally:
        ctx.sw.run("session", "interactive-close", "--interactive-id", iid)

    # (4) 錯誤面：不存在 selector → SESSION_NOT_FOUND
    err = ctx.sw.run("cmd", "submit", "--selector", "COM9", "--cmd", "echo x", "--cmd-timeout", "5")
    ctx.note("error.json", str(err))
    if err.get("error_code") != "SESSION_NOT_FOUND":
        return CaseResult("FAIL", reason=f"不存在 selector 未回 SESSION_NOT_FOUND（{err.get('error_code')}）")
    return CaseResult("PASS")


@_case("p1-cmd-serial", "多來源並發序列化（單 UART 單寫入者、無 cross-talk）",
       hints=("arbiter per-session PriorityQueue 序列化並發 submit；SESSION_QUEUE_FULL 退避重試",
              "cross-talk＝某 source stdout 混入他 source marker"),
       requires=("two_boards",))
def p1_cmd_serial(ctx):
    n_threads, rounds = 5, 3
    all_markers = {f"A{n}_R{r}_MARK" for n in range(n_threads) for r in range(rounds)}

    def worker(n: int) -> dict:
        source = f"agent:rhw{n}"
        outs, submits = [], 0
        for r in range(rounds):
            marker = f"A{n}_R{r}_MARK"
            cmd = _submit_source_wait(ctx, "COM0", f"echo {marker}", source)
            submits += 1
            outs.append((marker, cmd.get("status"), cmd.get("stdout") or ""))
        return {"source": source, "outs": outs, "submits": submits}

    start_seq = ctx.sw.run("wal", "current-seq").get("seq") or 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as ex:
        agg = list(ex.map(worker, range(n_threads)))
    ctx.note("agg.json", str(agg))

    submits_by_source = {}
    for w in agg:
        submits_by_source[w["source"]] = w["submits"]
        for marker, status, stdout in w["outs"]:
            if status != "done" or marker not in stdout:
                return CaseResult("FAIL", reason=f"{w['source']} {marker} 未完成（status={status}）")
            foreign = [m for m in all_markers if m != marker and m in stdout]
            if foreign:
                return CaseResult("FAIL", reason=f"{w['source']} stdout cross-talk 混入 {foreign}")

    # WAL TX 計數 == 各 source 提交數（本輪 start_seq 起算，避開歷史）
    exp = ctx.sw.run("wal", "export", "--from-seq", str(start_seq))
    tx_by_source: dict[str, int] = {}
    for rec in exp.get("records") or []:
        if rec.get("dir") == "TX":
            src = rec.get("source") or ""
            if src.startswith("agent:rhw"):
                tx_by_source[src] = tx_by_source.get(src, 0) + 1
    ctx.note("tx-by-source.json", str(tx_by_source))
    for source, want in submits_by_source.items():
        if tx_by_source.get(source, 0) != want:
            return CaseResult("FAIL",
                              reason=f"{source} WAL TX 計數 {tx_by_source.get(source, 0)} != 提交數 {want}")
    return CaseResult("PASS")


@_case("p1-cmd-file", "UART 檔案 push/pull round-trip＋RPC 不凍結",
       hints=("無 health 子命令：以 daemon status 當輕量 RPC 探針量延遲（#52 歷史病灶 19.8s）",
              "file.push/pull 走 target 端 `printf|base64 -d`；busybox DUT 常缺 base64，缺則協定不可用",
              "file.* 未在 CLI _LONG_RPC_METHODS（#123 defer）→ 必須顯式全域 --timeout，否則 5s 假逾時",
              "md5 不符先查 UART 傳輸掉字／chunk-size"),
       requires=("two_boards",))
def p1_cmd_file(ctx):
    ctx.case_dir.mkdir(parents=True, exist_ok=True)
    # 前置探測：file push/pull 協定依賴 target 端 `base64 -d` 還原分段。busybox
    # DUT（prplOS/BDK）常無 base64 → 協定寫出空檔（md5 = 空檔），屬環境/部署限制
    # 而非 serialwrap 缺陷，此類板卡無法驗此案 → SKIP 並註明（#122 實機）。
    probe = ctx.sw.submit_and_wait("COM0", "printf %s QjY0X09L | base64 -d")  # QjY0X09L=b64("B64_OK")
    ctx.note("base64-probe.json", str(probe))
    if "B64_OK" not in (probe.get("stdout") or ""):
        return CaseResult("SKIP",
                          reason="target 缺 base64（busybox DUT），file push/pull UART 協定不可用"
                                 "——known deployed/env limitation（#122）")
    fd, local = tempfile.mkstemp(prefix="rhw-", suffix=".bin")
    os.close(fd)
    data = random.randbytes(256 * 1024)
    with open(local, "wb") as fh:
        fh.write(data)
    src_md5 = hashlib.md5(data).hexdigest()
    remote = "/tmp/rhw.bin"
    pulled = os.path.join(str(ctx.case_dir), "rhw-pulled.bin")

    stop = threading.Event()
    max_lat = {"v": 0.0}

    def probe():
        while not stop.is_set():
            t0 = time.monotonic()
            ctx.sw.run("daemon", "status")  # 輕量 RPC；無獨立 health ping 子命令
            max_lat["v"] = max(max_lat["v"], time.monotonic() - t0)
            time.sleep(0.5)

    th = threading.Thread(target=probe, daemon=True)
    th.start()
    try:
        # 全域 --timeout 120 必須在子命令之前（file.* 不在 CLI LONG_RPC floor，#123 defer）；
        # subprocess timeout 對齊放大到 180s，避免 client 端 5s 假逾時。
        push = ctx.sw.run("--timeout", "120", "file", "push", "--selector", "COM0",
                          "--local", local, "--remote", remote, timeout=180)
        ctx.note("push.json", str(push))
    finally:
        stop.set()
        th.join(timeout=5)
    if not push.get("ok"):
        return CaseResult("FAIL", reason=f"file push 失敗（{push.get('error_code')}）")

    pull = ctx.sw.run("--timeout", "120", "file", "pull", "--selector", "COM0",
                      "--remote", remote, "--local", pulled, timeout=180)
    ctx.note("pull.json", str(pull))
    ctx.sw.submit_and_wait("COM0", f"rm -f {remote}")  # 清理板上暫存
    try:
        dst_md5 = hashlib.md5(open(pulled, "rb").read()).hexdigest()
    except OSError as exc:
        return CaseResult("FAIL", reason=f"pull 檔案讀取失敗：{exc!r}")
    finally:
        try:
            os.unlink(local)
        except OSError:
            pass
    if dst_md5 != src_md5:
        return CaseResult("FAIL", reason=f"round-trip md5 不符（src={src_md5} dst={dst_md5}）")
    if max_lat["v"] >= 3.0:
        return CaseResult("FAIL", reason=f"push 期間 RPC 探針最大延遲 {max_lat['v']:.1f}s（≥3s，疑 event loop 凍結）")
    return CaseResult("PASS")
