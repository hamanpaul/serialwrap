from __future__ import annotations

import os
import time
import uuid

from .config import SessionProfile
from .uart_io import UARTBridge


def _wait_or_fail(bridge: UARTBridge, pattern: str, timeout_s: float, err: str) -> tuple[bool, str | None]:
    if bridge.wait_for_regex(pattern, timeout_s):
        return True, None
    return False, err


def _resolve_login_user(sp: SessionProfile) -> tuple[str | None, str | None]:
    if sp.user_env:
        user = os.environ.get(sp.user_env)
        if not user:
            return None, "USER_ENV_MISSING"
        return user, None
    if sp.username:
        return sp.username, None
    return None, None


def _resolve_login_password(sp: SessionProfile) -> tuple[str | None, str | None]:
    if not sp.pass_env:
        return None, "PASS_ENV_REQUIRED"
    password = os.environ.get(sp.pass_env)
    if not password:
        return None, "PASS_ENV_MISSING"
    return password, None


def _prompt_timeout_error(sp: SessionProfile) -> str:
    if sp.platform == "bcm":
        return "BCM_PROMPT_TIMEOUT"
    if sp.platform == "shell":
        return "SHELL_PROMPT_TIMEOUT"
    return "PRPL_PROMPT_TIMEOUT"


def _maybe_login(bridge: UARTBridge, sp: SessionProfile) -> tuple[bool, str | None]:
    user, err = _resolve_login_user(sp)
    needs_login = bool(user or sp.pass_env)
    if not needs_login:
        return False, None

    ok, err = _wait_or_fail(bridge, sp.login_regex, sp.timeout_s, "LOGIN_PROMPT_TIMEOUT")
    if not ok:
        return False, err

    if not user:
        return False, "LOGIN_USER_REQUIRED"
    bridge.send_command(user, source="system")

    if bridge.wait_for_regex(sp.password_regex, sp.timeout_s):
        password, perr = _resolve_login_password(sp)
        if perr is not None:
            return False, perr
        assert password is not None
        bridge.send_secret(password)
        ok, err = _wait_or_fail(bridge, sp.prompt_regex, sp.timeout_s, _prompt_timeout_error(sp))
        return ok, err

    ok, err = _wait_or_fail(bridge, sp.prompt_regex, sp.timeout_s, _prompt_timeout_error(sp))
    return ok, err


def ensure_ready(bridge: UARTBridge, sp: SessionProfile) -> tuple[bool, str | None]:
    bridge.clear_rx_buffer()
    bridge.send_command("", source="system")

    if not bridge.wait_for_regex(sp.prompt_regex, sp.timeout_s):
        ok, err = _maybe_login(bridge, sp)
        if err is not None:
            return ok, err
        if not ok:
            return False, _prompt_timeout_error(sp)

    if sp.post_login_cmd:
        bridge.send_command(sp.post_login_cmd, source="system")
        ok, err = _wait_or_fail(bridge, sp.prompt_regex, sp.timeout_s, "POST_LOGIN_CMD_TIMEOUT")
        if not ok:
            return ok, err

    nonce = uuid.uuid4().hex[:8]
    probe = sp.ready_probe.replace("${nonce}", nonce)
    bridge.send_command(probe, source="system")
    ok, err = _wait_or_fail(bridge, nonce, sp.timeout_s, "READY_NONCE_TIMEOUT")
    if not ok:
        return ok, err
    ok, err = _wait_or_fail(bridge, sp.prompt_regex, sp.timeout_s, "READY_PROMPT_TIMEOUT")
    if not ok:
        return ok, err
    return True, None
