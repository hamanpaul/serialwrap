"""DeviceSource 介面、POSIX 實作與藍牙排除 pure function。

此模組提供：
- ``DeviceSource``：Protocol，定義 ``scan() -> dict[str, DeviceInfo]`` 介面。
- ``PosixDeviceSource``：搬自 ``DeviceWatcher._scan``，掃描 /dev/serial/by-id（+by-path），
  行為與原實作等價（seen_real 去重、by-id 優先）。
- ``WindowsDeviceSource``：Windows 原生 COM 列舉（SERIALCOMM 登錄表）＋雙重藍牙排除（BTHENUM PortName + bthmodem 啟發式兜底）＋手動排除清單（#84 PORT-4）。
- ``exclude_bluetooth``：pure function，從 SERIALCOMM 登錄表列舉剔除藍牙埠與手動排除。
"""
from __future__ import annotations

import os
from typing import Protocol

from sw_core.device_watcher import DeviceInfo


class DeviceSource(Protocol):
    """裝置來源協定：統一 scan() 介面。"""

    def scan(self) -> dict[str, DeviceInfo]:
        """掃描並回傳目前可用裝置。key 為裝置路徑（by-id 或 COMx），value 為 DeviceInfo。"""
        ...


class PosixDeviceSource:
    """掃 /dev/serial/by-id（+by-path）。

    由 ``DeviceWatcher._scan`` 搬入，行為不變：
    - 多目錄依序掃描，同一 real_path 只保留第一筆（by-id 優先於 by-path）。
    - 按檔名排序，確保結果穩定。
    """

    def __init__(self, scan_dirs: list[str]) -> None:
        self._scan_dirs = scan_dirs

    def scan(self) -> dict[str, DeviceInfo]:
        """掃描所有 scan_dirs，回傳 {symlink_path: DeviceInfo}。"""
        out: dict[str, DeviceInfo] = {}
        seen_real: set[str] = set()
        for scan_dir in self._scan_dirs:
            if not os.path.isdir(scan_dir):
                continue
            for name in sorted(os.listdir(scan_dir)):
                path = os.path.join(scan_dir, name)
                if not os.path.exists(path):
                    continue
                real_path = os.path.realpath(path)
                if real_path in seen_real:
                    continue
                seen_real.add(real_path)
                out[path] = DeviceInfo(by_id=path, real_path=real_path)
        return out


def _read_serialcomm() -> dict[str, str]:
    """讀 HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM，回傳 {device_path: COMname}。

    ``winreg`` 延遲 import，使模組在非 Windows 仍可正常 import；
    測試可透過 monkeypatch 覆蓋此函式。
    """
    import winreg  # noqa: PLC0415

    out: dict[str, str] = {}
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except FileNotFoundError:
        return out
    try:
        i = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            out[name] = value
            i += 1
    finally:
        winreg.CloseKey(key)
    return out


def _read_bt_ports() -> set[str]:
    """遞迴掃 HKLM\\SYSTEM\\CurrentControlSet\\Enum\\BTHENUM 下各裝置的
    ``Device Parameters\\PortName``，收集所有藍牙 COM 名稱。

    ``winreg`` 延遲 import，使模組在非 Windows 仍可正常 import；
    測試可透過 monkeypatch 覆蓋此函式。
    """
    import winreg  # noqa: PLC0415

    ports: set[str] = set()

    def walk(root: int, path: str) -> None:
        try:
            key = winreg.OpenKey(root, path)
        except OSError:
            return
        try:
            # 本層若有 Device Parameters\PortName 則收集
            try:
                pp = winreg.OpenKey(root, path + r"\Device Parameters")
                try:
                    val, _ = winreg.QueryValueEx(pp, "PortName")
                    if isinstance(val, str):
                        ports.add(val)
                except OSError:
                    pass
                finally:
                    winreg.CloseKey(pp)
            except OSError:
                pass
            # 遞迴子鍵
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                walk(root, path + "\\" + sub)
                i += 1
        finally:
            winreg.CloseKey(key)

    walk(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum\BTHENUM")
    return ports


class WindowsDeviceSource:
    """Windows 原生 COM 列舉（SERIALCOMM 登錄表）＋藍牙排除（#84 PORT-4）。

    掃描流程：
    1. ``_read_serialcomm()`` 取得所有 COM port 登錄條目。
    2. ``_read_bt_ports()`` 取得 BTHENUM 下的藍牙 COM 集合。
    3. ``exclude_bluetooth()`` 剔除藍牙埠與 ``_exclude_coms`` 手動排除清單。
    4. 回傳 ``{COMname: DeviceInfo(by_id=COMname, real_path=r"\\\\.\\\\ COMname")}``。
    """

    def __init__(self, exclude_coms: set[str] | None = None) -> None:
        self._exclude_coms: set[str] = exclude_coms or set()

    def scan(self) -> dict[str, DeviceInfo]:
        """掃描 Windows COM port，剔除藍牙埠後回傳可用裝置字典。"""
        serialcomm = _read_serialcomm()
        bt_ports = _read_bt_ports()
        kept = exclude_bluetooth(serialcomm, bt_ports, self._exclude_coms)
        return {
            com: DeviceInfo(by_id=com, real_path=rf"\\.\{com}")
            for com in kept
        }


def exclude_bluetooth(
    serialcomm: dict[str, str],
    bt_ports: set[str],
    exclude: set[str],
) -> dict[str, str]:
    """從 SERIALCOMM 登錄表列舉剔除藍牙埠與手動排除，回傳保留的 ``{COMname: COMname}``。

    Args:
        serialcomm: SERIALCOMM 登錄值，格式為 ``{device_path: COMname}``，
                    例如 ``{r"\\Device\\BthModem0": "COM3"}``。
        bt_ports:   由 BTHENUM PortName 收集到的藍牙 COM 名集合（第一道過濾）。
        exclude:    config ``windows.exclude_coms`` 手動排除清單（第三道過濾）。

    Returns:
        保留下來的 ``{COMname: COMname}``，藍牙與手動排除項目已剔除。

    判據（任一成立即剔除）：
        1. COM 名在 ``bt_ports``（BTHENUM PortName 明確標記）。
        2. device_path value-name 含 ``'bthmodem'``（小寫比對，兜底啟發式）。
        3. COM 名在 ``exclude``（config 手動排除）。
    """
    kept: dict[str, str] = {}
    for device_path, com in serialcomm.items():
        if com in bt_ports or com in exclude:
            continue
        if "bthmodem" in device_path.lower():
            continue
        kept[com] = com
    return kept
