from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from collections.abc import Mapping
from typing import Any, TextIO

from .client import _parse_endpoint
from .constants import LOOPBACK_TCP_HOSTS

TRACE_LOGGER_NAME = "serialwrap.cli_trace"
_TRACE_HANDLER_ATTR = "_serialwrap_cli_trace_handler"
_MAX_NUMERIC_LOG_LEVEL_DIGITS = 4300


def _parse_log_level(raw: str | None) -> int | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    digits = value.lstrip("-")
    if digits.isdigit():
        if len(digits) > _MAX_NUMERIC_LOG_LEVEL_DIGITS:
            return None
        try:
            return int(value)
        except ValueError:
            # Unicode digit lookalikes (例如 ``²``) 與重複負號仍視為未知設定，
            # 不能讓診斷設定擊穿 CLI。
            return None
    levels = {
        "CRITICAL": logging.CRITICAL,
        "FATAL": logging.FATAL,
        "ERROR": logging.ERROR,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "INFO": logging.INFO,
        "DEBUG": logging.DEBUG,
        "NOTSET": logging.NOTSET,
    }
    return levels.get(value.upper())


def resolve_cli_trace_level(verbose: int, env: Mapping[str, str] | None = None) -> int:
    if verbose >= 2:
        return logging.DEBUG
    if verbose >= 1:
        return logging.INFO
    configured = _parse_log_level((env or os.environ).get("SERIALWRAP_LOG_LEVEL"))
    if configured is None:
        return logging.WARNING
    return configured


def configure_cli_trace_logger(
    verbose: int,
    *,
    stream: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> logging.Logger:
    logger = logging.getLogger(TRACE_LOGGER_NAME)
    logger.propagate = False
    logger.setLevel(resolve_cli_trace_level(verbose, env=env))

    target_stream = sys.stderr if stream is None else stream
    owned_handlers = [h for h in logger.handlers if getattr(h, _TRACE_HANDLER_ATTR, False)]
    if owned_handlers:
        handler = owned_handlers[0]
        for extra in owned_handlers[1:]:
            logger.removeHandler(extra)
    else:
        handler = logging.StreamHandler(target_stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        setattr(handler, _TRACE_HANDLER_ATTR, True)
        logger.addHandler(handler)
    if getattr(handler, "stream", None) is not target_stream:
        try:
            handler.setStream(target_stream)
        except (OSError, ValueError):
            # pytest/caller-owned capture stream 可能在兩次 CLI 呼叫間已關閉；
            # trace handler 必須自癒，不能讓診斷設定反過來擊穿一般 RPC CLI。
            logger.removeHandler(handler)
            handler = logging.StreamHandler(target_stream)
            handler.setFormatter(logging.Formatter("%(message)s"))
            setattr(handler, _TRACE_HANDLER_ATTR, True)
            logger.addHandler(handler)
    return logger


def _hash_endpoint(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8", errors="surrogateescape")).hexdigest()


def _loopback_endpoint_id(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def describe_trace_endpoint(endpoint: str) -> tuple[str, str]:
    try:
        transport, address = _parse_endpoint(endpoint)
    except ValueError:
        return "unknown", _hash_endpoint(endpoint)
    if transport == "unix":
        return "unix", _hash_endpoint(str(address))
    host, port = address
    if host in LOOPBACK_TCP_HOSTS:
        return "tcp", _loopback_endpoint_id(host, port)
    return "tcp", _hash_endpoint(f"tcp://{host}:{port}")


def emit_rpc_trace(
    *,
    logger: logging.Logger,
    endpoint: str,
    endpoint_source: str,
    method: str,
    timeout_s: float,
    response: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> None:
    if not logger.isEnabledFor(logging.INFO):
        return
    endpoint_transport, endpoint_id = describe_trace_endpoint(endpoint)
    errno_value = metadata.get("errno")
    if not isinstance(errno_value, int):
        errno_value = None
    errno_name = metadata.get("errno_name")
    if not isinstance(errno_name, str) or errno_value is None:
        errno_name = None
    retry_count = metadata.get("retry_count")
    if not isinstance(retry_count, int):
        retry_count = 0
    elapsed_ms = metadata.get("elapsed_ms")
    if not isinstance(elapsed_ms, int):
        elapsed_ms = 0
    error_code = None if response.get("ok") else response.get("error_code")
    if error_code is not None and not isinstance(error_code, str):
        error_code = str(error_code)
    payload = {
        "endpoint_transport": endpoint_transport,
        "endpoint_id": endpoint_id,
        "endpoint_source": endpoint_source,
        "method": method,
        "elapsed_ms": elapsed_ms,
        "error_code": error_code,
        "errno": errno_value,
        "errno_name": errno_name,
        "retry_count": retry_count,
        "timeout_s": timeout_s,
    }
    logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
