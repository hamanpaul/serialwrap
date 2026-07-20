"""realhw case 模組——import 各子模組觸發 register()。

P0×8＋P1×20＋remote×7＋longrun×1。
"""
from __future__ import annotations

from . import p0  # noqa: F401
from . import p1_console  # noqa: F401
from . import p1_cmd  # noqa: F401
from . import p1_wal  # noqa: F401
from . import p1_restart  # noqa: F401
from . import p1_handoff  # noqa: F401
from . import p1_hotplug  # noqa: F401
from . import longrun  # noqa: F401
from . import remote  # noqa: F401
