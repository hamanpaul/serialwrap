from __future__ import annotations

import os
import re
import time
import uuid

from .auth import SessionAuth
from .config import ProfileTemplate, SessionProfile
from .constants import BOOT_BANNER_PATTERNS, ERROR_RX_FLOOD, RX_FLOOD_BYTES_PER_10S
from .uart_io import UARTBridge

# RX 洪水可遮蔽的失敗碼集合（#153）：這些碼在「console 被灌爆」時全是同一個病灶
# （probe/等待被洪水淹沒），反分類為 RX_FLOOD 讓上層知道該等排空而非重建 session。
# LOGIN_REQUIRED／CREDENTIALS_*／USER_ENV_MISSING 等**不在**集合內，永不被遮蔽
# （login prompt 可見＝可行動，優先於 flood）。
_FLOOD_MASKABLE_ERRORS = frozenset({
    "PROMPT_UNAVAILABLE",
    "READY_NONCE_TIMEOUT",
    "READY_PROMPT_TIMEOUT",
    "POST_LOGIN_CMD_TIMEOUT",
    "LOGIN_PROMPT_TIMEOUT",
})


def _maybe_reclassify_flood(bridge: UARTBridge, err: str | None) -> str | None:
    """probe 失敗碼的 RX 洪水反分類（#153，單一 choke point）。

    err 屬 ``_FLOOD_MASKABLE_ERRORS`` 或以 ``_PROMPT_TIMEOUT`` 結尾時，查
    ``bridge.rx_stats()``；視窗內 raw RX bytes 超過 ``RX_FLOOD_BYTES_PER_10S``
    即回 ``RX_FLOOD``，否則原樣。對不支援 ``rx_stats`` 的 bridge（測試 fake）
    一律原樣直通（getattr 防禦），行為零變更。
    """
    if err is None:
        return err
    if err not in _FLOOD_MASKABLE_ERRORS and not err.endswith("_PROMPT_TIMEOUT"):
        return err
    stats_fn = getattr(bridge, "rx_stats", None)
    if not callable(stats_fn):
        return err
    try:
        stats = stats_fn()
    except Exception:
        return err
    if not isinstance(stats, dict):
        return err
    value = stats.get("rx_bytes_last_10s")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return err
    if value >= RX_FLOOD_BYTES_PER_10S:
        return ERROR_RX_FLOOD
    return err


def detect_boot_banner(text: str) -> bool:
    """``text`` 是否含 boot banner 樣式（U-Boot 版本行／autoboot 倒數行）（#130）。

    採**大小寫敏感的 substring 比對**（非 regex）：樣式為固定字面字串、
    成本低，且對 RX chunk 任意切割位置容忍度高（呼叫端以 rolling tail 餵入）。
    """
    if not text:
        return False
    return any(pattern in text for pattern in BOOT_BANNER_PATTERNS)


def _wait_or_fail(bridge: UARTBridge, pattern: str, timeout_s: float, err: str) -> tuple[bool, str | None]:
    if bridge.wait_for_regex(pattern, timeout_s):
        return True, None
    return False, err


def _resolve_login_user(sp: SessionProfile, auth: SessionAuth | None) -> tuple[str | None, str | None]:
    if auth is not None and auth.username:
        return auth.username, None
    # fallback: 直接從 os.environ 讀取（向後相容）
    if sp.user_env:
        user = os.environ.get(sp.user_env)
        if not user:
            return None, "USER_ENV_MISSING"
        return user, None
    if sp.username:
        return sp.username, None
    return None, None


def _resolve_login_password(sp: SessionProfile, auth: SessionAuth | None) -> tuple[str | None, str | None]:
    if auth is not None and auth.password:
        return auth.password, None
    # fallback: 直接從 os.environ 讀取（向後相容）
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


def _probe_prompt(bridge: UARTBridge, sp: SessionProfile) -> bool:
    bridge.clear_rx_buffer()
    bridge.send_command("", source="system")
    return bridge.wait_for_regex(sp.prompt_regex, sp.timeout_s)


def _classify_non_ready_state(bridge: UARTBridge, sp: SessionProfile) -> str:
    snapshot = bridge.rx_tail()
    if re.search(sp.login_regex, snapshot):
        return "LOGIN_REQUIRED"
    return "PROMPT_UNAVAILABLE"


def _finalize_ready(bridge: UARTBridge, sp: SessionProfile) -> tuple[bool, str | None]:
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


def _maybe_login(bridge: UARTBridge, sp: SessionProfile, auth: SessionAuth | None) -> tuple[bool, str | None]:
    user, err = _resolve_login_user(sp, auth)
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
        password, perr = _resolve_login_password(sp, auth)
        if perr is not None:
            return False, perr
        assert password is not None
        bridge.send_secret(password)
        ok, err = _wait_or_fail(bridge, sp.prompt_regex, sp.timeout_s, _prompt_timeout_error(sp))
        bridge.clear_rx_buffer()
        return ok, err

    ok, err = _wait_or_fail(bridge, sp.prompt_regex, sp.timeout_s, _prompt_timeout_error(sp))
    return ok, err


def probe_ready(bridge: UARTBridge, sp: SessionProfile) -> tuple[bool, str | None]:
    # #153：兩個公開出口統一包裝失敗碼，所有 sink（attach/recover/reprobe/auto-login）
    # 自動涵蓋 RX 洪水反分類；成功路徑（err=None）不受影響。
    if not _probe_prompt(bridge, sp):
        return False, _maybe_reclassify_flood(bridge, _classify_non_ready_state(bridge, sp))
    ok, err = _finalize_ready(bridge, sp)
    if not ok:
        return ok, _maybe_reclassify_flood(bridge, err)
    return ok, err


def ensure_ready(bridge: UARTBridge, sp: SessionProfile, auth: SessionAuth | None = None) -> tuple[bool, str | None]:
    if not _probe_prompt(bridge, sp):
        ok, err = _maybe_login(bridge, sp, auth)
        if err is not None:
            return ok, _maybe_reclassify_flood(bridge, err)
        if not ok:
            return False, _maybe_reclassify_flood(bridge, _prompt_timeout_error(sp))
    ok, err = _finalize_ready(bridge, sp)
    if not ok:
        return ok, _maybe_reclassify_flood(bridge, err)
    return ok, err


# ---------------------------------------------------------------------------
# Auto-detect：用 UART 輸出匹配最佳 ProfileTemplate
# ---------------------------------------------------------------------------

def detect_template(
    bridge: UARTBridge,
    templates: list[ProfileTemplate],
    probe_timeout_s: float = 3.0,
) -> ProfileTemplate | None:
    """送 ``\\r`` 到 UART，收集回應，依序嘗試各 template 的 regex 進行匹配。

    回傳最先匹配的 ``ProfileTemplate``，全部不符合則回傳 ``None``。
    templates 應已由 ``config.py`` 排序（passthrough 在最後）。
    """
    bridge.clear_rx_buffer()
    bridge.send_command("", source="system")
    time.sleep(probe_timeout_s)
    snapshot = bridge.rx_tail()

    # 第一輪：嘗試 prompt_regex（已處於可操作 prompt）
    login_candidate: ProfileTemplate | None = None
    for tpl in templates:
        if tpl.platform == "passthrough":
            continue
        try:
            if re.search(tpl.prompt_regex, snapshot):
                return tpl
        except re.error:
            continue
        # 第二輪備選：login_regex 匹配（尚未登入）
        if login_candidate is None:
            try:
                if re.search(tpl.login_regex, snapshot):
                    login_candidate = tpl
            except re.error:
                continue

    if login_candidate is not None:
        return login_candidate

    return None
