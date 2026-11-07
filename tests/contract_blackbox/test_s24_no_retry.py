"""P8 Task 6（09-slice24-transfer-flash.md）：`--retries` 不重試有副作用的方法（s24-xfer-no-retry-side-effect）。

StubDaemon 對受測 method 的請求直接斷線：`serialwrap --retries 3` 對 `file push`、`file pull`、`device release`、
`device attach` 各只送出 1 筆該 method 的請求且 exit 非 0（§4.2：副作用方法絕不重試）；對照組 `mcu status`
（11 個唯讀重試白名單之一）同腳本送出 4 筆。外部 oracle 為 stub 實際收到的請求。
stdlib＋pytest，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import json

import pytest

from bb_harness import run_cli

pytestmark = [pytest.mark.core_portable]

SIDE_EFFECT = [
    ("file.push", ["file", "push", "--selector", "COM0", "--local", "l", "--remote", "r"]),
    ("file.pull", ["file", "pull", "--selector", "COM0", "--remote", "r"]),
    ("device.release", ["device", "release", "--selector", "COM0"]),
    ("device.attach", ["device", "attach", "--selector", "COM0"]),
]


def _sent(stub, method: str) -> int:
    n = 0
    for raw in stub.requests():
        try:
            if json.loads(raw.decode("utf-8")).get("method") == method:
                n += 1
        except ValueError:
            continue
    return n


@pytest.mark.process_rpc
def test_side_effect_methods_not_retried(env, stubd):
    for method, args in SIDE_EFFECT:
        s = stubd({"methods": {method: [{"close": True}]}})
        r = run_cli(["--retries", "3", "--socket", s.endpoint, *args], env=env)
        assert r.rc != 0, (method, r.rc)
        assert _sent(s, method) == 1, f"{method} 不得重試：{s.requests()}"
    s = stubd({"methods": {"mcu.status": [{"close": True}]}})
    r = run_cli(["--retries", "3", "--socket", s.endpoint, "mcu", "status"], env=env)
    assert r.rc != 0
    assert _sent(s, "mcu.status") == 4, f"唯讀白名單 mcu.status 應重試 3 次：{s.requests()}"
