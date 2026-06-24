from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from sw_core.config import load_profiles
from sw_core.constants import LOCK_PATH, PROFILE_DIR, SOCKET_PATH, ensure_runtime_dirs
from sw_core.daemon_lock import SingletonLock
from sw_core.rpc import JsonRpcUnixServer
from sw_core.service import SerialwrapService
from sw_core.session_manager import StateLoadError

# 這些 RPC method 的 handler 會長時間同步阻塞，須丟到 executor 執行，否則會卡住單執行緒
# asyncio event loop，導致期間全 daemon 的所有 RPC 凍結（#52 根因；#80 補齊其餘阻塞 handler）。
# 安全性：這些 handler 都經 service.rpc → SessionManager/WalWriter 的鎖保護方法，已被 arbiter
# worker／device watcher 等多執行緒並發呼叫，故於 executor 執行緒跑亦為執行緒安全；RPC 為
# 每連線請求/回應模型，offload 不影響回應對應（各 CLI 呼叫各開一條連線）。
BLOCKING_RPC_METHODS = {
    # UART 上同步等待（base64 分段傳輸 / probe / login / sleep）
    "file.push", "file.pull",
    "session.recover",        # force 路徑硬 sleep 10s + probe 數十秒（#80 REACT-1）
    "session.attach",         # reprobe 分支做完整 ensure_ready/probe，最壞數十秒（#80 REACT-2）
    "session.self_test",      # 同步等 UART（2×timeout_s）（#80 REACT-3）
    "session.console_attach",  # recover 升級分支可同步觸發 recover（#80 REACT-6）
    # 整檔讀取（WAL/log tail），大檔在 loop 上同步讀會凍結全 daemon（#80 REACT-5）
    # command.result_tail 是 CLI `serialwrap cmd result-tail` 實際送的方法（service.rpc 內與 legacy
    # result.tail 同樣 get_background_result 讀取/切片大 capture 並序列化）；漏了它，最常用的查詢路徑
    # 仍跑在 asyncio loop 上，大 capture 慢查詢照樣凍結其他 RPC（含 health.ping）（#80 Codex 必修）。
    "command.result_tail", "result.tail", "log.tail_raw", "log.tail_text", "wal.range",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="serialwrapd", description="serialwrap daemon")
    parser.add_argument("--profile-dir", default=PROFILE_DIR)
    parser.add_argument("--socket", default=SOCKET_PATH)
    parser.add_argument("--lock", default=LOCK_PATH)
    return parser


async def _run_async(args: argparse.Namespace) -> int:
    ensure_runtime_dirs()
    result = load_profiles(args.profile_dir)
    if not result.profiles and not result.templates:
        sys.stderr.write("serialwrapd: no profiles loaded\n")

    lock = SingletonLock(args.lock, args.socket)
    try:
        lock.acquire()
    except RuntimeError as exc:
        sys.stderr.write(f"serialwrapd: {exc}\n")
        return 2

    try:
        service = SerialwrapService(
            result.profiles,
            templates=result.templates,
            max_sessions=result.max_sessions,
        )
    except StateLoadError as exc:
        # _load_state fail closed（既有 state.json 讀取失敗，非 JSON 損毀）：拒絕啟動，避免以空狀態
        # 覆寫 RELEASED 交接致重啟 two-reader（#82 / Codex 必修）。比照 lock 失敗：釋放 lock、非零退出。
        sys.stderr.write(f"serialwrapd: {exc}\n")
        lock.release()
        return 3
    stop_event = asyncio.Event()

    def _handle(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "daemon.stop":
            stop_event.set()
            return {"ok": True, "stopping": True}
        return service.rpc(method, params)

    server = JsonRpcUnixServer(args.socket, _handle, blocking_methods=BLOCKING_RPC_METHODS)

    def _stop(*_unused: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    try:
        service.start()
        await server.start()
        await stop_event.wait()
    finally:
        await server.stop()
        service.stop()
        lock.release()

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(asyncio.run(_run_async(args)))
