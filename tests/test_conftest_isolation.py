"""#120 conftest 三層防線釘測。

純 pytest 裸函式風格：`python3 -m unittest discover` 仍會 import 本檔（不得炸），
但不收集裸函式——本檔的斷言只在 pytest（conftest 已載入）下有意義。
"""
from __future__ import annotations

import os

_ISO_DIR_KEYS = (
    "SERIALWRAP_STATE_DIR",
    "SERIALWRAP_RUN_DIR",
    "SERIALWRAP_WAL_DIR",
    "SERIALWRAP_CONFIG_DIR",
    "SERIALWRAP_LOG_DIR",
    "SERIALWRAP_EVENTS_DIR",
    "SERIALWRAP_EVENTS_RUNTIME_DIR",
    "SERIALWRAP_BY_ID_DIR",
    "SERIALWRAP_BY_PATH_DIR",
)

_POPPED_KEYS = (
    "SERIALWRAP_ENDPOINT",
    "SERIALWRAP_PROFILE_DIR",
    "SERIALWRAP_TTYMCU_PATH",
    "SERIALWRAP_EVENTS_LOG_PATH",
    "SERIALWRAP_DATA_DIR",
)


def test_nine_iso_dir_envs_point_into_iso_root():
    """(a) 9 個目錄 env 皆存在且指向 conftest 的隔離 tmpdir。"""
    for key in _ISO_DIR_KEYS:
        assert key in os.environ, f"{key} 未被 conftest 第 1 層設定"
        assert "sw-pytest-iso-" in os.environ[key], (
            f"{key}={os.environ[key]!r} 未指向隔離目錄（外層 shell 穿透？）"
        )


def test_five_high_priority_envs_are_popped():
    """(b) 5 個優先序高於目錄推導的 env 已被 pop——外層 export 不得穿透。"""
    for key in _POPPED_KEYS:
        assert key not in os.environ, f"{key} 未被 pop——外層 shell export 可穿透隔離"


def test_autouse_fixture_patches_state_path(tmp_path):
    """(c) autouse fixture 已把 session_manager.STATE_PATH patch 到本測試的 tmp_path。"""
    import sw_core.session_manager as sm

    assert sm.STATE_PATH == str(tmp_path / "state.json")
