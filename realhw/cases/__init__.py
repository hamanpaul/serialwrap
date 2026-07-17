"""realhw case 模組——import 各子模組觸發 register()。

Tasks 5-9 逐步加入 P0×8 + P1×20；Task 9 的 longrun 由另檔加入。
"""
from __future__ import annotations

from . import p0  # noqa: F401
from . import p1_console  # noqa: F401
