"""契約目錄 loader 與結構規則（Rust 重構 P0-T1）。

讀取 `contracts/` 下四個目錄檔（`error_codes.yaml`、`session_last_errors.yaml`、
`behavior_changes.yaml`、`diagnostic_aux.yaml`），逐條檢查結構，回傳不可變的資料物件。

- 每個 `validate_*_doc` 只累積問題字串（格式 `<檔名>:<定位>: <訊息>`），不 raise。
- 交叉參照（例如 `behavior_change` ID 是否存在、碼是否由程式抽出）屬 T6
  `check_cross_refs`，不在 loader 內處理。
- 只 import 標準函式庫與 PyYAML，不得 import `sw_core`（§3.1、§11.3）。
- 型別判斷中 bool 一律先於 int 排除（`isinstance(True, int)` 為真）。
"""

from __future__ import annotations

import datetime as _dt
import re
import types
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from . import CONTRACTS_DIR

# ---------------------------------------------------------------------------
# 封閉集合（與 docs/rust-refactor/plans/01-stage0-contracts.md「共用介面」一致）
# ---------------------------------------------------------------------------

INTERFACES: Final[tuple[str, ...]] = ("rpc", "rpc_frame", "cli_client", "cli_local", "rec", "proc")
RETRYABLE: Final[tuple[str, ...]] = ("yes", "no", "conditional", "fix_args")
CODE_STATUS: Final[tuple[str, ...]] = ("EXISTING", "APPROVED", "IMPLEMENTED", "VERIFIED")
EFFECTIVE_STATUS: Final[frozenset[str]] = frozenset({"EXISTING", "IMPLEMENTED", "VERIFIED"})
CHANGE_STATUS: Final[tuple[str, ...]] = ("PROPOSED", "APPROVED", "IMPLEMENTED", "VERIFIED")
SESSION_STATES: Final[tuple[str, ...]] = (
    "DETACHED", "ATTACHING", "ATTACHED", "READY", "RECOVERING", "RELEASED", "FLASHING",
)
PLATFORMS: Final[tuple[str, ...]] = ("posix", "windows")
# 回此碼時是否可能已有外部 UART TX（§11.3 不變量 #1／#5）
UART_TX: Final[tuple[str, ...]] = ("none", "possible", "confirmed")
CHANGE_SCOPES: Final[tuple[str, ...]] = ("both", "rust_only", "python_when_manifest_present")
AB_PARTICIPATION: Final[tuple[str, ...]] = ("diff", "absolute_only")
# 由 agent 推導的 SC／DF 不得冒用擁有者名義（owner_decision 才歸屬 hamanpaul）
APPROVAL_OWNER_BY_BASIS: Final[Mapping[str, str]] = types.MappingProxyType({
    "owner_decision": "hamanpaul",
    "spec_final": "spec-final",
    "reconciliation": "spec-reconciliation",
})
# exit_codes／x-exit-codes 的封閉 context 集合
CLI_CONTEXTS: Final[tuple[str, ...]] = (
    "_run_rpc", "daemon start", "daemon status", "daemon stop", "daemon restart", "event", "connect", "benches", "remote", "setup",
    "service <action>", "cmd submit", "profile test", "doctor", "argparse-boundary", "fallthrough",
)
# §11.3 六項不變量；ID 與 P2 cases.yaml 的 invariants 欄一致
SAFETY_INVARIANTS: Final[tuple[str, ...]] = (
    "zero_tx_on_reject", "no_forbidden_interleave", "no_stale_context_replay",
    "no_reopen_before_handoff", "no_auto_resend", "insufficient_evidence_not_pass",
)

FILE_ERROR_CODES: Final[str] = "error_codes.yaml"
FILE_SESSION_LAST_ERRORS: Final[str] = "session_last_errors.yaml"
FILE_BEHAVIOR_CHANGES: Final[str] = "behavior_changes.yaml"
FILE_DIAGNOSTIC_AUX: Final[str] = "diagnostic_aux.yaml"
CATALOG_FILES: Final[tuple[str, ...]] = (
    FILE_ERROR_CODES, FILE_SESSION_LAST_ERRORS, FILE_BEHAVIOR_CHANGES, FILE_DIAGNOSTIC_AUX,
)

_CHANGE_KINDS: Final[tuple[str, ...]] = ("BC", "SC", "DF")
_APPROVAL_BASES: Final[tuple[str, ...]] = ("owner_decision", "spec_final", "reconciliation")
_OWNER_CONFIRMATIONS: Final[tuple[str, ...]] = ("not_required", "pending_default", "confirmed")
_LAST_ERROR_KINDS: Final[tuple[str, ...]] = ("named", "dynamic_prefix")
_PROMOTED_VIA: Final[tuple[str, ...]] = ("session.attach", "session.recover")
_ERROR_CODE_NAMESPACES: Final[tuple[str, ...]] = ("forbidden", "allowed_shared")
_RUST_STAGES: Final[tuple[str, ...]] = (
    "stage-1a", "stage-1b", "slice-2.1", "slice-2.2", "slice-2.3", "slice-2.4", "stage-3",
)
_PHASES: Final[tuple[str, ...]] = tuple(f"P{i}" for i in range(10))
_OWNER_ITEM_MIN: Final[int] = 1
_SAFETY_SPEC_REF: Final[str] = "§11.3"
_PASSTHROUGH_NAMESPACE_REF: Final[str] = "session_last_errors"

# ---------------------------------------------------------------------------
# 格式規則（一律以 fullmatch 比對，避免 `$` 放行結尾換行）
# ---------------------------------------------------------------------------

_CODE_RE: Final = re.compile(r"[A-Z][A-Z0-9_]*[A-Z0-9]")
_CONSTANT_RE: Final = re.compile(r"[A-Z_][A-Z0-9_]*")
_CONTRACT_TEST_RE: Final = re.compile(r"[^\s:]+\.py(?:::[A-Za-z_][\w\[\]\-.]*)*")
_BC_ID_RE: Final = re.compile(r"(?:BC|SC|DF)-[0-9]{2}")
_DYNAMIC_VALUE_RE: Final = re.compile(r"[A-Z][A-Z0-9_]*:\*")
_PASSTHROUGH_ID_RE: Final = re.compile(r"[a-z][a-z0-9_]*")
_DATE_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SEMVER_RE: Final = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_CASE_ID_RE: Final = re.compile(r"[a-z0-9][a-z0-9-]*")
_CLEARED_BY_RE: Final = re.compile(r"(?:auto|operator):.+")
_AFFECT_PASSTHROUGH_RE: Final = re.compile(r"error_codes:passthrough:([a-z][a-z0-9_]*)")
_AFFECT_LAST_ERROR_RE: Final = re.compile(r"session_last_errors:(.+)")

# ---------------------------------------------------------------------------
# 欄位集合（封閉鍵集；未知鍵一律判 FAIL）
# ---------------------------------------------------------------------------

_EC_TOP_KEYS: Final = ("schema_version", "revision", "namespace", "codes", "passthrough")
_EC_REQUIRED: Final = frozenset({
    "code", "status", "reachable", "scope", "meaning", "side_effect", "retryable", "uart_tx",
    "agent_action", "extra_fields", "contract_tests", "behavior_change",
})
_EC_OPTIONAL: Final = frozenset({
    "unreachable_reason", "constant", "sources", "ok_response", "exit_codes",
})
_PT_REQUIRED: Final = frozenset({
    "id", "namespace_ref", "scope", "rule", "owner_confirmation", "reconciliation_refs",
})
_SCOPE_KEYS: Final = frozenset({"interfaces", "methods", "platforms"})

