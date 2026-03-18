"""Expect 語法比對引擎。

支援 YAML test case 中的 ``expect`` 區塊，
包括精確匹配、巢狀欄位、contains、matches、has_keys 等。
"""
from __future__ import annotations

import re
from typing import Any


class ExpectError(Exception):
    """期望不符合時拋出。"""

    def __init__(self, path: str, expected: Any, actual: Any) -> None:
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(f"[{path}] 期望 {expected!r}，實際 {actual!r}")


def check_expect(data: dict[str, Any], expect: dict[str, Any]) -> list[ExpectError]:
    """檢查 data 是否符合 expect 規則。

    回傳所有不符合的 ExpectError 列表（空 = 全部通過）。
    """
    errors: list[ExpectError] = []
    for key, expected in expect.items():
        if key == "has_keys":
            _check_has_keys(data, expected, errors)
        elif key == "not":
            _check_not(data, expected, errors)
        else:
            actual = _resolve_path(data, key)
            _check_value(key, expected, actual, errors)
    return errors


def _resolve_path(data: Any, path: str) -> Any:
    """依照 dot 分隔路徑解析巢狀欄位。"""
    parts = path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _check_has_keys(data: dict[str, Any], keys: list[str], errors: list[ExpectError]) -> None:
    for key in keys:
        val = _resolve_path(data, key)
        if val is None:
            errors.append(ExpectError(f"has_keys.{key}", "存在", None))


def _check_not(data: dict[str, Any], not_expect: dict[str, Any], errors: list[ExpectError]) -> None:
    for key, expected in not_expect.items():
        actual = _resolve_path(data, key)
        if actual == expected:
            errors.append(ExpectError(f"not.{key}", f"不等於 {expected!r}", actual))


def _check_value(path: str, expected: Any, actual: Any, errors: list[ExpectError]) -> None:
    if isinstance(expected, dict):
        if "contains" in expected:
            if not isinstance(actual, str) or expected["contains"] not in actual:
                errors.append(ExpectError(path, f"包含 {expected['contains']!r}", actual))
        elif "matches" in expected:
            if not isinstance(actual, str) or not re.search(expected["matches"], actual):
                errors.append(ExpectError(path, f"匹配 {expected['matches']!r}", actual))
        elif "gte" in expected:
            if actual is None or actual < expected["gte"]:
                errors.append(ExpectError(path, f">= {expected['gte']}", actual))
        elif "lte" in expected:
            if actual is None or actual > expected["lte"]:
                errors.append(ExpectError(path, f"<= {expected['lte']}", actual))
        else:
            # 當作精確匹配 dict
            if actual != expected:
                errors.append(ExpectError(path, expected, actual))
    else:
        if actual != expected:
            errors.append(ExpectError(path, expected, actual))
