"""Manifest 與私有目錄輔助函式。"""

from __future__ import annotations

import json
import os
import pathlib
import platform
import subprocess
import tempfile
from typing import Any


def ensure_private_dir(path: pathlib.Path) -> None:
    """建立私有目錄，POSIX 下設權限 0o700。"""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)


def write_json_atomic(path: pathlib.Path, obj: dict[str, Any]) -> None:
    """原子化寫入 JSON 檔，POSIX 下設權限 0o600。"""
    content = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    target_dir = path.parent
    ensure_private_dir(target_dir)
    fd, tmp_path = tempfile.mkstemp(dir=str(target_dir), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if os.name != "nt":
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, str(path))
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


_UNKNOWN = "unknown"
_QUERY_TIMEOUT_S = 15.0


def _powershell(command: str) -> str:
    """以唯讀 PowerShell 查詢取得 stdout（去頭尾空白）；失敗一律 raise，由呼叫端記 unknown。"""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=_QUERY_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        raise OSError(f"powershell 查詢失敗 rc={proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _read_winreg_build() -> str:
    """讀 registry `CurrentBuild.UBR`（Windows 專用；其他平台 import winreg 即失敗）。"""
    import winreg  # noqa: PLC0415  # Windows 限定

    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
        build = winreg.QueryValueEx(key, "CurrentBuild")[0]
        ubr = winreg.QueryValueEx(key, "UBR")[0]
    return f"{build}.{ubr}"


def _query_windows_build() -> str:
    ver = _powershell("cmd /c ver")
    return f"{ver} ({_read_winreg_build()})"


def _query_secure_boot() -> bool:
    out = _powershell("Confirm-SecureBootUEFI").lower()
    if out not in {"true", "false"}:
        raise ValueError(f"Confirm-SecureBootUEFI 輸出無法判讀：{out!r}")
    return out == "true"


def _query_hvci() -> bool:
    out = _powershell(
        "(Get-CimInstance -ClassName Win32_DeviceGuard "
        "-Namespace root\\Microsoft\\Windows\\DeviceGuard).SecurityServicesRunning -join ','"
    )
    if not out:
        raise ValueError("Win32_DeviceGuard 查無 SecurityServicesRunning")
    return "2" in [part.strip() for part in out.split(",")]


def _query_endpoint_av() -> dict[str, Any]:
    out = _powershell(
        "Get-CimInstance -Namespace root\\SecurityCenter2 -ClassName AntiVirusProduct "
        "| Select-Object displayName,productState | ConvertTo-Json -Compress"
    )
    if not out:
        raise ValueError("AntiVirusProduct 查無資料")
    data = json.loads(out)
    items = data if isinstance(data, list) else [data]
    names = [str(i.get("displayName")) for i in items if i.get("displayName")]
    states = [str(i.get("productState")) for i in items if i.get("productState") is not None]
    if not names:
        raise ValueError("AntiVirusProduct 缺 displayName")
    return {"product": ",".join(names), "state": ",".join(states) or _UNKNOWN}


def platform_info(
    *,
    endpoint: str | None = None,
    com_driver: dict[str, Any] | None = None,
    com_driver_install_method: str | None = None,
) -> dict[str, Any]:
    """收集平台環境資訊。

    Windows 另以唯讀查詢記錄 build、Secure Boot、HVCI、端點防毒（§1.1.1、§1.2、§15.8.3）；
    任何一項查詢失敗或權限不足一律記字串 `"unknown"` 並把欄位名加入 `evidence_insufficient`，
    不得以 0／False 冒充。`com_driver` 只在 `endpoint == "win-serial"` 時填寫。
    """
    insufficient: list[str] = []
    info: dict[str, Any] = {
        "os": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "evidence_insufficient": insufficient,
    }
    if info["os"] != "Windows":
        return info

    def _probe(field: str, fn: Any, unknown: Any = _UNKNOWN) -> Any:
        try:
            return fn()
        except Exception:  # noqa: BLE001  # 查詢失敗即證據不足，不得中斷 bridge
            insufficient.append(field)
            return unknown

    info["windows_build"] = _probe("windows_build", _query_windows_build)
    info["secure_boot"] = _probe("secure_boot", _query_secure_boot)
    info["hvci"] = _probe("hvci", _query_hvci)
    info["endpoint_av"] = _probe(
        "endpoint_av", _query_endpoint_av, {"product": _UNKNOWN, "state": _UNKNOWN}
    )
    if endpoint == "win-serial":
        driver = {"name": _UNKNOWN, "version": _UNKNOWN, "sha256": _UNKNOWN}
        driver.update({k: v for k, v in (com_driver or {}).items() if v not in (None, "")})
        driver["install_method"] = com_driver_install_method or _UNKNOWN
        if any(driver[k] == _UNKNOWN for k in ("name", "version", "sha256", "install_method")):
            insufficient.append("com_driver")
        info["com_driver"] = driver
    return info


def bridge_manifest(
    *,
    run_id: str,
    case_id: str,
    tool: dict[str, Any],
    transport: dict[str, Any],
    dut: dict[str, Any],
    evidence: dict[str, Any],
    platform_info: dict[str, Any],
    started_at: str,
    closed_at: str | None,
    closed_cleanly: bool,
    failure: str | None,
) -> dict[str, Any]:
    """組裝 bridge-manifest.v1 根物件。"""
    return {
        "schema_version": "bridge-manifest.v1",
        "run_id": run_id,
        "case_id": case_id,
        "tool": tool,
        "transport": transport,
        "dut": dut,
        "evidence": evidence,
        "platform_info": platform_info,
        "started_at": started_at,
        "closed_at": closed_at,
        "closed_cleanly": closed_cleanly,
        "failure": failure,
    }
