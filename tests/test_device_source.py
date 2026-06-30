from __future__ import annotations

from sw_core.device_source import exclude_bluetooth


def test_exclude_bluetooth_real_machine_case():
    # 實機：COM3/COM4=BthModem（藍牙）、COM8=CH340
    serialcomm = {
        r"\Device\BthModem0": "COM3",
        r"\Device\BthModem1": "COM4",
        r"\Device\Serial2": "COM8",
    }
    bt_ports = {"COM3", "COM4"}  # 由 BTHENUM PortName 收集
    kept = exclude_bluetooth(serialcomm, bt_ports, set())
    assert set(kept.keys()) == {"COM8"}


def test_exclude_bluetooth_by_value_name_heuristic():
    serialcomm = {r"\Device\BthModem9": "COM9", r"\Device\Serial0": "COM10"}
    kept = exclude_bluetooth(serialcomm, set(), set())  # 無 BTHENUM 資料時靠 value-name 兜底
    assert set(kept.keys()) == {"COM10"}


def test_exclude_bluetooth_manual_override():
    serialcomm = {r"\Device\Serial2": "COM8"}
    kept = exclude_bluetooth(serialcomm, set(), {"COM8"})
    assert kept == {}


def test_windows_device_source_scan(monkeypatch):
    """WindowsDeviceSource.scan() 應過濾藍牙埠、保留非藍牙埠，並建立正確 DeviceInfo。"""
    from sw_core import device_source as ds

    monkeypatch.setattr(ds, "_read_serialcomm", lambda: {
        r"\Device\BthModem0": "COM3",
        r"\Device\Serial2": "COM8",
    })
    monkeypatch.setattr(ds, "_read_bt_ports", lambda: {"COM3"})
    src = ds.WindowsDeviceSource()
    devices = src.scan()
    assert set(devices.keys()) == {"COM8"}
    assert devices["COM8"].real_path == r"\\.\COM8"
    assert devices["COM8"].by_id == "COM8"


def test_windows_device_source_exclude_coms(monkeypatch):
    """WindowsDeviceSource(exclude_coms={"COM8"}) 應把 _exclude_coms 傳進 exclude_bluetooth，
    使 scan() 結果不含 COM8（驗證 exclude_coms 確實生效）。"""
    from sw_core import device_source as ds

    monkeypatch.setattr(ds, "_read_serialcomm", lambda: {
        r"\Device\Serial2": "COM8",
        r"\Device\Serial3": "COM9",
    })
    monkeypatch.setattr(ds, "_read_bt_ports", lambda: set())
    src = ds.WindowsDeviceSource(exclude_coms={"COM8"})
    devices = src.scan()
    assert "COM8" not in devices
    assert "COM9" in devices
    assert devices["COM9"].by_id == "COM9"
