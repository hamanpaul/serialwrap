from __future__ import annotations

import json
import socket
import time
from typing import Any
from urllib.parse import urlsplit

# --- #123：CLI 假性 TIMEOUT 韌性 ---------------------------------------------
# 唯讀或冪等清理 RPC 方法白名單（MINOR-4 措辭修正）：這些方法在 daemon 端不
# 改變使用者可見狀態、重送無副作用（純查詢，或查詢附帶的清理動作本身冪等），
# 才允許 ``retries`` 於 TIMEOUT／連線失敗時做指數退避重試。寫入類／長操作方法
# （session.recover、session.attach、command.submit…）一律不重試——CLI 看到
# TIMEOUT 時 daemon 端很可能仍在執行，重送會造成重複動作或 two-writer 競態。
# 對照 sw_core/service.py 的 rpc() 分派：以下方法皆為純查詢（list／get／status）；
# 其中 session.console_list 額外會 prune 已死的 console client
# （sw_core/uart_io.py 的 UARTBridge.list_consoles → _prune_stale_consoles_locked
# 副作用），該清理本身冪等——重送只會把「已經死掉、本該被清掉」的項目再清一次，
# 不影響任何仍存活的 console，故仍安全納入 retry 白名單。
RETRYABLE_READONLY_METHODS = frozenset({
    "health.ping",
    "health.status",
    "session.list",
    "session.get_state",
    "session.activity",
    "session.console_list",  # 冪等 prune 副作用，見上方註解
    "session.log_status",
    "device.list",
    "alias.list",
    "mcu.patterns",
    "mcu.status",
})

# 指數退避起始秒數（0.5s 起、每次 ×2）；獨立成模組常數供測試縮短。
_RETRY_BACKOFF_BASE_S = 0.5
# 單次退避 delay 上限（NIT-7）：×2 成長不設限的話，重試次數稍多就會把單次
# 等待拉到分鐘級，失去「快速重試」的意義；夾在 5s，讓呼叫端仍可預估總耗時
# 上界（見 rpc_call docstring 的總耗時公式）。
_RETRY_BACKOFF_MAX_S = 5.0

# 視為暫時性、可重試錯誤的 error_code 集合（NIT-6）：TIMEOUT／SOCKET_ERROR
# 為既有兩類；EMPTY_RESPONSE（對端關閉連線但未送任何位元組，例如 daemon 重啟
# 撞上請求中）與 SOCKET_ERROR 性質一致——皆非「daemon 已明確處理並回覆失敗」，
# 對唯讀白名單方法重送同樣安全，故一併納入。
_RETRYABLE_TRANSIENT_ERROR_CODES = frozenset({"TIMEOUT", "SOCKET_ERROR", "EMPTY_RESPONSE"})

# TIMEOUT 後輕量健康探測的單次 timeout：ping + status 兩段合計上限 2s，
# 避免探測本身把已逾時的 CLI 呼叫拖得更久。
_PROBE_TIMEOUT_S = 1.0

# 探測整段的 wall-clock 硬上限（MINOR-3）：以 time.monotonic() 起算，超過即
# 放棄剩餘探測階段（省略對應 enrich 欄位）。個別 timeout 相加的 2s 只是「兩次
# 呼叫各自不逾時」的理論上界，實際受連線建立／OS 排程等疊加影響不保證成立；
# 此處另設 2.5s 明確 deadline 兜底，避免探測本身失控地拖時間。
_PROBE_DEADLINE_S = 2.5

# 探測方法自身逾時不再遞迴探測（也避免 doctor 等 0.5s ping 逾時被探測拖慢）。
_PROBE_METHODS = ("health.ping", "health.status")


