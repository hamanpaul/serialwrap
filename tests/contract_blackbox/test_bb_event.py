"""黑箱契約測試：event 指令群組（P3-T15）。

驗證 10 個 event 子指令 RPC 參數 wire 格式、add 支援 NaN 與 key 順序保真、
陣列 payload 直傳、缺失檔案與壞 JSON 的 INTERNAL_ERROR、非重試性及 bench 錯誤 context。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bb_harness import NL, run_cli

pytestmark = [pytest.mark.process_rpc]


@pytest.mark.core_portable
def test_event_subcommands_wire_params(env, stubd, tmp_path):
    s = stubd({"default": [{"reply": {"ok": True, "result": "ok"}}]})

    # 1. add
    rule_file = tmp_path / "rule.json"
    rule_file.write_text('{"rule_id":"r1","pattern":"test"}', encoding="utf-8")
    r = run_cli(["--socket", s.endpoint, "event", "add", "--file", str(rule_file)], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.rule_set"
    assert req["params"] == {"rule_id": "r1", "pattern": "test"}

    # 2. rm
    r = run_cli(["--socket", s.endpoint, "event", "rm", "r1"], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.rule_delete"
    assert req["params"] == {"rule_id": "r1"}

    # 3. list (預設)
    r = run_cli(["--socket", s.endpoint, "event", "list"], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.rule_list"
    assert req["params"] == {"selector": None, "owner": None}

    # 3b. list (帶 filter)
    r = run_cli(["--socket", s.endpoint, "event", "list", "--selector", "COM0", "--owner", "user1"], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.rule_list"
    assert req["params"] == {"selector": "COM0", "owner": "user1"}

    # 4. show
    r = run_cli(["--socket", s.endpoint, "event", "show", "r1"], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.rule_get"
    assert req["params"] == {"rule_id": "r1"}

    # 5. enable
    r = run_cli(["--socket", s.endpoint, "event", "enable", "--selector", "COM0"], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.com_enable"
    assert req["params"] == {"selector": "COM0"}

    # 6. disable
    r = run_cli(["--socket", s.endpoint, "event", "disable", "--selector", "COM0"], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.com_disable"
    assert req["params"] == {"selector": "COM0"}

    # 7. status (預設)
    r = run_cli(["--socket", s.endpoint, "event", "status"], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.com_status"
    assert req["params"] == {"selector": None}

    # 7b. status (帶 selector)
    r = run_cli(["--socket", s.endpoint, "event", "status", "--selector", "COM0"], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.com_status"
    assert req["params"] == {"selector": "COM0"}

    # 8. reset (rule-id)
    r = run_cli(["--socket", s.endpoint, "event", "reset", "--rule-id", "r1"], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.reset"
    assert req["params"] == {"rule_id": "r1", "selector": None}

    # 8b. reset (selector)
    r = run_cli(["--socket", s.endpoint, "event", "reset", "--selector", "COM0"], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.reset"
    assert req["params"] == {"rule_id": None, "selector": "COM0"}

    # 9. reload
    r = run_cli(["--socket", s.endpoint, "event", "reload"], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.reload"
    assert req["params"] == {}

    # 10. tail (預設)
    r = run_cli(["--socket", s.endpoint, "event", "tail"], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.tail"
    assert req["params"] == {"rule_id": None, "selector": None, "n": 50, "since_ts": None}

    # 10b. tail (自訂參數)
    r = run_cli(
        ["--socket", s.endpoint, "event", "tail", "--rule-id", "r1", "--selector", "COM0", "-n", "10", "--since", "12345"],
        env=env,
    )
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["method"] == "event.tail"
    assert req["params"] == {"rule_id": "r1", "selector": "COM0", "n": 10, "since_ts": 12345}


@pytest.mark.core_portable
def test_event_add_nan_and_key_order(env, stubd, tmp_path):
    s = stubd({"methods": {"event.rule_set": [{"reply": {"ok": True}}]}})
    f = tmp_path / "nan.json"
    raw_json = '{"b":1,"a":[1.5,NaN],"z":"\u00e9"}'
    f.write_text(raw_json, encoding="utf-8")

    r = run_cli(["--socket", s.endpoint, "event", "add", "--file", str(f)], env=env)
    assert r.rc == 0
    raw_req = s.requests()[-1]
    assert b'"params":{"b":1,"a":[1.5,NaN],"z":"\xc3\xa9"}' in raw_req


@pytest.mark.core_portable
def test_event_add_list_payload(env, stubd, tmp_path):
    s = stubd({"methods": {"event.rule_set": [{"reply": {"ok": True}}]}})
    f = tmp_path / "list.json"
    f.write_text("[1,2]", encoding="utf-8")

    r = run_cli(["--socket", s.endpoint, "event", "add", "--file", str(f)], env=env)
    assert r.rc == 0
    req = json.loads(s.requests()[-1].decode("utf-8"))
    assert req["params"] == [1, 2]


@pytest.mark.core_portable
def test_event_add_missing_file(env):
    r = run_cli(["event", "add", "--file", "missing.json"], env=env)
    assert r.rc == 1
    assert json.loads(r.stdout.decode("utf-8")) == {
        "error_code": "INTERNAL_ERROR",
        "message": "[Errno 2] No such file or directory: 'missing.json'",
        "ok": False,
    }
    expected_err = b"serialwrap: event.rule_set failed: INTERNAL_ERROR: [Errno 2] No such file or directory: 'missing.json'" + NL
    assert r.stderr == expected_err


@pytest.mark.core_portable
def test_event_add_bad_json_crlf(env, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'{\r\n"a": 1,\r\n"b": \r\n}')

    r = run_cli(["event", "add", "--file", str(bad)], env=env)
    assert r.rc == 1
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["ok"] is False
    assert data["error_code"] == "INTERNAL_ERROR"
    assert "line 4 column 1 (char 16)" in data["message"]
    assert b"serialwrap: event.rule_set failed: INTERNAL_ERROR:" in r.stderr
    assert b"line 4 column 1 (char 16)" in r.stderr


@pytest.mark.core_portable
def test_event_no_retries(env, stubd):
    s = stubd({"default": [{"close": True}]})
    r = run_cli(["--socket", s.endpoint, "--retries", "3", "event", "list"], env=env)
    assert r.rc == 2
    # event.* 不在白名單內，即使 --retries 3 也不會重送，只送 1 筆
    assert len(s.requests()) == 1


@pytest.mark.core_portable
def test_event_bench_not_remembered(env):
    r = run_cli(["--bench", "nope", "event", "list"], env=env)
    assert r.rc == 1
    data = json.loads(r.stdout.decode("utf-8"))
    assert data["ok"] is False
    assert data["error_code"] == "BENCH_ENDPOINT_NOT_REMEMBERED"
    assert b"serialwrap: event.rule_list failed: BENCH_ENDPOINT_NOT_REMEMBERED:" in r.stderr