_LE_TOP_KEYS: Final = ("schema_version", "revision", "namespace", "values")
_LE_REQUIRED: Final = frozenset({
    "value", "kind", "reason_forms", "reachable", "meaning", "agent_action", "manual_recovery",
    "logging", "detail", "terminal", "auto_reprobe", "states", "set_by", "cleared_by",
    "promoted_via", "error_code_namespace", "contract_tests",
})
_LE_OPTIONAL: Final = frozenset({"unreachable_reason"})
_AUTO_REPROBE_KEYS: Final = frozenset({"attached", "detached"})

_BC_TOP_KEYS: Final = (
    "schema_version", "revision", "changes", "pending_owner_confirmations", "safety_invariants",
)
_BC_REQUIRED: Final = frozenset({
    "id", "kind", "title", "status", "approval", "owner_confirmation", "owner_item",
    "default_if_pending", "applies_to", "approved_at_revision", "spec_refs",
    "reconciliation_refs", "scope", "ab_participation", "phases", "case_ids",
    "compatibility_impact", "approval_conditions", "related_decisions",
})
_APPROVAL_KEYS: Final = frozenset({"owner", "date", "decision_ref", "basis"})
_APPLIES_TO_KEYS: Final = frozenset({"python", "rust"})
_PENDING_KEYS: Final = frozenset({"item", "topic", "refs", "default", "affects"})
_INVARIANT_KEYS: Final = frozenset({"id", "title", "spec_ref", "error_codes", "case_ids"})

_AUX_LISTS: Final[Mapping[str, tuple[str, ...]]] = types.MappingProxyType({
    "warning_codes": ("code", "meaning"),
    "error_details": ("value", "parent_code", "meaning"),
    "classifications": ("value", "recommended_actions", "meaning"),
    "recommended_actions": ("value", "meaning"),
    "recovery_actions": ("value", "meaning"),
    "recover_actions": ("value", "meaning"),
})
_AUX_TOP_KEYS: Final[tuple[str, ...]] = ("schema_version", "revision", *_AUX_LISTS)


# ---------------------------------------------------------------------------
# 資料物件（不可變；欄位對應計畫「共用介面」）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    interfaces: tuple[str, ...]
    methods: tuple[str, ...]
    platforms: tuple[str, ...]


@dataclass(frozen=True)
class ErrorCodeEntry:
    code: str
    status: str
    reachable: bool
    unreachable_reason: str | None
    constant: str | None
    scope: Scope
    meaning: str
    side_effect: str
    retryable: str
    uart_tx: str
    ok_response: bool
    agent_action: str
    extra_fields: tuple[str, ...]
    exit_codes: Mapping[str, int]
    contract_tests: tuple[str, ...]
    behavior_change: tuple[str, ...]
    sources: tuple[str, ...]


@dataclass(frozen=True)
class PassthroughEntry:
    id: str
    namespace_ref: str
    scope: Scope
    rule: str
    owner_confirmation: str
    reconciliation_refs: tuple[str, ...]


@dataclass(frozen=True)
class LastErrorEntry:
    value: str
    kind: str
    reason_forms: tuple[str, ...]
    reachable: bool
    unreachable_reason: str | None
    meaning: str
    agent_action: str
    terminal: bool
    auto_reprobe_attached: bool
    auto_reprobe_detached: bool
    manual_recovery: str
    logging: str
    detail: str
    states: tuple[str, ...]
    set_by: tuple[str, ...]
    cleared_by: tuple[str, ...]
    promoted_via: tuple[str, ...]
    error_code_namespace: str
    contract_tests: tuple[str, ...]


@dataclass(frozen=True)
class Approval:
    owner: str
    date: str
    decision_ref: str
    basis: str


@dataclass(frozen=True)
class BehaviorChange:
    id: str
    kind: str
    title: str
    status: str
    approval: Approval
    owner_confirmation: str
    owner_item: int | None
    default_if_pending: str | None
    applies_python_min: str | None
    applies_rust_stage: str | None
    approved_at_revision: int
    spec_refs: tuple[str, ...]
    reconciliation_refs: tuple[str, ...]
    scope: str
    ab_participation: str
    phases: tuple[str, ...]
    case_ids: tuple[str, ...]
    compatibility_impact: str
    approval_conditions: str
    related_decisions: tuple[str, ...]


@dataclass(frozen=True)
class OwnerConfirmation:
    item: int
    topic: str
    refs: tuple[str, ...]
    default: str
    affects: tuple[str, ...]


@dataclass(frozen=True)
class SafetyInvariant:
    id: str
    title: str
    spec_ref: str
    error_codes: tuple[str, ...]
    case_ids: tuple[str, ...]


@dataclass(frozen=True)
class AuxCatalog:
    warning_codes: tuple[tuple[str, str], ...]
    error_details: tuple[tuple[str, str, str], ...]
    classifications: tuple[tuple[str, tuple[str, ...], str], ...]
    recommended_actions: tuple[tuple[str, str], ...]
    recovery_actions: tuple[tuple[str, str], ...]
    recover_actions: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Catalogs:
    error_codes: tuple[ErrorCodeEntry, ...]
    passthrough: tuple[PassthroughEntry, ...]
    last_errors: tuple[LastErrorEntry, ...]
    behavior_changes: tuple[BehaviorChange, ...]
    owner_confirmations: tuple[OwnerConfirmation, ...]
    safety_invariants: tuple[SafetyInvariant, ...]
    aux: AuxCatalog
    revisions: Mapping[str, int]  # 檔名 → catalog_revision（不是 contract_revision）


class CatalogError(Exception):
    """契約目錄載入或結構檢查失敗；`problems` 彙整全部問題字串。"""

    problems: list[str]

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


# ---------------------------------------------------------------------------
# 問題累積與共用檢查輔助
# ---------------------------------------------------------------------------


class _Problems:
    """累積問題字串，格式 `<檔名>:<定位>: <訊息>`。"""

    def __init__(self, filename: str) -> None:
        self._filename = filename
        self.items: list[str] = []

    def add(self, loc: str, message: str) -> None:
        self.items.append(f"{self._filename}:{loc}: {message}")


def _type_name(value: object) -> str:
    return type(value).__name__


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _in_owner_item(value: object) -> bool:
    return _is_int(value) and value >= _OWNER_ITEM_MIN


def _label(entry: object, key: str, index: int) -> str:
    """定位用標籤：優先取字串型的識別欄位，否則以索引代替。"""
    if isinstance(entry, dict) and isinstance(entry.get(key), str):
        return str(entry[key])
    return f"#{index}"


def _require_mapping(p: _Problems, loc: str, value: object) -> bool:
    if isinstance(value, dict):
        return True
    p.add(loc, f"必須為 mapping，實際為 {_type_name(value)}")
    return False


_CONTRACT_REVISION_KEY: Final[str] = "contract_revision"
_CONTRACT_REVISION_MESSAGE: Final[str] = (
    "不得出現 contract_revision 鍵（tree SHA 只記於 release manifest，不寫入 contracts/）"
)


def _unknown_key_message(key: object) -> str:
    if key == _CONTRACT_REVISION_KEY:
        return _CONTRACT_REVISION_MESSAGE
    return f"未知鍵 {key!r}"


def _check_closed(p: _Problems, loc: str, entry: Mapping[Any, Any],
                  required: Collection[str], optional: Collection[str] = ()) -> None:
    """檢查必要鍵齊全且無未知鍵（封閉鍵集）；`contract_revision` 於任何位置皆禁止。"""
    for key in sorted(required):
        if key not in entry:
            p.add(loc, f"缺少必要鍵 '{key}'")
    for key in entry:
        if key not in required and key not in optional:
            p.add(loc, _unknown_key_message(key))


