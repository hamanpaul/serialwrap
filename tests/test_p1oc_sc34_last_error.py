"""tests/test_p1oc_sc34_last_error.py — Task P1-OC-T2 SC-34 attach／recover last_error 分離失敗測試。

依 02b-stage0-owner-confirmations.md Task T2：
驗證 session.attach 與 session.recover 在失敗時 error_code 固定為 SESSION_NOT_READY，
原本的 last_error 改由 last_error、last_error_detail 欄位回報，
並移除契約中的 last_error_passthrough meta 項。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from tests.stage0_fakes import make_manager, make_profile

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("last_error,detail", [
    ("CREDENTIALS_UNRESOLVED", None),
    ("READY_NONCE_TIMEOUT", "tail…"),
    (None, None),
])
def test_attach_state_result_separates_last_error(tmp_path: Path, last_error: str | None, detail: str | None) -> None:
    """_attach_state_result 應將 last_error / last_error_detail 分離出獨立欄位，error_code 固定為 SESSION_NOT_READY。"""
    mgr = make_manager(tmp_path, [make_profile(ready_probe="echo __READY__${nonce}")])
    s = mgr.get_session("COM0")
    assert s is not None
    s.state = "ATTACHED"
    s.last_error = last_error
    s.last_error_detail = detail
    r = mgr._attach_state_result(s, s.to_public_dict())
    assert list(r.keys()) == ["ok", "error_code", "last_error", "last_error_detail", "session"]
    assert r["ok"] is False and r["error_code"] == "SESSION_NOT_READY"
    assert r["last_error"] == last_error and r["last_error_detail"] == detail
    assert r["session"]["last_error"] == last_error


def test_attach_flashing_and_recovering_unchanged(tmp_path: Path) -> None:
    """FLASHING 與 RECOVERING 狀態之 attach error_code 不變，且頂層不含 last_error（回歸防護）。"""
    mgr = make_manager(tmp_path, [make_profile(ready_probe="echo x")])
    s = mgr.get_session("COM0")
    assert s is not None
    for state, code in (("FLASHING", "FLASHING_BUSY"), ("RECOVERING", "SESSION_RECOVERING")):
        s.state = state
        s.last_error = "PROMPT_UNAVAILABLE"
        r = mgr._attach_state_result(s, s.to_public_dict())
        assert r["error_code"] == code and "last_error" not in r


def test_attach_after_probe_failure_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ATTACHED + bridge 於同步 probe 失敗後，attach_session 應回 SESSION_NOT_READY 並附 last_error 欄位。"""
    from sw_core.device_watcher import DeviceInfo

    mgr = make_manager(tmp_path, [make_profile(ready_probe="echo x")])
    s = mgr.get_session("COM0")
    assert s is not None
    mgr._devices[s.profile.device_by_id] = DeviceInfo(by_id=s.profile.device_by_id, real_path="/dev/ttyUSB0")
    s.state = "ATTACHED"
    s.bridge = mock.MagicMock()
    snap = s.to_public_dict()
    snap["state"] = "ATTACHED"
    snap["last_error"] = "LOGIN_PROMPT_TIMEOUT"
    snap["last_error_detail"] = "x"
    monkeypatch.setattr(mgr, "_probe_existing_bridge", lambda sess, br: {"ok": True, "session": snap})

    r = mgr.attach_session("COM0")
    assert r["ok"] is False
    assert r["error_code"] == "SESSION_NOT_READY"
    assert r["last_error"] == "LOGIN_PROMPT_TIMEOUT"
    assert r["last_error_detail"] == "x"
    assert list(r.keys()) == ["ok", "error_code", "last_error", "last_error_detail", "session"]


