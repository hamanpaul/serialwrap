"""sw_core.doctor_cmd — serialwrap 環境診斷（doctor）。

提供 :func:`run_doctor`，對安裝／執行環境做一系列**唯讀**檢查並回報結果。
每項檢查永不拋例外（探測失敗一律視為不 ok 並給修復提示），讓 `serialwrap
doctor` 在任何破損環境下都能順利印出報告而非崩潰。

回傳清單中每個項目的結構::

    {"check": str, "ok": bool, "detail": str, "fix": str}

``fix`` 在 ``ok`` 為真時為空字串。

依賴僅限 stdlib + :mod:`sw_core.sysenv` + :mod:`sw_core.constants` +
:mod:`sw_core.runtime_config`，不觸碰 daemon／socket。
"""

from __future__ import annotations

import os
import sys

from sw_core.constants import DEVICE_BY_ID_DIR
from sw_core.sysenv import SystemEffects


def _check_python() -> dict:
    """Python 版本是否 ≥ 3.10。"""
    ok = sys.version_info >= (3, 10)
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return {
        "check": "python",
        "ok": ok,
        "detail": version,
        "fix": "" if ok else "需 Python ≥ 3.10",
    }


def _check_pyyaml() -> dict:
    """PyYAML 是否可匯入（runtime_config／profiles 需要）。"""
    try:
        import yaml  # noqa: F401
        ok = True
        detail = getattr(yaml, "__version__", "已安裝")
    except Exception:  # pragma: no cover - 環境相依
        ok = False
        detail = "未安裝"
    return {
        "check": "pyyaml",
        "ok": ok,
        "detail": detail,
        "fix": "" if ok else "安裝 PyYAML（pipx 安裝會自動帶入）",
    }


def _check_on_path(fx, name: str) -> dict:
    """指定二進位是否在 PATH 上。"""
    path = fx.which(name)
    ok = path is not None
    return {
        "check": f"{name}_on_path",
        "ok": ok,
        "detail": path or "找不到",
        "fix": "" if ok else "確認 ~/.local/bin 在 PATH（pipx ensurepath）",
    }


def _check_dialout(fx) -> dict:
    """目前使用者是否屬於 dialout 群組（存取 /dev/ttyUSB* 必需）。"""
    ok = fx.user_in_group("dialout")
    return {
        "check": "dialout",
        "ok": ok,
        "detail": "已在 dialout 群組" if ok else "不在 dialout 群組",
        "fix": "" if ok else "sudo usermod -aG dialout $USER（之後重新登入）",
    }


def _check_systemd(fx) -> dict:
    """systemd 是否為 init（advisory：缺少不致命，可走 on-demand）。"""
    ok = fx.has_systemd()
    return {
        "check": "systemd",
        "ok": ok,
        "detail": "systemd 為 init" if ok else "未偵測到 systemd",
        "fix": "" if ok else (
            "WSL 可於 /etc/wsl.conf 設 [boot] systemd=true 後 wsl --shutdown"
            "（on-demand 模式可忽略）"
        ),
    }


def _check_supervision_mode(home) -> dict:
    """目前有效的監管模式（advisory，永遠 ok）。"""
    # 延遲匯入：避免 doctor 對 cli 形成匯入循環，且 mode 在呼叫時才解析。
    from sw_core.cli import _default_runtime_config

    mode = _default_runtime_config().mode() or "on-demand"
    return {"check": "supervision_mode", "ok": True, "detail": mode, "fix": ""}


def _check_devices() -> dict:
    """是否有 USB-serial 裝置出現在 by-id 目錄。"""
    detail = "0"
    ok = False
    try:
        if os.path.isdir(DEVICE_BY_ID_DIR):
            entries = os.listdir(DEVICE_BY_ID_DIR)
            ok = len(entries) > 0
            detail = str(len(entries))
    except Exception:  # pragma: no cover - 環境相依
        ok = False
        detail = "無法讀取"
    return {
        "check": "devices",
        "ok": ok,
        "detail": detail,
        "fix": "" if ok else (
            "確認 USB-serial 已接上且有 /dev/serial/by-id/* "
            "（或設 SERIALWRAP_BY_ID_DIR）"
        ),
    }


def _check_wsl_systemd(fx) -> dict:
    """WSL 環境下是否啟用 systemd（非 WSL 一律視為 ok）。"""
    is_wsl = fx.is_wsl()
    ok = (not is_wsl) or fx.has_systemd()
    if not is_wsl:
        detail = "非 WSL 環境"
    elif ok:
        detail = "WSL 已啟用 systemd"
    else:
        detail = "WSL 未啟用 systemd"
    return {
        "check": "wsl_systemd",
        "ok": ok,
        "detail": detail,
        "fix": "" if ok else (
            "於 /etc/wsl.conf 設 [boot] systemd=true 後 wsl --shutdown"
            "（on-demand 模式可忽略）"
        ),
    }


def run_doctor(fx=None, home=None) -> list[dict]:
    """執行所有環境檢查並回傳結果清單（每項皆唯讀、永不拋例外）。

    Args:
        fx:   effects 介面；``None`` 時用 :class:`SystemEffects`。
        home: 使用者家目錄（目前僅 supervision_mode 取用，保留供測試）。

    Returns:
        檢查結果清單，每項為
        ``{"check", "ok", "detail", "fix"}``。
    """
    fx = fx if fx is not None else SystemEffects()
    return [
        _check_python(),
        _check_pyyaml(),
        _check_on_path(fx, "serialwrap"),
        _check_on_path(fx, "serialwrapd"),
        _check_dialout(fx),
        _check_systemd(fx),
        _check_supervision_mode(home),
        _check_devices(),
        _check_wsl_systemd(fx),
    ]
