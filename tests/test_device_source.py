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