def _parse_endpoint(endpoint: str) -> tuple[str, tuple[str, int] | str]:
    """解析 endpoint 字串，回傳 (transport, address)。

    支援格式：
    - ``tcp://host:port``        → ("tcp", ("host", port))
    - ``unix:///path/to/sock``   → ("unix", "/path/to/sock")
    - ``/path/to/sock``          → ("unix", "/path/to/sock")

    解析失敗時 raise ``ValueError``。
    """
    if "://" in endpoint:
        parsed = urlsplit(endpoint)
        if parsed.scheme == "tcp":
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError(f"invalid tcp endpoint: {endpoint!r}") from exc
            host = parsed.hostname
            if parsed.path or parsed.query or parsed.fragment or not host or port is None:
                raise ValueError(f"invalid tcp endpoint: {endpoint!r}")
            return "tcp", (host, port)

        if parsed.scheme == "unix":
            path = parsed.path
            if parsed.netloc:
                raise ValueError(
                    f"invalid unix endpoint: {endpoint!r} "
                    "(unix endpoint must use an absolute path such as 'unix:///path/to/sock')"
                )
            if parsed.query or parsed.fragment or not path or not path.startswith("/"):
                raise ValueError(
                    f"invalid unix endpoint: {endpoint!r} "
                    "(unix endpoint path must be absolute)"
                )
            return "unix", path

        raise ValueError(f"unsupported endpoint scheme: {parsed.scheme!r}")
    # plain path (backward compat)
    return "unix", endpoint


def _af_unix_available() -> bool:
    """本平台是否支援 ``AF_UNIX``（native Windows 的 CPython 不提供，#131）。"""
    return hasattr(socket, "AF_UNIX")


def rpc_call(
    socket_path: str,
    method: str,
    params: dict[str, Any],
    *,
    req_id: int = 1,
    timeout_s: float = 5.0,
    retries: int = 0,
) -> dict[str, Any]:
    """執行 RPC 呼叫（#123 起附 TIMEOUT enrich 與唯讀 retry）。

    ``socket_path`` 可以是：
    - 純路徑（AF_UNIX，向後相容）
    - ``unix:///path``（AF_UNIX）
    - ``tcp://host:port``（AF_INET，用於 ssh-tunnel 連接遠端 daemon）

    ``retries``（#123）：TIMEOUT／連線失敗（``SOCKET_ERROR``／``EMPTY_RESPONSE``，
    NIT-6：兩者與 TIMEOUT 一樣視為暫時性、非「daemon 已處理並回覆失敗」）時的
    重試次數，**僅作用於** ``RETRYABLE_READONLY_METHODS`` 白名單內的冪等唯讀
    方法，以 ``_RETRY_BACKOFF_BASE_S`` 起、單次上限 ``_RETRY_BACKOFF_MAX_S``
    （NIT-7）的指數退避重送；其餘方法一律單發。

    總耗時上界（NIT-7）：白名單方法在 ``retries=N`` 下最壞情況 ≈
    ``(N+1) × timeout_s + Σ_{i=0}^{N-1} min(_RETRY_BACKOFF_BASE_S × 2**i, _RETRY_BACKOFF_MAX_S)``
    （前段為 N+1 次請求各自等到逾時，後段為退避總和，兩者相加即整體 CLI 呼叫
    可能卡住的最長時間；退避單次已夾在 5s，總耗時隨 N 線性成長、不會爆炸）。

    TIMEOUT enrich（#123）：最終仍為 TIMEOUT 時（探測方法自身除外），
    以新連線補一次輕量 ``health.ping`` 探測，於錯誤 JSON 附
    ``daemon_reachable``（bool）；可達時再嘗試以 ``health.status`` 取
    in-flight commands／sessions 計數附為 ``daemon_busy``（取不到就省略）。
    既有欄位不動（additive），呼叫端可據此分辨「daemon 死了／斷線」與
    「daemon 活著但長操作還在跑」。
    """
    attempts = 1 + max(0, int(retries)) if method in RETRYABLE_READONLY_METHODS else 1
    delay_s = _RETRY_BACKOFF_BASE_S
    resp: dict[str, Any] = {"ok": False, "error_code": "TIMEOUT"}
    for attempt in range(attempts):
        resp = _rpc_call_once(socket_path, method, params, req_id=req_id, timeout_s=timeout_s)
        if resp.get("ok") or resp.get("error_code") not in _RETRYABLE_TRANSIENT_ERROR_CODES:
            break
        if attempt + 1 < attempts:
            time.sleep(min(delay_s, _RETRY_BACKOFF_MAX_S))
            delay_s *= 2
    if not resp.get("ok") and resp.get("error_code") == "TIMEOUT" and method not in _PROBE_METHODS:
        resp.update(_probe_daemon_after_timeout(socket_path))
    return resp


