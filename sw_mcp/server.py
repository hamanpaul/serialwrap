#!/usr/bin/env python3
"""serialwrap MCP adapter — 將 MCP 工具名映射到內部 RPC method。"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sw_core.client import rpc_call
from sw_core.constants import SOCKET_PATH

_TOOL_MAP = {
    "serialwrap_clear_session": "session.clear",
    "serialwrap_get_health": "health.status",
    "serialwrap_get_command": "command.get",
    "serialwrap_get_session_state": "session.get_state",
    "serialwrap_bind_session": "session.bind",
    "serialwrap_attach_session": "session.attach",
    "serialwrap_self_test": "session.self_test",
    "serialwrap_recover_session": "session.recover",
    "serialwrap_list_devices": "device.list",
    "serialwrap_list_sessions": "session.list",
    "serialwrap_submit_command": "command.submit",
    "serialwrap_tail_command_result": "command.result_tail",
    "serialwrap_tail_results": "result.tail",
    "serialwrap_attach_console": "session.console_attach",
    "serialwrap_detach_console": "session.console_detach",
    "serialwrap_list_consoles": "session.console_list",
    "serialwrap_open_interactive": "session.interactive_open",
    "serialwrap_send_interactive_keys": "session.interactive_send",
    "serialwrap_get_interactive_status": "session.interactive_status",
    "serialwrap_close_interactive": "session.interactive_close",
    "serialwrap_log_start": "session.log_start",
    "serialwrap_log_stop": "session.log_stop",
    "serialwrap_log_status": "session.log_status",
    "serialwrap_wal_reset": "wal.reset",
    "serialwrap_wal_current_seq": "wal.current_seq",
    "serialwrap_alias_list": "alias.list",
    "serialwrap_alias_set": "alias.set",
    "serialwrap_alias_assign": "alias.assign",
    "serialwrap_alias_unassign": "alias.unassign",
    "serialwrap_cancel_command": "command.cancel",
    "serialwrap_ping": "health.ping",
    "serialwrap_log_tail_text": "log.tail_text",
    "serialwrap_wal_range": "wal.range",
    "serialwrap_file_push": "file.push",
    "serialwrap_file_pull": "file.pull",
}

# ── 型別縮寫 ──────────────────────────────────────────────────
_STR = {"type": "string"}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}


def _td(
    name: str,
    desc: str,
    props: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    """產生單一 MCP tool 定義。"""
    schema: dict[str, Any] = {"type": "object", "properties": props or {}}
    if required:
        schema["required"] = required
    return {"name": name, "description": desc, "inputSchema": schema}


_TOOL_DEFS: list[dict[str, Any]] = [
    # ── health ────────────────────────────────────────────────
    _td("serialwrap_ping", "健康檢查 ping，回傳 pong"),
    _td("serialwrap_get_health", "取得 daemon 完整健康狀態"),
    # ── device ────────────────────────────────────────────────
    _td("serialwrap_list_devices", "列出所有偵測到的 serial 裝置"),
    # ── session 管理 ──────────────────────────────────────────
    _td("serialwrap_list_sessions", "列出所有 session"),
    _td(
        "serialwrap_get_session_state",
        "取得指定 session 狀態",
        {"selector": _STR},
        ["selector"],
    ),
    _td(
        "serialwrap_bind_session",
        "將 session 綁定到指定裝置",
        {"selector": _STR, "device_by_id": _STR},
        ["selector", "device_by_id"],
    ),
    _td(
        "serialwrap_attach_session",
        "啟動 session（bridge + 登入流程）",
        {"selector": _STR},
        ["selector"],
    ),
    _td(
        "serialwrap_self_test",
        "對 session 執行 self-test（ready probe）",
        {"selector": _STR, "timeout_s": _INT},
        ["selector"],
    ),
    _td(
        "serialwrap_recover_session",
        "嘗試恢復 session 至 READY",
        {"selector": _STR, "timeout_s": _INT, "force": _BOOL},
        ["selector"],
    ),
    _td(
        "serialwrap_clear_session",
        "清除 session 狀態（detach + 重設）",
        {"selector": _STR},
        ["selector"],
    ),
    # ── console ───────────────────────────────────────────────
    _td(
        "serialwrap_attach_console",
        "將 human console 附掛到 session",
        {"selector": _STR, "label": _STR},
        ["selector"],
    ),
    _td(
        "serialwrap_detach_console",
        "將 human console 從 session 拔除",
        {"selector": _STR, "client_id": _STR},
        ["selector", "client_id"],
    ),
    _td(
        "serialwrap_list_consoles",
        "列出 session 上已附掛的 console",
        {"selector": _STR},
        ["selector"],
    ),
    # ── interactive ───────────────────────────────────────────
    _td(
        "serialwrap_open_interactive",
        "開啟 interactive lease（vi/top 等互動命令）",
        {
            "selector": _STR,
            "owner": _STR,
            "timeout_s": _INT,
            "command": _STR,
        },
        ["selector"],
    ),
    _td(
        "serialwrap_send_interactive_keys",
        "送出按鍵資料到 interactive session",
        {
            "interactive_id": _STR,
            "data": _STR,
            "encoding": _STR,
        },
        ["interactive_id"],
    ),
    _td(
        "serialwrap_get_interactive_status",
        "取得 interactive session 狀態與畫面截圖",
        {"interactive_id": _STR, "screen_chars": _INT},
        ["interactive_id"],
    ),
    _td(
        "serialwrap_close_interactive",
        "關閉 interactive lease",
        {"interactive_id": _STR},
        ["interactive_id"],
    ),
    # ── alias ─────────────────────────────────────────────────
    _td("serialwrap_alias_list", "列出所有已設定的 alias"),
    _td(
        "serialwrap_alias_set",
        "為指定 session 設定 alias",
        {"session_id": _STR, "alias": _STR},
        ["session_id", "alias"],
    ),
    _td(
        "serialwrap_alias_assign",
        "以 by-id 路徑與 alias 建立新 target",
        {"by_id": _STR, "alias": _STR, "profile": _STR},
        ["by_id", "alias"],
    ),
    _td(
        "serialwrap_alias_unassign",
        "移除 alias 指派",
        {"alias": _STR},
        ["alias"],
    ),
    # ── command ───────────────────────────────────────────────
    _td(
        "serialwrap_submit_command",
        "提交命令到 UART（透過 arbiter 排程）",
        {
            "selector": _STR,
            "cmd": _STR,
            "source": _STR,
            "mode": _STR,
            "timeout_s": _INT,
            "priority": _INT,
        },
        ["selector", "cmd"],
    ),
    _td(
        "serialwrap_get_command",
        "取得命令執行結果",
        {"cmd_id": _STR},
        ["cmd_id"],
    ),
    _td(
        "serialwrap_tail_command_result",
        "逐段讀取 background 命令的 capture 結果",
        {"cmd_id": _STR, "from_chunk": _INT, "limit": _INT},
        ["cmd_id"],
    ),
    _td(
        "serialwrap_cancel_command",
        "取消排程中或執行中的命令",
        {"cmd_id": _STR},
        ["cmd_id"],
    ),
    # ── result / log ──────────────────────────────────────────
    _td(
        "serialwrap_tail_results",
        "讀取 WAL raw records（legacy; 若帶 cmd_id 會走 background result）",
        {
            "cmd_id": _STR,
            "selector": _STR,
            "from_seq": _INT,
            "from_chunk": _INT,
            "limit": _INT,
        },
    ),
    _td(
        "serialwrap_log_tail_text",
        "讀取 WAL 純文字行（已解碼 payload）",
        {"selector": _STR, "from_seq": _INT, "limit": _INT},
    ),
    # ── session capture ───────────────────────────────────────
    _td(
        "serialwrap_log_start",
        "對 session 啟動 RX capture（agent log）",
        {"selector": _STR},
        ["selector"],
    ),
    _td(
        "serialwrap_log_stop",
        "停止 session 的 RX capture",
        {"selector": _STR},
        ["selector"],
    ),
    _td(
        "serialwrap_log_status",
        "查詢 session 的 capture 狀態",
        {"selector": _STR},
        ["selector"],
    ),
    # ── WAL ───────────────────────────────────────────────────
    _td(
        "serialwrap_wal_range",
        "依序號範圍讀取 WAL raw records",
        {"from_seq": _INT, "to_seq": _INT, "limit": _INT},
    ),
    _td("serialwrap_wal_reset", "重設 WAL（清除所有記錄）"),
    _td("serialwrap_wal_current_seq", "取得目前 WAL 序號"),
    # ── File Transfer ────────────────────────────────────────────
    _td(
        "serialwrap_file_push",
        "將本地檔案推送到 target 裝置",
        {
            "selector": _STR,
            "local_path": _STR,
            "remote_path": _STR,
            "chunk_size": _INT,
        },
        ["selector", "local_path", "remote_path"],
    ),
    _td(
        "serialwrap_file_pull",
        "從 target 裝置拉取檔案到本地",
        {
            "selector": _STR,
            "remote_path": _STR,
            "local_path": _STR,
        },
        ["selector", "remote_path"],
    ),
]


def list_tools() -> list[dict[str, Any]]:
    """回傳所有可用 MCP 工具的定義清單。"""
    return list(_TOOL_DEFS)


def call_tool(endpoint: str, tool: str, params: dict[str, Any]) -> dict[str, Any]:
    if tool == "list_tools":
        return {"ok": True, "tools": list_tools()}
    method = _TOOL_MAP.get(tool)
    if method is None:
        return {"ok": False, "error_code": "TOOL_NOT_FOUND", "tool": tool}
    return rpc_call(endpoint, method, params)


def run_stdio(endpoint: str) -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({"ok": False, "error_code": "INVALID_JSON"}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue

        if not isinstance(req, dict):
            sys.stdout.write(json.dumps({"ok": False, "error_code": "INVALID_REQUEST"}, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue

        tool = str(req.get("tool") or "")
        params = req.get("params") if isinstance(req.get("params"), dict) else {}
        resp = call_tool(endpoint, tool, params)
        sys.stdout.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="serialwrap-mcp",
        description="serialwrap MCP adapter（支援本機 Unix socket 與遠端 endpoint）",
        epilog=(
            "examples:\n"
            "  serialwrap-mcp --tool serialwrap_ping --params '{}'\n"
            "  serialwrap-mcp --endpoint tcp://127.0.0.1:7777 --tool serialwrap_list_sessions"
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument("--socket", default=SOCKET_PATH, help="本機 daemon 的 Unix socket 路徑（預設: %(default)s）")
    p.add_argument("--endpoint", default=None, metavar="ENDPOINT", help="遠端 daemon endpoint，例如 tcp://127.0.0.1:7777（優先於 --socket）")
    p.add_argument("--tool", help="單次執行的 MCP tool 名稱；省略時進入 stdio 模式")
    p.add_argument("--params", default="{}", help="傳給 --tool 的 JSON 參數字串（預設: %(default)s）")
    args = p.parse_args(argv)

    endpoint = args.endpoint if args.endpoint else args.socket

    if args.tool:
        try:
            params = json.loads(args.params)
        except json.JSONDecodeError:
            params = {}
        resp = call_tool(endpoint, args.tool, params if isinstance(params, dict) else {})
        sys.stdout.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n")
        return 0 if resp.get("ok") else 2

    return run_stdio(endpoint)


if __name__ == "__main__":
    raise SystemExit(main())
