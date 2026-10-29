"""S1X-a 第 3 輪 SPEC-GAP 1／2 的契約補記（編排者裁決，S1X-a 第 4 輪）。

- SPEC-GAP 1：OD-63 佔位上的 `session bind` 比照 `session attach` 的 RELEASED 早退，回
  `{"ok":true,"released":true,"recommended_action":"device_attach","session":…}`
  （`5e7c970`）。`contracts/rpc_schema.json` 的 `session.bind` result 只列
  `ok`／`already_bound`／`session`，`side_effects` 寫「離開 RELEASED」，DF-04 也沒提到 bind。
  裁決：比照 `session.attach` 的 RELEASED 分支補 `released`、`recommended_action` 屬性與
  `x-order`，side_effects 註明「佔位不離開 RELEASED」，DF-04 compatibility_impact 補述。
- SPEC-GAP 2：佔位開埠失敗且無 passthrough fallback template 時不留列（`_pending_com`
  保留原 COM，熱插或重啟重建）。裁決：維持現行，於 DF-04／OD-63 說明補記。

schema 沒有 `additionalProperties:false`，單靠 jsonschema 驗證照過；此處另以「回應鍵為
properties 子集、鍵序為 x-order 子序列」判定，比照 `test_contracts_rpc_schema.py` 的 `_admits`。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from test_s1x_od63_released_placeholder import _manager, _no_holder, _plug, _seed_state
from test_s1x_r3_od63_placeholder_paths import _join_attach_workers, _patch_open

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO = Path(__file__).resolve().parents[1]


def _schema() -> dict[str, Any]:
    return json.loads((REPO / "contracts" / "rpc_schema.json").read_text(encoding="utf-8"))


def _df04() -> dict[str, Any]:
    changes = yaml.safe_load(
        (REPO / "contracts" / "behavior_changes.yaml").read_text(encoding="utf-8")
    )["changes"]
    return next(entry for entry in changes if entry["id"] == "DF-04")


def _admits(result: dict[str, Any], keys: list[str]) -> bool:
    keyset = set(keys)
    if not set(result.get("required", [])) <= keyset:
        return False
    if not keyset <= set(result["properties"]):
        return False
    return [k for k in result["x-order"] if k in keyset] == keys


def _bind_ok_literal_orders() -> list[list[str]]:
    """抽出 `bind_session` 內 `"ok": True` 的字面回應鍵序。"""
    tree = ast.parse((REPO / "sw_core" / "session_manager.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "bind_session"
    )
    out: list[list[str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        pairs = dict(zip((getattr(k, "value", None) for k in node.keys), node.values))
        ok = pairs.get("ok")
        if isinstance(ok, ast.Constant) and ok.value is True:
            out.append([k.value for k in node.keys if isinstance(k, ast.Constant)])
    return out


def test_bind_placeholder_runtime_response_matches_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真 SessionManager 對佔位 bind 的回應：鍵為 result properties 子集、鍵序依 x-order，值通過驗證。"""
    import jsonschema

    _seed_state(tmp_path)
    mgr = _manager(tmp_path)
    _plug(mgr)
    _no_holder(monkeypatch, mgr)
    opens: list[str] = []
    _patch_open(monkeypatch, opens)

    resp = mgr.bind_session("COM3", "/dev/serial/by-id/usb-NEW")
    _join_attach_workers(mgr)

    assert list(resp) == ["ok", "released", "recommended_action", "session"], resp
    schema = _schema()
    result = schema["x-methods"]["session.bind"]["result"]
    assert _admits(result, list(resp)), (result.get("x-order"), sorted(result["properties"]))
    assert result["properties"]["released"] == {"type": "boolean", "const": True}
    assert result["properties"]["recommended_action"] == {"type": "string"}
    jsonschema.validate(instance=resp, schema={"$defs": schema["$defs"], **result})


def test_bind_ok_literal_orders_admitted_by_result_schema() -> None:
    """`bind_session` 每個 `ok:true` 字面回應（已綁定、佔位早退、一般成功）皆被 result 以同序接受。"""
    literals = _bind_ok_literal_orders()
    assert ["ok", "released", "recommended_action", "session"] in literals, literals
    result = _schema()["x-methods"]["session.bind"]["result"]
    for keys in literals:
        assert _admits(result, keys), keys


