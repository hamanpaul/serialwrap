from __future__ import annotations
import shutil
from pathlib import Path


def migrate_legacy_state(legacy, dest) -> bool:
    """把 legacy state.json 搬到 dest；僅當 legacy 存在且 dest 尚不存在時才搬。回傳是否實際搬移。"""
    legacy, dest = Path(legacy), Path(dest)
    if not legacy.exists() or dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy, dest)
    return True
