"""testbed.yaml 與 config.json 雙來源等價單測（openspec 5.3）——不 import testpilot。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "reliability"))

from serialwrap_reliability import testbed_loader  # noqa: E402


CONFIG_JSON = {
    "_readme": ["底線鍵＝註解，loader 必須丟棄"],
    "boards": [
        {
            "com": "COM0",
            "alias": "dut-prpl",
            "serial": "AC01QZT0",
            "busid": "8-1",
            "platform": "prpl",
        },
        {
            "com": "COM1",
            "alias": "sta-prpl",
            "serial": "AQ00OAQ7",
            "busid": "8-2",
            "platform": "brcm",
            "profile": "brcm-template",
        },
    ],
    "usbipd_exe": "/mnt/c/Program Files/usbipd-win/usbipd.exe",
    "win_serialwrap_exe": "/mnt/c/serialwrap/serialwrap.exe",
    "tmux_prefix": "realhw",
    "timeouts": {"ready_wait_s": 180, "reboot_wait_s": 300, "human_active_window_s": 60},
    "longrun": {"snapshot_interval_s": 300, "agent_workers": 4},
}

TESTBED_RAW = {
    "testbed": {
        "name": "serialwrap-reliability-bench",
        "run_backend": "serialwrap",
        "devices": {
            "STA": {
                "role": "sta",
                "transport": "serialwrap",
                "selector": "COM1",
                "alias": "sta-prpl",
                "serial": "AQ00OAQ7",
                "busid": "8-2",
                "platform": "brcm",
                "profile": "brcm-template",
            },
            "DUT": {
                "role": "dut",
                "transport": "serialwrap",
                "selector": "COM0",
                "alias": "dut-prpl",
                "serial": "AC01QZT0",
                "busid": "8-1",
                "platform": "prpl",
            },
        },
        "variables": {},
        "serialwrap_reliability": {
            "usbipd_exe": "/mnt/c/Program Files/usbipd-win/usbipd.exe",
            "win_serialwrap_exe": "/mnt/c/serialwrap/serialwrap.exe",
            "tmux_prefix": "realhw",
            "timeouts": {
                "ready_wait_s": 180,
                "reboot_wait_s": 300,
                "human_active_window_s": 60,
            },
            "longrun": {"duration": "15m", "snapshot_interval_s": 300, "agent_workers": 4},
        },
    }
}


def test_two_sources_equivalent(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(CONFIG_JSON, ensure_ascii=False), encoding="utf-8")
    cfg_json = testbed_loader.config_json_to_cfg(p, duration="15m")
    cfg_yaml = testbed_loader.testbed_to_cfg(TESTBED_RAW)
    assert cfg_json == cfg_yaml


def test_config_json_reorders_boards_to_match_testbed_equivalence(tmp_path):
    p = tmp_path / "config.json"
    reversed_cfg = dict(CONFIG_JSON)
    reversed_cfg["boards"] = list(reversed(CONFIG_JSON["boards"]))
    p.write_text(json.dumps(reversed_cfg, ensure_ascii=False), encoding="utf-8")
    cfg_json = testbed_loader.config_json_to_cfg(p, duration="15m")
    assert [b["com"] for b in cfg_json["boards"]] == ["COM0", "COM1"]
    assert cfg_json == testbed_loader.testbed_to_cfg(TESTBED_RAW)


def test_testbed_boards_sorted_by_selector():
    cfg = testbed_loader.testbed_to_cfg(TESTBED_RAW)
    assert [b["com"] for b in cfg["boards"]] == ["COM0", "COM1"]
    assert cfg["boards"][1]["profile"] == "brcm-template"


def test_selector_natural_sort_handles_com10():
    raw = {
        "testbed": {
            "devices": {
                "A": {"selector": "COM10", "alias": "ten"},
                "B": {"selector": "COM2", "alias": "two"},
                "C": {"selector": "COM1", "alias": "one"},
            },
            "serialwrap_reliability": {},
        }
    }
    cfg = testbed_loader.testbed_to_cfg(raw)
    assert [b["com"] for b in cfg["boards"]] == ["COM1", "COM2", "COM10"]


def test_testbed_duration_converted_and_stripped():
    cfg = testbed_loader.testbed_to_cfg(TESTBED_RAW)
    assert cfg["duration_s"] == 900
    assert "duration" not in cfg["longrun"]


def test_win_serialwrap_exe_passthrough():
    cfg = testbed_loader.testbed_to_cfg(TESTBED_RAW)
    assert cfg["win_serialwrap_exe"] == "/mnt/c/serialwrap/serialwrap.exe"


def test_config_json_drops_comment_keys(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(CONFIG_JSON, ensure_ascii=False), encoding="utf-8")
    cfg = testbed_loader.config_json_to_cfg(p)
    assert not any(k.startswith("_") for k in cfg)
    assert "duration_s" not in cfg


def test_load_testbed_cfg_from_yaml_file(tmp_path):
    p = tmp_path / "testbed.yaml"
    p.write_text(
        """
testbed:
  name: serialwrap-reliability-bench
  run_backend: serialwrap
  devices:
    STA:
      role: sta
      transport: serialwrap
      selector: COM1
      alias: sta-prpl
      serial: AQ00OAQ7
      busid: 8-2
      platform: brcm
      profile: brcm-template
    DUT:
      role: dut
      transport: serialwrap
      selector: COM0
      alias: dut-prpl
      serial: AC01QZT0
      busid: 8-1
      platform: prpl
  variables: {}
  serialwrap_reliability:
    usbipd_exe: /mnt/c/Program Files/usbipd-win/usbipd.exe
    win_serialwrap_exe: /mnt/c/serialwrap/serialwrap.exe
    tmux_prefix: realhw
    timeouts:
      ready_wait_s: 180
      reboot_wait_s: 300
      human_active_window_s: 60
    longrun:
      duration: 15m
      snapshot_interval_s: 300
      agent_workers: 4
""".lstrip(),
        encoding="utf-8",
    )
    assert testbed_loader.load_testbed_cfg(p) == testbed_loader.testbed_to_cfg(TESTBED_RAW)


def test_loader_uses_public_realhw_load_cfg(monkeypatch, tmp_path):
    import realhw

    calls: list[tuple[object, object]] = []

    def fake_load_cfg(config_path=None, *, injected=None):
        calls.append((config_path, injected))
        if injected is not None:
            return {
                "boards": injected["boards"],
                "usbipd_exe": injected.get("usbipd_exe", ""),
                "win_serialwrap_exe": injected.get("win_serialwrap_exe", ""),
                "tmux_prefix": injected.get("tmux_prefix", "realhw"),
                "timeouts": injected.get("timeouts", {}),
                "longrun": injected.get("longrun", {}),
            }
        return {
            "boards": CONFIG_JSON["boards"],
            "usbipd_exe": CONFIG_JSON["usbipd_exe"],
            "win_serialwrap_exe": CONFIG_JSON["win_serialwrap_exe"],
            "tmux_prefix": CONFIG_JSON["tmux_prefix"],
            "timeouts": CONFIG_JSON["timeouts"],
            "longrun": CONFIG_JSON["longrun"],
        }

    monkeypatch.setattr(realhw, "load_cfg", fake_load_cfg)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(CONFIG_JSON, ensure_ascii=False), encoding="utf-8")
    testbed_loader.testbed_to_cfg(TESTBED_RAW)
    testbed_loader.config_json_to_cfg(p, duration="15m")
    assert calls[0][1] is not None
    assert calls[1][0] == p
