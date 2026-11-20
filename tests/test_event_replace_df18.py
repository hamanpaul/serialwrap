"""DF-18（P7 Task 13-py）：counter／registry 的 tmp→正式檔改名改用 ``os.replace``。

Windows 上 ``os.rename`` 的目標已存在會拋 ``FileExistsError``，使第二次寫入同一 counter 或事件規則失敗；
``os.replace`` 在 POSIX 與 ``os.rename`` 同語意、在 Windows 為覆寫改名。
"""
from __future__ import annotations

import os
import unittest.mock as m

import pytest

from sw_core.event_engine.counter import Counter, CounterStore
from sw_core.event_engine.registry import RuleRegistry

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

RULE = {
    "schema_version": 1, "owner": "o", "name": "n", "kind": "tool", "selectors": ["COM0"],
    "pattern": {"kind": "contains", "value": "x"}, "handler": {"exec": ["true"]},
}


def test_counter_save_uses_replace(tmp_path):
    store = CounterStore(str(tmp_path))
    with m.patch("sw_core.event_engine.counter.os.replace", wraps=os.replace) as rep, \
         m.patch("sw_core.event_engine.counter.os.rename", side_effect=AssertionError("must not use os.rename")):
        store.save("o.n", Counter(fires=1))
        store.save("o.n", Counter(fires=2))
    assert rep.call_count == 2
    assert store.load("o.n").fires == 2


def test_registry_upsert_uses_replace(tmp_path):
    reg = RuleRegistry(str(tmp_path))
    with m.patch("sw_core.event_engine.registry.os.replace", wraps=os.replace) as rep, \
         m.patch("sw_core.event_engine.registry.os.rename", side_effect=AssertionError("must not use os.rename")):
        reg.upsert(RULE)
        reg.upsert(RULE)
    assert rep.call_count == 2
    assert [r.rule_id for r in reg.load_all().rules] == ["o.n"]
