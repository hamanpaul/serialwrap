"""transport stall 純偵測（#150，仿 multi_open.py 純函式慣例）。

「TX 通、RX 凍」的 USB/usbip read-endpoint stall（dmesg 常見 ``urb stopped: -32``）
過去被折疊進 PROMPT_UNAVAILABLE／*_PROMPT_TIMEOUT，誤導 operator 去 power-cycle DUT
或反覆 recover——實際上 serialwrap 的 recover/release+attach 都無法自復，需 host 層
USB re-enumeration。本模組提供：

- :func:`is_transport_refinable_error`：哪些失敗碼允許 transport 層精煉。
- :func:`classify_probe_failure`：純分類函式（probe 全程零 raw RX＋RX 已凍結逾閾
  → TRANSPORT_STALL）；rx_delta==0 與 #153 RX_FLOOD（高 RX）特徵天然互斥。
- :func:`resolve_usb_busid`／:func:`transport_stall_hint`：best-effort 解析 USB busid
  並產生可執行的 host 層復原指令提示。
- :func:`is_never_rx_probe_failure`／:func:`never_rx_hint`：自 attach 起從未收到任何
  RX 的 probe 失敗（:func:`classify_probe_failure` 條件 3 刻意排除的那一類）不改
  last_error，只補分方向診斷的 detail。

全部為純函式、無副作用；接線與告警去重在 ``session_manager._refine_probe_failure``。
"""
from __future__ import annotations

import os
import re

from .constants import ERROR_TRANSPORT_STALL, TRANSPORT_STALL_MIN_RX_AGE_S

# USB busid 路徑段樣式：如 "1-1"、"8-2.1"（不含 interface 後綴 "1-1:1.0"）。
_BUSID_RE = re.compile(r"\d+-\d+(\.\d+)*")


def is_transport_refinable_error(err: str) -> bool:
    """``err`` 是否允許被精煉為 TRANSPORT_STALL。

    涵蓋 PROMPT_UNAVAILABLE、LOGIN_PROMPT_TIMEOUT 與 login_fsm 的
    BCM/SHELL/PRPL/READY 等 ``*_PROMPT_TIMEOUT`` 家族。CREDENTIALS_UNRESOLVED
    在 probe 前就早退、本就不會進來，此處自然排除（非集合成員）。
    """
    return (
        err == "PROMPT_UNAVAILABLE"
        or err == "LOGIN_PROMPT_TIMEOUT"
        or err.endswith("_PROMPT_TIMEOUT")
    )


def classify_probe_failure(
    err: str,
    *,
    rx_delta: int,
    last_rx_mono: float,
    now: float,
) -> tuple[str, bool]:
    """probe 失敗碼的 transport 層純分類（#150）。

    翻轉為 ``(TRANSPORT_STALL, True)`` 需同時滿足：

    1. ``err`` 屬可精煉集合（:func:`is_transport_refinable_error`）。
    2. ``rx_delta == 0``——probe 全程零 raw RX，連 TX echo 都無（與 #153 flood
       的高 RX 特徵天然互斥；此條件同時是未來 RX_FLOOD 分支的插槽）。
    3. ``last_rx_mono > 0.0``——本 session 曾有 RX，排除「從未活過」的死線/空埠
       （維持原 PROMPT_UNAVAILABLE 語意）。
    4. ``now - last_rx_mono >= TRANSPORT_STALL_MIN_RX_AGE_S``——RX 已凍結逾閾。

    其餘一律回 ``(err, False)`` 原樣。
    """
    if not err or not is_transport_refinable_error(err):
        return err, False
    if rx_delta != 0:
        return err, False
    if last_rx_mono <= 0.0:
        return err, False
    if now - last_rx_mono < TRANSPORT_STALL_MIN_RX_AGE_S:
        return err, False
    return ERROR_TRANSPORT_STALL, True


def is_never_rx_probe_failure(err: str | None, *, rx_delta: int, last_rx_mono: float) -> bool:
    """probe 失敗時，本 session 是否自 attach 起從未收到任何 RX。

    與 :func:`classify_probe_failure` 互補：那邊以條件 3 排除「從未活過」的 session
    （維持 PROMPT_UNAVAILABLE，不翻成 TRANSPORT_STALL）；這裡只認出這一類，供呼叫端
    補 detail，last_error 本身不變。成立條件：

    1. ``err == "PROMPT_UNAVAILABLE"``——login FSM 的 ``*_PROMPT_TIMEOUT`` 家族已有
       rx tail detail（#174），不在此改寫。
    2. ``rx_delta == 0``——probe 全程連 echo 都沒有。
    3. ``last_rx_mono <= 0.0``——自 attach 起沒有任何 RX。
    """
    return err == "PROMPT_UNAVAILABLE" and rx_delta == 0 and last_rx_mono <= 0.0


def never_rx_hint(real_path: str | None) -> str:
    """自 attach 起零 RX 的 probe 失敗 detail：提示先分收發方向，再決定動哪一層。"""
    where = f"（{real_path}）" if real_path else ""
    return (
        f"自 attach 起未收到任何 RX{where}，probe 期間連 echo 都沒有：先分收發方向再動作，"
        "不要反覆 recover、換 profile 或重裝。常見原因：DUT 未上電或 console 不在這個埠；"
        "DUT TX 到轉接器 RX 的線或 GND 未接好；host 端 USB 路徑異常"
        "（dmesg 查 `urb stopped: -32` 或 usbip 斷線）。baud 不符時通常仍收得到亂碼位元組，"
        "不會是零 RX。"
    )


def resolve_usb_busid(real_path: str | None) -> str | None:
    """由 tty real_path best-effort 解析 USB busid（POSIX-only；失敗一律回 None）。

    例：``/dev/ttyUSB0`` → ``/sys/class/tty/ttyUSB0/device`` realpath 為
    ``/sys/devices/platform/vhci_hcd.0/usb1/1-1/1-1:1.0/ttyUSB0`` → 由深至淺找
    第一個匹配 busid 樣式的路徑段 → ``"1-1"``。
    """
    if os.name != "posix" or not real_path:
        return None
    try:
        name = os.path.basename(real_path)
        if not name:
            return None
        resolved = os.path.realpath(f"/sys/class/tty/{name}/device")
        for seg in reversed(resolved.split("/")):
            if _BUSID_RE.fullmatch(seg):
                return seg
    except Exception:
        return None
    return None


def transport_stall_hint(real_path: str | None, rx_age_s: float) -> str:
    """產生 TRANSPORT_STALL 的人類可讀 detail（含 host 層復原指令，#150）。"""
    base = (
        f"零 RX {round(rx_age_s, 1)} 秒且 probe 期間連 echo 都無："
        "疑似 USB/usbip read-endpoint stall（dmesg 常見 `urb stopped: -32`）；"
        "serialwrap 的 recover/release+attach 無法自復，需 host 層 USB re-enumeration。"
        "亦可能為 DUT 斷電/當機，請先以 dmesg 佐證再動作。"
    )
    busid = resolve_usb_busid(real_path)
    if busid:
        cmd = (
            f"sudo sh -c 'echo 0 > /sys/bus/usb/devices/{busid}/authorized; "
            f"echo 1 > /sys/bus/usb/devices/{busid}/authorized'"
        )
        return f"{base} 復原指令：{cmd}（usbip 環境亦可 usbipd detach/attach 後重新 attach）。"
    return (
        f"{base} 復原方式：對該 USB 裝置做 authorized 0→1 toggle"
        "（/sys/bus/usb/devices/<busid>/authorized）或 usbipd detach/attach 後重新 attach。"
    )
