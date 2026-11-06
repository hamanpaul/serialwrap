"""serialwrap CLI 執行包裝與每次呼叫的證據記錄。"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from typing import Any

from .daemon_harness import resolve_cli_command


def cli_run(
    argv: list[str],
    *,
    socket_path: str,
    env: dict[str, str],
    timeout: float = 10.0,
    log_path: pathlib.Path | None = None,
) -> dict[str, Any]:
    """執行 JSON CLI，保留 stdout/stderr 並可追加 cli.ndjson。"""
    endpoint_flag = "--endpoint" if socket_path.startswith("tcp://") or os.name == "nt" else "--socket"
    full_argv = [*resolve_cli_command(), endpoint_flag, socket_path, *argv]
    started_ns = time.monotonic_ns()
    try:
        proc = subprocess.run(
            full_argv,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        result = {"ok": False, "error_code": "CLI_TIMEOUT", "argv": argv, "stdout": stdout, "stderr": stderr, "rc": None}
        _append_log(log_path, full_argv, result, started_ns)
        return result

    out = (proc.stdout or "").strip()
    if not out:
        result = {
            "ok": False,
            "error_code": "EMPTY_STDOUT",
            "stderr": (proc.stderr or "").strip(),
            "rc": proc.returncode,
        }
    else:
        try:
            obj = json.loads(out)
        except json.JSONDecodeError as exc:
            result = {
                "ok": False,
                "error_code": f"BAD_JSON:{exc}",
                "stdout": out,
                "stderr": (proc.stderr or "").strip(),
                "rc": proc.returncode,
            }
        else:
            if not isinstance(obj, dict):
                result = {"ok": False, "error_code": "INVALID_RESPONSE", "response": obj, "rc": proc.returncode}
            else:
                obj.setdefault("_rc", proc.returncode)
                result = obj
    _append_log(log_path, full_argv, {
        "rc": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "response": result,
    }, started_ns)
    return result


def _append_log(
    path: pathlib.Path | None, argv: list[str], result: dict[str, Any], started_ns: int
) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "mono_start_ns": started_ns,
        "argv": argv,
        **result,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()


__all__ = ["cli_run"]
