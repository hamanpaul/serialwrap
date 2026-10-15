"""契約 T10：WAL 紀錄、META 事件與 state.json 的 schema 測試（TDD red 階段）。

對應 `docs/rust-refactor/plans/01-stage0-contracts.md` Task T10。被測契約檔為
`contracts/wal_record.schema.json`、`contracts/wal_meta_events.schema.json`、
`contracts/state_file.schema.json`；本檔建立時尚不存在，測試應因「契約檔不存在」
或 schema 規則缺失而失敗，不得因模組匯入錯誤而失敗。

約定：
- 模組頂層不匯入 `sw_core`／`jsonschema`，一律於函式內延遲匯入，確保整體 collection 不受影響。
- `tools.contracts` 不在本檔使用（本檔只驗 schema 與 Python 基線輸出）。
- 只有標明 L1 白箱的常數對照測試會匯入 `sw_core`。
- state.json 寫入明確注入 `tmp_path`，不觸碰真實 state 路徑。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = REPO_ROOT / "contracts"
WAL_SCHEMA = "wal_record.schema.json"
META_SCHEMA = "wal_meta_events.schema.json"
STATE_SCHEMA = "state_file.schema.json"
RECREATED_EVENT = "WAL_RECREATED_AFTER_EXTERNAL_UNLINK"

# 04 §1.3 的三行實跑範例。RX／TX 行逐字保留。
# META 行的 meta 改為 credentials_unresolved 的實際格式：原範例 {"event":"x"} 不屬於
# wal_meta_events.schema.json 的封閉 oneOf 列舉，依 plan T10 不可能通過。此為對 plan
# 「逐字貼入」的唯一偏離，待擁有者或協調者確認。
_WAL_INVENTORY_LINES = (
    '{"cmd_id":null,"com":"COM0","crc32":"a81bc28c","dir":"RX","len":14,"loss_flag":false,'
    '"meta":{},"mono_ts_ns":5798622898976,"payload_b64":"aGVsbG8NChtbMG0Lf/8=","seq":1,'
    '"source":"uart","wall_ts":"2026-10-15T03:06:17.663778+00:00"}',
    '{"cmd_id":"c1","com":"COM0","crc32":"0e048d3e","dir":"TX","len":2,"loss_flag":false,'
    '"meta":{"a":"中","z":1},"mono_ts_ns":5798623076941,"payload_b64":"w6k=","seq":2,'
    '"source":"agent:x","wall_ts":"2026-10-15T03:06:17.663955+00:00"}',
    '{"cmd_id":null,"com":"COM0","crc32":"00000000","dir":"META","len":0,"loss_flag":false,'
    '"meta":{"event":"credentials_unresolved","reason":"key_absent","env_file":""},'
    '"mono_ts_ns":5798623144560,"payload_b64":"","seq":3,"source":"system",'
    '"wall_ts":"2026-10-15T03:06:17.664022+00:00"}',
)

# 04 §2.2 的 state.json 實跑範例；updated_at 的 "..." 換成合法 ISO 字串。
_STATE_INVENTORY_EXAMPLE = (
    '{"aliases":{"custom":{"session_id":"p:COM5","updated_at":"2026-10-15T03:07:39.596535+00:00"},'
    '"lab+1":{"session_id":"p:COM5","updated_at":"2026-10-15T03:07:39.596535+00:00"},'
    '"prpl-template+2":{"session_id":"prpl-template:COM0","updated_at":"2026-10-15T03:07:39.596535+00:00"},'
    '"zz":{"device_by_id":"/dev/serial/by-id/Z","profile":"prpl-template","updated_at":"2026-10-15T03:07:39.596535+00:00"}},'
    '"bindings":{},"profile_detected":{},'
    '"profile_pins":{"/dev/serial/by-id/X":"prpl-template"},'
    '"released":{"prpl-template:COM0":{"by_id":"/dev/serial/by-id/X","reason":"r","released_at":"t","released_by":"cli"}}}'
)


def _load_schema(name: str) -> dict[str, Any]:
    path = CONTRACTS_DIR / name
    assert path.is_file(), f"契約檔不存在：contracts/{name}"
    return json.loads(path.read_text(encoding="utf-8"))


def _resolver_for(name: str) -> Any:
    """預載 WAL 與 META 兩檔，讓 wal_record 的外部 $ref 可解析（jsonschema 4.10.3 無 referencing）。"""
    from jsonschema import RefResolver

    store = {(CONTRACTS_DIR / fn).as_uri(): _load_schema(fn) for fn in (WAL_SCHEMA, META_SCHEMA)}
    return RefResolver(
        base_uri=(CONTRACTS_DIR / name).as_uri(),
        referrer=_load_schema(name),
        store=store,
    )


def _wal_validator() -> Any:
    from jsonschema import Draft202012Validator

    return Draft202012Validator(_load_schema(WAL_SCHEMA), resolver=_resolver_for(WAL_SCHEMA))


def _wal_meta_record(meta: dict[str, Any], *, seq: int = 4, loss_flag: bool = True) -> dict[str, Any]:
    """外層 META 紀錄（arch §3.8：dir=META、source=system、loss_flag=true、payload 為空）。

    com 暫用一般 COM 值；P1 依 arch §3.8 修訂改為 "WAL"。
    """
    return {
        "cmd_id": None,
        "com": "COM0",
        "crc32": "00000000",
        "dir": "META",
        "len": 0,
        "loss_flag": loss_flag,
        "meta": meta,
        "mono_ts_ns": 5798623144560,
        "payload_b64": "",
        "seq": seq,
        "source": "system",
        "wall_ts": "2026-10-15T03:06:17+00:00",
    }


def _find_key_value(node: Any, key: str, value: Any) -> bool:
    if isinstance(node, dict):
        if node.get(key) == value:
            return True
        return any(_find_key_value(v, key, value) for v in node.values())
    if isinstance(node, list):
        return any(_find_key_value(v, key, value) for v in node)
    return False


def _defines_event(node: Any, event: str) -> bool:
    """節點是否以 const 或 enum 「定義」某事件名（資料中的字面值不算定義）。"""
    if isinstance(node, dict):
        if node.get("const") == event:
            return True
        enum = node.get("enum")
        if isinstance(enum, list) and event in enum:
            return True
        return any(_defines_event(v, event) for v in node.values())
    if isinstance(node, list):
        return any(_defines_event(v, event) for v in node)
    return False


def _branch_event(branch: dict[str, Any]) -> Any:
    event_prop = (branch.get("properties") or {}).get("event") or {}
    return event_prop.get("const")


def _range_order_violation(meta: dict[str, Any]) -> str | None:
    """輔助檢查 potentially_lost_seq_range 的 x-constraint「first <= last」。

    JSON Schema 無法比較陣列內兩元素，故由測試驗證；型別與形狀仍由 schema 負責。
    """
    rng = meta.get("potentially_lost_seq_range")
    if not isinstance(rng, list) or len(rng) != 2:
        return None
    first, last = rng
    if isinstance(first, int) and isinstance(last, int) and first > last:
        return f"first({first}) > last({last})"
    return None


def _assert_keys_sorted(node: Any, path: str = "$") -> None:
    if isinstance(node, dict):
        keys = list(node.keys())
        assert keys == sorted(keys), f"{path} 的 key 非字母序：{keys}"
        for key, value in node.items():
            _assert_keys_sorted(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _assert_keys_sorted(value, f"{path}[{index}]")


def _profile(name: str, com: str, alias: str, device_by_id: str) -> Any:
    from sw_core.config import SessionProfile, UartProfile

    return SessionProfile(
        profile_name=name,
        com=com,
        act_no=1,
        alias=alias,
        device_by_id=device_by_id,
        platform="prpl",
        uart=UartProfile(),
    )


# ---------------------------------------------------------------------------
# wal_record.schema.json
# ---------------------------------------------------------------------------


def test_wal_schema_valid_draft2020() -> None:
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(_load_schema(WAL_SCHEMA))


def test_wal_records_from_writer_validate(tmp_path: Path) -> None:
    from sw_core.wal import WalWriter

    wal = WalWriter(wal_dir=str(tmp_path))
    wal.append(com="COM0", direction="RX", source="device", payload=b"hello\r\n")
    wal.append(
        com="COM0",
        direction="TX",
        source="agent:x",
        payload=b"echo hi\r\n",
        cmd_id="c1",
        meta={"a": "中", "z": 1},
    )
    wal.append(
        com="COM0",
        direction="META",
        source="system",
        payload=b"",
        meta={"event": "credentials_unresolved", "reason": "key_absent", "env_file": ""},
    )

    lines = Path(wal.wal_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    validator = _wal_validator()
    for line in lines:
        record = json.loads(line)
        validator.validate(record)
        assert list(record.keys()) == sorted(record.keys())


def test_wal_inventory_examples_validate() -> None:
    validator = _wal_validator()
    for line in _WAL_INVENTORY_LINES:
        validator.validate(json.loads(line))


def test_wal_crc32_uppercase_rejected() -> None:
    from jsonschema import ValidationError

    record = json.loads(_WAL_INVENTORY_LINES[0])
    record["crc32"] = record["crc32"].upper()
    with pytest.raises(ValidationError):
        _wal_validator().validate(record)


def test_wal_unknown_key_rejected() -> None:
    from jsonschema import ValidationError

    record = json.loads(_WAL_INVENTORY_LINES[0])
    record["extra"] = 1
    with pytest.raises(ValidationError):
        _wal_validator().validate(record)


def test_wal_rotate_threshold_matches_constant() -> None:
    """L1 白箱：比對 sw_core 的 rotate 門檻常數（原始碼為 64 * 1024 * 1024，無法以 ast.literal_eval 求值）。"""
    from sw_core.constants import DEFAULT_WAL_ROTATE_BYTES

    schema = _load_schema(WAL_SCHEMA)
    assert schema["x-rotate"]["threshold_bytes"] == DEFAULT_WAL_ROTATE_BYTES


# ---------------------------------------------------------------------------
# wal_meta_events.schema.json
# ---------------------------------------------------------------------------


def test_meta_schema_single_source() -> None:
    assert (CONTRACTS_DIR / META_SCHEMA).is_file(), f"契約檔不存在：contracts/{META_SCHEMA}"
    defining = sorted(
        path.name
        for path in CONTRACTS_DIR.rglob("*.json")
        if _defines_event(json.loads(path.read_text(encoding="utf-8")), RECREATED_EVENT)
    )
    assert defining == [META_SCHEMA], f"{RECREATED_EVENT} 應只由 {META_SCHEMA} 定義，實際：{defining}"
    assert _find_key_value(_load_schema(WAL_SCHEMA), "$ref", META_SCHEMA), (
        f"{WAL_SCHEMA} 應以 $ref 指向 {META_SCHEMA}"
    )


def test_wal_recreated_meta_schema_present() -> None:
    meta_schema = _load_schema(META_SCHEMA)
    branches = [b for b in meta_schema.get("oneOf", []) if _branch_event(b) == RECREATED_EVENT]
    assert branches, f"{META_SCHEMA} 缺少 {RECREATED_EVENT} 分支"
    assert branches[0].get("x-behavior-change") == "BC-01"


def test_wal_recreated_range_positive() -> None:
    validator = _wal_validator()
    base = {"event": RECREATED_EVENT, "reason": "external_unlink", "previous_identity": "2049:131073"}
    cases = [
        {**base, "range_known": True, "potentially_lost_seq_range": [3, 9]},
        {**base, "range_known": False, "potentially_lost_seq_range": None},
    ]
    for meta in cases:
        validator.validate(_wal_meta_record(meta))
        assert _range_order_violation(meta) is None


def test_wal_recreated_range_negative() -> None:
    from jsonschema import ValidationError

    validator = _wal_validator()
    base = {"event": RECREATED_EVENT, "reason": "external_unlink", "previous_identity": None}

    schema_only_cases = {
        "range_known=false 卻給區間": {**base, "range_known": False, "potentially_lost_seq_range": [3, 9]},
        "range_known=true 卻給 null": {**base, "range_known": True, "potentially_lost_seq_range": None},
        "range_known=true 元素為字串": {**base, "range_known": True, "potentially_lost_seq_range": ["3", "9"]},
    }
    for label, meta in schema_only_cases.items():
        try:
            validator.validate(_wal_meta_record(meta))
        except ValidationError:
            continue
        pytest.fail(f"schema 應拒絕：{label}")

    # first > last：schema 無法表達，允許 schema 拒絕或輔助檢查 FAIL 二擇一。
    order_meta = {**base, "range_known": True, "potentially_lost_seq_range": [9, 3]}
    try:
        validator.validate(_wal_meta_record(order_meta))
    except ValidationError:
        pass
    else:
        assert _range_order_violation(order_meta) is not None, "[9,3] 應被 first<=last 輔助檢查拒絕"

    # x-constraint 標示必須存在於 META schema 中。
    assert _find_key_value(_load_schema(META_SCHEMA), "x-constraint", "first <= last"), (
        'potentially_lost_seq_range 應以 x-constraint: "first <= last" 標示'
    )


# ---------------------------------------------------------------------------
# state_file.schema.json
# ---------------------------------------------------------------------------


def test_state_schema_valid_draft2020() -> None:
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(_load_schema(STATE_SCHEMA))


def test_state_from_manager_validates(tmp_path: Path) -> None:
    from jsonschema import Draft202012Validator

    from sw_core.config import ProfileTemplate
    from sw_core.session_manager import SessionManager
    from sw_core.wal import WalWriter

    profiles = [
        _profile("p", "COM0", "lab+1", "/dev/serial/by-id/orig"),
        _profile("p", "COM1", "lab+2", "/dev/serial/by-id/other"),
    ]
    state_path = tmp_path / "state.json"
    mgr = SessionManager(
        profiles,
        WalWriter(wal_dir=str(tmp_path / "wal")),
        templates=[ProfileTemplate(profile_name="prpl-template")],
        on_ready=lambda _s: None,
        on_detached=lambda _s: None,
        state_path=str(state_path),
    )

    # 設定步驟本身必須成功，否則後續 schema 斷言會在空資料上通過而失去意義。
    assert mgr.assign_alias("/dev/serial/by-id/zz", "zz", "prpl-template")["ok"] is True
    assert mgr.pin_session("/dev/serial/by-id/pinned", "prpl-template")["ok"] is True

    # __init__ 會略過 device_by_id 為空的 YAML target，因此於建構後以執行期替換產生 by_id 為 None 的 session。
    session = mgr.get_session("COM1")
    assert session is not None
    session.profile = dataclasses.replace(session.profile, device_by_id=None)
    assert mgr.release_device("COM1", source="agent:flash", reason={"why": "flash"})["ok"] is True
    mgr._save_state()

    raw = state_path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    doc = json.loads(raw)
    Draft202012Validator(_load_schema(STATE_SCHEMA)).validate(doc)
    _assert_keys_sorted(doc)
    assert doc["released"]["p:COM1"]["by_id"] is None
    assert isinstance(doc["released"]["p:COM1"]["reason"], dict)


def test_state_inventory_example_validates() -> None:
    from jsonschema import Draft202012Validator

    Draft202012Validator(_load_schema(STATE_SCHEMA)).validate(json.loads(_STATE_INVENTORY_EXAMPLE))


def test_state_extra_top_key_rejected() -> None:
    from jsonschema import Draft202012Validator, ValidationError

    doc = json.loads(_STATE_INVENTORY_EXAMPLE)
    doc["version"] = 1
    with pytest.raises(ValidationError):
        Draft202012Validator(_load_schema(STATE_SCHEMA)).validate(doc)
