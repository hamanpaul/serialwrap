"""T2：AST 碼抽取器（tools/contracts/extract.py）的失敗測試（TDD red）。

依 docs/rust-refactor/plans/01-stage0-contracts.md Task T2 撰寫。
實作尚未存在時，所有測試應因 tools.contracts.extract 缺失（ModuleNotFoundError）
或 contracts/code_extraction.yaml 缺失而失敗，不得因語法或收集錯誤失敗。

所有 tools.contracts.* 的 import 一律延遲到函式內（_load），
確保 `python3 -m pytest -q tests/` 能完成 collection。
"""

from __future__ import annotations

import ast
import fnmatch
import importlib
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

BASE_CONFIG = textwrap.dedent("""\
    schema_version: 1
    roots: ["pkg/**/*.py"]
    exclude: []
    code_pattern: '^[A-Z][A-Z0-9_]*[A-Z0-9]$'
    dynamic_prefix_pattern: '^[A-Z][A-Z0-9_]*:'
    max_depth: 12
    exclude_functions: []
    namespaces:
      error_code:
        sinks:
          - {kind: dict_key, key: error_code}
          - {kind: subscript_store, key: error_code}
          - {kind: call_keyword, keyword: error_code}
          - {kind: param_default, param: error_code}
          - {kind: call_arg, callee: TunnelError, index: 0}
          - {kind: call_arg, callee: RuntimeError, index: 0, value_pattern: '^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$', implies_interface: proc}
        relays:
          - {kind: call_method_arg, method: get, arg: error_code}
          - {kind: subscript_load, key: error_code}
          - {kind: attribute_load, attr: code}
        passthrough:
          - {kind: attribute_load, attr: last_error, emits: last_error_passthrough}
          - {kind: call_method_arg, method: get, arg: last_error, emits: last_error_passthrough}
      session_last_error:
        sinks:
          - {kind: attribute_store, attr: last_error}
        relays:
          - {kind: attribute_load, attr: last_error}
        passthrough: []
    allow_unresolved: []
    exclude_values: []
    """)


def _load():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module("tools.contracts.extract")


def _run(tmp_path, files, config=BASE_CONFIG):
    ex = _load()
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(src), encoding="utf-8")
    (tmp_path / "pkg" / "__init__.py").touch()
    cfg = tmp_path / "contracts" / "code_extraction.yaml"
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text(config, encoding="utf-8")
    return ex.extract(tmp_path, ex.load_extraction_config(cfg))


def _reasons(res) -> set[str]:
    """回傳 res.unresolved 的 reason 集合（對應計畫中的 U）。"""
    return {u.reason for u in res.unresolved}


def _with(text: str, old: str, new: str) -> str:
    """將 text 中唯一出現的 old 換成 new；出現次數不為 1 即失敗，避免替換失效而假綠。"""
    assert text.count(old) == 1, f"替換目標必須恰出現一次：{old!r}"
    return text.replace(old, new)


def _write_config(tmp_path, text: str) -> Path:
    cfg = tmp_path / "contracts" / "code_extraction.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8")
    return cfg


# ---------------------------------------------------------------------------
# 常數傳遞、參數／回傳傳遞、relay、passthrough
# ---------------------------------------------------------------------------


def test_imported_constant_is_propagated(tmp_path):
    res = _run(tmp_path, {
        "pkg/constants.py": 'ERROR_X = "X_CODE"\n',
        "pkg/svc.py": """\
            from .constants import ERROR_X as E
            def f():
                return {"ok": False, "error_code": E}
            """,
    })
    assert res.values("error_code") == {"X_CODE"}
    assert res.unresolved == ()


def test_return_tuple_index_propagation(tmp_path):
    res = _run(tmp_path, {"pkg/ft.py": """\
        def _probe():
            if a():
                return None, "TRANSFER_TIMEOUT"
            return None, "TARGET_DECODER_MISSING"
        def a():
            return True
        def push():
            dec, probe_error = _probe()
            if probe_error is not None:
                return {"ok": False, "error_code": probe_error}
        """})
    assert res.values("error_code") == {"TRANSFER_TIMEOUT", "TARGET_DECODER_MISSING"}