def test_bind_result_mirrors_attach_released_branch() -> None:
    """`released`、`recommended_action` 的宣告與 `session.attach` 成功分支（RELEASED 早退）相同。"""
    schema = _schema()["x-methods"]
    attach_ok = next(
        b for b in schema["session.attach"]["result"]["oneOf"] if "released" in b["properties"]
    )
    bind = schema["session.bind"]["result"]
    for key in ("released", "recommended_action"):
        assert bind["properties"][key] == attach_ok["properties"][key], key
    attach_order = [k for k in attach_ok["x-order"] if k in bind["properties"]]
    assert [k for k in bind["x-order"] if k in attach_ok["properties"]] == attach_order


def test_bind_side_effects_note_placeholder_stays_released() -> None:
    """side_effects 須註明佔位不離開 RELEASED（不解除阻擋、不寫 override、不 spawn）。

    sol 增量審查 S1-109f659-1 確切修法 2：另補一筆帶 x-condition 的佔位早退項；一般路徑
    那筆的 x-condition 排除佔位。
    """
    effects = _schema()["x-methods"]["session.bind"]["side_effects"]
    texts = [json.dumps(e, ensure_ascii=False) for e in effects]
    assert any("佔位不離開 RELEASED" in t for t in texts), texts
    assert any("OD-63" in t for t in texts), texts
    rebind = next(e for e in effects if "離開 RELEASED、" in e["what"])
    assert "佔位" in rebind.get("x-condition", ""), rebind
    placeholder = [e for e in effects if e is not rebind and "OD-63" in e.get("x-condition", "")]
    assert len(placeholder) == 1, effects
    assert "佔位不離開 RELEASED" in placeholder[0]["what"], placeholder
    assert placeholder[0]["when"] == "on_success" and placeholder[0]["idempotent"] is True


def test_df04_compatibility_impact_covers_bind_and_no_fallback_open_failure() -> None:
    """DF-04 補述：佔位 bind 的回應；佔位開埠失敗且無 passthrough fallback template 時不留列。"""
    impact = _df04()["compatibility_impact"]
    assert "session.bind" in impact, impact
    assert "released:true" in impact and "recommended_action" in impact, impact
    assert "fallback template" in impact and "不留列" in impact, impact
    assert "_pending_com" in impact and "不保證沿用原 COM" in impact, impact


def test_placeholder_open_failure_without_fallback_reserves_com_until_unplug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """伴隨確認（SPEC-GAP 2 維持現行）：無 fallback 時不留列、COM3 只保留到裝置拔除為止。"""
    from test_s1x_od63_released_placeholder import BY_ID, SID

    _seed_state(tmp_path)
    mgr = _manager(tmp_path)
    _plug(mgr)
    _no_holder(monkeypatch, mgr)
    opens: list[str] = []
    _patch_open(monkeypatch, opens, PermissionError(13, "Permission denied"))

    assert mgr.attach_device("COM3")["ok"] is True
    _join_attach_workers(mgr)

    assert SID not in mgr._sessions
    assert mgr.get_session("COM3") is None
    assert mgr.attach_device("COM3")["error_code"] == "SESSION_NOT_FOUND"
    assert mgr._pending_com.get(BY_ID) == "COM3"
    mgr.update_devices(dict(mgr._devices))
    _join_attach_workers(mgr)
    assert mgr.get_session("COM3") is None, "同一裝置集合重掃不會重試開埠"
    assert mgr._pending_com.get(BY_ID) == "COM3"
    mgr.update_devices({})
    assert BY_ID not in mgr._pending_com, "裝置拔除後不再保留原 COM（之後依 dynamic 規則重新分配）"


def test_openspec_od63_scenario_notes_no_fallback_open_failure() -> None:
    """OD-63 情境說明補記無 passthrough fallback template 時的開埠失敗行為。"""
    text = (REPO / "openspec" / "specs" / "device-handoff" / "spec.md").read_text(encoding="utf-8")
    start = text.index("#### Scenario: 所屬 template 已不存在的 released 紀錄以佔位 session 解除（OD-63）")
    scenario = text[start:text.index("### Requirement", start)]
    assert "無 passthrough fallback template" in scenario, scenario
    assert "不留列" in scenario and "_pending_com" in scenario, scenario
    assert "不保證沿用原 COM" in scenario, scenario
