"""OD-56（2026-10-29，rulings/p7.md P7-BO-1）：s21_core.linux 兩個語意已變的舊 nodeid 由替換測試承擔，移除三元組須逐名
出現在 CI 的 `regen_lock.py --check --base … --allow-removal` 清單；只限這兩名，windows 集合不得一併豁免（P7-LW-1）。"""
from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

TRIPLES = (
    "s21_core:linux:cargo:sw-core::login_quiet::unsupported_profile_no_auto_tx",
    "s21_core:linux:cargo:sw-core::session_manager::detect_skips_passthrough_console_unlock_and_unsupported",
)


def _allowed() -> list[str]:
    wf = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8"))
    steps = [st for st in wf["jobs"]["pytest"]["steps"] if "regen_lock.py --check --base" in str(st.get("run", ""))]
    assert len(steps) == 1
    tokens = shlex.split(str(steps[0]["run"]))
    return tokens[tokens.index("--allow-removal") + 1:]


def test_od56_p7_bo1_triples_allowed_linux_only():
    allowed = _allowed()
    assert [t for t in TRIPLES if t not in allowed] == []
    windows = [t.replace(":linux:", ":windows:") for t in TRIPLES]
    assert [t for t in windows if t in allowed] == [], "windows 集合待原生一對一替換，不得豁免"
    # 替換測試仍在 s21_core.linux（不是單純刪除）
    import json
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    linux = set(lock["sets"]["s21_core"]["linux"]["nodeids"])
    assert {"cargo:sw-core::login_quiet::post_login_cmd_profile_logs_in",
            "cargo:sw-core::session_manager::detect_skips_passthrough_and_console_unlock"} <= linux
