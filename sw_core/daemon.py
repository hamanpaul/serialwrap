from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from sw_core.config import ProfileTemplate, load_profiles
from sw_core.constants import CONFIG_DIR, DEFAULT_ENDPOINT, LOCK_PATH, PROFILE_DIR, ensure_runtime_dirs
from sw_core.runtime_config import RuntimeConfig
from sw_core.service import SerialwrapService
from sw_core.session_manager import StateLoadError
from sw_core.sysenv import force_utf8_stdio

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
    # daemon status：health() 內做同步 /proc 掃描（detect_multi_open），多裝置/大量 pid
    # 下會在 asyncio event loop 上同步耗時，凍結全 daemon RPC（含 health.ping 探針）（#101 I2/6b）。
    "health.status",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="serialwrapd", description="serialwrap daemon")
    parser.add_argument("--profile-dir", default=PROFILE_DIR)
    # 預設值改為 DEFAULT_ENDPOINT（POSIX 上等同 SOCKET_PATH，Windows 上為 TCP URL）（#84 PORT-4）。
    parser.add_argument("--socket", default=DEFAULT_ENDPOINT)
    parser.add_argument("--lock", default=LOCK_PATH)
    return parser


def _build_lock(args: argparse.Namespace) -> object:
    """依平台後端選擇器建構 singleton lock（延遲 import，避免非該平台模組被提前載入）。

    Windows（win）→ WindowsSingletonLock；POSIX（posix）→ SingletonLock。
    """
    from sw_core.platform_backends import select_lock_backend  # noqa: PLC0415
    if select_lock_backend() == "win":
        from sw_core.lock_win import WindowsSingletonLock  # noqa: PLC0415
        return WindowsSingletonLock(args.lock, args.socket)
    from sw_core.lock_posix import SingletonLock  # noqa: PLC0415
    return SingletonLock(args.lock, args.socket)


def _build_server(
    args: argparse.Namespace,
    handler: object,
) -> object:
    """依平台後端選擇器建構 RPC server（延遲 import，避免非該平台模組被提前載入）。

    Windows（win）→ TcpRpcServer；POSIX（posix）→ JsonRpcUnixServer。
    """
    from sw_core.platform_backends import select_rpc_backend  # noqa: PLC0415
    if select_rpc_backend() == "win":
        from sw_core.rpc_win import TcpRpcServer  # noqa: PLC0415
        return TcpRpcServer(args.socket, handler, blocking_methods=BLOCKING_RPC_METHODS)
    from sw_core.rpc_posix import JsonRpcUnixServer  # noqa: PLC0415
    return JsonRpcUnixServer(args.socket, handler, blocking_methods=BLOCKING_RPC_METHODS)


def _write_config_endpoint(endpoint: str) -> None:
    """daemon 啟動成功後，將有效 endpoint 寫入 config.yaml，供 CLI _resolve_endpoint 讀取。

    僅更新 socket_path，不改動 supervision_mode（避免覆蓋 setup 寫入的監管模式）。
    寫入失敗時僅記 stderr，不中斷 daemon 運行。
    """
    import os  # noqa: PLC0415
    try:
        cfg = RuntimeConfig(os.path.join(CONFIG_DIR, "config.yaml"))
        cfg.set_socket(endpoint)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"serialwrapd: 寫入 config.yaml endpoint 失敗（非致命）: {exc}\n")


def _write_effective_endpoint(args: argparse.Namespace) -> None:
    """server 啟動成功後，把有效 bind endpoint 寫入 config.yaml（全平台，#173）。

    先前僅 Windows（``select_rpc_backend() == "win"``）呼叫 :func:`_write_config_endpoint`，
    理由是「POSIX on-demand 模式下 CLI 一定算得出同一個 SOCKET_PATH 預設值，不需要
    config.yaml 記錄」。這個假設在部署 wrapper 以 ``SERIALWRAP_STATE_DIR`` 搬移 socket
    位置時不成立：任何不經過該 wrapper 的 client（如其他工具內嵌的 venv）永遠連不到
    daemon，且沒有任何診斷機制能指出這件事（#173 根因）。改為全平台無條件寫入，讓
    「daemon 實際綁在哪」有一個與呼叫端 env 無關的單一事實來源。
    """
    _write_config_endpoint(args.socket)


def _make_windows_passthrough_templates(
    templates: list[ProfileTemplate],
) -> list[ProfileTemplate]:
    """Windows daemon 路徑：若 templates 中無 passthrough template，注入預設
    windows-default-passthrough，使空閒 COM 可被 SessionManager 動態接管。

    根因（#84 PORT-4）：無任何 template 時，_attach_by_id 守衛
    ``if session is None and self._templates:`` 為 False，動態接管永遠跳過。
    注入後 _templates 非空，守衛通過，_attach_by_id_dynamic 走
    _default_passthrough_template fallback 建立 passthrough session。

    POSIX 行為：呼叫方（_run_async）僅在 select_device_backend()=="win" 時呼叫，
    POSIX 路徑完全不受影響。
    """
    if any(t.platform == "passthrough" for t in templates):
        return list(templates)
    return list(templates) + [
        ProfileTemplate(
            profile_name="windows-default-passthrough",
            platform="passthrough",
            ready_probe="",  # 空 → command_capable=False → _default_passthrough_template 優先選為 generic fallback
        )
    ]


async def _run_async(args: argparse.Namespace) -> int:
    ensure_runtime_dirs()
    result = load_profiles(args.profile_dir)
    if not result.profiles and not result.templates:
        sys.stderr.write("serialwrapd: no profiles loaded\n")

    # Windows daemon 路徑（#84 PORT-4）：確保 templates 中至少有一個 passthrough template，
    # 使空閒 COM 能被 SessionManager._attach_by_id 動態接管。
    # POSIX 路徑（select_device_backend()!="win"）不注入，維持既有「無 profiles → 不自動接管」行為。
    from sw_core.platform_backends import select_device_backend  # noqa: PLC0415
    templates = (
        _make_windows_passthrough_templates(result.templates)
        if select_device_backend() == "win"
        else result.templates
    )

    # 依平台後端選擇器建構 lock（Windows: WindowsSingletonLock / POSIX: SingletonLock）。
    lock = _build_lock(args)
    try:
        lock.acquire()
    except RuntimeError as exc:
        sys.stderr.write(f"serialwrapd: {exc}\n")
        return 2

    try:
        service = SerialwrapService(
            result.profiles,
            templates=templates,
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

    # 依平台後端選擇器建構 RPC server（Windows: TcpRpcServer / POSIX: JsonRpcUnixServer）。
    server = _build_server(args, _handle)

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
        # server 啟動後寫入有效 endpoint，CLI _resolve_endpoint 靠此發現 daemon
        # （#84 PORT-4 起 Windows 皆如此；#173 起 POSIX 也無條件寫入，理由見
        # _write_effective_endpoint docstring）。
        _write_effective_endpoint(args)
        await stop_event.wait()
    finally:
        await server.stop()
        service.stop()
        lock.release()

    return 0


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()  # Windows console cp1252 印繁中 help 會崩（#118），須在 parse_args 前
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(asyncio.run(_run_async(args)))


if __name__ == "__main__":
    sys.exit(main())
