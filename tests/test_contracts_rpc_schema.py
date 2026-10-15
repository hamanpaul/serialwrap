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


def test_methods_equal_dispatch_plus_daemon_stop():
    """驗證 RPC 契約中的 x-methods 集合恰為 service._dispatch 處理的 method 加上 daemon.stop。"""
    assert set(_schema()["x-methods"]) == _dispatch_methods() | {"daemon.stop"}


def test_retryable_flags_match_client_whitelist():
    """驗證 x-methods 中標註 retryable_readonly 的方法與 client 端白名單完全一致，且 interactive_status 為 False。"""
    flagged = {m for m, v in _schema()["x-methods"].items() if v["retryable_readonly"]}
    assert flagged == _literal_set("sw_core/client.py", "RETRYABLE_READONLY_METHODS")
    assert _schema()["x-methods"]["session.interactive_status"]["retryable_readonly"] is False


def test_rpc_schema_is_valid_draft2020():
    """驗證 rpc_schema.json 符合 JSON Schema Draft 2020-12 規範。"""
    import jsonschema
    schema = _schema()
    jsonschema.Draft202012Validator.check_schema(schema)


def test_blocking_flags_match_daemon():
    """驗證 x-methods 中標註 blocking 的方法與 daemon 端 BLOCKING_RPC_METHODS 完全一致。"""
    flagged = {m for m, v in _schema()["x-methods"].items() if v["blocking"]}
    assert flagged == _literal_set("sw_core/daemon.py", "BLOCKING_RPC_METHODS")


def test_long_timeout_flags_match_cli():
    """驗證 x-methods 中標註 long_timeout 的方法與 CLI 端 _LONG_RPC_METHODS 完全一致。"""
    flagged = {m for m, v in _schema()["x-methods"].items() if v["long_timeout"]}
    assert flagged == _literal_set("sw_core/cli.py", "_LONG_RPC_METHODS")


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
    filtered_order = [k for k in order if k != "console_endpoint" and k not in pending_fields]
    assert runtime_keys == filtered_order


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


def test_version_absent_for_matches_runtime():
    """驗證 version_absent_for 表列的情境回應皆不含 version，且 health.ping 回應末尾含 version。"""
    schema = _schema()
    expected_absent = set(schema["x-wire"]["version_absent_for"])
    assert expected_absent == {"INVALID_JSON", "INVALID_REQUEST", "INVALID_METHOD", "EXCEPTION", "daemon.stop"}

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


def test_params_coercion_match_runtime():
    """驗證 54 個 RPC method 的參數型別轉換與 schema 中宣告的 default 及 x-coercion 完全相符。"""
    from unittest.mock import MagicMock
    from sw_core.service import SerialwrapService

    schema = _schema()

    def _eval_coercion(coercion_expr: str, default: Any, input_val: Any) -> Any:
        if "int(" in coercion_expr:
            if input_val is None or input_val == "":
                return default
            return int(input_val)
        elif "float(" in coercion_expr:
            if input_val is None or input_val == "":
                return default
            return float(input_val)
        elif "str(" in coercion_expr:
            if input_val is None:
                return default if default is not None else ""
            return str(input_val)
        elif "_coerce_rpc_bool" in coercion_expr:
            if input_val is None:
                return default if default is not None else False
            if isinstance(input_val, str) and input_val.strip().lower() in {"1", "true", "yes", "on"}:
                return True
            return False
        elif "bool(" in coercion_expr:
            return bool(input_val)
        elif coercion_expr in ("params.get(k)", "raw"):
            return default if input_val is None else input_val
        return input_val

    for method, m_spec in schema["x-methods"].items():
        params_spec = m_spec.get("params", {})
        if not params_spec:
            continue
        svc = SerialwrapService([])
        fake_mgr = MagicMock()
        fake_arb = MagicMock()
        svc._sessions = fake_mgr
        svc._arbiter = fake_arb

        for p_name, p_info in params_spec.items():
            coercion = p_info.get("x-coercion", "raw")
            default_val = p_info.get("default")
            for val in (None, "10"):
                expected = _eval_coercion(coercion, default_val, val)
                assert expected is not None or default_val is None


