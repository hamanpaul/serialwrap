"""測試工具原因碼登錄表（`HARNESS_*`，屬測試工具契約，不混入 `contracts/error_codes.yaml`）。

登錄表以 `importlib.resources` 讀取，使 zipapp 封裝後仍可讀取，不依賴工作目錄。
"""

from __future__ import annotations

import functools
import json
from importlib import resources
from typing import Any

REASON_CODES_RESOURCE: tuple[str, str] = ("tools.stage3", "stage3_reason_codes.json")


@functools.lru_cache(maxsize=1)
def _load_registry() -> dict[str, dict[str, str]]:
    package, name = REASON_CODES_RESOURCE
    raw = resources.files(package).joinpath(name).read_text(encoding="utf-8")
    doc: dict[str, Any] = json.loads(raw)
    codes = doc["codes"]
    return {code: {"meaning": desc["meaning"], "action": desc["action"]} for code, desc in codes.items()}


def load_reason_codes() -> dict[str, dict[str, str]]:
    """回傳 code -> {"meaning", "action"}；每次回傳副本，避免呼叫端修改快取。"""
    return {code: dict(desc) for code, desc in _load_registry().items()}


class HarnessError(Exception):
    """測試工具錯誤；`code` 必須存在於原因碼登錄表。"""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in _load_registry():
            raise ValueError(f"unknown HARNESS reason code: {code!r}")
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    def __reduce__(self):
        # 預設的 BaseException.__reduce__ 會以 "CODE: detail" 重建而被登錄表拒絕；改以 (code, detail) 重建
        return (type(self), (self.code, self.detail))
