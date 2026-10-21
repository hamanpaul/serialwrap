#!/usr/bin/env python3
"""P4-T1 委派哨兵：黑箱 bundle 的 `python_cli_delegate.entry`（計畫 Task 1 步驟 3）。

被呼叫即寫入 `$SW_DELEGATE_MARKER`（未設定時為 `dirname($SERIALWRAP_RELEASE_MANIFEST)/DELEGATED`，即 bundle 根；兩者皆未設定則為目前工作目錄），
內容為 argv JSON，然後 exit 97。
任何漏網委派都會留下 marker 並使測試失敗（Review Focus 2）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    manifest = os.environ.get("SERIALWRAP_RELEASE_MANIFEST")
    default = Path(manifest).parent / "DELEGATED" if manifest else Path.cwd() / "DELEGATED"
    marker = Path(os.environ.get("SW_DELEGATE_MARKER") or default)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"argv": argv}, ensure_ascii=False) + "\n", encoding="utf-8")
    return 97


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