def _check_bool(p: _Problems, loc: str, value: object) -> bool:
    if isinstance(value, bool):
        return True
    p.add(loc, f"必須為布林值（true／false），實際為 {_type_name(value)}")
    return False


def _check_str(p: _Problems, loc: str, value: object, *, nonempty: bool = True) -> bool:
    if isinstance(value, bool):
        p.add(loc, "必須為加引號的字串（YAML 1.1 會把未加引號的 yes／no 轉成布林值）")
        return False
    if isinstance(value, _dt.date):
        p.add(loc, "必須為加引號的字串（未加引號的日期會被 YAML 轉成 datetime.date）")
        return False
    if not isinstance(value, str):
        p.add(loc, f"必須為字串，實際為 {_type_name(value)}")
        return False
    if nonempty and not value.strip():
        p.add(loc, "不得為空字串")
        return False
    return True


def _check_text(p: _Problems, loc: str, value: object) -> bool:
    """非空字串，且不得含 `|` 或換行（SKILL 表格需要）。"""
    if not _check_str(p, loc, value):
        return False
    assert isinstance(value, str)
    if "|" in value or "\n" in value or "\r" in value:
        p.add(loc, f"不得含 '|' 或換行（SKILL 表格需要）: {value!r}")
        return False
    return True


def _check_enum(p: _Problems, loc: str, value: object, allowed: tuple[str, ...]) -> bool:
    if not _check_str(p, loc, value):
        return False
    if value not in allowed:
        p.add(loc, f"必須為 {' / '.join(allowed)} 之一，實際為 {value!r}")
        return False
    return True


def _check_str_list(p: _Problems, loc: str, value: object, *, nonempty: bool = False,
                    pattern: re.Pattern[str] | None = None, pattern_desc: str = "",
                    allowed: tuple[str, ...] | None = None) -> bool:
    if not isinstance(value, list):
        p.add(loc, f"必須為 list，實際為 {_type_name(value)}")
        return False
    ok = True
    if nonempty and not value:
        p.add(loc, "不得為空 list")
        ok = False
    for j, item in enumerate(value):
        item_loc = f"{loc}[{j}]"
        if not _check_str(p, item_loc, item):
            ok = False
            continue
        if pattern is not None and pattern.fullmatch(item) is None:
            p.add(item_loc, f"不符合格式 {pattern_desc}: {item!r}")
            ok = False
        if allowed is not None and item not in allowed:
            p.add(item_loc, f"必須為 {' / '.join(allowed)} 之一，實際為 {item!r}")
            ok = False
    return ok


def _check_header(p: _Problems, doc: object, keys: tuple[str, ...],
                  namespace: str | None) -> int | None:
    """檢查頂層封閉鍵、schema_version、revision 與 namespace；回傳有效的 revision。"""
    if not isinstance(doc, dict):
        p.add("(根)", f"頂層必須為 mapping，實際為 {_type_name(doc)}")
        return None
    for key in keys:
        if key not in doc:
            p.add("(根)", f"缺少頂層鍵 '{key}'")
    for key in doc:
        if key not in keys:
            p.add("(根)", f"不得有其他頂層鍵 {key!r}" if key != _CONTRACT_REVISION_KEY else _CONTRACT_REVISION_MESSAGE)
    if "schema_version" in doc and not (_is_int(doc["schema_version"]) and doc["schema_version"] == 1):
        p.add("schema_version", f"必須為整數 1，實際為 {doc['schema_version']!r}")
    revision: int | None = None
    if "revision" in doc:
        raw = doc["revision"]
        if _is_int(raw) and raw >= 1:
            revision = raw
        else:
            p.add("revision", f"必須為 ≥1 的整數（catalog_revision），實際為 {raw!r}")
    if namespace is not None and "namespace" in doc:
        if _check_str(p, "namespace", doc["namespace"]) and doc["namespace"] != namespace:
            p.add("namespace", f"必須為 {namespace}，實際為 {doc['namespace']!r}")
    return revision


def _scope_interfaces(scope: object) -> list[Any]:
    if isinstance(scope, dict) and isinstance(scope.get("interfaces"), list):
        return scope["interfaces"]
    return []


def _check_scope(p: _Problems, loc: str, scope: object, *, methods_nonempty: bool) -> None:
    if not _require_mapping(p, loc, scope):
        return
    assert isinstance(scope, dict)
    _check_closed(p, loc, scope, _SCOPE_KEYS)
    if "interfaces" in scope:
        _check_str_list(p, f"{loc}.interfaces", scope["interfaces"], nonempty=True, allowed=INTERFACES)
    if "methods" in scope:
        _check_str_list(p, f"{loc}.methods", scope["methods"], nonempty=methods_nonempty)
    if "platforms" in scope:
        _check_str_list(p, f"{loc}.platforms", scope["platforms"], nonempty=True, allowed=PLATFORMS)


def _check_code_name(p: _Problems, loc: str, code: str) -> None:
    if _CODE_RE.fullmatch(code) is None:
        p.add(loc, f"不符合 code 格式 ^[A-Z][A-Z0-9_]*[A-Z0-9]$: {code!r}")
    elif code.startswith("HARNESS_"):
        p.add(loc, "HARNESS_ 前綴為測試工具原因碼，不得收錄於產品目錄（ADR-08）")


# ---------------------------------------------------------------------------
# error_codes.yaml
# ---------------------------------------------------------------------------


def validate_error_codes_doc(doc: object) -> list[str]:
    """驗證 `error_codes.yaml` 結構（計畫 T1 規則 1～4、5～11 與 passthrough）。"""
    p = _Problems(FILE_ERROR_CODES)
    _check_header(p, doc, _EC_TOP_KEYS, namespace="error_code")
    if not isinstance(doc, dict):
        return p.items
    if "codes" in doc:
        if isinstance(doc["codes"], list):
            _check_codes(p, doc["codes"])
        else:
            p.add("codes", f"必須為 list，實際為 {_type_name(doc['codes'])}")
    if "passthrough" in doc:
        if isinstance(doc["passthrough"], list):
            _check_passthrough(p, doc["passthrough"])
        else:
            p.add("passthrough", f"必須為 list，實際為 {_type_name(doc['passthrough'])}")
    return p.items


def _check_codes(p: _Problems, codes: list[Any]) -> None:
    seen: set[str] = set()
    high: str | None = None
    for i, entry in enumerate(codes):
        loc = f"codes[{i}]({_label(entry, 'code', i)})"
        if not _require_mapping(p, loc, entry):
            continue
        _check_closed(p, loc, entry, _EC_REQUIRED, _EC_OPTIONAL)
        code = entry.get("code")
        if "code" in entry and _check_str(p, f"{loc}.code", code):
            _check_code_name(p, f"{loc}.code", code)
            if code in seen:
                p.add(f"{loc}.code", f"code 重複（與先前項目相同）: {code}")
            elif high is not None and code < high:
                p.add(f"{loc}.code", f"codes 未依 code 升冪排序（{code} 位於 {high} 之後）")
            seen.add(code)
            if high is None or code > high:
                high = code
        _check_code_entry(p, loc, entry)


