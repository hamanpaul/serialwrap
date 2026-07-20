"""serialwrap-reliability——realhw 引擎的 testpilot plugin 殼（dev-only editable dist）。

注意：本模組不得 import plugin 或 testpilot；serialwrap CI 未安裝 testpilot 時，
仍需能安全 import 本 package。
"""
from __future__ import annotations

__version__ = "0.1.0"
PLUGIN_API_VERSION = "1.1"
