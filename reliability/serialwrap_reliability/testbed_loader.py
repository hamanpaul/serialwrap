"""testbed.yaml（plugin）與 config.json（standalone）雙來源 → 同形 realhw cfg dict。

等價律（openspec 5.3）：兩條路都經 ``realhw.load_cfg`` 單一正規化，
本模組只負責把 testbed facts 轉回 realhw cfg 所需形狀。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from serialwrap_reliability.core import ensure_realhw_importable

_BOARD_OPTIONAL_KEYS: tuple[str, ...] = ("alias", "serial", "busid", "platform", "profile")
_SECTION_KEYS: tuple[str, ...] = ("usbipd_exe", "win_serialwrap_exe", "tmux_prefix", "timeouts")


def _selector_sort_key(selector: str) -> tuple[str, int]:
    text = str(selector or "")
    match = re.match(r"^([A-Za-z]+)(\d+)$", text)
    if match:
        return (match.group(1).upper(), int(match.group(2)))
    return (text.upper(), 0)


def _sort_boards(boards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(boards, key=lambda board: _selector_sort_key(str(board.get("com", ""))))


def testbed_to_cfg(raw: dict[str, Any]) -> dict[str, Any]:
    """TestbedConfig.raw（整份 YAML dict）→ realhw cfg dict。"""
    ensure_realhw_importable()
    import realhw

    testbed = raw.get("testbed", raw) or {}
    section = dict(testbed.get("serialwrap_reliability") or {})

    devices = testbed.get("devices") or {}
    boards: list[dict[str, Any]] = []
    for _, dev in sorted(
        devices.items(), key=lambda kv: _selector_sort_key(str((kv[1] or {}).get("selector", "")))
    ):
        dev = dev or {}
        board: dict[str, Any] = {"com": str(dev.get("selector", ""))}
        for key in _BOARD_OPTIONAL_KEYS:
            if key in dev:
                board[key] = dev[key]
        boards.append(board)

    facts: dict[str, Any] = {"boards": boards}
    for key in _SECTION_KEYS:
        if key in section:
            facts[key] = section[key]

    longrun = dict(section.get("longrun") or {})
    duration = longrun.pop("duration", None)
    if longrun or "longrun" in section:
        facts["longrun"] = longrun

    cfg = realhw.load_cfg(injected=facts)
    cfg["boards"] = _sort_boards(list(cfg.get("boards", [])))
    if duration:
        from realhw import harness

        cfg["duration_s"] = harness.parse_duration(str(duration))
    return cfg


def config_json_to_cfg(path: Path | str, *, duration: str | None = None) -> dict[str, Any]:
    """standalone config.json → cfg。"""
    ensure_realhw_importable()
    import realhw
    from realhw import harness

    cfg = {
        key: value
        for key, value in realhw.load_cfg(Path(path)).items()
        if not str(key).startswith("_")
    }
    cfg["boards"] = _sort_boards(list(cfg.get("boards", [])))
    if duration:
        cfg["duration_s"] = harness.parse_duration(duration)
    return cfg


def load_testbed_cfg(path: Path | str) -> dict[str, Any]:
    """讀 testbed.yaml 檔 → cfg（PyYAML 延遲 import）。"""
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return testbed_to_cfg(raw)
