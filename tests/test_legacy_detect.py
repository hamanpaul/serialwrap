"""Task 12 — legacy ~/.paul_tools 安裝偵測的單元測試。

驗證重點：
- 無 legacy 安裝時回 None。
- 偵測到 ~/.paul_tools/serialwrap 時回報 path/serialwrap 與退役指引。
"""


def test_detect_legacy_install_none_when_absent(tmp_path):
    from sw_core.setup_cmd import detect_legacy_install
    assert detect_legacy_install(home=tmp_path) is None


def test_detect_legacy_install_found(tmp_path):
    from sw_core.setup_cmd import detect_legacy_install
    pt = tmp_path / ".paul_tools"; pt.mkdir()
    (pt / "serialwrap").write_text("#!/bin/sh\n")
    info = detect_legacy_install(home=tmp_path)
    assert info and info["serialwrap"] is True
    assert "paul_tools" in info["path"]