def test_attach_human_lease_branch_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """有 human lease 的 ATTACHED session attach 不 probe，回應形狀分離 last_error。"""
    from sw_core.device_watcher import DeviceInfo

    mgr = make_manager(tmp_path, [make_profile(ready_probe="echo x")])
    s = mgr.get_session("COM0")
    assert s is not None
    mgr._devices[s.profile.device_by_id] = DeviceInfo(by_id=s.profile.device_by_id, real_path="/dev/ttyUSB0")
    s.state = "ATTACHED"
    s.last_error = "PROMPT_UNAVAILABLE"
    s.last_error_detail = "x"
    s.bridge = mock.MagicMock()

    fake_lease = mock.MagicMock()
    fake_lease.owner = "human:tester"
    monkeypatch.setattr(mgr, "_refresh_interactive_locked", lambda sess: (fake_lease, mock.MagicMock()))

    r = mgr.attach_session("COM0")
    assert r["ok"] is False
    assert r["error_code"] == "SESSION_NOT_READY"
    assert r["last_error"] == "PROMPT_UNAVAILABLE"
    assert r["last_error_detail"] == "x"
    assert list(r.keys()) == ["ok", "error_code", "last_error", "last_error_detail", "session"]


def test_attach_never_puts_last_error_in_error_code(tmp_path: Path) -> None:
    """對 session_last_errors.yaml 每個具名值與動態前綴，attach error_code 恆為 SESSION_NOT_READY。"""
    from tools.contracts import catalog

    items = catalog.load_session_last_errors(ROOT)
    values: list[str] = []
    for it in items:
        if it.kind == "dynamic_prefix":
            values.append(it.value.replace("*", "x"))
        else:
            values.append(it.value)

    mgr = make_manager(tmp_path, [make_profile(ready_probe="echo __READY__${nonce}")])
    s = mgr.get_session("COM0")
    assert s is not None
    s.state = "ATTACHED"
    for val in values:
        s.last_error = val
        r = mgr._attach_state_result(s, s.to_public_dict())
        assert r["error_code"] == "SESSION_NOT_READY"
        assert r["last_error"] == val


def test_recover_reprobe_not_recovered_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """recover REPROBE 未恢復時，回應頂層 error_code 固定為 SESSION_NOT_READY 並附 last_error。"""
    mgr = make_manager(tmp_path, [make_profile(ready_probe="echo x")])
    s = mgr.get_session("COM0")
    assert s is not None
    s.state = "ATTACHED"
    s.bridge = mock.MagicMock()
    snap = s.to_public_dict()
    snap["state"] = "ATTACHED"
    snap["last_error"] = "PROMPT_UNAVAILABLE"
    snap["last_error_detail"] = "detail_x"
    monkeypatch.setattr(mgr, "_probe_existing_bridge", lambda sess, br: {"ok": True, "session": snap})

    r = mgr.recover_session("COM0")
    assert r["ok"] is True
    assert r["recovering"] is False
    assert r["action"] == "REPROBE"
    assert r["recovered"] is False
    assert r["error_code"] == "SESSION_NOT_READY"
    assert r["last_error"] == "PROMPT_UNAVAILABLE"
    assert r["last_error_detail"] == "detail_x"
    assert list(r.keys())[:8] == [
        "ok", "recovering", "action", "recovered", "session",
        "error_code", "last_error", "last_error_detail",
    ]


def test_recover_bootloader_stuck_by_last_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """last_error 為 BOOTLOADER_STUCK 且無 RX 證據時，分類仍為 BOOTLOADER 且 error_code 為 SESSION_NOT_READY。"""
    mgr = make_manager(tmp_path, [make_profile(ready_probe="echo x")])
    s = mgr.get_session("COM0")
    assert s is not None
    s.state = "ATTACHED"
    s.bridge = mock.MagicMock()
    snap = s.to_public_dict()
    snap["state"] = "ATTACHED"
    snap["last_error"] = "BOOTLOADER_STUCK"
    snap["last_error_detail"] = "matched prompt"
    monkeypatch.setattr(mgr, "_probe_existing_bridge", lambda sess, br: {"ok": True, "session": snap})
    monkeypatch.setattr(mgr, "_bootloader_evidence", lambda sess, br: None)

    r = mgr.recover_session("COM0")
    assert r["ok"] is True
    assert r["action"] == "REPROBE"
    assert r["recovered"] is False
    assert r["classification"] == "BOOTLOADER"
    assert r["recommended_action"] == "recover_interactive"
    assert r["error_code"] == "SESSION_NOT_READY"
    assert r["last_error"] == "BOOTLOADER_STUCK"
    assert r["last_error_detail"] == "matched prompt"


