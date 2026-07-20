"""serialwrap_reliability 核心邏輯——只提供 repo root 與 realhw bootstrap。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT: Path = Path(__file__).resolve().parents[2]


def ensure_realhw_importable() -> Path:
    """把 repo root 冪等插入 ``sys.path``，讓 ``import realhw`` 可用。"""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return REPO_ROOT


def load_registry() -> list[Any]:
    """載入 realhw registry，回傳淺拷貝。"""
    ensure_realhw_importable()
    import realhw.cases  # noqa: F401
    from realhw import harness

    return list(harness.REGISTRY)
