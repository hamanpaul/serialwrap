"""serialwrap_regression——已修 bug 的實機回歸 testpilot plugin（#155）。

以 TestPilot 為殼，把已 CLOSED 且有實際修正的 issue 寫成實機回歸 case，
在真 bench 上常跑（改動後／發版前），防止回歸。與 serialwrap-reliability
（穩定性／soak）刻意分開；共用 realhw harness 基礎設施。
"""
from __future__ import annotations

__version__ = "0.1.0"