def test_param_chain_into_last_error(tmp_path):
    res = _run(tmp_path, {"pkg/sm.py": """\
        class M:
            def _detach(self, s, *, reason):
                s.last_error = reason
            def _by_id(self, b, *, reason):
                self._detach(b, reason=reason)
            def clear(self, s):
                self._detach(s, reason="CLEARED")
            def update(self, b, x):
                self._by_id(b, reason="DEVICE_REMOVED" if x else "DEVICE_REBOUND_REQUIRED")
        """})
    assert res.values("session_last_error") == {"CLEARED", "DEVICE_REMOVED", "DEVICE_REBOUND_REQUIRED"}


def test_literal_dict_key(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f():
            return {"ok": False, "error_code": "FOO_BAR"}
        """})
    assert res.values("error_code") == {"FOO_BAR"}


def test_dict_call_keyword_form(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f():
            return dict(ok=False, error_code="FOO")
        """})
    assert res.values("error_code") == {"FOO"}


def test_module_attribute_constant(tmp_path):
    res = _run(tmp_path, {
        "pkg/constants.py": 'ERROR_X = "X_CODE"\n',
        "pkg/svc.py": """\
            from . import constants as c
            def f():
                return {"error_code": c.ERROR_X}
            """,
    })
    assert res.values("error_code") == {"X_CODE"}


def test_class_attribute_constant(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        class S:
            CODE = "S_CODE"
            def m(self):
                return {"error_code": self.CODE}
        """})
    assert res.values("error_code") == {"S_CODE"}


def test_boolop_dict_get_with_passthrough(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f(state, snap):
            code = {"FLASHING": "FLASHING_BUSY"}.get(state) or snap.get("last_error") or "NOT_READY"
            return {"error_code": code}
        """})
    assert res.values("error_code") == {"FLASHING_BUSY", "NOT_READY"}
    assert {"last_error_passthrough"} <= res.values("error_code", include_passthrough=True)


def test_subscript_store(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f(rec):
            rec["error_code"] = "SEND_FAILED"
        """})
    assert res.values("error_code") == {"SEND_FAILED"}


def test_relay_get_not_emitted(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f(rec, result):
            rec["error_code"] = result.get("error_code") or "COMMAND_FAILED"
        """})
    assert res.values("error_code") == {"COMMAND_FAILED"}
    assert _reasons(res) == set()


def test_param_default(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def unregister(sid, error_code: str = "FLUSHED_BY_RECOVERY"):
            rec["error_code"] = error_code
        """})
    assert res.values("error_code") == {"FLUSHED_BY_RECOVERY"}


def test_call_keyword_sink(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f(arb, sid):
            arb.unregister(sid, error_code="FLUSHED_BY_SHUTDOWN")
        """})
    assert res.values("error_code") == {"FLUSHED_BY_SHUTDOWN"}


def test_positional_callsite_propagation(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        class R:
            def _fail(self, code):
                return {"error_code": code}
            def a(self):
                return self._fail("A_CODE")
            def b(self):
                return self._fail(code="B_CODE")
        """})
    assert res.values("error_code") == {"A_CODE", "B_CODE"}


def test_exception_call_arg(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        class TunnelError(Exception):
            ...
        def f(rt):
            raise TunnelError("INVALID_TARGET", "x")
        def g(rt):
            raise rt.TunnelError("SSH_NOT_FOUND")
        """})
    assert res.values("error_code") == {"INVALID_TARGET", "SSH_NOT_FOUND"}


def test_exception_code_attr_is_relay(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f(run):
            try:
                run()
            except TunnelError as exc:
                return {"error_code": exc.code}
        """})
    assert res.values("error_code") == set()
    assert _reasons(res) == set()


def test_runtime_error_value_pattern(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f():
            raise RuntimeError("DAEMON_ALREADY_RUNNING")
        def g():
            raise RuntimeError("serial not ready")
        """})
    assert res.values("error_code") == {"DAEMON_ALREADY_RUNNING"}
    # 不符 value_pattern 的 RuntimeError 字串須靜默忽略，不得產生 unresolved
    assert _reasons(res) == set()
    assert [e.implies_interface for e in res.emissions if e.value == "DAEMON_ALREADY_RUNNING"] == ["proc"]


