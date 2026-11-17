from __future__ import annotations

from sw_core.device_source import exclude_bluetooth
import pytest

pytestmark = [pytest.mark.memory_only]


@pytest.mark.core_portable
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


@pytest.mark.core_portable
def test_exclude_bluetooth_by_value_name_heuristic():
    serialcomm = {r"\Device\BthModem9": "COM9", r"\Device\Serial0": "COM10"}
    kept = exclude_bluetooth(serialcomm, set(), set())  # 無 BTHENUM 資料時靠 value-name 兜底
    assert set(kept.keys()) == {"COM10"}


@pytest.mark.core_portable
def test_exclude_bluetooth_manual_override():
    serialcomm = {r"\Device\Serial2": "COM8"}
    kept = exclude_bluetooth(serialcomm, set(), {"COM8"})
    assert kept == {}


@pytest.mark.core_portable
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


@pytest.mark.core_portable
def test_windows_device_source_bt_cache(monkeypatch):
    """TTL 快取：第二次 scan 在 TTL 內不重呼叫 _read_bt_ports（Copilot review fix）。"""
    from sw_core import device_source as ds

    monkeypatch.setattr(ds, "_read_serialcomm", lambda: {r"\Device\Serial2": "COM8"})
    call_count = 0

    def counting_read_bt_ports():
        nonlocal call_count
        call_count += 1
        return set()

    monkeypatch.setattr(ds, "_read_bt_ports", counting_read_bt_ports)
    src = ds.WindowsDeviceSource()

    src.scan()  # 首次：應呼叫 _read_bt_ports
    assert call_count == 1

    src.scan()  # TTL 內：不應重呼叫
    assert call_count == 1, "TTL 內第二次 scan 不應重呼叫 _read_bt_ports"

    # 模擬 TTL 過期（強制讓快取失效）
    src._bt_cache = (src._bt_cache[0] - ds.WindowsDeviceSource._BT_TTL - 1.0, set())
    src.scan()  # TTL 逾期：應重呼叫
    assert call_count == 2, "TTL 逾期後應重呼叫 _read_bt_ports"


@pytest.mark.core_portable
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


@pytest.mark.posix_only
def test_posix_by_path_dedup_and_sort(tmp_path):
    """by-id 優先、相同 real path 去重，且各掃描目錄依檔名排序。"""
    from sw_core.device_source import PosixDeviceSource

    by_id = tmp_path / "by-id"
    by_path = tmp_path / "by-path"
    by_id.mkdir()
    by_path.mkdir()
    targets = {name: tmp_path / name for name in ("tty-a", "tty-b", "tty-c", "tty-d")}
    for target in targets.values():
        target.write_bytes(b"")

    # 以反向建立順序確認掃描結果由檔名排序，而非目錄列舉順序決定。
    (by_id / "z-device").symlink_to(targets["tty-b"])
    (by_id / "a-device").symlink_to(targets["tty-a"])
    (by_path / "z-duplicate").symlink_to(targets["tty-b"])
    (by_path / "n-unique").symlink_to(targets["tty-d"])
    (by_path / "a-duplicate").symlink_to(targets["tty-a"])
    (by_path / "m-unique").symlink_to(targets["tty-c"])

    devices = PosixDeviceSource([str(by_id), str(by_path)]).scan()

    assert list(devices) == [
        str(by_id / "a-device"),
        str(by_id / "z-device"),
        str(by_path / "m-unique"),
        str(by_path / "n-unique"),
    ]
    assert devices[str(by_id / "a-device")].real_path == str(targets["tty-a"])
    assert devices[str(by_id / "z-device")].real_path == str(targets["tty-b"])
    assert devices[str(by_path / "m-unique")].real_path == str(targets["tty-c"])
    assert devices[str(by_path / "n-unique")].real_path == str(targets["tty-d"])
