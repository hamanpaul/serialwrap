"""#122 drivers 純函式單測（不碰 live）。"""
from __future__ import annotations

from realhw import drivers

USBIPD_LIST = """Connected:
BUSID  VID:PID    DEVICE                        STATE
8-1    0403:6001  USB Serial Converter          Attached
8-2    0403:6001  USB Serial Converter          Attached

Persisted:
GUID  DEVICE
"""


def test_parse_usbipd_list_maps_busids():
    got = drivers.parse_usbipd_list(USBIPD_LIST)
    assert got == ["8-1", "8-2"]


def test_strip_ansi():
    assert drivers.strip_ansi("a\x1b[31mred\x1b[0mb\x1b(B") == "aredb"


def test_find_marker_ignores_ansi_and_wraps():
    pane = "prompt$ echo MARK_42\r\n\x1b[1mMARK_42\x1b[0m\r\nprompt$"
    assert drivers.find_marker(pane, "MARK_42")
    assert not drivers.find_marker(pane, "MARK_99")
