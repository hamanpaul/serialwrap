"""tests/test_contracts_rpc_schema.py — Task P0-T8 RPC 契約 schema 驗證測試。

本模組為 L1 白箱測試，驗證 contracts/rpc_schema.json 符合 JSON Schema Draft 2020-12，
且其 wire framing、method 清單、參數轉型、selector 別名鏈、錯誤碼目錄與先後順序、
副作用、共用資料結構欄位順序與 pending 狀態推進協定皆與 Python 基線原始碼完全對齊。
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
MISSING: Any = object()
LEASE_KEYS = ["interactive_owner", "human_attached", "human_active", "recovery_mode"]
ST_EXTRA_KEYS = ["suggested_profile", "hint", "matched_prompt", "rx_tail", "rx_bytes_last_10s"]


def _schema() -> dict[str, Any]:
    """載入 RPC 契約 schema JSON 檔案。"""
    schema_path = ROOT / "contracts" / "rpc_schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _dispatch_methods() -> set[str]:
    """從 sw_core/service.py 的 _dispatch 函式 AST 抽出處理的 RPC method 集合。"""
    tree = ast.parse((ROOT / "sw_core" / "service.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_dispatch")
    found: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "method":
            for op, comp in zip(node.ops, node.comparators):
                if isinstance(op, ast.Eq) and isinstance(comp, ast.Constant):
                    found.add(comp.value)
                if isinstance(op, ast.In) and isinstance(comp, (ast.Tuple, ast.Set, ast.List)):
                    found |= {e.value for e in comp.elts if isinstance(e, ast.Constant)}
    return found


def _literal_set(path: str, name: str) -> set[str]:
    """從指定 Python 檔案頂層 AST 抽出字面 frozenset / set 集合。"""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            value = node.value.args[0] if isinstance(node.value, ast.Call) else node.value
            return {e.value for e in value.elts}
    raise AssertionError(name)


def _roundtrip(payload: bytes, handler=None) -> list[bytes]:
    """建立 throwaway 127.0.0.1 TCP server，經真實 serve_connection 進行 NDJSON 單次 roundtrip。"""
    import asyncio
    from sw_core.rpc_posix import serve_connection

    if handler is None:
        handler = lambda m, p: {"ok": True}

    async def main():
        server = await asyncio.start_server(
            lambda r, w: serve_connection(r, w, handler, set()), "127.0.0.1", 0
        )
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        lines = []
        while True:
            line = await reader.readline()
            if not line:
                break
            lines.append(line)
        writer.close()
        server.close()
        await server.wait_closed()
        return lines

    return asyncio.run(main())


def _load_error_codes():
    """延遲載入錯誤碼目錄。"""
    try:
        from tools.contracts.catalog import load_error_codes
        return load_error_codes(ROOT)
    except (ImportError, ModuleNotFoundError):
        import yaml
        doc = yaml.safe_load((ROOT / "contracts" / "error_codes.yaml").read_text(encoding="utf-8"))
        codes = [entry["code"] for entry in doc.get("error_codes", [])]
        passthrough = [entry["id"] for entry in doc.get("passthrough", [])]
        return codes, passthrough


def _load_behavior_changes():
    """延遲載入行為變更目錄。"""
    try:
        from tools.contracts.catalog import load_behavior_changes
        changes, _, _ = load_behavior_changes(ROOT)
        return changes
    except (ImportError, ModuleNotFoundError):
        import yaml
        doc = yaml.safe_load((ROOT / "contracts" / "behavior_changes.yaml").read_text(encoding="utf-8"))
        return doc.get("changes", [])


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_methods_equal_dispatch_plus_daemon_stop():
    """驗證 RPC 契約中的 x-methods 集合恰為 service._dispatch 處理的 method 加上 daemon.stop 與 daemon.handoff_stop。"""
    assert set(_schema()["x-methods"]) == _dispatch_methods() | {"daemon.stop", "daemon.handoff_stop"}


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_retryable_flags_match_client_whitelist():
    """驗證 x-methods 中標註 retryable_readonly 的方法與 client 端白名單完全一致，且 interactive_status 為 False。"""
    flagged = {m for m, v in _schema()["x-methods"].items() if v["retryable_readonly"]}
    assert flagged == _literal_set("sw_core/client.py", "RETRYABLE_READONLY_METHODS")
    assert _schema()["x-methods"]["session.interactive_status"]["retryable_readonly"] is False


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_rpc_schema_is_valid_draft2020():
    """驗證 rpc_schema.json 符合 JSON Schema Draft 2020-12 規範。"""
    import jsonschema
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_blocking_flags_match_daemon():
    """驗證 x-methods 中標註 blocking 的方法與 daemon 端 BLOCKING_RPC_METHODS 完全一致。"""
    flagged = {m for m, v in _schema()["x-methods"].items() if v["blocking"]}
    assert flagged == _literal_set("sw_core/daemon.py", "BLOCKING_RPC_METHODS")


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_long_timeout_flags_match_cli():
    """驗證 x-methods 中標註 long_timeout 的方法與 CLI 端 _LONG_RPC_METHODS 完全一致。"""
    flagged = {m for m, v in _schema()["x-methods"].items() if v["long_timeout"]}
    assert flagged == _literal_set("sw_core/cli.py", "_LONG_RPC_METHODS")


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_method_errors_in_catalog():
    """驗證 x-methods 宣告的每個錯誤碼皆屬於 error_codes 目錄碼或 passthrough meta 項。"""
    schema = _schema()
    error_codes, passthrough = _load_error_codes()
    valid_codes = {e if isinstance(e, str) else e.code for e in error_codes} | {
        "@" + (p if isinstance(p, str) else p.id) for p in passthrough
    }
    for method, m_spec in schema["x-methods"].items():
        for err in m_spec.get("errors", []):
            assert err in valid_codes, f"Method {method} has undeclared error_code {err}"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_every_rpc_code_referenced():
    """驗證 error_codes 目錄中 scope 包含 'rpc' 的所有錯誤碼，皆出現在某 method 的 errors 或 x-generic-errors 中。"""
    schema = _schema()
    error_codes, _ = _load_error_codes()
    rpc_codes = {
        e.code for e in error_codes
        if hasattr(e, "scope") and "rpc" in getattr(e.scope, "interfaces", ())
    }
    referenced = set(schema.get("x-generic-errors", []))
    for m_spec in schema["x-methods"].values():
        referenced.update(m_spec.get("errors", []))
    assert rpc_codes <= referenced, f"Unreferenced rpc error codes: {rpc_codes - referenced}"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_session_public_order_matches_runtime():
    """驗證 SessionRuntime.to_public_dict() 的鍵順序與 $defs.SessionPublic.x-order 扣除條件欄後完全一致。"""
    from sw_core.session_manager import SessionRuntime
    from sw_core.config import SessionProfile

    prof = SessionProfile(
        profile_name="p", com="COM0", act_no=1, alias="test", device_by_id="usb-test", platform="shell"
    )
    rt = SessionRuntime(session_id="p:COM0", profile=prof)
    runtime_keys = list(rt.to_public_dict().keys())

    schema = _schema()
    order = list(schema["$defs"]["SessionPublic"]["x-order"])
    pending_fields: set[str] = set()
    for p in schema.get("x-pending", []):
        adds = p.get("adds", {})
        if "SessionPublic" in adds:
            pending_fields.update(adds["SessionPublic"])
    # console_endpoint 為條件欄（x-conditional）：有 PTY 平台（POSIX）不出現；無 PTY 平台
    # （Windows TCP console，session_manager.py to_public_dict）bridge 未起時仍恆輸出 null。
    # 依平台決定期望集合，而非一律剔除，否則 Windows 原生執行會誤判。
    from sw_core.uart_io import _pty_available

    conditional_absent = {"console_endpoint"} if _pty_available() else set()
    filtered_order = [k for k in order if k not in conditional_absent and k not in pending_fields]
    assert runtime_keys == filtered_order


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_session_public_instance_validates():
    """驗證真實 SessionRuntime.to_public_dict() 實例符合 $defs.SessionPublic 的 JSON Schema 定義。"""
    import jsonschema
    from sw_core.session_manager import SessionRuntime
    from sw_core.config import SessionProfile

    prof = SessionProfile(
        profile_name="p", com="COM0", act_no=1, alias="test", device_by_id="usb-test", platform="shell"
    )
    rt = SessionRuntime(session_id="p:COM0", profile=prof)
    schema = _schema()
    validator_schema = {
        "$schema": schema.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
        "$defs": schema.get("$defs", {}),
        "$ref": "#/$defs/SessionPublic",
    }
    jsonschema.validate(instance=rt.to_public_dict(), schema=validator_schema)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_command_record_order_matches_arbiter():
    """驗證 CommandArbiter 命令記錄的鍵順序與 $defs.CommandRecord.x-order 完全一致。"""
    from sw_core.arbiter import CommandArbiter

    arb = CommandArbiter(send_cb=lambda cmd, rec: {"ok": True, "stdout": ""})
    arb.register_session("p:COM0")
    resp = arb.submit(
        session_id="p:COM0", command="echo hi", source="agent", mode="fg", timeout_s=1.0
    )
    rec = arb.get(resp["cmd_id"])["command"]
    schema = _schema()
    expected_order = schema["$defs"]["CommandRecord"]["x-order"]
    assert list(rec.keys()) == expected_order


@pytest.mark.core_portable
@pytest.mark.process_rpc
def test_early_response_bytes_match_framing():
    """驗證 INVALID_JSON 與 INVALID_REQUEST 在真實 framing 層的回應位元組完全等於 x-wire.early_responses。"""
    schema = _schema()
    early = schema["x-wire"]["early_responses"]

    lines_json = _roundtrip(b"{bad\n")
    assert len(lines_json) == 1
    assert lines_json[0].decode("utf-8") == early["INVALID_JSON"]

    lines_req = _roundtrip(b"[1]\n")
    assert len(lines_req) == 1
    assert lines_req[0].decode("utf-8") == early["INVALID_REQUEST"]


@pytest.mark.core_portable
@pytest.mark.process_rpc
def test_version_absent_for_matches_runtime():
    """驗證 version_absent_for 表列的情境回應皆不含 version，且 health.ping 回應末尾含 version。"""
    schema = _schema()
    expected_absent = set(schema["x-wire"]["version_absent_for"])
    assert expected_absent == {
        "INVALID_JSON", "INVALID_REQUEST", "INVALID_METHOD", "EXCEPTION",
        "daemon.stop", "daemon.handoff_stop",
    }

    # INVALID_JSON
    resp = json.loads(_roundtrip(b"{bad\n")[0].decode("utf-8"))
    assert "version" not in resp

    # INVALID_REQUEST
    resp = json.loads(_roundtrip(b"[1]\n")[0].decode("utf-8"))
    assert "version" not in resp

    # INVALID_METHOD (method 缺省)
    resp = json.loads(_roundtrip(b'{"id":1}\n')[0].decode("utf-8"))
    assert "version" not in resp

    # EXCEPTION
    def _exploding_handler(m, p):
        raise RuntimeError("boom")
    resp = json.loads(_roundtrip(b'{"id":1,"method":"foo"}\n', handler=_exploding_handler)[0].decode("utf-8"))
    assert "version" not in resp

    # daemon.stop
    def _daemon_stop_handler(m, p):
        if m == "daemon.stop":
            return {"ok": True, "stopping": True}
        return {"ok": True}
    resp = json.loads(_roundtrip(b'{"id":1,"method":"daemon.stop"}\n', handler=_daemon_stop_handler)[0].decode("utf-8"))
    assert "version" not in resp

    # health.ping (經 service 包裝，末尾帶 version)
    from sw_core.service import SerialwrapService
    svc = SerialwrapService([])
    ping_resp = svc.rpc("health.ping", {})
    assert "version" in ping_resp
    assert list(ping_resp.keys())[-1] == "version"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_line_limit_matches_golden():
    """驗證 x-wire.max_request_line_bytes 與 contracts/golden/rpc/line_limit.json 完全對齊。"""
    schema = _schema()
    golden_path = ROOT / "contracts" / schema["x-wire"]["line_limit_golden"]
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    assert schema["x-wire"]["max_request_line_bytes"] == golden["max_ok_bytes"]
    assert golden["first_close_bytes"] == golden["max_ok_bytes"] + 1
    assert golden["unit"] == "bytes before LF"


@pytest.mark.posix_only
@pytest.mark.process_rpc
def test_line_limit_capture_reproducible(tmp_path: Path):
    """驗證 scripts/stage0/capture_rpc_line_limit.py 重跑產生的資料與 golden 一致（除 captured_by 之外）。"""
    import subprocess
    script = ROOT / "scripts" / "stage0" / "capture_rpc_line_limit.py"
    out_file = tmp_path / "line_limit.json"
    subprocess.run(
        [sys.executable, str(script), "--out", str(out_file)],
        check=True,
        capture_output=True,
        text=True,
    )
    captured = json.loads(out_file.read_text(encoding="utf-8"))
    golden_path = ROOT / "contracts" / "golden" / "rpc" / "line_limit.json"
    golden = json.loads(golden_path.read_text(encoding="utf-8"))

    captured.pop("captured_by", None)
    golden.pop("captured_by", None)
    assert captured == golden


def _oracle_bool(value: Any) -> bool:
    """依 schema x-notes（strict_human_lock）的真值表：None→False；bool 原樣；int 非零為真；
    字串僅 1/true/yes/on（去空白、小寫）為真，其餘字串為假；其餘型別以 bool() 判定。"""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _oracle(expr: str, k: str, value: Any) -> tuple[str, Any]:
    """以 schema 的 x-coercion 字串求值（期望值由 schema 字串得出，不呼叫產品轉型函式）。"""
    params = {} if value is MISSING else {k: value}
    env = {
        "params": params, "k": k, "str": str, "int": int, "float": float,
        "bool": bool, "max": max, "_coerce_rpc_bool": _oracle_bool,
    }
    try:
        return "ok", eval(expr, {"__builtins__": {}}, env)  # noqa: S307 - 僅評估本檔 schema 字串
    except (TypeError, ValueError) as exc:
        return "raises", exc


def _oracle_regex(value: Any) -> bool:
    """與 command_completion.valid_completion_regex 的可觀測規則一致（本測試值域內）：None 為合法；
    其餘須為非空字串且可編譯。"""
    import re

    if value is None:
        return True
    if not isinstance(value, str) or not value:
        return False
    try:
        re.compile(value)
    except re.error:
        return False
    return True


_VALIDATORS = {"valid_completion_regex": _oracle_regex}


def _chain_keys(spec: dict[str, Any]) -> list[str]:
    chain = spec.get("selector_chain")
    if not chain or chain == "raw_selector":
        return []
    return chain.split(">")


# 分支選擇鍵：不得放入基線，否則會遮蔽同方法其他參數的觀察（例 result.tail 有 cmd_id 即走 cmd 分支）。
_NO_BASELINE: dict[str, set[str]] = {"result.tail": {"cmd_id"}}
# 僅在特定分支才會被產品讀取的參數：補上使其所在分支成立的前提（result.tail 的 from_chunk 只在 cmd_id 分支）。
_EXTRA_BASE: dict[tuple[str, str], dict[str, Any]] = {("result.tail", "from_chunk"): {"cmd_id": "b_cmd_id"}}


def _baseline(spec: dict[str, Any], target: str, method: str = "") -> dict[str, Any]:
    """組出能通過 guard 的最小請求：其餘必填字串給 b_<name>；selector 鏈只給首鍵（且目標不在鏈上時）。"""
    chain = _chain_keys(spec)
    base: dict[str, Any] = {}
    if chain and target not in chain:
        base[chain[0]] = f"b_{chain[0]}"
    for name, p in spec.get("params", {}).items():
        if name == target or name in chain or name in _NO_BASELINE.get(method, set()):
            continue
        if p.get("default") == "" and p.get("type") == "string":
            base[name] = f"b_{name}"
    base.update(_EXTRA_BASE.get((method, target), {}))
    return base


class _Recorder:
    """recording fake：記錄所有 manager／arbiter／engine／wal 方法的呼叫（名稱、位置與關鍵字參數）。"""

    def __init__(self, forced_error: str | None = None) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._forced = forced_error

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        def _call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if self._forced is not None:
                return {"ok": False, "error_code": self._forced}
            if name == "get_session_state":
                return {"ok": True, "session": {
                    "state": "READY", "command_capable": True, "com": "COM0",
                    "session_id": "p:COM0", "device_by_id": "dev",
                }}
            if name.endswith("_with_meta"):  # log.tail／wal.range 等回 (rows, truncated)
                return ([], False)
            if name == "health":  # WAL 健康快照：wal_file_exists 為真且 current_seq > 0 才視為可讀
                return {"ok": True, "wal_file_exists": True, "current_seq": 1}
            return {"ok": True}

        return _call

    def values(self) -> list[Any]:
        out: list[Any] = []
        for _, args, kwargs in self.calls:
            out.extend(args)
            out.extend(kwargs.values())
        return out


def _svc() -> Any:
    from sw_core.service import SerialwrapService

    return SerialwrapService([])


def _run(svc: Any, method: str, params: dict[str, Any], forced: str | None = None) -> tuple[dict[str, Any], _Recorder]:
    """以 recording fake 替換所有 manager，對真實 _dispatch 送請求。例外比照 serve_connection 映為 EXCEPTION。"""
    rec = _Recorder(forced)
    svc._sessions = rec
    svc._arbiter = rec
    svc._engine = rec
    svc._wal = rec
    try:
        resp = svc._dispatch(method, params)
    except Exception:  # noqa: BLE001 - 比照 rpc_posix.serve_connection 的 EXCEPTION 映射
        resp = {"ok": False, "error_code": "EXCEPTION"}
    return resp, rec


def _same(a: Any, b: Any) -> bool:
    return type(a) is type(b) and a == b


def _dispatch_param_names() -> dict[str, set[str]]:
    """以 AST 抽出 service._dispatch 每個頂層分支實際讀取的 params.get("…") 名稱集合。"""
    tree = ast.parse((ROOT / "sw_core" / "service.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_dispatch")
    out: dict[str, set[str]] = {}
    for node in fn.body:
        if not isinstance(node, ast.If):
            continue
        names: set[str] = set()
        methods: set[str] = set()
        for c in ast.walk(node.test):
            if isinstance(c, ast.Compare) and isinstance(c.left, ast.Name) and c.left.id == "method":
                for comp in c.comparators:
                    if isinstance(comp, ast.Constant):
                        methods.add(comp.value)
                    elif isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                        methods |= {e.value for e in comp.elts if isinstance(e, ast.Constant)}
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "get"
                    and isinstance(sub.func.value, ast.Name) and sub.func.value.id == "params"
                    and sub.args and isinstance(sub.args[0], ast.Constant)):
                names.add(sub.args[0].value)
        for m in methods:
            out.setdefault(m, set()).update(names)
    return out


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_coercion_expression_matches_default_for_missing_and_none():
    """機械檢查（不執行產品碼）：x-coercion 對省略與 None 求值必須等於 default（且型別相同）。"""
    for m, spec in _schema()["x-methods"].items():
        for k, p in spec.get("params", {}).items():
            assert "x-coercion" in p, (m, k)
            for value in (MISSING, None):
                kind, out = _oracle(p["x-coercion"], k, value)
                assert kind == "ok", (m, k, out)
                assert _same(out, p["default"]), (m, k, value, out, p["default"])


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_params_names_match_dispatch_reads():
    """機械檢查：每個 _dispatch 分支讀取的 params 名稱集合 == schema params 鍵 ∪ x-aliases。"""
    schema = _schema()
    ast_names = _dispatch_param_names()
    for m, spec in schema["x-methods"].items():
        if m in {"daemon.stop", "daemon.handoff_stop"}:
            continue
        expected = set(spec.get("params", {}))
        for p in spec.get("params", {}).values():
            expected |= set(p.get("x-aliases", []))
        assert ast_names.get(m, set()) == expected, (m, sorted(ast_names.get(m, set())), sorted(expected))


CASES: tuple[tuple[str, Any], ...] = (
    ("omitted", MISSING), ("none", None), ("numeric_string", "10"), ("empty_string", ""), ("zero", 0),
)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_params_coercion_match_runtime():
    """執行期：每個 method × 每個 param × {省略, None, "10", "", 0}，recording fake 收到的值必須等於
    x-coercion 求值結果；求值例外時產品須回 INVALID_ARGS／EXCEPTION 且不得呼叫 manager。"""
    svc = _svc()
    dispatched = _dispatch_methods()
    failures: list[str] = []
    checked = 0
    for m, spec in _schema()["x-methods"].items():
        if m not in dispatched:
            continue
        for k, p in spec.get("params", {}).items():
            for label, value in CASES:
                params = _baseline(spec, k, m)
                if value is not MISSING:
                    params[k] = value
                kind, expected = _oracle(p["x-coercion"], k, value)
                resp, rec = _run(svc, m, params)
                checked += 1
                if "x-validator" in p and kind == "ok" and value is not MISSING:
                    if not _VALIDATORS[p["x-validator"]](value):
                        if not (resp.get("error_code") == "INVALID_COMPLETION_REGEX" and not rec.calls):
                            failures.append(f"{m}.{k}[{label}] invalid-expected got {resp!r}")
                        continue
                if p.get("x-forwarded", True) is False and kind == "ok" and "x-enum" not in p:
                    continue  # 服務層本地使用、不轉傳（wal.range.to_seq）；由 test_wal_range_to_seq_filters_locally 驗證
                if "x-enum" in p and kind == "ok":
                    if expected not in p["x-enum"]:
                        if not (resp.get("error_code") == "INVALID_ARGS" and not rec.calls):
                            failures.append(f"{m}.{k}[{label}] enum-reject-expected got {resp!r}")
                        continue
                    if resp.get("error_code") == "INVALID_ARGS":
                        failures.append(f"{m}.{k}[{label}] valid enum rejected {resp!r}")
                        continue
                    if p.get("x-forwarded", True) is False:
                        continue  # 僅驗證、不轉傳（file.pull.ack_mode）
                if kind == "raises":
                    if not (resp.get("error_code") in {"INVALID_ARGS", "EXCEPTION"} and not rec.calls):
                        failures.append(f"{m}.{k}[{label}] raises-expected got {resp!r}")
                    continue
                observed = any(_same(v, expected) for v in rec.values())
                # 假值（""／0／None）可能使產品改走「未提供」分支（例 log.tail 無 selector＝全部 session、
                # result.tail 無 cmd_id＝legacy 分支），此時不以 manager 收到該假值為準，只要求未回錯誤碼。
                falsy_branch = not bool(expected) and resp.get("error_code") in (None, "INVALID_ARGS", "EXCEPTION")
                if not (observed or falsy_branch):
                    failures.append(f"{m}.{k}[{label}] expected={expected!r} calls={rec.calls!r} resp={resp!r}")
    assert checked > 100
    assert not failures, "\n".join(failures[:40])


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_selector_chain_match_runtime():
    """執行期：selector_chain 非 null 的 30 個 method（24 鏈＋6 raw）。鏈上每一鍵單獨給值時，該值必須
    出現在某個 manager 呼叫的參數中；同時給鏈上前兩鍵時取較前者（後者不得出現）。raw 方法原樣傳遞。"""
    svc = _svc()
    checked = 0
    for m, spec in _schema()["x-methods"].items():
        chain_spec = spec.get("selector_chain")
        if chain_spec is None:
            continue
        checked += 1
        if chain_spec == "raw_selector":
            resp, rec = _run(svc, m, {"selector": 7})
            _, expected = _oracle(spec["params"]["selector"]["x-coercion"], "selector", 7)
            assert any(_same(v, expected) for v in rec.values()), (m, expected, resp, rec.calls)
            continue
        chain = _chain_keys(spec)
        for key in chain:
            params = _baseline(spec, key, m)
            params[key] = f"val_{key}"
            resp, rec = _run(svc, m, params)
            assert f"val_{key}" in rec.values(), (m, key, resp, rec.calls)
        if len(chain) >= 2:
            params = _baseline(spec, chain[0], m)
            params[chain[0]] = "first"
            params[chain[1]] = "second"
            resp, rec = _run(svc, m, params)
            vals = rec.values()
            assert "first" in vals and "second" not in vals, (m, resp, rec.calls)
    assert checked == 30


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_wal_range_to_seq_filters_locally():
    """wal.range.to_seq 不轉傳 WAL：服務層保留 seq ≤ to_seq；0 或省略不過濾（schema x-forwarded: false）。"""
    svc = _svc()
    rows = [{"seq": 5}, {"seq": 20}]

    class _WalRows(_Recorder):
        def __getattr__(self, name: str):
            if name == "tail_raw":
                def _rows(*args, **kwargs):
                    self.calls.append((name, args, kwargs))
                    return list(rows)
                return _rows
            if name == "health":
                return lambda *a, **k: {"ok": True, "wal_file_exists": True, "current_seq": 1, "wal_path": "x"}
            if name == "available_from_seq":
                return lambda *a, **k: 0
            return super().__getattr__(name)

    for params, expect in (({"to_seq": 10}, [5]), ({}, [5, 20]), ({"to_seq": 0}, [5, 20])):
        rec = _WalRows()
        svc._wal = rec
        resp = svc._dispatch("wal.range", params)
        assert [r["seq"] for r in resp.get("records", [])] == expect, (params, resp)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_event_com_status_selector_scalar_types_validate():
    """event.com_status 的 selector 原樣傳入引擎：字串、數字、布林的成功回應須符合 result schema；None 走 coms 分支。"""
    import jsonschema

    schema = _schema()
    result = schema["x-methods"]["event.com_status"]["result"]

    class _Engine(_Recorder):
        def __getattr__(self, name: str):
            if name == "com_status":
                def _status(com=None):
                    if com is None:
                        return {"coms": ["COM0"]}
                    return {"selector": com, "enabled": True, "active_rules": []}
                return _status
            return super().__getattr__(name)

    svc = _svc()
    svc._engine = _Engine()
    for sel in ("COM0", 7, True):
        resp = svc._dispatch("event.com_status", {"selector": sel})
        assert resp["ok"] is True and resp["selector"] == sel, resp
        jsonschema.validate(instance=resp, schema={"$defs": schema["$defs"], **result})
    resp = svc._dispatch("event.com_status", {})
    jsonschema.validate(instance=resp, schema={"$defs": schema["$defs"], **result})


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_error_order_precedence():
    """執行期：error_order[0] 與 error_order[1] 同時成立時回應 error_order[0]。
    對 error_order[0] 為 INVALID_ARGS 的方法：無必填參數＋所有 manager 回 error_order[1] → INVALID_ARGS。
    command.submit：壞 regex 先於 selector guard 與 manager 條件。
    無法於 service 層同時成立的 method 須明列於 _NOT_SERVICE_TESTABLE 並附理由（不得靜默略過）。"""
    svc = _svc()
    schema = _schema()
    covered: set[str] = set()
    multi = {m for m, s in schema["x-methods"].items() if len(s.get("error_order", [])) >= 2}
    for m in sorted(multi):
        eo = schema["x-methods"][m]["error_order"]
        if eo[0] != "INVALID_ARGS":
            continue
        trigger = _PRECEDENCE_TRIGGER.get(m, {})
        resp, _ = _run(svc, m, trigger, forced=eo[1])
        assert resp.get("error_code") == "INVALID_ARGS", (m, eo, resp)
        covered.add(m)
    resp, _ = _run(svc, "command.submit", {"completion_regex": "("}, forced="SESSION_NOT_FOUND")
    assert resp.get("error_code") == "INVALID_COMPLETION_REGEX", resp
    covered.add("command.submit")
    not_testable = {m: reason for m, reason in _NOT_SERVICE_TESTABLE.items()}
    assert multi - covered == set(not_testable), sorted(multi - covered - set(not_testable))


# 多數方法的 INVALID_ARGS 為「必填參數缺漏」；log.tail_* 的 INVALID_ARGS 則為 from_seq 非法值（先於 selector 查詢）。
_PRECEDENCE_TRIGGER: dict[str, dict[str, Any]] = {
    "log.tail_raw": {"from_seq": "abc", "selector": "COM0"},
    "log.tail_text": {"from_seq": "abc", "selector": "COM0"},
}

_NOT_SERVICE_TESTABLE: dict[str, str] = {
    "command.cancel": "CMD_NOT_FOUND 與 CMD_NOT_CANCELABLE 同出於單一 arbiter 呼叫的內部判定，無法由替身同時造成，留待 P2 整合測試",
    "result.tail": "cmd_id 分支（CMD_NOT_FOUND）與 com/selector 分支（SESSION_NOT_FOUND）互斥，同一請求不會同時成立",
    "daemon.handoff_stop": "交接 method 由 daemon 層處理，不經 service._dispatch",
}


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_result_schemas_not_placeholders():
    """每個 x-methods.*.result 與 $defs.* 皆非空物件，且具 type、$ref 或 oneOf（C18 的本地鏡像）。"""
    schema = _schema()
    for m, m_spec in schema["x-methods"].items():
        result = m_spec.get("result")
        assert result != {}, f"Method {m} result schema is empty object"
        assert "type" in result or "$ref" in result or "oneOf" in result, f"Method {m} result missing type/$ref/oneOf"
    for d, d_spec in schema.get("$defs", {}).items():
        assert d_spec != {}, f"$defs.{d} is empty object"
        assert "type" in d_spec or "$ref" in d_spec, f"$defs.{d} missing type/$ref"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_side_effects_declared_for_readonly_whitelist():
    """retryable_readonly 方法皆宣告 side_effects；session.list 的效果皆 idempotent。
    device.list 為純讀（見 spec-errata.md T8-ERR-01），side_effects 必須為空列表。"""
    schema = _schema()
    for m, v in schema["x-methods"].items():
        if v.get("retryable_readonly"):
            assert "side_effects" in v, f"Method {m} missing side_effects field"
            assert isinstance(v["side_effects"], list)
    assert schema["x-methods"]["device.list"]["side_effects"] == []
    effects = schema["x-methods"]["session.list"]["side_effects"]
    assert effects, "session.list side_effects should not be empty"
    for eff in effects:
        assert eff.get("idempotent") is True, f"session.list effect {eff} not marked idempotent"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_uart_tx_effects_are_conditional_and_gated():
    """含 UART TX 的副作用不得宣稱 when=always（拒絕路徑零 TX，§11.3 第 1 項），且必須寫明 x-condition。"""
    for m, v in _schema()["x-methods"].items():
        for eff in v.get("side_effects", []):
            if eff.get("x-uart-tx") is not None:
                assert eff["when"] != "always", (m, eff["what"])
                assert eff.get("x-condition"), (m, eff["what"])


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_section_94_fields_carry_unit_and_updated_by():
    """§9.4 凍結欄位須標 x-updated-by；數值型欄位另須標 x-unit。"""
    need_updated = {"multi_open", "multi_open_detail", "rx_bytes_last_10s", "rx_rate_bps",
                    "last_rx_age_s", "last_tx_age_s", "human_active", "ready_reconfirm_pending", "foreign_holders"}
    need_unit = {"rx_bytes_last_10s", "rx_rate_bps", "last_rx_age_s", "last_tx_age_s"}
    seen: dict[str, int] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                for name, prop in props.items():
                    if name in need_updated and isinstance(prop, dict):
                        seen[name] = seen.get(name, 0) + 1
                        assert "x-updated-by" in prop, f"{name} missing x-updated-by"
                        if name in need_unit:
                            assert "x-unit" in prop, f"{name} missing x-unit"
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(_schema())
    assert {"multi_open", "human_active", "rx_bytes_last_10s", "last_rx_age_s"} <= set(seen), seen


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_record_error_codes_match_catalog():
    """CommandRecord／CommandResultTail.error_code 的 x-record-error-codes 與目錄 rec 範圍（非 APPROVED）雙向一致，
    且目錄 scope.methods 所列的讀取 method 其 errors 或紀錄層清單皆涵蓋該碼。"""
    schema = _schema()
    codes, _ = _load_error_codes()
    rec_entries = [c for c in codes if "rec" in c.scope.interfaces and c.status != "APPROVED"]
    expected = sorted(c.code for c in rec_entries)
    for owner in ("CommandRecord", "CommandResultTail"):
        got = schema["$defs"][owner]["properties"]["error_code"]["x-record-error-codes"]
        assert got == expected, (owner, sorted(set(got) ^ set(expected)))
    record = set(expected)
    for c in rec_entries:
        for m in c.scope.methods:
            spec = schema["x-methods"].get(m)
            if spec is None:
                continue
            assert c.code in set(spec.get("errors", [])) | record, (m, c.code)


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_session_list_ready_reconfirm_not_evaluated():
    """session.list 不主動評估 ready_reconfirm 到期（GAP-G-13）：list_sessions 與 to_public_dict 不得呼叫
    expire_ready_reconfirm；到期翻轉只由 1 s tick 發生。"""
    schema = _schema()
    items = schema["x-methods"]["session.list"]["result"]["properties"]["sessions"]["items"]
    assert items == {"$ref": "#/$defs/SessionPublic"}
    props = schema["$defs"]["SessionPublic"]["properties"]
    assert "1 s tick" in props["ready_reconfirm_pending"].get("x-updated-by", "")

    tree = ast.parse((ROOT / "sw_core" / "session_manager.py").read_text(encoding="utf-8"))

    def calls_expire(fn_name: str) -> bool:
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == fn_name)
        return any(isinstance(c, ast.Attribute) and c.attr == "expire_ready_reconfirm" for c in ast.walk(fn))

    assert not calls_expire("list_sessions")
    assert not calls_expire("to_public_dict")
    assert any(isinstance(c, ast.Attribute) and c.attr == "expire_ready_reconfirm" for c in ast.walk(tree))

    from sw_core.session_manager import SessionRuntime
    from sw_core.config import SessionProfile

    prof = SessionProfile(
        profile_name="p", com="COM0", act_no=1, alias="test", device_by_id="usb-test", platform="shell"
    )
    rt = SessionRuntime(session_id="p:COM0", profile=prof)
    rt.state = "READY"
    rt.ready_reconfirm_pending = True
    rt.ready_reconfirm_failed = False
    rt.ready_reconfirm_deadline = 100.0
    assert rt.to_public_dict()["ready_reconfirm_pending"] is True
    rt.expire_ready_reconfirm(now=150.0)
    assert rt.to_public_dict()["ready_reconfirm_pending"] is False
    assert rt.to_public_dict()["ready_reconfirm_failed"] is True


def _source_literal_orders(path: str, fn_names: set[str], append: tuple[str, ...] = ()) -> list[list[str]]:
    """抽出指定函式內所有含 "ok" 鍵的字面 dict 鍵序；**spread 依來源展開（lease／extra）。"""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    out: list[list[str]] = []
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name in fn_names):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, ast.Dict) or not any(isinstance(k, ast.Constant) and k.value == "ok" for k in node.keys):
                continue
            keys: list[str] = []
            for k, v in zip(node.keys, node.values):
                if k is None:
                    text = ast.unparse(v)
                    if "lease" in text:
                        keys.extend(LEASE_KEYS)
                    elif text == "extra":
                        keys.extend(ST_EXTRA_KEYS)
                    else:
                        raise AssertionError(f"未知 spread：{text}")
                else:
                    keys.append(k.value)
            for extra in append:
                if extra not in keys:
                    keys.append(extra)
            out.append(keys)
    return out


def _admits(branch: dict[str, Any], keys: list[str]) -> bool:
    keyset = set(keys)
    if not set(branch.get("required", [])) <= keyset:
        return False
    if not keyset <= set(branch["properties"]):
        return False
    return [k for k in branch["x-order"] if k in keyset] == keys


def _branches(method: str) -> list[dict[str, Any]]:
    return _schema()["x-methods"][method]["result"]["oneOf"]


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_self_test_recover_branch_orders_match_source_literals():
    """session.self_test／session.recover 的每個字面回應鍵序（session_manager.py）必須被某一 oneOf 分支
    以相同相對順序接受；wrapper self_test() 於最後追加 command_capable。"""
    st_literals = _source_literal_orders(
        "sw_core/session_manager.py", {"_self_test_impl"}, append=("command_capable",)
    )
    assert st_literals, "no self_test literals found"
    st_branches = _branches("session.self_test")
    for keys in st_literals:
        assert any(_admits(b, keys) for b in st_branches), keys

    rc_literals = _source_literal_orders("sw_core/session_manager.py", {"recover_session", "_force_recover", "_recover_after_failure"})
    assert rc_literals, "no recover literals found"
    rc_branches = _branches("session.recover")
    for keys in rc_literals:
        assert any(_admits(b, keys) for b in rc_branches), keys


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_session_not_found_runtime_response_validates_branch():
    """執行期（真實 SessionManager，查無 session）：回應鍵序與值須符合 SESSION_NOT_FOUND 分支。"""
    import jsonschema

    schema = _schema()
    svc = _svc()
    for m in ("session.self_test", "session.recover"):
        resp = svc._dispatch(m, {"selector": "COM9"})
        assert resp["ok"] is False and resp["error_code"] == "SESSION_NOT_FOUND", resp
        assert list(resp) == list(resp), m
        branch = next(b for b in _branches(m) if b["x-branch"] == "SESSION_NOT_FOUND")
        assert _admits(branch, list(resp)), (m, list(resp))
        jsonschema.validate(instance=resp, schema={"$defs": schema["$defs"], **branch})


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_foreign_holders_includes_self_pid(tmp_path: Path):
    """驗證 health.status 之 foreign_holders 包含自身 pid，且 multi_open 偵測結果正確映射。"""
    schema = _schema()
    m_spec = schema["x-methods"]["health.status"]
    props = m_spec["result"]["properties"]
    assert "daemon 自身 pid" in props["foreign_holders"].get("x-updated-by", "")

    from sw_core.multi_open import detect_multi_open
    proc_root = tmp_path / "proc"
    my_pid = os.getpid()
    pid_dir = proc_root / str(my_pid)
    pid_dir.mkdir(parents=True)
    (pid_dir / "cmdline").write_bytes(b"/usr/bin/serialwrapd\0--socket\0/tmp/s.sock\0")
    fd_dir = pid_dir / "fd"
    fd_dir.mkdir()
    # #R2：holder 以 inode 身分判定，故 tty 以暫存實體檔案代表（字串相同但不存在者不計入）。
    fake_tty = tmp_path / "dev" / "pts999"
    fake_tty.parent.mkdir(parents=True)
    fake_tty.write_bytes(b"")
    (fd_dir / "0").symlink_to(fake_tty)

    result = detect_multi_open(proc_root=str(proc_root), tty_paths=[str(fake_tty)])
    assert result["holders"].get(str(fake_tty)) == my_pid


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_pending_ids_exist():
    """驗證 x-pending 中每一筆的 change 識別碼皆存在於 behavior_changes.yaml 中。"""
    schema = _schema()
    changes = _load_behavior_changes()
    change_ids = {c.id if hasattr(c, "id") else c["id"] for c in changes}
    for item in schema.get("x-pending", []):
        change_id = item.get("change")
        assert change_id in change_ids, f"x-pending change {change_id} not found in behavior_changes"


@pytest.mark.core_portable
@pytest.mark.memory_only
def test_pending_fields_absent_while_approved():
    """驗證處於 APPROVED 狀態的變更項目其擴充欄位尚未出現在執行期回應中。"""
    schema = _schema()
    changes = _load_behavior_changes()
    status_by_id = {
        (c.id if hasattr(c, "id") else c["id"]): (c.status if hasattr(c, "status") else c["status"])
        for c in changes
    }

    from sw_core.session_manager import SessionRuntime
    from sw_core.config import SessionProfile
    from sw_core.service import SerialwrapService

    prof = SessionProfile(
        profile_name="p", com="COM0", act_no=1, alias="test", device_by_id="usb-test", platform="shell"
    )
    rt = SessionRuntime(session_id="p:COM0", profile=prof)
    pub = rt.to_public_dict()

    if status_by_id.get("SC-04") == "APPROVED":
        assert "drain_stalled" not in pub
        assert "drain_timeout_epoch" not in pub

    if status_by_id.get("SC-19") == "APPROVED":
        svc = SerialwrapService([])
        status_res = svc._dispatch("health.status", {})
        assert "test_device_allowlist" not in status_res