def _check_code_entry(p: _Problems, loc: str, e: dict[Any, Any]) -> None:
    rc = e.get("reachable")
    if "reachable" in e:
        _check_bool(p, f"{loc}.reachable", rc)
    # reachable 為 true 時 unreachable_reason 可缺省或為 null（與 dataclass 欄位 str | None 一致）；
    # reachable 為 false 時仍須為非空 str（null 或缺省皆拒絕）。
    if e.get("unreachable_reason") is not None:
        _check_str(p, f"{loc}.unreachable_reason", e["unreachable_reason"])
    elif rc is False:
        p.add(f"{loc}.unreachable_reason", "reachable 為 false 時必須提供非空 unreachable_reason")
    if "status" in e:
        _check_enum(p, f"{loc}.status", e["status"], CODE_STATUS)
    if "scope" in e:
        _check_scope(p, f"{loc}.scope", e["scope"], methods_nonempty=False)
    for key in ("meaning", "side_effect", "agent_action"):
        if key in e:
            _check_text(p, f"{loc}.{key}", e[key])
    if "retryable" in e:
        _check_enum(p, f"{loc}.retryable", e["retryable"], RETRYABLE)
    if "uart_tx" in e:
        _check_enum(p, f"{loc}.uart_tx", e["uart_tx"], UART_TX)
    if "ok_response" in e:
        _check_bool(p, f"{loc}.ok_response", e["ok_response"])
    if "extra_fields" in e:
        _check_str_list(p, f"{loc}.extra_fields", e["extra_fields"])
    if "constant" in e and _check_str(p, f"{loc}.constant", e["constant"]):
        if _CONSTANT_RE.fullmatch(e["constant"]) is None:
            p.add(f"{loc}.constant", f"不符合 ^[A-Z_][A-Z0-9_]*$: {e['constant']!r}")
    if "sources" in e:
        _check_str_list(p, f"{loc}.sources", e["sources"])
    if "contract_tests" in e:
        _check_str_list(p, f"{loc}.contract_tests", e["contract_tests"],
                        pattern=_CONTRACT_TEST_RE, pattern_desc=r"^[^\s:]+\.py(::...)*$")
    if "behavior_change" in e:
        _check_str_list(p, f"{loc}.behavior_change", e["behavior_change"],
                        pattern=_BC_ID_RE, pattern_desc=r"^(BC|SC|DF)-\d{2}$")
    if "exit_codes" in e:
        _check_exit_codes(p, f"{loc}.exit_codes", e["exit_codes"])
    exit_codes = e.get("exit_codes")
    if "cli_local" in _scope_interfaces(e.get("scope")) and not (isinstance(exit_codes, dict) and exit_codes):
        p.add(f"{loc}.exit_codes", "scope.interfaces 含 cli_local 時 exit_codes 必須為非空 mapping")
    _check_code_status_rules(p, loc, e)


def _check_exit_codes(p: _Problems, loc: str, value: object) -> None:
    if not isinstance(value, dict):
        p.add(loc, f"必須為 mapping[str,int]，實際為 {_type_name(value)}")
        return
    for key, code in value.items():
        key_loc = f"{loc}[{key!r}]"
        if not (isinstance(key, str) and key in CLI_CONTEXTS):
            p.add(key_loc, f"鍵必須屬 CLI_CONTEXTS 封閉集合: {key!r}")
        if not _is_int(code):
            p.add(key_loc, f"值必須為整數，實際為 {code!r}")


def _check_code_status_rules(p: _Problems, loc: str, e: dict[Any, Any]) -> None:
    """狀態與可達性、變更參照、契約測試的一致性（計畫 T1 規則 10）。"""
    status = e.get("status")
    rc = e.get("reachable")
    if status == "APPROVED":
        if rc is not False:
            p.add(f"{loc}.reachable", "status 為 APPROVED（已核准未實作）時 reachable 必須為 false")
        bc = e.get("behavior_change")
        if not (isinstance(bc, list) and bc):
            p.add(f"{loc}.behavior_change", "status 為 APPROVED 時 behavior_change 不得為空")
    elif status in ("IMPLEMENTED", "VERIFIED"):
        if rc is not True:
            p.add(f"{loc}.reachable", f"status 為 {status} 時 reachable 必須為 true")
        ct = e.get("contract_tests")
        if not (isinstance(ct, list) and ct):
            p.add(f"{loc}.contract_tests", f"status 為 {status} 時 contract_tests 不得為空")


def _check_passthrough(p: _Problems, items: list[Any]) -> None:
    seen: set[str] = set()
    for i, pt in enumerate(items):
        loc = f"passthrough[{i}]({_label(pt, 'id', i)})"
        if not _require_mapping(p, loc, pt):
            continue
        _check_closed(p, loc, pt, _PT_REQUIRED)
        if "id" in pt and _check_str(p, f"{loc}.id", pt["id"]):
            pid = pt["id"]
            if _PASSTHROUGH_ID_RE.fullmatch(pid) is None:
                p.add(f"{loc}.id", f"不符合 ^[a-z][a-z0-9_]*$: {pid!r}")
            elif pid in seen:
                p.add(f"{loc}.id", f"id 重複: {pid}")
            seen.add(pid)
        if "namespace_ref" in pt and _check_str(p, f"{loc}.namespace_ref", pt["namespace_ref"]):
            if pt["namespace_ref"] != _PASSTHROUGH_NAMESPACE_REF:
                p.add(f"{loc}.namespace_ref", f"必須為 {_PASSTHROUGH_NAMESPACE_REF}，實際為 {pt['namespace_ref']!r}")
        if "scope" in pt:
            _check_scope(p, f"{loc}.scope", pt["scope"], methods_nonempty=True)
        if "rule" in pt:
            _check_text(p, f"{loc}.rule", pt["rule"])
        if "owner_confirmation" in pt:
            _check_enum(p, f"{loc}.owner_confirmation", pt["owner_confirmation"], _OWNER_CONFIRMATIONS)
        if "reconciliation_refs" in pt:
            _check_str_list(p, f"{loc}.reconciliation_refs", pt["reconciliation_refs"])


# ---------------------------------------------------------------------------
# session_last_errors.yaml
# ---------------------------------------------------------------------------


def validate_session_last_errors_doc(doc: object) -> list[str]:
    """驗證 `session_last_errors.yaml` 結構與終態／重探／提升路徑的一致性（計畫 T1 規則 5、6）。"""
    p = _Problems(FILE_SESSION_LAST_ERRORS)
    _check_header(p, doc, _LE_TOP_KEYS, namespace="session_last_error")
    if not isinstance(doc, dict):
        return p.items
    if "values" in doc:
        if isinstance(doc["values"], list):
            _check_last_errors(p, doc["values"])
        else:
            p.add("values", f"必須為 list，實際為 {_type_name(doc['values'])}")
    return p.items


