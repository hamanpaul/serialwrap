"""事先核准的比較映射規則定義（P2-T7）。

只允許白名單內之核准規則，不提供任意正規化或 regex，保護分母與比對嚴謹性。
"""

from __future__ import annotations

import json
import pathlib
from typing import Sequence

_RULES_FILE = pathlib.Path(__file__).parent / "mapping_rules.json"

APPROVED_RULES: frozenset[str] = frozenset({"secret_tx_unlogged", "wal_com_alias"})

if _RULES_FILE.is_file():
    try:
        with _RULES_FILE.open("r", encoding="utf-8") as _f:
            _raw_data = json.load(_f)
        _disk_rules = frozenset(_raw_data.get("rules", {}).keys())
        if _disk_rules != APPROVED_RULES:
            raise ValueError(f"mapping_rules.json 與 APPROVED_RULES 不一致：{_disk_rules} != {APPROVED_RULES}")
    except Exception as _exc:
        raise RuntimeError(f"載入 mapping_rules.json 失敗：{_exc}") from _exc


def load_rules(names: Sequence[str]) -> tuple[str, ...]:
    """載入並驗證規則清單；若有未知規則名則拋出 ValueError。"""
    result: list[str] = []
    for name in names:
        if name not in APPROVED_RULES:
            raise ValueError(f"未知比較規則：{name}")
        result.append(name)
    return tuple(result)

