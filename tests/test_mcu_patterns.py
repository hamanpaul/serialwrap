import pytest
from sw_core.mcu_patterns import McuPattern, McuPatternRegistry


def test_default_registry_has_ti_cc26xx():
    reg = McuPatternRegistry.default()
    p = reg.get("ti-cc26xx")
    assert p.probe == b"\x55\x55"
    assert p.expect == b"\x00\xcc"
    assert p.baud == 115200
    assert p.non_destructive is True


def test_loader_rejects_non_reviewed_destructive_probe():
    bad = {"family": "evil", "probe": "aa55", "expect": "00cc",
           "baud": 115200, "timeout_ms": 500, "non_destructive": False}
    with pytest.raises(ValueError, match="non_destructive"):
        McuPattern.from_dict(bad)


def test_direct_constructor_also_rejects_destructive():
    # 守衛在 __post_init__，直接建構也擋（不只 from_dict）
    with pytest.raises(ValueError, match="non_destructive"):
        McuPattern(family="bypass", probe=b"\xaa", expect=b"\xcc",
                   baud=115200, timeout_ms=500, non_destructive=False)


def test_load_appends_custom_family():
    reg = McuPatternRegistry.load([
        {"family": "acme-foo", "probe": "7f7f", "expect": "0006",
         "baud": 57600, "timeout_ms": 300, "non_destructive": True},
    ])
    families = {p.family for p in reg.all()}
    assert families == {"ti-cc26xx", "acme-foo"}
    assert reg.get("acme-foo").baud == 57600


def test_get_unknown_family_raises_keyerror():
    with pytest.raises(KeyError):
        McuPatternRegistry.default().get("does-not-exist")


def test_render_support_list_lists_families():
    reg = McuPatternRegistry.default()
    text = reg.render_support_list(candidates=[])
    assert "ti-cc26xx" in text
    assert "55 55" in text.lower() or "0x55" in text.lower()


def test_render_support_list_includes_candidates():
    reg = McuPatternRegistry.default()
    text = reg.render_support_list(candidates=[
        {"com": "COM0", "by_id": "usb-FTDI", "real_path": "/dev/ttyUSB1"},
    ])
    assert "COM0" in text and "/dev/ttyUSB1" in text
