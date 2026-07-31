"""case 模組自動載入：import 本 package 下所有 f*.py 觸發 register()。

新增 family 檔（fNN_<slug>.py）即自動納入，毋須改本檔。
"""
from __future__ import annotations

import importlib
import pkgutil

for _mod in pkgutil.iter_modules(__path__):
    if _mod.name.startswith("f"):
        importlib.import_module(f"{__name__}.{_mod.name}")
