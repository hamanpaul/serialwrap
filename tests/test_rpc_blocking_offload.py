"""tests/test_rpc_blocking_offload.py — #80 阻塞型 RPC handler 丟 executor。

延伸 #52：把會同步阻塞 event loop 的 handler（recover/attach/self_test/console_attach 與
整檔讀取 result.tail/log.tail_*/wal.range）納入 BLOCKING_RPC_METHODS，offload 到 executor，
避免單一慢 RPC 凍結全 daemon。機制本身（run_in_executor）由 test_issue52_rpc_concurrency 覆蓋。
"""
import pathlib

import sw_core
from sw_core.daemon import BLOCKING_RPC_METHODS

_EXPECTED = {
    "file.push", "file.pull",
    "session.recover", "session.attach", "session.self_test", "session.console_attach",
    "result.tail", "log.tail_raw", "log.tail_text", "wal.range",
}


def test_blocking_methods_include_known_blocking_handlers():
    assert _EXPECTED <= BLOCKING_RPC_METHODS


def test_blocking_methods_are_real_dispatch_strings():
    """防打錯方法名：每個 offload 的方法都應是 service.rpc 實際分派的字串。"""
    src = (pathlib.Path(sw_core.__file__).parent / "service.py").read_text(encoding="utf-8")
    for m in _EXPECTED - {"file.push", "file.pull"}:
        assert f'"{m}"' in src, f"{m} 不是 service.py 的分派方法（可能打錯名）"