def _check_last_errors(p: _Problems, values: list[Any]) -> None:
    seen: set[str] = set()
    high: str | None = None
    for i, e in enumerate(values):
        loc = f"values[{i}]({_label(e, 'value', i)})"
        if not _require_mapping(p, loc, e):
            continue
        _check_closed(p, loc, e, _LE_REQUIRED, _LE_OPTIONAL)
        kind = e.get("kind")
        if "kind" in e:
            _check_enum(p, f"{loc}.kind", kind, _LAST_ERROR_KINDS)
        if "value" in e and _check_str(p, f"{loc}.value", e["value"]):
            value = e["value"]
            if kind == "dynamic_prefix" and _DYNAMIC_VALUE_RE.fullmatch(value) is None:
                p.add(f"{loc}.value", f"dynamic_prefix 之 value 必須符合 ^[A-Z][A-Z0-9_]*:\\*$: {value!r}")
            elif kind == "named" and _CODE_RE.fullmatch(value) is None:
                p.add(f"{loc}.value", f"named 之 value 必須符合 ^[A-Z][A-Z0-9_]*[A-Z0-9]$: {value!r}")
            if value in seen:
                p.add(f"{loc}.value", f"value 重複: {value}")
            elif high is not None and value < high:
                p.add(f"{loc}.value", f"values 未依 value 升冪排序（{value} 位於 {high} 之後）")
            seen.add(value)
            if high is None or value > high:
                high = value
        if "reason_forms" in e:
            _check_str_list(p, f"{loc}.reason_forms", e["reason_forms"])
            if kind == "dynamic_prefix" and not (isinstance(e["reason_forms"], list) and e["reason_forms"]):
                p.add(f"{loc}.reason_forms", "dynamic_prefix 之 reason_forms 不得為空")
        rc = e.get("reachable")
        if "reachable" in e:
            _check_bool(p, f"{loc}.reachable", rc)
        # 與 _check_code_entry 相同：reachable 為 true 時 null／缺省皆可，false 時須為非空 str。
        if e.get("unreachable_reason") is not None:
            _check_str(p, f"{loc}.unreachable_reason", e["unreachable_reason"])
        elif rc is False:
            p.add(f"{loc}.unreachable_reason", "reachable 為 false 時必須提供非空 unreachable_reason")
        for key in ("meaning", "agent_action", "manual_recovery", "logging", "detail"):
            if key in e:
                _check_text(p, f"{loc}.{key}", e[key])
        if "terminal" in e:
            _check_bool(p, f"{loc}.terminal", e["terminal"])
        if "auto_reprobe" in e:
            auto = e["auto_reprobe"]
            if isinstance(auto, dict) and set(auto) == _AUTO_REPROBE_KEYS:
                _check_bool(p, f"{loc}.auto_reprobe.attached", auto["attached"])
                _check_bool(p, f"{loc}.auto_reprobe.detached", auto["detached"])
            else:
                p.add(f"{loc}.auto_reprobe", "必須為 mapping，且僅含 attached／detached 兩個布林鍵")
        if "states" in e:
            _check_str_list(p, f"{loc}.states", e["states"], nonempty=True, allowed=SESSION_STATES)
        if "set_by" in e:
            _check_str_list(p, f"{loc}.set_by", e["set_by"], nonempty=True)
        if "cleared_by" in e:
            _check_str_list(p, f"{loc}.cleared_by", e["cleared_by"], nonempty=True,
                            pattern=_CLEARED_BY_RE, pattern_desc="^(auto|operator):.+")
        if "promoted_via" in e:
            _check_str_list(p, f"{loc}.promoted_via", e["promoted_via"], allowed=_PROMOTED_VIA)
        if "error_code_namespace" in e:
            _check_enum(p, f"{loc}.error_code_namespace", e["error_code_namespace"], _ERROR_CODE_NAMESPACES)
        if "contract_tests" in e:
            _check_str_list(p, f"{loc}.contract_tests", e["contract_tests"],
                            pattern=_CONTRACT_TEST_RE, pattern_desc=r"^[^\s:]+\.py(::...)*$")
        _check_last_error_consistency(p, loc, e)


def _check_last_error_consistency(p: _Problems, loc: str, e: dict[Any, Any]) -> None:
    terminal = e.get("terminal")
    auto = e.get("auto_reprobe")
    attached = auto.get("attached") if isinstance(auto, dict) else None
    detached = auto.get("detached") if isinstance(auto, dict) else None
    cleared = e.get("cleared_by") if isinstance(e.get("cleared_by"), list) else []
    has_auto_clear = any(isinstance(x, str) and x.startswith("auto:") for x in cleared)
    any_reprobe = attached is True or detached is True
    if terminal is True:
        if any_reprobe:
            p.add(f"{loc}.terminal", "terminal 為 true 時 auto_reprobe.attached 與 detached 必須皆為 false（不自動重探）")
        if has_auto_clear:
            p.add(f"{loc}.cleared_by", "terminal 為 true 時 cleared_by 不得含 auto: 清除來源（只能由操作者解除）")
    elif terminal is False and not (has_auto_clear or any_reprobe):
        p.add(f"{loc}.terminal", "terminal 為 false 時須至少有一筆 auto: 清除來源或 auto_reprobe 任一為 true")
    promoted = e.get("promoted_via")
    states = e.get("states") if isinstance(e.get("states"), list) else []
    if isinstance(promoted, list) and promoted and "ATTACHED" not in states:
        p.add(f"{loc}.promoted_via",
              "promoted_via 非空時 states 必須含 ATTACHED（提升路徑只在 bridge 存在的 ATTACHED 態可達）")


# ---------------------------------------------------------------------------
# behavior_changes.yaml
# ---------------------------------------------------------------------------


def validate_behavior_changes_doc(doc: object) -> list[str]:
    """驗證 `behavior_changes.yaml`（BC／SC／DF 登記、待確認項目、安全不變量）結構。"""
    p = _Problems(FILE_BEHAVIOR_CHANGES)
    file_rev = _check_header(p, doc, _BC_TOP_KEYS, namespace=None)
    if not isinstance(doc, dict):
        return p.items
    changes_raw = doc.get("changes")
    if "changes" in doc and not isinstance(changes_raw, list):
        p.add("changes", f"必須為 list，實際為 {_type_name(changes_raw)}")
    changes: list[Any] = changes_raw if isinstance(changes_raw, list) else []
    dict_changes = [c for c in changes if isinstance(c, dict)]
    change_ids = {c["id"] for c in dict_changes if isinstance(c.get("id"), str)}
    all_case_ids: set[str] = set()
    for c in dict_changes:
        cids = c.get("case_ids")
        if isinstance(cids, list):
            all_case_ids.update(x for x in cids if isinstance(x, str))
    _check_changes(p, changes, file_rev)
    _check_pending(p, doc.get("pending_owner_confirmations"), change_ids)
    _check_invariants(p, doc.get("safety_invariants"), all_case_ids)
    return p.items


