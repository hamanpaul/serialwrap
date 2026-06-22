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

# 這些 RPC method 的 handler 會長時間阻塞（UART 上的 base64 分段傳輸），須丟到 executor
# 執行，否則會卡住單執行緒 asyncio event loop，導致傳輸期間全 daemon 的所有 RPC 凍結（#52）。
BLOCKING_RPC_METHODS = {"file.push", "file.pull"}


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

    service = SerialwrapService(
        result.profiles,
        templates=result.templates,
        max_sessions=result.max_sessions,
    )
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
