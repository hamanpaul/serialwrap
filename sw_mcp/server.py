#!/usr/bin/env python3
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
}


def call_tool(socket_path: str, tool: str, params: dict[str, Any]) -> dict[str, Any]:
    method = _TOOL_MAP.get(tool)
    if method is None:
        return {"ok": False, "error_code": "TOOL_NOT_FOUND", "tool": tool}
    return rpc_call(socket_path, method, params)


def run_stdio(socket_path: str) -> int:
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
        resp = call_tool(socket_path, tool, params)
        sys.stdout.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="serialwrap-mcp")
    p.add_argument("--socket", default=SOCKET_PATH)
    p.add_argument("--tool")
    p.add_argument("--params", default="{}")
    args = p.parse_args(argv)

    if args.tool:
        try:
            params = json.loads(args.params)
        except json.JSONDecodeError:
            params = {}
        resp = call_tool(args.socket, args.tool, params if isinstance(params, dict) else {})
        sys.stdout.write(json.dumps(resp, ensure_ascii=False, separators=(",", ":")) + "\n")
        return 0 if resp.get("ok") else 2

    return run_stdio(args.socket)


if __name__ == "__main__":
    raise SystemExit(main())