def _check_changes(p: _Problems, changes: list[Any], file_rev: int | None) -> None:
    ids_seen: set[str] = set()
    case_seen: set[str] = set()
    for i, raw in enumerate(changes):
        loc = f"changes[{i}]({_label(raw, 'id', i)})"
        if not _require_mapping(p, loc, raw):
            continue
        _check_closed(p, loc, raw, _BC_REQUIRED)
        cid = raw.get("id")
        kind = raw.get("kind")
        if "id" in raw and _check_str(p, f"{loc}.id", cid):
            if _BC_ID_RE.fullmatch(cid) is None:
                p.add(f"{loc}.id", f"不符合 ^(BC|SC|DF)-\\d{{2}}$: {cid!r}")
            elif cid in ids_seen:
                p.add(f"{loc}.id", f"id 重複: {cid}")
            ids_seen.add(cid)
        if "kind" in raw:
            _check_enum(p, f"{loc}.kind", kind, _CHANGE_KINDS)
        if isinstance(cid, str) and isinstance(kind, str) and cid.split("-", 1)[0] != kind:
            p.add(f"{loc}.kind", f"kind 必須與 id 前綴相同（id={cid}，kind={kind}）")
        if "title" in raw:
            _check_str(p, f"{loc}.title", raw["title"])
        if "status" in raw:
            _check_enum(p, f"{loc}.status", raw["status"], CHANGE_STATUS)
        if "approval" in raw:
            _check_approval(p, f"{loc}.approval", raw["approval"], kind)
        oc = raw.get("owner_confirmation")
        oi = raw.get("owner_item")
        dip = raw.get("default_if_pending")
        if "owner_confirmation" in raw:
            _check_enum(p, f"{loc}.owner_confirmation", oc, _OWNER_CONFIRMATIONS)
        if "owner_item" in raw and oi is not None and not _in_owner_item(oi):
            p.add(f"{loc}.owner_item", f"必須為 null 或 ≥1 的整數，實際為 {oi!r}")
        if "default_if_pending" in raw and dip is not None:
            _check_str(p, f"{loc}.default_if_pending", dip)
        if oc == "pending_default":
            if not _in_owner_item(oi):
                p.add(f"{loc}.owner_item", "owner_confirmation 為 pending_default 時 owner_item 必須為 ≥1 的整數")
            if not (isinstance(dip, str) and dip.strip()):
                p.add(f"{loc}.default_if_pending", "owner_confirmation 為 pending_default 時 default_if_pending 必須為非空字串")
        scope = raw.get("scope")
        if "applies_to" in raw:
            _check_applies_to(p, loc, raw["applies_to"], scope)
        if "approved_at_revision" in raw:
            ar = raw["approved_at_revision"]
            if not _is_int(ar) or ar < 1:
                p.add(f"{loc}.approved_at_revision", f"必須為 ≥1 的整數（catalog_revision），實際為 {ar!r}")
            elif file_rev is not None and ar > file_rev:
                p.add(f"{loc}.approved_at_revision", f"不得大於檔案 revision（{file_rev}）")
        if "spec_refs" in raw:
            _check_str_list(p, f"{loc}.spec_refs", raw["spec_refs"], nonempty=True)
        if "reconciliation_refs" in raw:
            _check_str_list(p, f"{loc}.reconciliation_refs", raw["reconciliation_refs"])
        scope_ok = "scope" in raw and _check_enum(p, f"{loc}.scope", scope, CHANGE_SCOPES)
        ab = raw.get("ab_participation")
        ab_ok = "ab_participation" in raw and _check_enum(p, f"{loc}.ab_participation", ab, AB_PARTICIPATION)
        if scope_ok and ab_ok and (scope == "both") != (ab == "diff"):
            p.add(f"{loc}.ab_participation",
                  "scope 為 both 時 ab_participation 必須為 diff，其餘 scope 必須為 absolute_only")
        if "phases" in raw:
            _check_str_list(p, f"{loc}.phases", raw["phases"], nonempty=True, allowed=_PHASES)
        if "case_ids" in raw:
            cids = raw["case_ids"]
            if _check_str_list(p, f"{loc}.case_ids", cids, nonempty=True,
                               pattern=_CASE_ID_RE, pattern_desc="^[a-z0-9][a-z0-9-]*$"):
                for j, case_id in enumerate(cids):
                    if case_id in case_seen:
                        p.add(f"{loc}.case_ids[{j}]", f"case_ids 全檔重複: {case_id}")
                    case_seen.add(case_id)
        for key in ("compatibility_impact", "approval_conditions"):
            if key in raw:
                _check_str(p, f"{loc}.{key}", raw[key])
        if "related_decisions" in raw:
            _check_str_list(p, f"{loc}.related_decisions", raw["related_decisions"])


def _check_approval(p: _Problems, loc: str, approval: object, kind: object) -> None:
    if not _require_mapping(p, loc, approval):
        return
    assert isinstance(approval, dict)
    _check_closed(p, loc, approval, _APPROVAL_KEYS)
    owner = approval.get("owner")
    if "owner" in approval:
        _check_str(p, f"{loc}.owner", owner)
    if "date" in approval:
        date = approval["date"]
        if _check_str(p, f"{loc}.date", date) and _DATE_RE.fullmatch(date) is None:
            p.add(f"{loc}.date", f"必須符合 YYYY-MM-DD: {date!r}")
    decision_ref = approval.get("decision_ref")
    if "decision_ref" in approval:
        _check_str(p, f"{loc}.decision_ref", decision_ref)
    basis = approval.get("basis")
    if "basis" in approval:
        _check_enum(p, f"{loc}.basis", basis, _APPROVAL_BASES)
    if isinstance(basis, str) and basis in _APPROVAL_BASES and isinstance(owner, str):
        expected = APPROVAL_OWNER_BY_BASIS[basis]
        if owner != expected:
            p.add(f"{loc}.owner", f"owner 必須為 {expected}（basis={basis}）；SC／DF 不得冒用擁有者名義")
    if kind == "BC":
        if basis != "owner_decision":
            p.add(f"{loc}.basis", "kind 為 BC 時 basis 必須為 owner_decision")
        if decision_ref != "OD-04":
            p.add(f"{loc}.decision_ref", "kind 為 BC 時 decision_ref 必須為 OD-04")
    elif kind == "DF" and basis != "reconciliation":
        p.add(f"{loc}.basis", "kind 為 DF 時 basis 必須為 reconciliation")


def _check_applies_to(p: _Problems, loc: str, at: object, scope: object) -> None:
    aloc = f"{loc}.applies_to"
    if not _require_mapping(p, aloc, at):
        return
    assert isinstance(at, dict)
    _check_closed(p, aloc, at, _APPLIES_TO_KEYS)
    py = at.get("python")
    if "python" in at and py is not None:
        ploc = f"{aloc}.python"
        if _require_mapping(p, ploc, py):
            assert isinstance(py, dict)
            _check_closed(p, ploc, py, {"min_version"})
            if "min_version" in py and _check_str(p, f"{ploc}.min_version", py["min_version"]):
                if _SEMVER_RE.fullmatch(py["min_version"]) is None:
                    p.add(f"{ploc}.min_version", f"必須符合 ^\\d+\\.\\d+\\.\\d+$: {py['min_version']!r}")
    rs = at.get("rust")
    if "rust" in at and rs is not None:
        rloc = f"{aloc}.rust"
        if _require_mapping(p, rloc, rs):
            assert isinstance(rs, dict)
            _check_closed(p, rloc, rs, {"stage"})
            if "stage" in rs:
                _check_enum(p, f"{rloc}.stage", rs["stage"], _RUST_STAGES)
    if scope == "rust_only" and py is not None:
        p.add(f"{aloc}.python", "scope 為 rust_only 時 applies_to.python 必須為 null")
    if scope == "python_when_manifest_present" and py is None:
        p.add(f"{aloc}.python", "scope 為 python_when_manifest_present 時 applies_to.python 不得為 null")


def _check_pending(p: _Problems, raw: object, change_ids: set[str]) -> None:
    if not isinstance(raw, list):
        p.add("pending_owner_confirmations", f"必須為 list，實際為 {_type_name(raw)}")
        return
    items_seen: set[int] = set()
    for i, o in enumerate(raw):
        loc = f"pending_owner_confirmations[{i}]"
        if not _require_mapping(p, loc, o):
            continue
        assert isinstance(o, dict)
        _check_closed(p, loc, o, _PENDING_KEYS)
        if "item" in o:
            item = o["item"]
            if not _in_owner_item(item):
                p.add(f"{loc}.item", f"必須為 ≥1 的整數，實際為 {item!r}")
            elif item in items_seen:
                p.add(f"{loc}.item", f"item 重複: {item}")
            else:
                items_seen.add(item)
        if "topic" in o:
            _check_str(p, f"{loc}.topic", o["topic"])
        if "refs" in o:
            _check_str_list(p, f"{loc}.refs", o["refs"], nonempty=True)
        if "default" in o:
            _check_str(p, f"{loc}.default", o["default"])
        if "affects" in o and _check_str_list(p, f"{loc}.affects", o["affects"]):
            for j, ref in enumerate(o["affects"]):
                if ref in change_ids:
                    continue
                if _AFFECT_PASSTHROUGH_RE.fullmatch(ref) or _AFFECT_LAST_ERROR_RE.fullmatch(ref):
                    continue
                p.add(f"{loc}.affects[{j}]",
                      f"未知的 affects 參照 {ref!r}（須為 behavior change id、"
                      "error_codes:passthrough:<id> 或 session_last_errors:<value>）")


