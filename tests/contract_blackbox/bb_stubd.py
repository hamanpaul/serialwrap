"""黑箱用 stub daemon（P3-T2 紅燈階段 stub）。

實作於 Step 4：socketserver 逐行 NDJSON、依 script 回覆、請求原樣寫入 requests.log。
紅燈階段建構即 raise；stop 為 no-op，因為 stub 階段從未啟動任何資源。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


class StubDaemon:
    def __init__(self, workdir: Path, script: Mapping[str, Any], *, transport: str = "unix") -> None:
        raise NotImplementedError("P3-T2")

    def start(self, timeout_s: float = 10.0) -> None:
        raise NotImplementedError("P3-T2")

    @property
    def endpoint(self) -> str:
        raise NotImplementedError("P3-T2")

    def requests(self) -> list[bytes]:
        raise NotImplementedError("P3-T2")

    def stop(self) -> None:
        return None  # 紅燈階段從未啟動資源，無可釋放者
