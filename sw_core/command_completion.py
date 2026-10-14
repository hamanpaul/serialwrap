"""每命令完成條件的共用驗證，所有公開入口均在排隊／TX 前使用。"""

from __future__ import annotations

import re

COMPLETION_REGEX_MAX_BYTES = 1024
ERROR_INVALID_COMPLETION_REGEX = "INVALID_COMPLETION_REGEX"


def valid_completion_regex(value: object) -> bool:
    """None 表示使用 profile prompt；其他值必須是有界、不匹配空串的 regex。"""
    if value is None:
        return True
    if not isinstance(value, str) or not value:
        return False
    try:
        if len(value.encode("utf-8")) > COMPLETION_REGEX_MAX_BYTES:
            return False
        compiled = re.compile(value)
        return compiled.search("") is None
    except (re.error, UnicodeEncodeError, RecursionError):
        return False