def test_not_ready_code_unreachable_in_source() -> None:
    """NOT_READY 錯誤碼在源碼中不可達，且 error_codes.yaml 標記 reachable 為 False。"""
    from tools.contracts import catalog, extract

    cfg = extract.load_extraction_config(ROOT / "contracts" / "code_extraction.yaml")
    res = extract.extract(ROOT, cfg)
    assert "NOT_READY" not in res.values("error_code")
    codes, _ = catalog.load_error_codes(ROOT)
    not_ready = next(c for c in codes if c.code == "NOT_READY")
    assert not_ready.reachable is False


def test_catalog_has_no_passthrough_section() -> None:
    """契約中無 last_error_passthrough 相關段落與參照。"""
    from tools.contracts import catalog, extract

    codes, passthrough = catalog.load_error_codes(ROOT)
    assert passthrough == ()
    cfg = extract.load_extraction_config(ROOT / "contracts" / "code_extraction.yaml")
    ec_ns = cfg.namespaces.get("error_code")
    assert ec_ns is not None
    assert ec_ns.passthrough == []
    rpc_schema_text = (ROOT / "contracts" / "rpc_schema.json").read_text(encoding="utf-8")
    assert "@last_error_passthrough" not in rpc_schema_text


def test_rpc_attach_response_matches_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """經 SerialwrapService.rpc('session.attach') 取得之失敗回應，符合 rpc_schema.json attach 失敗分支與 x-order。"""
    import jsonschema
    from unittest.mock import patch
    from sw_core.device_watcher import DeviceInfo
    from sw_core.service import SerialwrapService
    from tests.stage0_fakes import make_bridge

    profile = make_profile(name="p", com="COM0", by_id="/dev/serial/by-id/COM0", platform="prpl", ready_probe="echo ready")
    with patch("sw_core.service.WalWriter"), patch("sw_core.service.DeviceWatcher"):
        svc = SerialwrapService([profile], state_path=str(tmp_path / "state.json"))
    session = svc._sessions.get_session("COM0")
    assert session is not None
    bridge, _ = make_bridge(tmp_path, com="COM0")
    session.bridge = bridge
    session.state = "ATTACHED"
    session.last_error = "CREDENTIALS_UNRESOLVED"
    session.last_error_detail = None
    svc._sessions._devices["/dev/serial/by-id/COM0"] = DeviceInfo(by_id="/dev/serial/by-id/COM0", real_path="/dev/ttyUSB0")

    monkeypatch.setattr(svc._sessions, "_probe_existing_bridge", lambda sess, br: {"ok": True, "session": session.to_public_dict()})
    resp = svc.rpc("session.attach", {"selector": "COM0"})

    assert resp["ok"] is False
    assert resp["error_code"] == "SESSION_NOT_READY"
    assert resp["last_error"] == "CREDENTIALS_UNRESOLVED"
    assert list(resp.keys()) == ["ok", "error_code", "last_error", "last_error_detail", "session"]

    schema_doc = json.loads((ROOT / "contracts" / "rpc_schema.json").read_text(encoding="utf-8"))
    attach_res = schema_doc["x-methods"]["session.attach"]["result"]
    branches = attach_res.get("oneOf", [attach_res])
    fail_schema = next((b for b in branches if b.get("x-order") == ["ok", "error_code", "last_error", "last_error_detail", "session"]), None)
    assert fail_schema is not None, "rpc_schema.json 必須包含 attach 失敗分支（x-order: [ok, error_code, last_error, last_error_detail, session]）"
    resolver = jsonschema.RefResolver.from_schema(schema_doc)
    jsonschema.validate(instance=resp, schema=fail_schema, resolver=resolver)
    assert list(resp.keys()) == fail_schema.get("x-order")
