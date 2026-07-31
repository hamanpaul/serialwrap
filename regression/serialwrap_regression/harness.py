"""Case 模型與 registry——回歸 plugin 自有，與 realhw 全域 REGISTRY 完全分離。"""
from __future__ import annotations

import dataclasses
from typing import Any, Callable

# 執行順序（#155 定案）：非破壞性 family 在前、破壞性（F9→F10）壓軸。
FAMILY_ORDER: tuple[str, ...] = ("F3", "F1", "F5", "F6", "F2", "F4", "F7", "F8", "F9", "F10")


@dataclasses.dataclass(frozen=True)
class Case:
    """單一回歸 case；oracle 一律為「對應 issue 當初的錯誤行為不得再現」。"""

    id: str
    family: str  # FAMILY_ORDER 之一
    title: str
    run: Callable[[Any], Any]  # (ctx: realhw.harness.Ctx) -> realhw.harness.CaseResult
    issues: tuple[str, ...]  # 對應已修 issue，至少一個，如 ("#94",)
    destructive: bool = False
    requires: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()


REGISTRY: list[Case] = []


def register(case: Case) -> Case:
    if case.family not in FAMILY_ORDER:
        raise ValueError(f"未知 family：{case.family}（case={case.id}）")
    if not case.issues:
        raise ValueError(f"case 必須掛至少一個已修 issue：{case.id}")
    if any(c.id == case.id for c in REGISTRY):
        raise ValueError(f"duplicate case id: {case.id}")
    REGISTRY.append(case)
    return case


def load_registry() -> list[Case]:
    """import cases package 觸發全部 register()（process-wide、冪等），回傳淺拷貝。"""
    from serialwrap_regression.preflight import ensure_realhw_importable

    ensure_realhw_importable()  # cases 檔頂層 import realhw.harness.CaseResult 需要
    from serialwrap_regression import cases  # noqa: F401  觸發自動載入

    return list(REGISTRY)


def ordered(registry: list[Case]) -> list[Case]:
    """依 FAMILY_ORDER 再依 id 排序。"""
    return sorted(registry, key=lambda c: (FAMILY_ORDER.index(c.family), c.id))