def _probe_daemon_after_timeout(endpoint: str) -> dict[str, Any]:
    """TIMEOUT 後的輕量 daemon 健康探測（#123）。

    以新連線發 ``health.ping``（1s timeout）判定 ``daemon_reachable``；
    可達時再以 ``health.status`` 撈 in-flight ``commands``／``sessions``
    計數組成 ``daemon_busy`` 上下文。兩段個別 timeout 合計上限 2s，但這只是
    「兩次呼叫各自不逾時」的上界，不保證整段探測的 wall-clock 總和一定
    ≤2s（連線建立、OS 排程等仍可能疊加拖時間）；MINOR-3：另加一道以
    ``time.monotonic()`` 起算的 ``_PROBE_DEADLINE_S`` 硬上限，一旦超過即放棄
    剩餘探測階段（省略對應欄位），確保探測本身絕不讓已逾時的呼叫端等更久。
    探測失敗（或提前放棄）只省略欄位、不拋錯。
    """
    deadline = time.monotonic() + _PROBE_DEADLINE_S
    info: dict[str, Any] = {}
    ping = _rpc_call_once(endpoint, "health.ping", {}, req_id=0, timeout_s=_PROBE_TIMEOUT_S)
    info["daemon_reachable"] = bool(ping.get("ok"))
    if not info["daemon_reachable"]:
        return info
    if time.monotonic() >= deadline:
        return info
    status = _rpc_call_once(endpoint, "health.status", {}, req_id=0, timeout_s=_PROBE_TIMEOUT_S)
    if status.get("ok"):
        busy: dict[str, Any] = {}
        for key in ("commands", "sessions"):
            value = status.get(key)
            if isinstance(value, int):
                busy[key] = value
        if busy:
            info["daemon_busy"] = busy
    return info


def _rpc_call_once(socket_path: str, method: str, params: dict[str, Any], *, req_id: int = 1, timeout_s: float = 5.0) -> dict[str, Any]:
    """單發 RPC 呼叫（無 retry、無 TIMEOUT enrich；#123 由 ``rpc_call`` 包裝）。"""
    try:
        transport, address = _parse_endpoint(socket_path)
    except ValueError as exc:
        return {"ok": False, "error_code": "INVALID_ENDPOINT", "message": str(exc)}

    try:
        if transport == "tcp":
            sock = socket.create_connection(address, timeout=timeout_s)  # type: ignore[arg-type]
            sock.settimeout(timeout_s)
        else:
            if not _af_unix_available():
                return {
                    "ok": False,
                    "error_code": "SOCKET_ERROR",
                    "message": f"本平台無 AF_UNIX，無法連接 unix endpoint {socket_path!r}（請改用 tcp://127.0.0.1:<port>，#131）",
                }
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout_s)
            sock.connect(address)  # type: ignore[arg-type]
    except socket.timeout:
        return {"ok": False, "error_code": "TIMEOUT"}
    except OSError as exc:
        return {"ok": False, "error_code": "SOCKET_ERROR", "message": str(exc)}

    try:
        req = {"id": req_id, "method": method, "params": params}
        payload = json.dumps(req, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        sock.sendall(payload)

        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n", 1)[0].strip()
        if not line:
            return {"ok": False, "error_code": "EMPTY_RESPONSE"}
        obj = json.loads(line.decode("utf-8", errors="replace"))
        if not isinstance(obj, dict):
            return {"ok": False, "error_code": "INVALID_RESPONSE"}
        return obj
    except socket.timeout:
        return {"ok": False, "error_code": "TIMEOUT"}
    except OSError as exc:
        return {"ok": False, "error_code": "SOCKET_ERROR", "message": str(exc)}
    finally:
        sock.close()