def _check_invariants(p: _Problems, raw: object, all_case_ids: set[str]) -> None:
    if not isinstance(raw, list):
        p.add("safety_invariants", f"必須為 list，實際為 {_type_name(raw)}")
        return
    ids: list[str] = []
    for i, s in enumerate(raw):
        loc = f"safety_invariants[{i}]({_label(s, 'id', i)})"
        if not _require_mapping(p, loc, s):
            continue
        assert isinstance(s, dict)
        _check_closed(p, loc, s, _INVARIANT_KEYS)
        if "id" in s and _check_str(p, f"{loc}.id", s["id"]):
            ids.append(s["id"])
        if "title" in s:
            _check_str(p, f"{loc}.title", s["title"])
        if "spec_ref" in s and _check_str(p, f"{loc}.spec_ref", s["spec_ref"]):
            if s["spec_ref"] != _SAFETY_SPEC_REF:
                p.add(f"{loc}.spec_ref", f"必須為 {_SAFETY_SPEC_REF}，實際為 {s['spec_ref']!r}")
        if "error_codes" in s:
            _check_str_list(p, f"{loc}.error_codes", s["error_codes"])
        if "case_ids" in s and _check_str_list(p, f"{loc}.case_ids", s["case_ids"],
                                               pattern=_CASE_ID_RE, pattern_desc="^[a-z0-9][a-z0-9-]*$"):
            for j, case_id in enumerate(s["case_ids"]):
                if case_id not in all_case_ids:
                    p.add(f"{loc}.case_ids[{j}]", f"case_ids 必須為本檔某 change 的 case_id: {case_id!r}")
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        p.add("safety_invariants", f"id 重複: {dupes}")
    expected = set(SAFETY_INVARIANTS)
    got = set(ids)
    if got != expected:
        missing = sorted(expected - got)
        extra = sorted(got - expected)
        p.add("safety_invariants",
              f"id 集合必須恰為 {list(SAFETY_INVARIANTS)}（缺少 {missing}，多出 {extra}）")


# ---------------------------------------------------------------------------
# diagnostic_aux.yaml
# ---------------------------------------------------------------------------


def validate_aux_doc(doc: object) -> list[str]:
    """驗證 `diagnostic_aux.yaml`：六個 list 鍵、值唯一、classification 動作必須可於 recommended_actions 找到。"""
    p = _Problems(FILE_DIAGNOSTIC_AUX)
    _check_header(p, doc, _AUX_TOP_KEYS, namespace=None)
    if not isinstance(doc, dict):
        return p.items
    recommended_raw = doc.get("recommended_actions")
    recommended_values: set[str] = set()
    if isinstance(recommended_raw, list):
        recommended_values = {
            r["value"] for r in recommended_raw if isinstance(r, dict) and isinstance(r.get("value"), str)
        }
    for name, fields in _AUX_LISTS.items():
        if name not in doc:
            continue
        lst = doc[name]
        if not isinstance(lst, list):
            p.add(name, f"必須為 list，實際為 {_type_name(lst)}")
            continue
        key_field = fields[0]
        seen: set[str] = set()
        for i, item in enumerate(lst):
            loc = f"{name}[{i}]({_label(item, key_field, i)})"
            if not _require_mapping(p, loc, item):
                continue
            assert isinstance(item, dict)
            _check_closed(p, loc, item, frozenset(fields))
            if key_field in item and _check_str(p, f"{loc}.{key_field}", item[key_field]):
                key = item[key_field]
                if key in seen:
                    p.add(f"{loc}.{key_field}", f"值重複: {key}")
                seen.add(key)
            if "meaning" in item:
                _check_str(p, f"{loc}.meaning", item["meaning"])
            if name == "error_details" and "parent_code" in item:
                _check_str(p, f"{loc}.parent_code", item["parent_code"])
            if name == "classifications" and "recommended_actions" in item:
                acts = item["recommended_actions"]
                if _check_str_list(p, f"{loc}.recommended_actions", acts, nonempty=True):
                    for j, act in enumerate(acts):
                        if act not in recommended_values:
                            p.add(f"{loc}.recommended_actions[{j}]",
                                  f"未在 recommended_actions 中: {act!r}")
    return p.items


_VALIDATORS: Final[Mapping[str, Any]] = types.MappingProxyType({
    FILE_ERROR_CODES: validate_error_codes_doc,
    FILE_SESSION_LAST_ERRORS: validate_session_last_errors_doc,
    FILE_BEHAVIOR_CHANGES: validate_behavior_changes_doc,
    FILE_DIAGNOSTIC_AUX: validate_aux_doc,
})