def test_non_sink_uppercase_ignored(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f():
            return {"warning": {"code": "CMD_LENGTH_WARNING"}}
        X = "EMPTY_COMMAND"
        """})
    assert res.values("error_code") == set()


def test_none_and_empty_ignored(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f():
            return {"error_code": None}
        def g():
            return {"error_code": ""}
        """})
    assert res.values("error_code") == set()
    assert _reasons(res) == set()


def test_malformed_code_in_explicit_sink_unresolved(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f():
            return {"error_code": "new-code"}
        def g():
            raise TunnelError("bad code")
        """})
    assert res.values("error_code") == set()
    assert sorted(u.reason for u in res.unresolved) == ["malformed_code", "malformed_code"]


def test_malformed_via_propagation_unresolved(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def _fail(code):
            return {"error_code": code}
        def h():
            return _fail("oops")
        """})
    assert _reasons(res) == {"malformed_code"}


def test_relay_get_default_resolved(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f(resp):
            return {"error_code": resp.get("error_code", "NEW_CODE")}
        def g(resp):
            fb = "FB_CODE"
            return {"error_code": resp.get("error_code", fb)}
        """})
    assert res.values("error_code") == {"NEW_CODE", "FB_CODE"}
    assert _reasons(res) == set()


def test_roots_glob_includes_flat_and_nested(tmp_path):
    res = _run(tmp_path, {
        "pkg/flat.py": """\
            def f():
                return {"error_code": "FLAT_CODE"}
            """,
        "pkg/sub/deep.py": """\
            def g():
                return {"error_code": "DEEP_CODE"}
            """,
    })
    assert {"FLAT_CODE", "DEEP_CODE"} <= res.values("error_code")
    # 對照：fnmatch 對 "pkg/**/*.py" 不匹配直層 pkg/flat.py，因此若實作改用 fnmatch 本測試會失敗
    assert fnmatch.fnmatch("pkg/flat.py", "pkg/**/*.py") is False


def test_canon_expr_version_stable():
    ex = _load()
    node = ast.parse("a.get('x') or f(b, k=1)", mode="eval").body
    assert ex.canon_expr(node) == (
        "BoolOp(op=Or(),values=["
        "Call(func=Attribute(value=Name(id='a'),attr='get'),args=[Constant(value='x')]),"
        "Call(func=Name(id='f'),args=[Name(id='b')],keywords=[keyword(arg='k',value=Constant(value=1))])"
        "])"
    )


def test_fstring_dynamic_prefix(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f(s, e):
            s.last_error = f"REPROBE_FAILED:{type(e).__name__}"
        """})
    assert res.values("session_last_error") == {"REPROBE_FAILED:*"}
    assert {e.kind for e in res.emissions if e.value == "REPROBE_FAILED:*"} == {"dynamic_prefix"}


def test_binop_dynamic_prefix(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f(s, reason):
            s.last_error = "BRIDGE_DOWN:" + reason
        """})
    assert res.values("session_last_error") == {"BRIDGE_DOWN:*"}


def test_last_error_self_relay(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f(s):
            s.last_error = s.last_error or "RECOVERY_TIMEOUT"
        """})
    assert res.values("session_last_error") == {"RECOVERY_TIMEOUT"}
    assert _reasons(res) == set()


