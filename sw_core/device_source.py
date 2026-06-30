"""DeviceSource 介面、POSIX 實作與藍牙排除 pure function。

此模組提供：
- ``DeviceSource``：Protocol，定義 ``scan() -> dict[str, DeviceInfo]`` 介面。
- ``PosixDeviceSource``：搬自 ``DeviceWatcher._scan``，掃描 /dev/serial/by-id（+by-path），
  行為與原實作等價（seen_real 去重、by-id 優先）。
- ``WindowsDeviceSource``：Task 7 補完列舉細節（佔位宣告）。
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


class WindowsDeviceSource:
    """Windows 裝置來源（Task 7 補完列舉細節）。"""

    def __init__(self, exclude_coms: set[str] | None = None) -> None:
        self._exclude_coms: set[str] = exclude_coms or set()

    def scan(self) -> dict[str, DeviceInfo]:
        """Task 7 實作；目前回傳空字典（佔位）。"""
        return {}


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
