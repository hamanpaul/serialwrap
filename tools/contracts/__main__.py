"""契約工具命令列入口（`python3 -m tools.contracts`）。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