def test_unknown_callee_unresolved(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def f():
            return {"error_code": compute()}
        """})
    assert _reasons(res) == {"unknown_callee"}
    assert all(u.function == "pkg/x.py::f" for u in res.unresolved)


def test_cycle_guard(tmp_path):
    res = _run(tmp_path, {"pkg/x.py": """\
        def a():
            return b()
        def b():
            return a()
        def f():
            return {"error_code": a()}
        """})
    assert _reasons(res) == {"cycle"}


def test_max_depth(tmp_path):
    chain = "".join(f"def f{i}():\n    return f{i + 1}()\n" for i in range(20))
    chain += 'def f20():\n    return "DEEP_CODE"\n'
    chain += "def top():\n    return {\"error_code\": f0()}\n"
    res = _run(tmp_path, {"pkg/x.py": chain})
    assert _reasons(res) == {"max_depth"}


def test_exclude_function(tmp_path):
    cfg = _with(BASE_CONFIG, "exclude_functions: []",
                'exclude_functions:\n  - "pkg/x.py::R.last_error"')
    cfg = _with(cfg, "- {kind: attribute_store, attr: last_error}",
                "- {kind: attribute_store, attr: _last_error}")
    res = _run(tmp_path, {"pkg/x.py": """\
        class R:
            @property
            def last_error(self):
                return self._last_error
            @last_error.setter
            def last_error(self, value):
                self._last_error = value
        """}, config=cfg)
    assert _reasons(res) == set()


def test_allow_unresolved_and_stale(tmp_path):
    allow_yaml = textwrap.dedent("""\
        allow_unresolved:
          - {namespace: error_code, function: "pkg/x.py::f", expr: "Call(func=Name(id='compute'))", reason: "測試：比對得到的項目"}
          - {namespace: error_code, function: "pkg/x.py::f", expr: "Name(id='nothing')", reason: "測試：比對不到的項目"}""")
    cfg = _with(BASE_CONFIG, "allow_unresolved: []", allow_yaml)
    res = _run(tmp_path, {"pkg/x.py": """\
        def f():
            return {"error_code": compute()}
        """}, config=cfg)
    assert res.unresolved == ()
    assert [a.expr for a in res.stale_allow_unresolved] == ["Name(id='nothing')"]


def test_exclude_values_and_stale(tmp_path):
    excl_yaml = textwrap.dedent("""\
        exclude_values:
          - {namespace: error_code, value: FOO, category: not_a_code, reason: "測試：有 emission 的排除項"}
          - {namespace: error_code, value: GHOST, category: not_a_code, reason: "測試：無 emission 的排除項"}""")
    cfg = _with(BASE_CONFIG, "exclude_values: []", excl_yaml)
    res = _run(tmp_path, {"pkg/x.py": """\
        def f():
            return {"error_code": "FOO"}
        """}, config=cfg)
    assert "FOO" not in res.values("error_code")
    assert any(ex.value == "FOO" and n >= 1 for ex, n in res.applied_exclusions)
    assert [ex.value for ex in res.stale_exclude_values] == ["GHOST"]


def test_config_unknown_kind_rejected(tmp_path):
    ex = _load()
    cfg = _with(BASE_CONFIG, "- {kind: dict_key, key: error_code}",
                "- {kind: magic, key: error_code}")
    path = _write_config(tmp_path, cfg)
    with pytest.raises(ex.ExtractionConfigError):
        ex.load_extraction_config(path)


def test_config_missing_field_rejected(tmp_path):
    ex = _load()
    no_index = _with(BASE_CONFIG, "{kind: call_arg, callee: TunnelError, index: 0}",
                     "{kind: call_arg, callee: TunnelError}")
    with pytest.raises(ex.ExtractionConfigError):
        ex.load_extraction_config(_write_config(tmp_path, no_index))
    no_category = _with(BASE_CONFIG, "exclude_values: []",
                        'exclude_values:\n  - {namespace: error_code, value: FOO, reason: "x"}')
    with pytest.raises(ex.ExtractionConfigError):
        ex.load_extraction_config(_write_config(tmp_path, no_category))


def test_config_duplicate_key_rejected(tmp_path):
    ex = _load()
    cfg = _with(BASE_CONFIG, "max_depth: 12", "max_depth: 12\nmax_depth: 13")
    with pytest.raises(ex.ExtractionConfigError):
        ex.load_extraction_config(_write_config(tmp_path, cfg))


# ---------------------------------------------------------------------------
# 黑箱獨立性：tools/contracts 不得 import sw_core；只得用 stdlib 與 PyYAML
# ---------------------------------------------------------------------------


def _tools_contracts_files() -> list[Path]:
    files = sorted((ROOT / "tools" / "contracts").rglob("*.py"))
    assert files, "tools/contracts 下找不到任何 .py（抽取器模組尚未實作）"
    return files


def _imported_top_names(tree: ast.AST) -> set[str]:
    """回傳 AST 內所有絕對 import 的頂層模組名；相對 import（level > 0）不計。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_no_sw_core_import():
    offenders = {}
    for path in _tools_contracts_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if "sw_core" in _imported_top_names(tree):
            offenders[str(path)] = ["sw_core"]
    assert offenders == {}


def test_tools_contracts_imports_stdlib_and_yaml_only():
    allowed = set(sys.stdlib_module_names) | {"yaml", "tools"}
    offenders = {}
    for path in _tools_contracts_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = _imported_top_names(tree) - allowed
        if bad:
            offenders[str(path)] = sorted(bad)
    assert offenders == {}


# ---------------------------------------------------------------------------
# 變異測試：每列在合成 repo 植入一個「目錄沒有的碼」，抽取器必須抓到
# （任一列抓不到即代表抽取路徑有洞；審查 B 必須執行並附輸出）
# ---------------------------------------------------------------------------

MUTATIONS = [
    pytest.param("error_code", {"pkg/m.py": """\
        def f():
            return {"error_code": "MUT_A"}
        """}, "MUT_A", id="dict_key"),
    pytest.param("error_code", {"pkg/m.py": """\
        def f():
            return dict(error_code="MUT_B")
        """}, "MUT_B", id="dict_call"),
    pytest.param("error_code", {"pkg/m.py": """\
        def f(rec):
            rec["error_code"] = "MUT_C"
        """}, "MUT_C", id="subscript_store"),
    pytest.param("error_code", {"pkg/m.py": """\
        def f(arb, sid):
            arb.unregister(sid, error_code="MUT_D")
        """}, "MUT_D", id="call_keyword"),
    pytest.param("error_code", {"pkg/m.py": """\
        def f(error_code="MUT_E"):
            return error_code
        """}, "MUT_E", id="param_default"),
    pytest.param("error_code", {"pkg/m.py": """\
        def f():
            raise TunnelError("MUT_F")
        """}, "MUT_F", id="call_arg_exc"),
    pytest.param("error_code", {"pkg/m.py": """\
        def f():
            raise RuntimeError("MUT_G_X")
        """}, "MUT_G_X", id="call_arg_proc"),
    pytest.param("session_last_error", {"pkg/m.py": """\
        def f(s):
            s.last_error = "MUT_H"
        """}, "MUT_H", id="attribute_store_le"),
    pytest.param("error_code", {
        "pkg/m.py": """\
            from .c import E
            def f():
                return {"error_code": E}
            """,
        "pkg/c.py": 'E = "MUT_I"\n',
    }, "MUT_I", id="imported_const"),
    pytest.param("error_code", {
        "pkg/m.py": """\
            from . import c as k
            def f():
                return {"error_code": k.E2}
            """,
        "pkg/c.py": 'E2 = "MUT_S"\n',
    }, "MUT_S", id="module_attr_const"),
    pytest.param("error_code", {"pkg/m.py": """\
        def p():
            return None, "MUT_J"
        def g():
            _, e = p()
            return {"error_code": e}
        """}, "MUT_J", id="return_tuple"),
    pytest.param("error_code", {"pkg/m.py": """\
        def g(code):
            return {"error_code": code}
        def h():
            return g("MUT_K")
        """}, "MUT_K", id="callsite_arg"),
    pytest.param("error_code", {"pkg/m.py": """\
        class C:
            def g(self, code):
                return {"error_code": code}
            def h(self):
                return self.g(code="MUT_L")
        """}, "MUT_L", id="kw_callsite_arg"),
    pytest.param("error_code", {"pkg/m.py": """\
        def f(resp):
            return {"error_code": resp.get("error_code", "MUT_M")}
        """}, "MUT_M", id="relay_default"),
    pytest.param("error_code", {"pkg/m.py": """\
        def f(snap):
            code = snap.get("last_error") or "MUT_N"
            return {"error_code": code}
        """}, "MUT_N", id="passthrough_or"),
    pytest.param("error_code", {"pkg/m.py": """\
        def f(x):
            return {"error_code": "MUT_O" if x else None}
        """}, "MUT_O", id="ifexp"),
    pytest.param("error_code", {"pkg/m.py": """\
        def f(state):
            return {"error_code": {"S": "MUT_P"}.get(state)}
        """}, "MUT_P", id="dict_get_lookup"),
    pytest.param("session_last_error", {"pkg/m.py": """\
        def f(s, x):
            s.last_error = f"MUT_Q:{x}"
        """}, "MUT_Q:*", id="fstring_prefix"),
    pytest.param("session_last_error", {"pkg/m.py": """\
        def f(s, x):
            s.last_error = "MUT_R:" + x
        """}, "MUT_R:*", id="binop_prefix"),
]


@pytest.mark.parametrize(("namespace", "files", "expected"), MUTATIONS)
def test_mutation_detects(tmp_path, namespace, files, expected):
    res = _run(tmp_path, files)
    assert expected in res.values(namespace)


# ---------------------------------------------------------------------------
# 真實 repo 測試：讀 contracts/code_extraction.yaml 對 sw_core 執行
# TRICKY_* 為測試資料（驗證抽取器沒漏掉已知的困難傳遞路徑），不是政策清單
# ---------------------------------------------------------------------------

TRICKY_ERROR_CODES = {
    "AUTOBOOT_QUIET", "READY_UNCONFIRMED", "CMD_TOO_LONG", "CMD_CONTAINS_NEWLINE",
    "INVALID_COMPLETION_REGEX", "FLUSHED_BY_RECOVERY", "FLUSHED_BY_SHUTDOWN", "SEND_FAILED",
    "COMMAND_FAILED", "TARGET_DECODER_MISSING", "TARGET_ENCODER_MISSING", "TRANSFER_PROBE_FAILED",
    "NOT_READY", "INVALID_TARGET", "BENCH_ENDPOINT_NOT_REMEMBERED", "TUNNEL_STATUS_ERROR",
    "DAEMON_ALREADY_RUNNING", "SOCKET_UNAVAILABLE", "NEEDS_SUDO", "NO_SYSTEMD",
}
TRICKY_LAST_ERRORS = {
    "CREDENTIALS_UNRESOLVED", "PROMPT_UNAVAILABLE", "BOOTLOADER_STUCK", "CLEARED", "RELEASED",
    "REBOUND", "DEVICE_REMOVED", "DEVICE_REBOUND_REQUIRED", "RECOVERY_TIMEOUT", "RECOVERY_ERROR",
    "REBOOTING", "TRANSPORT_STALL", "RX_FLOOD", "PROMPT_TIMEOUT", "LOGIN_REQUIRED",
    "REPROBE_FAILED:*", "ATTACH_FAILED:*", "BRIDGE_DOWN:*",
}
NOT_ERROR_CODES = {"CMD_LENGTH_WARNING", "EMPTY_COMMAND", "NOT_BOOTLOADER", "INVALID_INTERACTIVE_ENCODING"}


def _real():
    ex = _load()
    return ex.extract(ROOT, ex.load_extraction_config(ROOT / "contracts" / "code_extraction.yaml"))


def test_real_repo_tricky_error_codes_found():
    assert TRICKY_ERROR_CODES <= _real().values("error_code")


def test_real_repo_tricky_last_errors_found():
    assert TRICKY_LAST_ERRORS <= _real().values("session_last_error")


def test_real_repo_non_codes_excluded():
    assert not (NOT_ERROR_CODES & _real().values("error_code"))


def test_real_repo_passthrough_detected():
    assert "last_error_passthrough" in _real().values("error_code", include_passthrough=True)


def test_real_repo_proc_codes_marked():
    proc = {e.value for e in _real().emissions if e.implies_interface == "proc"}
    assert {"DAEMON_ALREADY_RUNNING", "SOCKET_UNAVAILABLE"} <= proc


def test_real_repo_no_unresolved_or_stale():
    res = _real()
    assert res.unresolved == ()
    assert res.stale_allow_unresolved == ()
    assert res.stale_exclude_values == ()


def test_real_repo_scans_flat_modules():
    # 防 fnmatch 類回歸：sw_core 直層模組必須被掃到
    files = {e.file for e in _real().emissions}
    assert {"sw_core/service.py", "sw_core/session_manager.py", "sw_core/cli.py", "sw_core/arbiter.py"} <= files
