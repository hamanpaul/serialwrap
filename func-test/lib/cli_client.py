"""CLI 執行包裝。

提供對 serialwrap CLI 的便捷呼叫，回傳解析過的 JSON 回應。
"""
from __future__ import annotations

import json
import subprocess
from typing import Any

from .daemon_harness import SERIALWRAP


def cli_run(
    argv: list[str],
    *,
    socket_path: str,
    env: dict[str, str],
    timeout: float = 10.0,
) -> dict[str, Any]:
    """執行 serialwrap CLI 子命令並回傳 JSON dict。"""
    full_argv = [SERIALWRAP, "--socket", socket_path] + argv
    try:
        proc = subprocess.run(
            full_argv,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_code": "CLI_TIMEOUT", "argv": argv}

    out = proc.stdout.strip()
    if not out:
        return {
            "ok": False,
            "error_code": "EMPTY_STDOUT",
            "stderr": proc.stderr.strip(),
            "rc": proc.returncode,
        }
    try:
        obj = json.loads(out)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error_code": f"BAD_JSON:{exc}",
            "stdout": out,
            "stderr": proc.stderr.strip(),
            "rc": proc.returncode,
        }
    obj.setdefault("_rc", proc.returncode)
    return obj