# ---------------------------------------------------------------------------
# YAML 嚴格載入（重複鍵、語法錯誤、非 UTF-8 一律轉 CatalogError）
# ---------------------------------------------------------------------------


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader 子類別：偵測同一 mapping 內的重複鍵（PyYAML 預設靜默保留最後一個）。"""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.duplicate_keys: list[tuple[str, int]] = []

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:  # type: ignore[override]
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)
            seen: set[Any] = set()
            for key_node, _value_node in node.value:
                key = self.construct_object(key_node, deep=True)
                try:
                    hash(key)
                except TypeError:
                    continue
                if key in seen:
                    self.duplicate_keys.append((str(key), key_node.start_mark.line + 1))
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


def load_yaml_strict(path: Path) -> object:
    """以嚴格模式載入 YAML：重複鍵、語法錯誤、禁止字元、過深巢狀與非 UTF-8 皆轉成 CatalogError。"""
    path = Path(path)
    name = path.name
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CatalogError([f"{name}:(位元組 {exc.start}): 非 UTF-8 編碼"]) from None
    except OSError as exc:
        raise CatalogError([f"{name}:(檔案): 無法讀取（{exc.strerror}）"]) from None
    # 建構子同樣可能拋出例外（如含 NUL 等禁止字元時的 ReaderError），因此納入同一個 try。
    loader: _StrictLoader | None = None
    try:
        loader = _StrictLoader(text)
        doc = loader.get_single_data()
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        mark = getattr(exc, "problem_mark", None)
        where = str(mark.line + 1) if mark is not None else "(未知行)"
        raise CatalogError([f"{name}:{where}: YAML 語法或型別錯誤: {exc}"]) from None
    finally:
        if loader is not None:
            loader.dispose()
    if loader.duplicate_keys:
        raise CatalogError([
            f"{name}:{line}: 重複鍵 {key!r}（同一 mapping 內，PyYAML 會靜默保留最後一個）"
            for key, line in loader.duplicate_keys
        ])
    return doc


# ---------------------------------------------------------------------------
# 建構資料物件（只讀已通過驗證的文件）
# ---------------------------------------------------------------------------


def _checked_doc(repo_root: Path, filename: str) -> Any:
    path = Path(repo_root) / CONTRACTS_DIR / filename
    if not path.is_file():
        raise CatalogError([f"{filename}:(檔案): 契約檔不存在（預期 {CONTRACTS_DIR}/{filename}）"])
    doc = load_yaml_strict(path)
    problems = _VALIDATORS[filename](doc)
    if problems:
        raise CatalogError(problems)
    return doc


def _build_scope(raw: Mapping[str, Any]) -> Scope:
    return Scope(
        interfaces=tuple(raw["interfaces"]),
        methods=tuple(raw["methods"]),
        platforms=tuple(raw["platforms"]),
    )


def _build_error_codes(doc: Mapping[str, Any]) -> tuple[tuple[ErrorCodeEntry, ...], tuple[PassthroughEntry, ...]]:
    codes = tuple(
        ErrorCodeEntry(
            code=e["code"],
            status=e["status"],
            reachable=e["reachable"],
            unreachable_reason=e.get("unreachable_reason"),
            constant=e.get("constant"),
            scope=_build_scope(e["scope"]),
            meaning=e["meaning"],
            side_effect=e["side_effect"],
            retryable=e["retryable"],
            uart_tx=e["uart_tx"],
            ok_response=e.get("ok_response", False),
            agent_action=e["agent_action"],
            extra_fields=tuple(e["extra_fields"]),
            exit_codes=types.MappingProxyType(dict(e.get("exit_codes") or {})),
            contract_tests=tuple(e["contract_tests"]),
            behavior_change=tuple(e["behavior_change"]),
            sources=tuple(e.get("sources", [])),
        )
        for e in doc["codes"]
    )
    passthrough = tuple(
        PassthroughEntry(
            id=pt["id"],
            namespace_ref=pt["namespace_ref"],
            scope=_build_scope(pt["scope"]),
            rule=pt["rule"],
            owner_confirmation=pt["owner_confirmation"],
            reconciliation_refs=tuple(pt["reconciliation_refs"]),
        )
        for pt in doc["passthrough"]
    )
    return codes, passthrough


def _build_last_errors(doc: Mapping[str, Any]) -> tuple[LastErrorEntry, ...]:
    return tuple(
        LastErrorEntry(
            value=v["value"],
            kind=v["kind"],
            reason_forms=tuple(v["reason_forms"]),
            reachable=v["reachable"],
            unreachable_reason=v.get("unreachable_reason"),
            meaning=v["meaning"],
            agent_action=v["agent_action"],
            terminal=v["terminal"],
            auto_reprobe_attached=v["auto_reprobe"]["attached"],
            auto_reprobe_detached=v["auto_reprobe"]["detached"],
            manual_recovery=v["manual_recovery"],
            logging=v["logging"],
            detail=v["detail"],
            states=tuple(v["states"]),
            set_by=tuple(v["set_by"]),
            cleared_by=tuple(v["cleared_by"]),
            promoted_via=tuple(v["promoted_via"]),
            error_code_namespace=v["error_code_namespace"],
            contract_tests=tuple(v["contract_tests"]),
        )
        for v in doc["values"]
    )


def _build_behavior_changes(
    doc: Mapping[str, Any],
) -> tuple[tuple[BehaviorChange, ...], tuple[OwnerConfirmation, ...], tuple[SafetyInvariant, ...]]:
    changes = []
    for c in doc["changes"]:
        py = c["applies_to"]["python"]
        rs = c["applies_to"]["rust"]
        a = c["approval"]
        changes.append(BehaviorChange(
            id=c["id"],
            kind=c["kind"],
            title=c["title"],
            status=c["status"],
            approval=Approval(owner=a["owner"], date=a["date"], decision_ref=a["decision_ref"], basis=a["basis"]),
            owner_confirmation=c["owner_confirmation"],
            owner_item=c["owner_item"],
            default_if_pending=c["default_if_pending"],
            applies_python_min=None if py is None else py["min_version"],
            applies_rust_stage=None if rs is None else rs["stage"],
            approved_at_revision=c["approved_at_revision"],
            spec_refs=tuple(c["spec_refs"]),
            reconciliation_refs=tuple(c["reconciliation_refs"]),
            scope=c["scope"],
            ab_participation=c["ab_participation"],
            phases=tuple(c["phases"]),
            case_ids=tuple(c["case_ids"]),
            compatibility_impact=c["compatibility_impact"],
            approval_conditions=c["approval_conditions"],
            related_decisions=tuple(c["related_decisions"]),
        ))
    confirmations = tuple(
        OwnerConfirmation(
            item=o["item"],
            topic=o["topic"],
            refs=tuple(o["refs"]),
            default=o["default"],
            affects=tuple(o["affects"]),
        )
        for o in doc["pending_owner_confirmations"]
    )
    invariants = tuple(
        SafetyInvariant(
            id=s["id"],
            title=s["title"],
            spec_ref=s["spec_ref"],
            error_codes=tuple(s["error_codes"]),
            case_ids=tuple(s["case_ids"]),
        )
        for s in doc["safety_invariants"]
    )
    return tuple(changes), confirmations, invariants


def _build_aux(doc: Mapping[str, Any]) -> AuxCatalog:
    return AuxCatalog(
        warning_codes=tuple((w["code"], w["meaning"]) for w in doc["warning_codes"]),
        error_details=tuple((d["value"], d["parent_code"], d["meaning"]) for d in doc["error_details"]),
        classifications=tuple(
            (c["value"], tuple(c["recommended_actions"]), c["meaning"]) for c in doc["classifications"]
        ),
        recommended_actions=tuple((r["value"], r["meaning"]) for r in doc["recommended_actions"]),
        recovery_actions=tuple((r["value"], r["meaning"]) for r in doc["recovery_actions"]),
        recover_actions=tuple((r["value"], r["meaning"]) for r in doc["recover_actions"]),
    )


# ---------------------------------------------------------------------------
# 公開載入函式
# ---------------------------------------------------------------------------


def load_error_codes(repo_root: Path) -> tuple[tuple[ErrorCodeEntry, ...], tuple[PassthroughEntry, ...]]:
    """只讀 `error_codes.yaml`；回傳 (codes, passthrough)。"""
    return _build_error_codes(_checked_doc(repo_root, FILE_ERROR_CODES))


def load_session_last_errors(repo_root: Path) -> tuple[LastErrorEntry, ...]:
    """只讀 `session_last_errors.yaml`。"""
    return _build_last_errors(_checked_doc(repo_root, FILE_SESSION_LAST_ERRORS))


def load_behavior_changes(
    repo_root: Path,
) -> tuple[tuple[BehaviorChange, ...], tuple[OwnerConfirmation, ...], tuple[SafetyInvariant, ...]]:
    """只讀 `behavior_changes.yaml`；回傳 (changes, owner_confirmations, safety_invariants)。"""
    return _build_behavior_changes(_checked_doc(repo_root, FILE_BEHAVIOR_CHANGES))


def load_aux(repo_root: Path) -> AuxCatalog:
    """只讀 `diagnostic_aux.yaml`。"""
    return _build_aux(_checked_doc(repo_root, FILE_DIAGNOSTIC_AUX))


def load_catalogs(repo_root: Path) -> Catalogs:
    """組合四個目錄檔；任一檔缺漏或有問題即 raise CatalogError，彙整全部檔案的問題。"""
    docs: dict[str, Any] = {}
    problems: list[str] = []
    for filename in CATALOG_FILES:
        try:
            docs[filename] = _checked_doc(repo_root, filename)
        except CatalogError as exc:
            problems.extend(exc.problems)
    if problems:
        raise CatalogError(problems)
    codes, passthrough = _build_error_codes(docs[FILE_ERROR_CODES])
    changes, confirmations, invariants = _build_behavior_changes(docs[FILE_BEHAVIOR_CHANGES])
    return Catalogs(
        error_codes=codes,
        passthrough=passthrough,
        last_errors=_build_last_errors(docs[FILE_SESSION_LAST_ERRORS]),
        behavior_changes=changes,
        owner_confirmations=confirmations,
        safety_invariants=invariants,
        aux=_build_aux(docs[FILE_DIAGNOSTIC_AUX]),
        revisions=types.MappingProxyType({name: docs[name]["revision"] for name in CATALOG_FILES}),
    )
