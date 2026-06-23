"""sw_core/service_ctl.py — serialwrap service 子命令後端（Task 9）。

透過 Effects boundary 包裝 systemctl，按監管模式決定是否需要 sudo。
sudo 邊界：
    - systemd-user  — 永不 sudo（user session 有足夠權限）
    - systemd-system
        - status     唯讀，免 sudo
        - start/stop/restart 改變狀態，需 root
            - with_sudo=False（預設）→ 不執行，回 hint
            - with_sudo=True         → 以 sudo 執行
    - on-demand / 其他 → 不呼叫 systemctl，回說明訊息
"""
from __future__ import annotations

from sw_core.sysenv import SystemEffects

_UNIT = "serialwrap"
_PRIVILEGED = {"start", "stop", "restart"}  # 改變狀態者；system 模式需 root


def service_action(action: str, *, mode: str, fx=None, with_sudo: bool = False) -> dict:
    """執行 systemctl action 並回傳結果字典。

    參數：
        action     — start | stop | restart | status
        mode       — supervision 模式（systemd-user / systemd-system / on-demand）
        fx         — Effects 實作（None 時使用 SystemEffects）
        with_sudo  — system 模式特權指令是否以 sudo 執行（預設 False）

    回傳 dict（必有 ok: bool）：
        ok=True  → rc, stdout, stderr
        ok=False → error_code, hint
    """
    fx = fx if fx is not None else SystemEffects()

    if mode == "systemd-user":
        rc, out, err = fx.run(["systemctl", "--user", action, _UNIT])
        return {"ok": rc == 0, "mode": mode, "action": action, "rc": rc, "stdout": out, "stderr": err}

    if mode == "systemd-system":
        base = ["systemctl", action, _UNIT]
        if action in _PRIVILEGED and not with_sudo:
            return {
                "ok": False,
                "mode": mode,
                "action": action,
                "error_code": "NEEDS_SUDO",
                "hint": "需 root：請執行 `sudo " + " ".join(base) + "`（或加 --with-sudo 讓本指令代跑）",
            }
        cmd = (["sudo"] + base) if (action in _PRIVILEGED and with_sudo) else base
        rc, out, err = fx.run(cmd)
        return {"ok": rc == 0, "mode": mode, "action": action, "rc": rc, "stdout": out, "stderr": err}

    # on-demand / 未知模式
    return {
        "ok": False,
        "mode": mode,
        "action": action,
        "error_code": "NO_SYSTEMD",
        "hint": "目前為 on-demand 監管模式（無 systemd 服務）。使用 `serialwrap daemon start/stop`。",
    }
