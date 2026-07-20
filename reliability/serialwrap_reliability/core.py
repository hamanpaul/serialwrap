"""serialwrap_reliability 核心邏輯——只提供 repo root 與 realhw bootstrap。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def resolve_repo_root(core_file: Path) -> Path:
    """由模組檔位置推回 repo root；僅支援 editable 安裝。"""
    root = Path(core_file).resolve().parents[2]
    if not (root / "realhw" / "harness.py").is_file():
        raise RuntimeError(
            f"serialwrap_reliability.core 僅支援 editable 安裝；"
            f"REPO_ROOT={root} 下找不到 realhw/harness.py。"
            "請從 repo root 執行 pip install -e reliability/"
        )
    return root


REPO_ROOT: Path = resolve_repo_root(Path(__file__))


def ensure_realhw_importable() -> Path:
    """把 repo root 冪等插入 ``sys.path``，讓 ``import realhw`` 可用。"""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return REPO_ROOT


def load_registry() -> list[Any]:
    """載入 realhw registry，回傳淺拷貝。

    首次呼叫會 import ``realhw.cases`` 觸發所有 case 的 ``register()``，
    將結果寫入 ``harness.REGISTRY``；此副作用是 process-wide 且不可逆。
    後續呼叫只讀取既有 registry，不重複註冊。
    """
    ensure_realhw_importable()
    import realhw.cases  # noqa: F401
    from realhw import harness

    return list(harness.REGISTRY)
