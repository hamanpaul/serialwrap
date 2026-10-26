#!/usr/bin/env python3
"""P7 Task 1（08-slice23-login-events.md）：擷取 Slice 2.3 Python 基線 golden（`contracts/golden/p7/*.ndjson`）。

這是基線擷取工具（可 import `sw_core`），不是黑箱測試。每個 golden 檔一行一筆 JSON
（`json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`），依案例 id（`<file>:<nnn>`）排序。
所有時間、亂數、環境變數、檔案路徑皆以 patch 固定，輸出與執行機器無關（暫存路徑一律正規化為 `/golden`）。

用法：
  python3 scripts/rust/capture_p7_goldens.py [--out contracts/golden/p7] [--check]
  --check：不寫檔，只比對既有檔是否與重新擷取結果相同（不同 exit 1）。

前置：無 google-re2（`import re2` 成功即 exit 2，EVT-D-S15 以無 re2 的 Python 為基準）；Python 3.12（re 錯誤訊息與 sre AST 依版本）。
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import io
import json
import logging
import os
import pathlib
import re
import sys
import tempfile
import types
from typing import Any, Callable
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

NAMES = ["env_file", "auth_resolution", "login_fsm_scenarios", "quiet_rx", "gate_responses", "reprobe_decisions",
         "reprobe_valid", "transport_stall", "self_test_responses", "recover_responses", "log_responses", "event_schema",
         "redos", "line_buffer", "event_payload", "event_log_tail", "event_engine_order", "registry_load", "counter_file"]

GOLDEN_ROOT = "/golden"


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def dumps_line(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def wire(obj: Any) -> str:
    """回應 dict 的位元組形狀：保留 key 順序（不排序）、RPC 線上格式（緊湊、ensure_ascii=False）。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def rows_with_ids(name: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, r in enumerate(rows, 1):
        r = dict(r)
        r["id"] = f"{name}:{i:03d}"
        out.append(r)
    return out


@contextlib.contextmanager
def captured_logs(logger_name: str):
    """收集 `logger_name` 的 WARNING 以上訊息（`record.getMessage()`；daemon 以 `%(message)s` 輸出）。"""
    records: list[str] = []

    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
            if record.levelno >= logging.WARNING:
                records.append(record.getMessage())

    lg = logging.getLogger(logger_name)
    h = _H()
    old_level, old_prop = lg.level, lg.propagate
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    try:
        yield records
    finally:
        lg.removeHandler(h)
        lg.setLevel(old_level)
        lg.propagate = old_prop


# ── profile 建構 ─────────────────────────────────────────────────────────────

def _templates() -> dict[str, Any]:
    import yaml
    from sw_core.config import _template_from_dict

    raw = yaml.safe_load((ROOT / "sw_core/assets/profiles/default.yaml").read_text(encoding="utf-8"))
    return {name: _template_from_dict(name, body, base_dir="/etc/serialwrap/profiles") for name, body in raw["profiles"].items()}


def session_profile(template: str | None = None, **over: Any):
    from sw_core.config import SessionProfile, UartProfile

    base: dict[str, Any] = {}
    if template is not None:
        tpl = _templates()[template]
        base = {f.name: getattr(tpl, f.name) for f in dataclasses.fields(tpl)}
        base["profile_name"] = template
    base.setdefault("profile_name", over.pop("profile_name", "p"))
    base.setdefault("platform", "prpl")
    base.update(dict(com="COM0", act_no=1, alias="lab+1", device_by_id="/dev/serial/by-id/fake"))
    base.update(over)
    if isinstance(base.get("bootloader_prompts"), list):
        base["bootloader_prompts"] = tuple(base["bootloader_prompts"])
    base.setdefault("uart", UartProfile())
    names = {f.name for f in dataclasses.fields(SessionProfile)}
    return SessionProfile(**{k: v for k, v in base.items() if k in names})


def profile_json(sp: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in dataclasses.fields(sp):
        v = getattr(sp, f.name)
        if dataclasses.is_dataclass(v):
            v = dataclasses.asdict(v)
        elif isinstance(v, tuple):
            v = list(v)
        out[f.name] = v
    return out


# ── env_file ────────────────────────────────────────────────────────────────

def gen_env_file() -> list[dict[str, Any]]:
    from sw_core.auth import parse_env_file

    inputs: list[bytes] = [
        b"",
        b"\n\n   \n",
        b"# comment\nA=1\n",
        b"export B=2\n",
        b"export   C = 3 \n",
        b"NOEQUALS\nD=4\n",
        b"=novalue\n  =x\nE=5\n",
        b"F='single'\nG=\"double\"\n",
        b"H='unpaired\nI=\"x'\n",
        b"J=\"\nK='\n",
        b"L=a=b=c\n",
        b"M=crlf\r\nN=two\r\n",
        b"O=cr\rP=only\r",
        "Q=　full　\n　R=wide\n".encode("utf-8"),
        b"S=\x1cfs\x1c\nT=\x1dgs\n",
        "﻿BOM=1\nU=2\n".encode("utf-8"),
        b"V=\xff\xfe\n",
        b"W=1\nX=2\nW=3\n",
        b"export=plain\nexport\n",
        b"Y= \"  spaced  \" \n",
        b"#notacomment=1\n  # indented=2\nZ=#hash\n",
        b"''=quotedkey\nAA=''\nAB=\"\"\n",
        b"AC=tail",
    ]
    rows = []
    with tempfile.TemporaryDirectory() as td:
        for data in inputs:
            p = os.path.join(td, "x.env")
            with open(p, "wb") as f:
                f.write(data)
            row: dict[str, Any] = {"input_b64": b64(data)}
            try:
                row["pairs"] = [[k, v] for k, v in parse_env_file(p).items()]
            except UnicodeDecodeError:
                row["error"] = "unreadable"
            rows.append(row)
    return rows_with_ids("env_file", rows)


# ── auth_resolution ─────────────────────────────────────────────────────────

def gen_auth_resolution() -> list[dict[str, Any]]:
    from sw_core import auth as auth_mod
    from sw_core.auth import SessionAuth
    from sw_core.login_fsm import _resolve_login_user

    cases: list[dict[str, Any]] = [
        {"profile": {}, "environ": {}, "files": {}},
        {"profile": {"user_env": "U", "pass_env": "P"}, "environ": {"U": "root", "P": "pw"}, "files": {}},
        {"profile": {"user_env": "U", "pass_env": "P"}, "environ": {"U": "root"}, "files": {}},
        {"profile": {"user_env": "U", "pass_env": "P"}, "environ": {"U": "", "P": "pw"}, "files": {}},
        {"profile": {"user_env": "U", "pass_env": "P", "username": "fallback"}, "environ": {"P": "pw"}, "files": {}},
        {"profile": {"username": "only"}, "environ": {}, "files": {}},
        {"profile": {"user_env": "U", "pass_env": "P", "env_file": "OPI.env"}, "environ": {}, "files": {"OPI.env": "U=root\nP=pw\n"}},
        {"profile": {"user_env": "U", "pass_env": "P", "env_file": "OPI.env"}, "environ": {"P": "envpw"}, "files": {"OPI.env": "U=root\n"}},
        {"profile": {"user_env": "U", "pass_env": "P", "env_file": "OPI.env"}, "environ": {"U": "eu", "P": "ep"}, "files": {"OPI.env": "U=fu\nP=\n"}},
        {"profile": {"user_env": "U", "pass_env": "P", "env_file": "OPI.env"}, "environ": {}, "files": {}},
        {"profile": {"user_env": "U", "pass_env": "P", "env_file": "OPI.env"}, "environ": {"U": "root", "P": "pw"}, "files": {}},
        {"profile": {"user_env": "U", "pass_env": "P", "env_file": "OPI.env"}, "environ": {}, "files": {"OPI.env": b"U=\xff\n"}},
        {"profile": {"user_env": "U", "pass_env": "P", "env_file": "OPI.env"}, "environ": {}, "files": {"OPI.env": "X=1\n"}},
        {"profile": {"user_env": "U", "pass_env": "P", "env_file": "~/creds.env"}, "environ": {}, "files": {"~/creds.env": "U=hu\nP=hp\n"}},
        {"profile": {"user_env": "U", "pass_env": "P", "env_file": "~/missing.env"}, "environ": {}, "files": {}},
        {"profile": {"env_file": "OPI.env"}, "environ": {}, "files": {"OPI.env": "A=1\n"}},
        {"profile": {"console_unlock_env": "K"}, "environ": {"K": "s3"}, "files": {}},
        {"profile": {"console_unlock_env": "K"}, "environ": {}, "files": {}},
        {"profile": {"console_unlock_env": "K", "env_file": "u.env"}, "environ": {}, "files": {"u.env": "K=fromfile\n"}},
        {"profile": {"console_unlock_env": "K", "env_file": "u.env"}, "environ": {}, "files": {}},
        {"profile": {"console_unlock_env": "K", "user_env": "U", "pass_env": "P"}, "environ": {"U": "u", "P": "p"}, "files": {}},
        {"profile": {"pass_env": "P"}, "environ": {"P": "pw"}, "files": {}},
        {"profile": {"user_env": "U"}, "environ": {"U": "root"}, "files": {}},
        {"profile": {"user_env": "U", "pass_env": "P", "env_file": "sub/dir.env"}, "environ": {}, "files": {"sub/dir.env": "U=a\nP=b\n"}},
    ]
    login_user_cases: list[dict[str, Any]] = [
        {"profile": {"user_env": "SW_U", "username": "root", "pass_env": "SW_P"}, "environ": {}, "auth": None},
        {"profile": {"user_env": "SW_U", "username": "root"}, "environ": {"SW_U": ""}, "auth": {"username": None}},
        {"profile": {"user_env": "SW_U"}, "environ": {"SW_U": "envuser"}, "auth": None},
        {"profile": {"username": "root"}, "environ": {}, "auth": None},
        {"profile": {}, "environ": {}, "auth": None},
        {"profile": {"user_env": "SW_U"}, "environ": {}, "auth": {"username": "authuser"}},
        {"profile": {"user_env": "SW_U", "username": "root"}, "environ": {"SW_U": "e"}, "auth": {"username": ""}},
    ]
    rows: list[dict[str, Any]] = []
    for case in cases:
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            cwd = os.path.join(td, "cwd")
            os.makedirs(home)
            os.makedirs(cwd)
            files_out: dict[str, Any] = {}
            for rel, content in case["files"].items():
                real = os.path.join(home, rel[2:]) if rel.startswith("~/") else os.path.join(cwd, rel)
                os.makedirs(os.path.dirname(real), exist_ok=True)
                data = content if isinstance(content, bytes) else content.encode("utf-8")
                with open(real, "wb") as f:
                    f.write(data)
                key = f"{GOLDEN_ROOT}/home/{rel[2:]}" if rel.startswith("~/") else rel
                files_out[key] = {"b64": b64(data)}
            environ = dict(case["environ"])
            environ["HOME"] = home
            sp = session_profile(**case["profile"])
            old = os.getcwd()
            os.chdir(cwd)
            try:
                with mock.patch.dict(os.environ, environ, clear=True), captured_logs(auth_mod.__name__) as logs:
                    auth, res = auth_mod.resolve_session_auth(sp)
            finally:
                os.chdir(old)

            def norm(s: str | None) -> str | None:
                return None if s is None else s.replace(td, GOLDEN_ROOT)

            env_out = dict(case["environ"])
            env_out["HOME"] = f"{GOLDEN_ROOT}/home"
            rows.append({
                "layer": "auth", "profile": profile_json(sp), "environ": env_out, "cwd": f"{GOLDEN_ROOT}/cwd", "files": files_out,
                "username": auth.username, "password_set": auth.password is not None, "console_unlock_set": auth.console_unlock is not None,
                "reason": res.reason, "env_file_path": norm(res.env_file_path), "warnings": [norm(w) for w in logs],
            })
    for case in login_user_cases:
        sp = session_profile(**case["profile"])
        auth = None if case["auth"] is None else SessionAuth(username=case["auth"].get("username"))
        with mock.patch.dict(os.environ, case["environ"], clear=True):
            user, err = _resolve_login_user(sp, auth)
        rows.append({"layer": "login_user", "profile": profile_json(sp), "environ": case["environ"], "auth": case["auth"],
                     "user": user, "err": err})
    return rows_with_ids("auth_resolution", rows)


# ── transport_stall ─────────────────────────────────────────────────────────

def gen_transport_stall() -> list[dict[str, Any]]:
    from sw_core import transport_stall as ts

    rows: list[dict[str, Any]] = []
    errs = ["PROMPT_UNAVAILABLE", "LOGIN_PROMPT_TIMEOUT", "BCM_PROMPT_TIMEOUT", "READY_PROMPT_TIMEOUT", "PROMPT_TIMEOUT",
            "READY_NONCE_TIMEOUT", "CREDENTIALS_UNRESOLVED", "RX_FLOOD", ""]
    for err in errs:
        rows.append({"kind": "refinable", "err": err, "refinable": bool(err) and ts.is_transport_refinable_error(err)})
    for err, rx_delta, last, now in [
        ("PROMPT_UNAVAILABLE", 0, 10.0, 40.0), ("PROMPT_UNAVAILABLE", 0, 10.0, 39.99), ("PROMPT_UNAVAILABLE", 1, 10.0, 100.0),
        ("PROMPT_UNAVAILABLE", 0, 0.0, 100.0), ("PROMPT_UNAVAILABLE", 0, -1.0, 100.0), ("SHELL_PROMPT_TIMEOUT", 0, 5.0, 35.0),
        ("READY_NONCE_TIMEOUT", 0, 5.0, 100.0), ("", 0, 5.0, 100.0), ("LOGIN_PROMPT_TIMEOUT", 0, 1.0, 31.0),
    ]:
        out, flipped = ts.classify_probe_failure(err, rx_delta=rx_delta, last_rx_mono=last, now=now)
        rows.append({"kind": "classify", "err": err, "rx_delta": rx_delta, "last_rx_mono": last, "now": now, "result": out, "flipped": flipped})
    for busid in ["1-1", "8-2.1", None]:
        for age in [30.0, 31.04, 31.05, 1234.56]:
            with mock.patch.object(ts, "resolve_usb_busid", return_value=busid):
                text = ts.transport_stall_hint("/dev/ttyUSB0", age)
            rows.append({"kind": "hint", "busid": busid, "rx_age_s": age, "text": text})
    return rows_with_ids("transport_stall", rows)


# ── event_schema ────────────────────────────────────────────────────────────

def _rule(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1, "owner": "o", "name": "n", "kind": "tool", "selectors": ["COM0"],
        "pattern": {"kind": "contains", "value": "x"}, "handler": {"exec": ["/bin/true"]},
    }
    for k, v in over.items():
        if v is _DROP:
            base.pop(k, None)
        else:
            base[k] = v
    return base


_DROP = object()


def _rule_file_dump(raw: dict[str, Any]) -> str:
    return json.dumps(dict(raw), ensure_ascii=False, indent=2, sort_keys=True)


def gen_event_schema() -> list[dict[str, Any]]:
    from sw_core.event_engine.schema import RuleSchemaError, validate_rule_dict

    inputs: list[Any] = [
        _rule(),
        [],
        "rule",
        _rule(schema_version=2), _rule(schema_version="1"), _rule(schema_version=1.0), _rule(schema_version=True), _rule(schema_version=_DROP),
        _rule(owner=""), _rule(owner="Bad"), _rule(owner="a" * 65), _rule(owner=None), _rule(owner=12),
        _rule(name="-x"), _rule(name="abc\n"), _rule(name="ok-name"),
        _rule(rule_id="x.y"), _rule(rule_id="o.n"),
        _rule(kind="human"), _rule(kind=_DROP),
        _rule(selectors=[]), _rule(selectors="COM0"), _rule(selectors=[""]), _rule(selectors=["COM0", 1]), _rule(selectors=["ALL"]),
        _rule(level="DEBUG"), _rule(level="WARN"), _rule(profile=5),
        _rule(pattern="x"), _rule(pattern={"kind": "glob", "value": "x"}), _rule(pattern={"kind": "contains", "value": ""}),
        _rule(pattern={"kind": "contains", "value": 3}),
        _rule(pattern={"kind": "contains", "value": "x", "flags": "q"}),
        _rule(pattern={"kind": "contains", "value": "x", "flags": "ism"}),
        _rule(pattern={"kind": "regex", "value": "("}),
        _rule(pattern={"kind": "regex", "value": "a" * 513}),
        _rule(pattern={"kind": "regex", "value": "a" * 512}),
        _rule(pattern={"kind": "regex", "value": "temp=(\\d+)C"}),
        _rule(pattern={"kind": "regex", "value": "a+b+"}),
        _rule(pattern={"kind": "regex", "value": "(a*)\\1"}),
        _rule(pattern={"kind": "regex", "value": "Kernel panic", "flags": "x"}),
        _rule(pattern={"kind": "regex", "value": "root@.*#", "flags": "i"}),
        _rule(scope="weird"), _rule(scope="any"),
        _rule(max_fires=-1), _rule(max_fires=1.5), _rule(max_fires=True), _rule(max_fires=0), _rule(max_fires=None), _rule(max_fires=2 ** 128),
        _rule(cooldown_ms="abc"), _rule(cooldown_ms=None), _rule(cooldown_ms=-1), _rule(cooldown_ms="12"), _rule(cooldown_ms=2 ** 128), _rule(cooldown_ms=1.9),
        _rule(timeout_ms=0), _rule(timeout_ms=2 ** 128), _rule(timeout_ms=[1]),
        _rule(handler="x"), _rule(handler={}), _rule(handler={"exec": ["a"], "shell": "b"}), _rule(handler={"exec": []}),
        _rule(handler={"exec": ["a", ""]}), _rule(handler={"exec": "a"}), _rule(handler={"shell": ""}), _rule(handler={"shell": 3}),
        _rule(handler={"shell": "echo hi"}),
        _rule(auto_enable_com_on_load=0), _rule(debug="yes"),
        _rule(extra_key={"nested": [1, 2]}),
        # 審查 L1：int() 十進位位數上限（4300）訊息與 invalid literal 的 %.200R 截斷
        _rule(cooldown_ms="9" * 5000), _rule(cooldown_ms="9" * 4300), _rule(cooldown_ms="0" * 4301),
        _rule(cooldown_ms=" -" + "9" * 4301 + " "), _rule(cooldown_ms="1" + "_1" * 4300), _rule(cooldown_ms="\uff19" * 4301),
        _rule(cooldown_ms="9" * 5000 + "x"), _rule(cooldown_ms="9" * 4301 + "_"), _rule(max_fires="9" * 4301),
        _rule(timeout_ms="+0" + "0" * 4300),
        # 審查 L2：regex `(?P>x)` 錯誤訊息經 schema 外露
        _rule(pattern={"kind": "regex", "value": "(?P>x)"}), _rule(pattern={"kind": "regex", "value": "(?P"}),
    ]
    rows = []
    for obj in inputs:
        row: dict[str, Any] = {"input": obj}
        try:
            rule = validate_rule_dict(obj)
            row.update(ok=True, raw_dump=_rule_file_dump(dict(rule.raw)), rule_id=rule.rule_id)
        except RuleSchemaError as exc:
            row.update(ok=False, exc_type="RuleSchemaError", message=str(exc))
        except (ValueError, TypeError, OverflowError, AttributeError) as exc:
            row.update(ok=False, exc_type=type(exc).__name__, message=str(exc))
        rows.append(row)
    return rows_with_ids("event_schema", rows)


# ── redos ───────────────────────────────────────────────────────────────────

def gen_redos() -> list[dict[str, Any]]:
    from sw_core.event_engine import schema as s

    samples = [
        ("Kernel panic", ""), ("temp=(\\d+)C", ""), ("root@.*#", ""), ("a+b+", ""), ("(a+)+", ""), ("(a|aa)+", ""),
        ("(a*)\\1", ""), ("a.*a.*", ""), ("(?:ab|abab)*", ""), ("a{0,4096}", ""), ("a{0,4096}b{0,4096}", ""), ("(a?){30}a{30}", ""),
        ("(a|aa){5}", ""), ("(foo|bar)(baz|qux)", ""), ("\\d+\\.\\d+", ""), ("[0-9a-f]{2}(:[0-9a-f]{2}){5}", "i"),
        ("a++b", ""), ("a*+b*", ""), ("(?>a+)+", ""), ("(?>a+)b+", ""), ("a{2}", ""), ("(ab){3}", ""), ("x*?y", ""),
        ("x*?y*?", ""), ("(?=a+)b", ""), ("(?!a+)b+", ""), ("(?<=a)b+", ""), ("(a)(?(1)b+|c)", ""), ("(a)?(?(1)(a+)+b|c)", ""),
        ("(?i:a+)b+", ""), ("^.*$", "m"), (".*", "s"), ("(", ""), ("a**", ""), ("[", ""), ("a|b|c", ""), ("(a|b)c+", ""),
        ("(?P<x>a+)(?P=x)", ""), ("\\bword\\b", ""), ("", ""),
        # sre_parse 分支正規化（共同前綴抽出、單字元分支轉 IN；SubPattern 以 identity 比較，重複不抽出）
        ("(?:xa|xb)", ""), ("(?:x|[ab]|\\d)", ""), ("(?:x|[^ab])", ""), ("(?:ab|ab)", ""), ("x|xy", ""), ("(?:a|.)", ""),
        ("\\D|x", ""), ("(?:(a)|(a))", ""), ("(?:a+b|a+c)", ""), ("(?:^a|^b)", ""), ("(?:[ab]c|[ab]d)", ""),
        ("(a)(?:\\1x|\\1y)", ""), ("(?:ab+|ab+c)", ""), ("(?:xa+|xa+)+", ""), ("(?:[a]|b)+", ""), ("(?:a|b|c)+", ""),
        ("(?:a|bc)+", ""), ("(?x) a + b +", ""), ("(?#c)a+", ""), ("a+", "i"),
        # 審查 L2：`(?P` 後非 `<`／`=` 的 unknown extension 訊息（含下一字元／跳脫）與 EOF
        ("(?P>x)", ""), ("(?P", ""), ("(?P\\d)", ""), ("(?P\\", ""), ("(?P)", ""), ("a(?P-x)", ""), ("(?P\u00e9)", ""),
        ("(?P\U0001F600)", ""), ("(?P\n)", ""), ("((?P'))", ""), ("(?x)(?P x)", ""),
    ]
    rows = []
    for pat, flags in samples:
        row: dict[str, Any] = {"pattern": pat, "flags": flags}
        try:
            fl = s._flags_from_string(flags)
            re.compile(pat, fl)
            parsed = s._sre_parse.parse(pat, fl)
            row.update(compile_ok=True, error=None, score=s._ambiguity_score(parsed), has_backref=s._has_backref(parsed),
                       suspicious=s._maybe_redos_suspicious(parsed))
        except re.error as exc:
            row.update(compile_ok=False, error=str(exc), score=None, has_backref=None, suspicious=None)
        rows.append(row)
    return rows_with_ids("redos", rows)


# ── line_buffer ─────────────────────────────────────────────────────────────

def gen_line_buffer() -> list[dict[str, Any]]:
    from sw_core.event_engine.line_buffer import LineBuffer

    seqs: list[list[bytes]] = [
        [b"hello\n"], [b"a\r\nb\n"], [b"partial", b" line\n"], [b"x\ny\nz"], [b"z", b"\n"],
        [b"a" * 16384 + b"\n"], [b"b" * 16385 + b"\n"], [b"c" * 16385], [b"d" * 20000, b"tail\n"],
        [b"e" * 16384, b"\r\n"], [b"f" * 16384 + b"\r", b"\n"], [b"g" * 16385 + b"\r\n"],
        [b"\xff\xfebad\n"], [b"\xe4\xb8", b"\xad\xe6\x96\x87\n"],
        [b"\x1b[31mred\x1b[0m\n"], [b"\x1b]0;title\x07after\n"], [b"\x1b]0;title\x1b\\after\n"], [b"a\x1bcb\n"], [b"\x1b(Bplain\n"],
        [b"\n\n\n"], [b""], [b"\r\n"],
    ]
    rows = []
    for feeds in seqs:
        lb = LineBuffer()
        outs = [lb.feed(f) for f in feeds]
        rows.append({"feeds": [b64(f) for f in feeds], "outputs": outs})
    return rows_with_ids("line_buffer", rows)


# ── event_payload ───────────────────────────────────────────────────────────

def gen_event_payload() -> list[dict[str, Any]]:
    from sw_core.event_engine.dispatcher import build_env, build_payload
    from sw_core.event_engine.matcher import MatchResult, MatcherFire
    from sw_core.event_engine.schema import validate_rule_dict

    inherited = {"PATH": "/usr/bin:/bin", "HOME": "/home/golden", "LANG": "C.UTF-8", "OTHER": "nope"}
    cases = [
        (_rule(), "COM0", MatchResult("x", []), 7, None, 1, "the x line"),
        (_rule(pattern={"kind": "regex", "value": "temp=(\\d+)C"}, level="WARN", scope="any", profile="prpl-template"),
         "COM1", MatchResult("temp=42C", ["42"]), 99, "cmd-1", 3, "sensor temp=42C ok"),
        (_rule(name="unicode"), "COM0", MatchResult("中文", []), 1, None, 2, "中文 line"),
        (_rule(name="long"), "COM0", MatchResult("y" * 4096, []), 5, None, 1, "y" * 4096),
        (_rule(name="longer"), "COM0", MatchResult("z" * 4097, []), 6, None, 1, "z" * 4097),
        (_rule(pattern={"kind": "regex", "value": "(a)?(b)"}), "COM2", MatchResult("b", [None, "b"]), 8, None, 2 ** 70, "b"),
    ]
    rows = []
    for raw, sel, m, seq, cmd, count, line in cases:
        rule = validate_rule_dict(raw)
        fire = MatcherFire(rule=rule, selector=sel, match=m, wal_seq=seq, matched_at=1700000000123, active_cmd_id=cmd)
        payload = build_payload(fire, fire_count=count, matched_line=line)
        stdin = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with mock.patch.dict(os.environ, inherited, clear=True):
            env = build_env(fire, fire_count=count)
        rows.append({
            "fire": {"rule": raw, "selector": sel, "matched_text": m.matched_text, "groups": m.groups, "wal_seq": seq,
                     "matched_at": 1700000000123, "active_cmd_id": cmd},
            "fire_count": count, "matched_line": line, "inherited": [[k, v] for k, v in inherited.items()],
            "stdin_b64": b64(stdin), "env": [[k, v] for k, v in env.items()],
        })
    return rows_with_ids("event_payload", rows)


# ── event_log_tail ──────────────────────────────────────────────────────────

def gen_event_log_tail() -> list[dict[str, Any]]:
    from sw_core.event_engine.event_log import EventLogger

    events = [
        {"type": "rule_loaded", "rule_id": "o.a"},
        {"type": "match_recorded", "rule_id": "o.a", "selector": "COM0", "ts": 1000},
        {"type": "fire_completed", "rule_id": "o.b", "selector": "COM1", "ts": 2000},
        {"type": "match_recorded", "rule_id": "o.a", "selector": "COM1"},
        {"type": "中文", "rule_id": "o.b", "selector": "COM0", "ts": 3000},
    ]
    queries: list[dict[str, Any]] = [
        {}, {"n": None}, {"n": 0}, {"n": 3}, {"n": -2}, {"n": True}, {"n": 100},
        {"rule_id": "o.a"}, {"rule_id": "o.b", "n": 1}, {"selector": "COM1"}, {"rule_id": "o.a", "selector": "COM0"},
        {"since_ts": 2000}, {"since_ts": 2001}, {"since_ts": 0}, {"since_ts": 1500, "rule_id": "o.b"}, {"rule_id": "missing"},
    ]
    rows = []
    ts0 = 1700000000000
    for q in queries:
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "events.ndjson")
            log = EventLogger(path)
            clock = iter(range(ts0, ts0 + 1000))
            with mock.patch("sw_core.event_engine.event_log.time.time", side_effect=lambda: next(clock) / 1000):
                for ev in events:
                    log.write(dict(ev))
            with open(path, "ab") as f:
                f.write(b"{not json}\n")
            try:
                got = log.tail(**q)
                entries = json.dumps(got, ensure_ascii=False)
            except Exception as exc:  # noqa: BLE001 — golden 記錄例外型別
                entries = "EXCEPTION"
                q = dict(q, _exc=type(exc).__name__)
        rows.append({"ts0": ts0, "events": events, "query": q, "rule_id": q.get("rule_id"), "selector": q.get("selector"),
                     "since_ts": q.get("since_ts"), "n": q.get("n"), "has_n": "n" in q, "entries": entries})
    return rows_with_ids("event_log_tail", rows)


# ── registry_load ───────────────────────────────────────────────────────────

def gen_registry_load() -> list[dict[str, Any]]:
    from sw_core.event_engine.registry import RuleRegistry

    good = json.dumps(_rule()).encode()
    dirs: list[dict[str, bytes]] = [
        {},
        {"o.n.json": good},
        {"o.n.json": good, "notes.txt": b"x", "o.m.json.tmp": b"{"},
        {"a.json": b"{", "b.json": b"", "c.json": b"[1,", "d.json": b"{\"a\": }"},
        {"e.json": json.dumps(_rule(schema_version=2)).encode()},
        {"f.json": json.dumps(_rule(cooldown_ms="abc")).encode()},
        {"g.json": json.dumps(_rule(cooldown_ms=None)).encode()},
        {"h.json": b"\xff\xfe"},
        {"i.json": b"[]"},
        {"z.json": json.dumps(_rule(name="z")).encode(), "a.json": json.dumps(_rule(name="a")).encode()},
        {"j.json": json.dumps(_rule(name="j"), ensure_ascii=False).encode() + b"\n\n"},
        {"k.json": b"{\"schema_version\": 1, \"owner\": \"o\", \"name\": \"k\", \"kind\": \"tool\"} trailing"},
    ]
    rows = []
    for files in dirs:
        with tempfile.TemporaryDirectory() as td:
            for name, data in files.items():
                with open(os.path.join(td, name), "wb") as f:
                    f.write(data)
            res = RuleRegistry(td).load_all()
            rows.append({"files": {k: b64(v) for k, v in files.items()}, "rules": [r.rule_id for r in res.rules],
                         "failed": [[os.path.basename(fl.path), fl.reason.replace(td, GOLDEN_ROOT)] for fl in res.failed]})
    return rows_with_ids("registry_load", rows)


# ── counter_file ────────────────────────────────────────────────────────────

def gen_counter_file() -> list[dict[str, Any]]:
    from sw_core.event_engine.counter import Counter, CounterStore

    rows: list[dict[str, Any]] = []
    for c in [Counter(), Counter(fires=3, last_fire_ts=1700000000123, exhausted=True), Counter(fires=2 ** 70, last_fire_ts=None)]:
        with tempfile.TemporaryDirectory() as td:
            st = CounterStore(td)
            st.save("o.n", c)
            data = pathlib.Path(st.path_for("o.n")).read_bytes()
            rows.append({"kind": "save", "counter": c.to_json(), "file_b64": b64(data)})
    for raw in [None, b"", b"{", b"[]", b"{}", b"{\"fires\": 5}", b"{\"fires\": \"7\", \"last_fire_ts\": \"9\", \"exhausted\": 1}",
                b"{\"fires\": 1.9}", b"{\"fires\": null}", b"\xff", b"{\"last_fire_ts\": null, \"exhausted\": false}"]:
        with tempfile.TemporaryDirectory() as td:
            st = CounterStore(td)
            if raw is not None:
                pathlib.Path(st.path_for("o.n")).write_bytes(raw)
            try:
                got: Any = st.load("o.n").to_json()
            except Exception as exc:  # noqa: BLE001
                got = {"raised": type(exc).__name__}
            rows.append({"kind": "load", "file_b64": None if raw is None else b64(raw), "counter": got})
    return rows_with_ids("counter_file", rows)


# ── event_engine_order ──────────────────────────────────────────────────────

def gen_event_engine_order() -> list[dict[str, Any]]:
    from sw_core.event_engine.engine import EngineDeps, EventEngine

    class _Bridge:
        def __init__(self, coms: list[str]) -> None:
            self._coms = coms

        def active_cmd_id_for(self, com: str) -> None:
            return None

        def profile_for(self, com: str) -> None:
            return None

        def known_coms(self) -> list[str]:
            return list(self._coms)

    def run(setup: dict[str, Any]) -> list[str]:
        with tempfile.TemporaryDirectory() as td:
            ev = os.path.join(td, "events.d")
            os.makedirs(ev)
            for name, obj in setup.get("files", {}).items():
                with open(os.path.join(ev, name), "w", encoding="utf-8") as f:
                    json.dump(obj, f)
            log = os.path.join(td, "events.ndjson")
            eng = EventEngine(EngineDeps(events_dir=ev, runtime_dir=os.path.join(td, "rt"), log_path=log,
                                         bridge=_Bridge(setup.get("known_coms", []))))
            try:
                eng.start()
                for op in setup.get("ops", []):
                    if op["op"] == "rule_set":
                        eng.rule_set(op["rule"])
                    elif op["op"] == "reload":
                        with open(os.path.join(ev, op["file"]), "w", encoding="utf-8") as f:
                            json.dump(op["rule"], f)
                        eng.reload()
            finally:
                eng.stop()
            types_ = []
            if os.path.exists(log):
                for line in pathlib.Path(log).read_text(encoding="utf-8").splitlines():
                    types_.append(json.loads(line)["type"])
            return types_

    setups = {
        "start": {"files": {"o.n.json": _rule()}, "known_coms": ["COM0", "COM1"]},
        "start_all": {"files": {"o.n.json": _rule(selectors=["ALL"])}, "known_coms": ["COM0", "COM1"]},
        "start_no_auto": {"files": {"o.n.json": _rule(auto_enable_com_on_load=False)}, "known_coms": ["COM0"]},
        "start_failed": {"files": {"bad.json": {"schema_version": 2}, "o.n.json": _rule()}, "known_coms": ["COM0"]},
        "rule_set": {"ops": [{"op": "rule_set", "rule": _rule()}], "known_coms": ["COM0"]},
        "rule_set_all": {"ops": [{"op": "rule_set", "rule": _rule(selectors=["ALL"])}], "known_coms": ["COM0", "COM1"]},
        "reload": {"files": {"o.n.json": _rule()}, "ops": [{"op": "reload", "file": "o.m.json", "rule": _rule(name="m", selectors=["COM1"])}],
                   "known_coms": ["COM0", "COM1"]},
    }
    rows = []
    for key, setup in setups.items():
        rows.append({"id": f"event_engine_order:{key}", "setup": setup, "types": run(setup),
                     "triggered_by": {"start": "auto", "rule_set": "rule_set", "reload": "reload"}.get(key.split("_")[0], "auto")})
    return rows


# ── login_fsm_scenarios ─────────────────────────────────────────────────────

def gen_login_fsm_scenarios() -> list[dict[str, Any]]:
    from p7_login_cases import login_cases

    return run_login_cases(login_cases())


def run_login_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from p7_fake_bridge import ScriptedBridge, VirtualClock
    from sw_core import login_fsm
    from sw_core.auth import SessionAuth

    rows = []
    for case in cases:
        tpl = case.get("template")
        sp = session_profile(tpl, **case.get("profile_over", {}))
        clock = VirtualClock()
        bridge = ScriptedBridge(case, clock)
        auth_raw = case.get("auth")
        auth = None if auth_raw is None else SessionAuth(**auth_raw)
        row_case = {k: v for k, v in case.items() if k not in ("template", "profile_over")}
        row_case["profile"] = profile_json(sp)
        result: Any = None
        raised = None
        with mock.patch.object(login_fsm.time, "monotonic", clock.monotonic), \
             mock.patch.object(login_fsm.time, "sleep", clock.sleep), \
             mock.patch.object(login_fsm.uuid, "uuid4", bridge.next_nonce_uuid), \
             mock.patch.dict(os.environ, case.get("env", {}), clear=True):
            try:
                if case["entry"] == "ensure_ready":
                    ok, err = login_fsm.ensure_ready(bridge, sp, auth)
                    result = [ok, err]
                elif case["entry"] == "probe_ready":
                    ok, err = login_fsm.probe_ready(bridge, sp)
                    result = [ok, err]
                elif case["entry"] == "detect_template":
                    tpls = [_templates()[n] for n in case["templates"]]
                    got = login_fsm.detect_template(bridge, tpls, case.get("probe_timeout_s", 3.0))
                    result = [got is not None, None if got is None else got.profile_name]
                else:
                    raise ValueError(case["entry"])
            except re.error as exc:
                raised = type(exc).__name__
                result = [None, None]   # 例外列：result 以 [null, null] 佔位（test 以 result[1] 收集錯誤碼集合）
        rows.append({"case": row_case, "sent": [[s, b64(p), log] for s, p, log in bridge.sent], "result": result,
                     "raised": raised, "virtual_elapsed_s": round(clock.now, 6)})
    return rows_with_ids("login_fsm_scenarios", rows)


# ── session_manager 驅動（quiet／gate／reprobe／self_test／recover／log）──────

def _sm_gen(name: str) -> Callable[[], list[dict[str, Any]]]:
    def run() -> list[dict[str, Any]]:
        import p7_sm_goldens

        return getattr(p7_sm_goldens, f"gen_{name}")()

    return run


GENERATORS: dict[str, Callable[[], list[dict[str, Any]]]] = {
    "env_file": gen_env_file,
    "auth_resolution": gen_auth_resolution,
    "login_fsm_scenarios": gen_login_fsm_scenarios,
    "quiet_rx": _sm_gen("quiet_rx"),
    "gate_responses": _sm_gen("gate_responses"),
    "reprobe_decisions": _sm_gen("reprobe_decisions"),
    "reprobe_valid": _sm_gen("reprobe_valid"),
    "transport_stall": gen_transport_stall,
    "self_test_responses": _sm_gen("self_test_responses"),
    "recover_responses": _sm_gen("recover_responses"),
    "log_responses": _sm_gen("log_responses"),
    "event_schema": gen_event_schema,
    "redos": gen_redos,
    "line_buffer": gen_line_buffer,
    "event_payload": gen_event_payload,
    "event_log_tail": gen_event_log_tail,
    "event_engine_order": gen_event_engine_order,
    "registry_load": gen_registry_load,
    "counter_file": gen_counter_file,
}


def render(name: str) -> bytes:
    rows = GENERATORS[name]()
    rows = sorted(rows, key=lambda r: r["id"])
    return "".join(dumps_line(r) + "\n" for r in rows).encode("utf-8")


def main(out_dir: str = "contracts/golden/p7", check: bool = False) -> int:
    try:
        import re2  # noqa: F401
    except ImportError:
        pass
    else:
        print("[capture_p7_goldens] 偵測到 google-re2；EVT-D-S15 golden 必須以無 re2 的 Python 擷取", file=sys.stderr)
        return 2
    out = pathlib.Path(out_dir)
    if not out.is_absolute():
        out = ROOT / out
    rc = 0
    if not check:
        out.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        data = render(name)
        path = out / f"{name}.ndjson"
        if check:
            if not path.is_file() or path.read_bytes() != data:
                print(f"[capture_p7_goldens] STALE {path}", file=sys.stderr)
                rc = 1
        else:
            path.write_bytes(data)
    return rc


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="contracts/golden/p7")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    sys.exit(main(a.out, a.check))