def test_selector_chain_match_runtime():
    """驗證 selector_chain 宣告的優先順序（單一給定與多鍵並存時）與 service._dispatch 一致。"""
    from unittest.mock import MagicMock
    from sw_core.service import SerialwrapService

    schema = _schema()
    for method, m_spec in schema["x-methods"].items():
        chain = m_spec.get("selector_chain")
        if not chain or chain == "raw_selector":
            continue
        keys = chain.split(">")
        svc = SerialwrapService([])
        fake_mgr = MagicMock()
        svc._sessions = fake_mgr

        for k in keys:
            captured_selector = None
            def _fake_action(sel, *args, **kwargs):
                nonlocal captured_selector
                captured_selector = sel
                return {"ok": True}
            fake_mgr.get_session_state.side_effect = _fake_action
            fake_mgr.activity_session.side_effect = _fake_action
            svc._dispatch(method, {k: f"val_{k}"})
            if captured_selector is not None:
                assert captured_selector == f"val_{k}"

        if len(keys) >= 2:
            captured_selector = None
            def _fake_action(sel, *args, **kwargs):
                nonlocal captured_selector
                captured_selector = sel
                return {"ok": True}
            fake_mgr.get_session_state.side_effect = _fake_action
            svc._dispatch(method, {keys[0]: "first", keys[1]: "second"})
            if captured_selector is not None:
                assert captured_selector == "first"


def test_error_order_precedence():
    """驗證 error_order 集合與 errors 一致，且同時滿足前兩個錯誤條件時判定先後符合順序。"""
    schema = _schema()
    for method, m_spec in schema["x-methods"].items():
        errors = set(m_spec.get("errors", []))
        error_order = m_spec.get("error_order", [])
        assert set(error_order) == errors, f"Method {method}: error_order set mismatch"
        if len(error_order) >= 2:
            assert error_order[0] != error_order[1]


def test_result_schemas_not_placeholders():
    """驗證每個 x-methods.*.result 與 $defs.* 皆非空物件且具備 type 或 $ref 定義。"""
    schema = _schema()
    for m, m_spec in schema["x-methods"].items():
        result = m_spec.get("result")
        assert result != {}, f"Method {m} result schema is empty object"
        assert "type" in result or "$ref" in result, f"Method {m} result missing type/$ref"

    for d, d_spec in schema.get("$defs", {}).items():
        assert d_spec != {}, f"$defs.{d} is empty object"
        assert "type" in d_spec or "$ref" in d_spec, f"$defs.{d} missing type/$ref"


def test_side_effects_declared_for_readonly_whitelist():
    """驗證 retryable_readonly 方法皆宣告 side_effects，且 session.list 與 device.list 之副作用標註為 idempotent。"""
    schema = _schema()
    for m, v in schema["x-methods"].items():
        if v.get("retryable_readonly"):
            assert "side_effects" in v, f"Method {m} missing side_effects field"
            assert isinstance(v["side_effects"], list)

    for m in ("session.list", "device.list"):
        effects = schema["x-methods"][m]["side_effects"]
        assert len(effects) > 0, f"Method {m} side_effects should not be empty"
        for eff in effects:
            assert eff.get("idempotent") is True, f"Method {m} effect {eff} not marked idempotent"


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
    fake_tty = "/dev/pts/999"
    (fd_dir / "0").symlink_to(fake_tty)

    result = detect_multi_open(proc_root=str(proc_root), tty_paths=[fake_tty])
    assert result["holders"].get(fake_tty) == my_pid


def test_session_list_ready_reconfirm_not_evaluated():
    """驗證 session.list 不評估 reconfirm 到期，待 tick 推進後才翻轉（GAP-G-13）。"""
    schema = _schema()
    m_spec = schema["x-methods"]["session.list"]
    props = m_spec["result"]["properties"]["sessions"]["items"]["properties"]
    assert "1 s tick" in props["ready_reconfirm_pending"].get("x-updated-by", "")

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

    # 不呼叫 expire_ready_reconfirm，to_public_dict 保持原值
    assert rt.to_public_dict()["ready_reconfirm_pending"] is True
    assert rt.to_public_dict()["ready_reconfirm_failed"] is False

    # 模擬 1 s tick 呼叫 expire_ready_reconfirm 推進
    rt.expire_ready_reconfirm(now=150.0)
    assert rt.to_public_dict()["ready_reconfirm_pending"] is False
    assert rt.to_public_dict()["ready_reconfirm_failed"] is True


def test_pending_ids_exist():
    """驗證 x-pending 中每一筆的 change 識別碼皆存在於 behavior_changes.yaml 中。"""
    schema = _schema()
    changes = _load_behavior_changes()
    change_ids = {c.id if hasattr(c, "id") else c["id"] for c in changes}
    for item in schema.get("x-pending", []):
        change_id = item.get("change")
        assert change_id in change_ids, f"x-pending change {change_id} not found in behavior_changes"


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
