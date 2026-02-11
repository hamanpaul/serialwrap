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


def ensure_ready(bridge: UARTBridge, sp: SessionProfile) -> tuple[bool, str | None]:
    bridge.clear_rx_buffer()
    bridge.send_command("", source="system")

    if sp.platform == "bcm":
        ok, err = _wait_or_fail(bridge, sp.login_regex, sp.timeout_s, "LOGIN_PROMPT_TIMEOUT")
        if not ok:
            return ok, err
        if sp.username:
            bridge.send_command(sp.username, source="system")
        ok, err = _wait_or_fail(bridge, sp.password_regex, sp.timeout_s, "PASSWORD_PROMPT_TIMEOUT")
        if not ok:
            return ok, err

        if not sp.password_env:
            return False, "PASSWORD_ENV_REQUIRED"
        password = os.environ.get(sp.password_env)
        if not password:
            return False, "PASSWORD_ENV_MISSING"
        bridge.send_secret(password)
        ok, err = _wait_or_fail(bridge, sp.prompt_regex, sp.timeout_s, "BCM_PROMPT_TIMEOUT")
        if not ok:
            return ok, err

        if sp.post_login_cmd:
            bridge.send_command(sp.post_login_cmd, source="system")
            ok, err = _wait_or_fail(bridge, sp.prompt_regex, sp.timeout_s, "POST_LOGIN_CMD_TIMEOUT")
            if not ok:
                return ok, err
    else:
        ok, err = _wait_or_fail(bridge, sp.prompt_regex, sp.timeout_s, "PRPL_PROMPT_TIMEOUT")
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
