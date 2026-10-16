"""黑箱 RPC 參數與 wire 格式契約測試。

依據 P3-T14 規範，以 tests/contract_blackbox/data/rpc_params.json 表格進行參數化測試。
stdlib-only，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bb_harness import NL, repo_root, run_cli

pytestmark = [pytest.mark.core_portable, pytest.mark.process_rpc]

DATA_PATH = Path(__file__).resolve().parent / "data" / "rpc_params.json"
ROWS = json.loads(DATA_PATH.read_text(encoding="utf-8"))

_LONG_TIMEOUT_METHODS = {
    "session.attach",
    "session.recover",
    "session.self_test",
    "session.console_attach",
}


@pytest.mark.parametrize("row", ROWS, ids=[f"{r['method']}_{'_'.join(r['argv'])}" for r in ROWS])
def test_rpc_params_wire(env, stubd, row):
    """驗證各指令發送之 RPC 請求符合 wire 契約，且 stdout 與 trace 符合規範。"""
    argv = row["argv"]
    method = row["method"]
    params = row["params"]

    cli_ver = (repo_root() / "VERSION").read_text(encoding="utf-8").strip()
    s = stubd({"default": [{"reply": {"ok": True, "version": cli_ver}}]})

    cmd = ["-v", "--socket", s.endpoint] + argv
    r = run_cli(cmd, env=env)

    assert r.rc == 0, f"rc={r.rc}, stderr={r.stderr.decode('utf-8', errors='replace')}"

    expected_out = (
        json.dumps({"ok": True, "version": cli_ver}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + NL
    )
    assert r.stdout == expected_out, f"stdout 不符: {r.stdout} vs {expected_out}"

    expected_req = f'{{"id":1,"method":"{method}","params":{params}}}\n'.encode("utf-8")
    reqs = s.requests()
    assert len(reqs) == 1, f"預期恰有 1 筆請求，實際有 {len(reqs)} 筆: {reqs}"
    assert reqs[0] == expected_req, f"wire 請求不符: {reqs[0]} vs {expected_req}"

    stderr_lines = r.stderr.decode("utf-8", errors="replace").splitlines()
    assert len(stderr_lines) >= 1, "缺少 trace 輸出"
    trace = json.loads(stderr_lines[0])
    assert trace["method"] == method
    expected_timeout = 45.0 if method in _LONG_TIMEOUT_METHODS else 5.0
    assert trace["timeout_s"] == expected_timeout, f"timeout_s 不符: {trace['timeout_s']} vs {expected_timeout}"
