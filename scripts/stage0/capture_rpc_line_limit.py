#!/usr/bin/env python3
"""擷取 RPC 單行請求長度上限的 Python 基線行為（RPC-H-02）。

以真實的 ``JsonRpcUnixServer``（POSIX，暫存目錄 socket）與 ``TcpRpcServer``
（``127.0.0.1:0``）啟動，handler 固定回 ``{"ok": true}``；對每個 server 送出
內容長度 ``n ∈ {65535, 65536, 65537}`` 的請求行，結尾分別為 ``\\n``、``\\r\\n`` 與
分段送入（前半、drain、等 50 ms、後半＋``\\n``），記錄是否收到回應行。

輸出 ``contracts/golden/rpc/line_limit.json``（DISK_STABLE 序列化）。兩個 server
結果不一致時 exit 1，不得在契約中留下平台分歧。

用法：``python3 scripts/stage0/capture_rpc_line_limit.py [--out PATH]``
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SIZES: tuple[int, ...] = (65535, 65536, 65537)
ENDINGS: tuple[str, ...] = ("lf", "crlf", "segmented")
CAPTURED_BY = "scripts/stage0/capture_rpc_line_limit.py"
SOURCE = "sw_core/rpc_posix.py serve_connection（asyncio StreamReader 預設 limit）"
DEFAULT_OUT = ROOT / "contracts" / "golden" / "rpc" / "line_limit.json"
# 真實 framing 會回填請求的 id（請求無 id 時為 null）；此處僅核對 ok 與 id。
EXPECTED_RESPONSE: dict[str, Any] = {"id": None, "ok": True}
READ_TIMEOUT_S = 5.0


class CaptureError(RuntimeError):
    """擷取失敗（非超限關閉）：截斷回應、非預期內容、逾時或傳輸例外。"""


def _payload(content_len: int) -> bytes:
    """產生恰為 ``content_len`` bytes（不含結尾換行）的合法 JSON 請求物件。"""
    head = b'{"method":"health.ping","pad":"'
    tail = b'"}'
    fill = content_len - len(head) - len(tail)
    if fill < 0:
        raise ValueError(f"content_len too small: {content_len}")
    return head + b"x" * fill + tail


async def _probe(open_conn: Any, content_len: int, ending: str) -> bool:
    """送出一行請求。

    回傳 True 代表收到完整 LF 結尾且內容為 ``{"ok": true}`` 的回應；回傳 False 代表
    連線在**零 bytes**回應後 EOF（超限關閉）。其餘情況（截斷回應、非預期內容、逾時、
    傳輸例外）一律拋 ``CaptureError``，使擷取非零退出，不得被固化成契約邊界。
    """
    reader, writer = await open_conn()
    try:
        body = _payload(content_len)
        if ending == "lf":
            writer.write(body + b"\n")
            await writer.drain()
        elif ending == "crlf":
            writer.write(body + b"\r\n")
            await writer.drain()
        elif ending == "segmented":
            half = len(body) // 2
            writer.write(body[:half])
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.write(body[half:] + b"\n")
            await writer.drain()
        else:  # pragma: no cover - 防禦性分支
            raise ValueError(ending)
        writer.write_eof()
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise CaptureError(f"讀取回應逾時：size={content_len} ending={ending}") from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
    if line == b"":
        return False
    if not line.endswith(b"\n"):
        raise CaptureError(f"回應未以 LF 結尾（截斷）：size={content_len} ending={ending} bytes={len(line)}")
    try:
        decoded = json.loads(line)
    except ValueError as exc:
        raise CaptureError(f"回應非合法 JSON：size={content_len} ending={ending}") from exc
    if decoded != EXPECTED_RESPONSE:
        raise CaptureError(f"回應內容非預期 {EXPECTED_RESPONSE}：size={content_len} ending={ending} got={decoded!r}")
    return True


async def _measure(open_conn: Any) -> dict[str, dict[str, bool]]:
    result: dict[str, dict[str, bool]] = {}
    for size in SIZES:
        result[str(size)] = {}
        for ending in ENDINGS:
            result[str(size)][ending] = await _probe(open_conn, size, ending)
    return result


def _derive(per_server: dict[str, dict[str, bool]], ending: str) -> int:
    """取連續通過的最大內容長度；若取樣不連續或非單調，拋 ValueError。"""
    max_ok = None
    for size in SIZES:
        ok = per_server[str(size)][ending]
        if ok:
            if max_ok is not None and size != max_ok + 1:
                raise ValueError(f"non-contiguous samples for {ending}")
            max_ok = size
        else:
            break
    if max_ok is None:
        raise ValueError(f"no passing size for {ending}")
    return max_ok


async def _capture_unix(handler: Any) -> dict[str, dict[str, bool]]:
    from sw_core.rpc_posix import JsonRpcUnixServer

    with tempfile.TemporaryDirectory(prefix="sw-line-limit-") as tmp:
        sock = os.path.join(tmp, "rpc.sock")
        server = JsonRpcUnixServer(sock, handler)
        await server.start()
        try:
            async def _open() -> tuple[Any, Any]:
                return await asyncio.open_unix_connection(sock)

            return await _measure(_open)
        finally:
            await server.stop()


async def _capture_tcp(handler: Any) -> dict[str, dict[str, bool]]:
    from sw_core.rpc_win import TcpRpcServer

    server = TcpRpcServer("tcp://127.0.0.1:0", handler)
    await server.start()
    try:
        # 私有屬性僅供擷取腳本讀取實際綁定埠（port 0 由 OS 指派），不影響產品行為。
        port = server._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

        async def _open() -> tuple[Any, Any]:
            return await asyncio.open_connection("127.0.0.1", port)

        return await _measure(_open)
    finally:
        await server.stop()


def _handler(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True}


def capture() -> dict[str, Any]:
    # 超長行時 asyncio 會把 server 端 LimitOverrunError 印成 "Unhandled exception in
    # client_connected_cb" traceback；這是預期基線行為，僅於擷取期間靜音，不改產品碼。
    asyncio_log = logging.getLogger("asyncio")
    prev_level = asyncio_log.level
    asyncio_log.setLevel(logging.CRITICAL)
    try:
        unix_res = asyncio.run(_capture_unix(_handler))
        tcp_res = asyncio.run(_capture_tcp(_handler))
    finally:
        asyncio_log.setLevel(prev_level)
    if unix_res != tcp_res:
        raise SystemExit(
            "posix_unix 與 tcp 的行長上限擷取結果不一致，停止產出契約：\n"
            f"posix_unix={json.dumps(unix_res, sort_keys=True)}\n"
            f"tcp={json.dumps(tcp_res, sort_keys=True)}"
        )
    max_ok = _derive(unix_res, "lf")
    first_close = next(s for s in SIZES if s > max_ok) if max_ok < SIZES[-1] else None
    if first_close is None or not all(not unix_res[str(s)]["lf"] for s in SIZES if s > max_ok):
        raise SystemExit("取樣未涵蓋第一個被拒絕的長度，無法判定上限")
    if first_close != max_ok + 1:
        raise SystemExit(f"第一個被拒絕長度 {first_close} 不等於上限+1（{max_ok + 1}）")
    crlf_max = _derive(unix_res, "crlf")
    segmented_same = all(unix_res[str(s)]["segmented"] == unix_res[str(s)]["lf"] for s in SIZES)
    return {
        "captured_by": CAPTURED_BY,
        "source": SOURCE,
        "unit": "bytes before LF",
        "max_ok_bytes": max_ok,
        "first_close_bytes": first_close,
        "crlf_max_ok_bytes": crlf_max,
        "segmented_same_as_single": segmented_same,
        "servers": {"posix_unix": unix_res, "tcp": tcp_res},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="輸出路徑（預設為契約 golden）")
    args = parser.parse_args(argv)
    try:
        doc = capture()
    except CaptureError as exc:
        print(f"擷取失敗（非零退出，不寫出 golden）：{exc}", file=sys.stderr)
        return 1
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
