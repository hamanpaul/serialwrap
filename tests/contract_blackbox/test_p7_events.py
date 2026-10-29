"""P7 Task 18（08-slice23-login-events.md）：Event Engine 黑箱（規則 CRUD、handler payload／env／fd、逾時、ReDoS）。

外部 CLI＋FakeTarget 觀測；Python 基線與 Rust（`SERIALWRAP_AB_LABEL=rust-*`）皆須通過。斷言值依 Python 基線實測
（2026-10-29，CPython 3.12：`event_engine/dispatcher.py` `build_payload`／`build_env`、`engine.py` 事件順序）。
stdlib＋PyYAML＋PTY，不得 import sw_core／serialwrap／serialwrapd。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bb_faketarget import FakeTarget
from bb_harness import is_rust, python_baseline_interpreter, run_cli
from bb_p7 import wait_state

pytestmark = [pytest.mark.posix_only, pytest.mark.posix_pty, pytest.mark.process_rpc]

HANDLER = r'''
import json, os, sys
d = {"stdin": sys.stdin.read(), "env": sorted(os.environ), "fds": sorted(os.listdir("/proc/self/fd"), key=int)}
open(sys.argv[1], "w").write(json.dumps(d))
'''
EVENT_ENV = {
    "SERIALWRAP_EVENT_SCHEMA_VERSION", "SERIALWRAP_EVENT_RULE_ID", "SERIALWRAP_EVENT_OWNER", "SERIALWRAP_EVENT_KIND",
    "SERIALWRAP_EVENT_SELECTOR", "SERIALWRAP_EVENT_MATCHED_AT", "SERIALWRAP_EVENT_MATCHED_TEXT",
    "SERIALWRAP_EVENT_FIRE_COUNT", "SERIALWRAP_EVENT_WAL_SEQ", "SERIALWRAP_EVENT_LEVEL", "SERIALWRAP_EVENT_SCOPE",
}
INHERITED = ("PATH", "HOME", "USER", "LANG", "LC_ALL")
PAYLOAD_KEYS = [
    "schema_version", "rule_id", "rule_name", "owner", "kind", "selector", "matched_at", "matched_line",
    "matched_text", "match_groups", "scope", "active_cmd_id", "wal_seq", "bridge_generation", "fire_count",
    "level", "profile",
]


def _rule(tmp: Path, name: str, pattern: dict, handler: dict, **kw) -> Path:
    r = {"schema_version": 1, "owner": "p7", "name": name, "kind": "tool", "selectors": ["COM0"], "pattern": pattern,
         "handler": handler, "auto_enable_com_on_load": True, "timeout_ms": 3000}
    r.update(kw)
    p = tmp / f"{name}.json"
    p.write_text(json.dumps(r), encoding="utf-8")
    return p


def _ready(p7, tmp_path) -> FakeTarget:
    tgt = FakeTarget(tmp_path / "t0", prompt="root@prplOS:~# ")
    p7.start("prpl", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    return tgt


def _tail(p7) -> list[dict]:
    rc, j = p7.cli("event", "tail", "-n", "50")
    assert rc == 0 and j and j["ok"] is True, (rc, j)
    return j["entries"]


def _wait_tail(p7, want_type: str, timeout_s: float = 10.0) -> list[dict]:
    deadline = time.monotonic() + timeout_s
    while True:
        entries = _tail(p7)
        if any(e["type"] == want_type for e in entries):
            return entries
        assert time.monotonic() < deadline, f"event tail 未出現 {want_type}：{entries!r}"
        time.sleep(0.2)


def _fire_handler(p7, tmp_path, tgt) -> dict:
    script = tmp_path / "h.py"
    script.write_text(HANDLER, encoding="utf-8")
    out = tmp_path / "out.json"
    rf = _rule(tmp_path, "panic", {"kind": "contains", "value": "Kernel panic"}, {"exec": [sys.executable, str(script), str(out)]})
    rc, j = p7.cli("event", "add", "--file", str(rf))
    assert rc == 0 and j["ok"] is True, (rc, j)
    time.sleep(1.0)
    tgt.emit("Kernel panic - not syncing\r\n")
    _wait_tail(p7, "fire_completed")
    return json.loads(out.read_text(encoding="utf-8"))


def test_rule_fire_handler_payload(p7, tmp_path):
    tgt = _ready(p7, tmp_path)
    got = _fire_handler(p7, tmp_path, tgt)
    payload = json.loads(got["stdin"])
    assert got["stdin"].endswith("\n") and got["stdin"].count("\n") == 1, got["stdin"]
    assert list(payload) == PAYLOAD_KEYS, payload
    assert {k: payload[k] for k in ("schema_version", "rule_id", "rule_name", "owner", "kind", "selector", "matched_line",
                                    "matched_text", "match_groups", "scope", "active_cmd_id", "bridge_generation",
                                    "fire_count", "level", "profile")} == {
        "schema_version": 1, "rule_id": "p7.panic", "rule_name": "panic", "owner": "p7", "kind": "tool",
        "selector": "COM0", "matched_line": "Kernel panic", "matched_text": "Kernel panic", "match_groups": [],
        "scope": "spontaneous", "active_cmd_id": None, "bridge_generation": 0, "fire_count": 1, "level": "INFO",
        "profile": "ALL"}, payload
    assert isinstance(payload["matched_at"], int) and isinstance(payload["wal_seq"], int), payload
    assert set(got["env"]) == EVENT_ENV | {k for k in INHERITED if k in p7.env}, got["env"]
    types = [e["type"] for e in _tail(p7)]
    assert types == ["com_enabled", "rule_loaded", "match_recorded", "fire_completed"], types
    done = [e for e in _tail(p7) if e["type"] == "fire_completed"][0]
    assert (done["rule_id"], done["exit_code"], done["fire_count"], done["stdout_tail"], done["stderr_tail"]) == (
        "p7.panic", 0, 1, "", ""), done
    assert done["wal_seq"] == payload["wal_seq"], (done, payload)


def test_start_load_order(p7, tmp_path):
    tgt = FakeTarget(tmp_path / "t0", prompt="root@prplOS:~# ")
    ev = Path(p7.env["SERIALWRAP_EVENTS_DIR"])
    ev.mkdir(parents=True, exist_ok=True)
    rule = {"schema_version": 1, "owner": "p7", "name": "pre", "kind": "tool", "selectors": ["COM0"],
            "pattern": {"kind": "contains", "value": "XYZ"}, "handler": {"exec": ["true"]},
            "auto_enable_com_on_load": True, "timeout_ms": 3000}
    (ev / "p7.pre.json").write_text(json.dumps(rule), encoding="utf-8")
    p7.start("prpl", [tgt])
    wait_state(p7.env, "COM0", "READY", timeout_s=60.0)
    entries = _tail(p7)
    assert [e["type"] for e in entries[:2]] == ["rule_loaded", "com_enabled"], entries
    assert entries[0]["rule_id"] == "p7.pre" and entries[1]["triggered_by"] == "auto", entries


def test_rule_crud_and_reload(p7, tmp_path):
    _ready(p7, tmp_path)
    rf = _rule(tmp_path, "crud", {"kind": "contains", "value": "CRUD"}, {"exec": ["true"]})
    rule = json.loads(rf.read_text(encoding="utf-8"))
    rc, j = p7.cli("event", "add", "--file", str(rf))
    assert rc == 0, (rc, j)
    assert {k: v for k, v in j.items() if k not in ("id", "version")} == {**rule, "ok": True}, j
    rc, j = p7.cli("event", "show", "p7.crud")
    assert rc == 0 and j["rule"] == rule, (rc, j)
    assert j["counter"] == {"exhausted": False, "fires": 0, "last_fire_ts": None}, j
    rc, j = p7.cli("event", "list")
    assert rc == 0 and j["rules"] == [{"exhausted": False, "fires": 0, "kind": "tool", "last_fire_ts": None,
                                       "owner": "p7", "rule_id": "p7.crud", "selectors": ["COM0"]}], j
    rc, j = p7.cli("event", "reload")
    assert rc == 0 and (j["added"], j["changed"], j["removed"]) == ([], [], []), j
    ev = Path(p7.env["SERIALWRAP_EVENTS_DIR"])
    extra = dict(rule, name="extra")
    (ev / "p7.extra.json").write_text(json.dumps(extra), encoding="utf-8")
    changed = dict(rule, timeout_ms=1500)
    (ev / "p7.crud.json").write_text(json.dumps(changed), encoding="utf-8")
    rc, j = p7.cli("event", "reload")
    assert rc == 0 and j["ok"] is True, (rc, j)
    assert (set(j["added"]), set(j["changed"]), set(j["removed"])) == ({"p7.extra"}, {"p7.crud"}, set()), j   # RPC-H-13
    (ev / "p7.extra.json").unlink()
    rc, j = p7.cli("event", "reload")
    assert rc == 0 and (set(j["added"]), set(j["changed"]), set(j["removed"])) == (set(), set(), {"p7.extra"}), j
    rc, j = p7.cli("event", "rm", "p7.crud")
    assert rc == 0 and j["deleted"] is True, (rc, j)
    rc, j = p7.cli("event", "list")
    assert rc == 0 and j["rules"] == [], j


def _pgrp_members(pgid: int) -> list[str]:
    """外部觀測：`/proc/*/stat` 第 5 欄（pgrp）等於 `pgid` 且非 zombie 的行程。"""
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            stat = Path(f"/proc/{name}/stat").read_text()
        except OSError:
            continue
        fields = stat[stat.rindex(")") + 2:].split()
        if int(fields[2]) == pgid and fields[0] != "Z":
            out.append(f"{name}:{stat[:80]}")
    return out


def test_handler_timeout_kills_group(p7, tmp_path):
    tgt = _ready(p7, tmp_path)
    pid_file = tmp_path / "handler.pid"
    rf = _rule(tmp_path, "slow", {"kind": "contains", "value": "SLOWME"},
               {"shell": f"echo $$ > '{pid_file}'; sleep 30 & sleep 30"}, timeout_ms=500)
    rc, j = p7.cli("event", "add", "--file", str(rf))
    assert rc == 0, (rc, j)
    time.sleep(1.0)
    tgt.emit("SLOWME\r\n")
    entries = _wait_tail(p7, "fire_timeout")
    tmo = [e for e in entries if e["type"] == "fire_timeout"][0]
    assert tmo["rule_id"] == "p7.slow" and 500 <= tmo["duration_ms"] <= 1500, tmo
    assert "fire_completed" not in [e["type"] for e in entries], entries
    pgid = int(pid_file.read_text().strip())   # start_new_session：shell 為群組 leader，pgid＝其 pid
    deadline = time.monotonic() + 5.0
    while _pgrp_members(pgid):
        assert time.monotonic() < deadline, f"handler 行程群組（含背景 sleep 30）應被終止：{_pgrp_members(pgid)!r}"
        time.sleep(0.2)


def test_handler_no_inherited_fds(p7, tmp_path):
    tgt = _ready(p7, tmp_path)
    got = _fire_handler(p7, tmp_path, tgt)
    fds = got["fds"]
    # 0、1、2 與 listdir 自身的目錄 fd（最小可用號 3）之外不得有其他 fd（close_fds=True；不得含 PTY 與 RPC socket）
    assert fds == ["0", "1", "2", "3"], fds


def test_redos_rejected_no_re2(p7, tmp_path):
    if not is_rust():
        probe = subprocess.run(python_baseline_interpreter() + ["-c", "import re2"], capture_output=True)
        if probe.returncode == 0:
            pytest.skip("baseline_variant_re2")
    _ready(p7, tmp_path)
    rf = _rule(tmp_path, "redos", {"kind": "regex", "value": "a+b+"}, {"exec": ["true"]})
    r = run_cli(["--socket", p7.env["SW_P7_SOCKET"], "event", "add", "--file", str(rf)], env=p7.env)
    assert r.rc == 2, r
    j = json.loads(r.stdout)
    assert (j["ok"], j["error_code"]) == (False, "INVALID_RULE_SCHEMA"), j
    rc, lst = p7.cli("event", "list")
    assert rc == 0 and lst["rules"] == [], lst
