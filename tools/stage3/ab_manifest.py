"""載入、驗證並核對 Sequential A/B 執行所需的凍結 manifest。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from string import Formatter
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from tools.stage3 import required_cases as required_cases_module
from tools.stage3.jsonio import dumps_compact
from tools.stage3.proc import Runner, run_cmd
from tools.stage3.reason_codes import HarnessError

LAYER_ADAPTER: Mapping[str, str] = {"l1.5": "l15_functest", "l2": "l2_regression", "l3": "l3_realhw"}
TEMPLATE_KEYS = frozenset({"cli_exe", "daemon_exe", "bundle_dir", "report_dir", "instance_dir", "seed",
                           "side", "round", "out_dir", "repo_root", "endpoint"})
_INVARIANTS = ("zero_tx_on_reject", "no_auto_resend", "no_forbidden_interleave",
               "no_stale_context_replay", "no_reopen_before_handoff", "insufficient_evidence_not_pass")
_STAGES = {"1A", "1B", "2.1", "2.2", "2.3", "2.4", "3"}
_PLATFORMS = {"linux", "windows"}
ReadBytes = Callable[[Path], bytes]


class _InputSnapshot:
    """每次入口呼叫使用的原始位元組快照；依解析後路徑快取 bytes 或讀取錯誤。"""

    def __init__(self, reader: ReadBytes | None = None) -> None:
        self._reader = reader or Path.read_bytes
        self._values: dict[Path, bytes | Exception] = {}

    @staticmethod
    def _key(path: Path) -> Path:
        return Path(path).resolve()

    def read_bytes(self, path: Path) -> bytes:
        key = self._key(path)
        if key not in self._values:
            try:
                self._values[key] = self._reader(key)
            except Exception as exc:
                self._values[key] = exc
        value = self._values[key]
        if isinstance(value, Exception):
            raise value
        return value

    def sha256(self, path: Path) -> str:
        return hashlib.sha256(self.read_bytes(path)).hexdigest()


def _read_bytes(path: Path, reader: ReadBytes | None = None) -> bytes:
    """由入口注入快照讀取器；獨立公開 helper 省略時仍直接讀取檔案。"""
    return (reader or Path.read_bytes)(Path(path))


def _sha256(path: Path, reader: ReadBytes | None = None) -> str:
    return hashlib.sha256(_read_bytes(path, reader)).hexdigest()


def _has_ascii_control(value: str) -> bool:
    return any(ord(char) <= 0x1F or ord(char) == 0x7F for char in value)


@dataclass(frozen=True)
class FrozenFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class FrozenTree:
    path: str
    tree_sha256: str


@dataclass(frozen=True)
class DutSpec:
    kind: str
    transport: str
    com_pair: Mapping[str, str] | None
    image: str | None
    image_digest: str | None
    boards: tuple[str, ...]
    reset_argv: tuple[str, ...]
    fingerprint_argv: tuple[str, ...]
    expected_fingerprint: Mapping[str, str]


@dataclass(frozen=True)
class Authorization:
    allow_destructive: bool
    decision_ref: str | None
    boards: tuple[str, ...]


@dataclass(frozen=True)
class SuiteSpec:
    adapter: str
    timeout_s: float
    selftest_report: FrozenFile | None
    testbed_template: FrozenFile | None
    realhw_config_template: FrozenFile | None
    tier: str | None
    extra_args: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseSpec:
    timeout_s: float
    poll_s: float
    endpoints: tuple[str, ...]
    device_paths: tuple[str, ...]
    lock_paths: tuple[str, ...]
    device_open_probe: bool
    accept_unreadable_holders: bool


@dataclass(frozen=True)
class ReverifySpec:
    enabled: bool
    order: tuple[str, ...]


@dataclass(frozen=True)
class AbSide:
    label: str
    role: str
    bundle_dir: Path
    release_manifest: FrozenFile
    cli_exe: Path
    daemon_exe: Path
    release_manifest_loaded_sha256: str
    activate_argv: tuple[str, ...]
    deactivate_argv: tuple[str, ...]
    manifest_doc: Mapping[str, Any]


@dataclass(frozen=True)
class AbManifest:
    schema_version: int
    run_id: str
    layer: str
    platform: str
    stage: str
    exit_item_id: str | None
    contract_revision: str
    case_list: FrozenFile
    case_lock: FrozenFile
    candidate_context: FrozenFile | None
    candidate_context_loaded_sha256: str | None
    candidate_context_doc: Mapping[str, Any] | None
    required_cases_lock: FrozenFile | None
    required_cases_sha256: str | None
    exit_catalog_sha256: str | None
    stage3_source_manifest_loaded_sha256: str | None
    stage3_source_manifest_doc: Mapping[str, Any] | None
    required_case_set: str
    case_ids: tuple[str, ...]
    tools: tuple[FrozenFile | FrozenTree, ...]
    allowed_differences: FrozenFile
    acceptance: FrozenFile | None
    profile_files: tuple[FrozenFile, ...]
    seed: int
    platform_settings: Mapping[str, Any]
    case_transport_map_loaded_sha256: str | None
    case_transport_map_doc: Mapping[str, Any] | None
    dut: DutSpec
    authorization: Authorization | None
    test_device_allowlist: tuple[str, ...]
    sides: tuple[AbSide, AbSide]
    suite: SuiteSpec
    endpoint: str | None
    release: ReleaseSpec
    reverify: ReverifySpec
    failure_retention: str | None
    repo_root: Path
    source_path: Path
    source_sha256: str
    manifest_schema_sha256: str
    trusted_pair_map_schema_sha256: str | None


def _invalid(detail: str) -> HarnessError:
    return HarnessError("HARNESS_MANIFEST_INVALID", detail)


def _schema_errors(value: Any, schema: Mapping[str, Any], root_schema: Mapping[str, Any], path: str = "$"
                   ) -> list[str]:
    """以標準函式庫檢查本 repo manifest schema 使用的 JSON Schema 關鍵字。"""
    errors: list[str] = []
    ref = schema.get("$ref")
    if isinstance(ref, str):
        prefix = "#/$defs/"
        if not ref.startswith(prefix):
            return [f"{path}: unsupported schema reference"]
        target: Any = root_schema
        for part in ref[len("#/"):].split("/"):
            target = target.get(part) if isinstance(target, Mapping) else None
        if not isinstance(target, Mapping):
            return [f"{path}: unresolved schema reference"]
        return _schema_errors(value, target, root_schema, path)

    expected_type = schema.get("type")
    if expected_type is not None:
        types = expected_type if isinstance(expected_type, list) else [expected_type]
        matches = False
        for kind in types:
            if kind == "object" and isinstance(value, dict):
                matches = True
            elif kind == "array" and isinstance(value, list):
                matches = True
            elif kind == "string" and isinstance(value, str):
                matches = True
            elif kind == "boolean" and isinstance(value, bool):
                matches = True
            elif kind == "integer" and isinstance(value, int) and not isinstance(value, bool):
                matches = True
            elif kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
                matches = True
            elif kind == "null" and value is None:
                matches = True
        if not matches:
            return [f"{path}: type mismatch"]

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: const mismatch")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: enum mismatch")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: string is too short")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            errors.append(f"{path}: pattern mismatch")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: below exclusive minimum")

    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            errors.extend(f"{path}: missing required property {name}" for name in required if name not in value)
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            for name, child in properties.items():
                if name in value and isinstance(child, Mapping):
                    errors.extend(_schema_errors(value[name], child, root_schema, f"{path}.{name}"))
            if schema.get("additionalProperties") is False:
                errors.extend(f"{path}: unexpected property {name}" for name in value if name not in properties)
            elif isinstance(schema.get("additionalProperties"), Mapping):
                extra_schema = schema["additionalProperties"]
                for name, child_value in value.items():
                    if name not in properties:
                        errors.extend(_schema_errors(child_value, extra_schema, root_schema, f"{path}.{name}"))
        elif isinstance(schema.get("additionalProperties"), Mapping):
            extra_schema = schema["additionalProperties"]
            for name, child_value in value.items():
                errors.extend(_schema_errors(child_value, extra_schema, root_schema, f"{path}.{name}"))
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: too many items")
        if schema.get("uniqueItems") is True:
            fingerprints = [json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                            for item in value]
            if len(fingerprints) != len(set(fingerprints)):
                errors.append(f"{path}: duplicate array items")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, child_value in enumerate(value):
                errors.extend(_schema_errors(child_value, items, root_schema, f"{path}[{index}]"))
        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list):
            for index, child_schema in enumerate(prefix_items[:len(value)]):
                if isinstance(child_schema, Mapping):
                    errors.extend(_schema_errors(value[index], child_schema, root_schema, f"{path}[{index}]"))
            if items is False and len(value) > len(prefix_items):
                errors.append(f"{path}: items are not allowed")
        elif items is False and value:
            errors.append(f"{path}: items are not allowed")
    contains = schema.get("contains")
    if isinstance(value, list) and isinstance(contains, Mapping):
        if not any(not _schema_errors(item, contains, root_schema, f"{path}[]") for item in value):
            errors.append(f"{path}: no item matches contains")
    for child in schema.get("allOf", []):
        if isinstance(child, Mapping):
            errors.extend(_schema_errors(value, child, root_schema, path))
    for child in schema.get("anyOf", []):
        if isinstance(child, Mapping) and not _schema_errors(value, child, root_schema, path):
            break
    else:
        if "anyOf" in schema:
            errors.append(f"{path}: no anyOf branch matches")
    condition = schema.get("if")
    if isinstance(condition, Mapping):
        branch = "then" if not _schema_errors(value, condition, root_schema, path) else "else"
        selected = schema.get(branch)
        if isinstance(selected, Mapping):
            errors.extend(_schema_errors(value, selected, root_schema, path))
    return errors


def _read_schema_document(
    schema_path: Path, *, label: str, read_bytes: ReadBytes | None = None,
) -> tuple[bytes, Mapping[str, Any]]:
    try:
        raw = _read_bytes(schema_path, read_bytes)
        schema = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _invalid(f"cannot read {label} schema: {exc}") from exc
    if not isinstance(schema, Mapping):
        raise _invalid(f"{label} schema must be an object")
    return raw, schema


def _validate_schema_document(
    value: Any, schema_path: Path, *, label: str, schema_bytes: bytes | None = None,
    read_bytes: ReadBytes | None = None,
) -> str:
    if schema_bytes is None:
        schema_bytes, schema = _read_schema_document(schema_path, label=label, read_bytes=read_bytes)
    else:
        try:
            schema = json.loads(schema_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise _invalid(f"cannot read {label} schema: {exc}") from exc
        if not isinstance(schema, Mapping):
            raise _invalid(f"{label} schema must be an object")
    errors = _schema_errors(value, schema, schema)
    if errors:
        raise _invalid(f"{label} schema validation failed: " + "; ".join(errors[:8]))
    return hashlib.sha256(schema_bytes).hexdigest()


def _validate_manifest_schema(
    value: Any, *, repo_root: Path, read_bytes: ReadBytes | None = None,
) -> str:
    """從明確的 repo_root 讀 schema，回傳實際驗證位元組的 SHA-256。"""
    schema_path = Path(repo_root) / "tests" / "acceptance" / "ab_manifest.schema.json"
    return _validate_schema_document(value, schema_path, label="A/B manifest", read_bytes=read_bytes)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(child) for child in value)
    return value


def _plain_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_value(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain_value(child) for child in value]
    if isinstance(value, list):
        return [_plain_value(child) for child in value]
    return value


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(f"{where} must be an object")
    return value


def _string(value: Any, where: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _invalid(f"{where} must be a string")
    return value


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise _invalid(f"{where} must be a positive number")
    return float(value)


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _invalid(f"{where} must be an array of strings")
    return tuple(value)


def _frozen(value: Any, where: str, *, tree_ok: bool = False) -> FrozenFile | FrozenTree:
    obj = _mapping(value, where)
    path = _string(obj.get("path"), f"{where}.path")
    if _has_ascii_control(path):
        raise _invalid(f"{where}.path contains ASCII control characters: {path!r}")
    if tree_ok and isinstance(obj.get("tree_sha256"), str):
        digest = obj["tree_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise _invalid(f"{where}.tree_sha256 must be lowercase SHA-256")
        return FrozenTree(path, digest)
    digest = _string(obj.get("sha256"), f"{where}.sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise _invalid(f"{where}.sha256 must be lowercase SHA-256")
    return FrozenFile(path, digest)


def _path(root: Path, value: str) -> Path:
    # Manifest 路徑使用 POSIX 分隔；Path 會在原生 Windows 轉成該平台格式。
    if _has_ascii_control(value):
        raise _invalid(f"path contains ASCII control characters: {value!r}")
    path = Path(value.replace("/", os.sep))
    return path if path.is_absolute() else root / path


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _require_canonical_path(root: Path, item: FrozenFile, relative: str, where: str) -> None:
    expected = (Path(root) / Path(*PurePosixPath(relative).parts)).resolve()
    if _path(Path(root), item.path).resolve() != expected:
        raise _invalid(f"{where} must reference the canonical path {relative}")


def _release_component_path(bundle: Path, doc: Mapping[str, Any], *, daemon: bool) -> Path | None:
    components = doc.get("components")
    if not isinstance(components, dict):
        return None
    component = components.get("daemon" if daemon else "cli")
    if not isinstance(component, dict):
        return None
    path: Any = None
    if daemon:
        launcher = component.get("launcher")
        if isinstance(launcher, dict):
            path = launcher.get("path")
        if path is None:
            runtime = component.get("runtime")
            if isinstance(runtime, dict):
                path = runtime.get("path")
        if path is None:
            entry = component.get("entry")
            if isinstance(entry, list) and entry and isinstance(entry[0], str):
                path = entry[0]
    else:
        path = component.get("path")
    if not isinstance(path, str) or not path:
        return None
    return _path(bundle, path)


def load_manifest(
    path: Path, *, repo_root: Path | None = None, trusted_pair_map: Mapping[str, Any] | None = None,
) -> AbManifest:
    """讀取並驗證 A/B manifest。

    Schema 一律從呼叫端的 ``repo_root/tests/acceptance`` 讀取，不從本模組所在 checkout 取用；
    載入時保存實際驗證之 schema 原始位元組的 SHA-256；``verify_frozen`` 比對載入摘要、目前檔案
    與 ``tool_revision`` 的原始 blob。Task 6 必須呼叫 ``load_manifest(path, repo_root=repo_root)``，
    讓載入與凍結檢查使用同一 checkout；省略 ``repo_root`` 時會綁定本模組 checkout，後續傳入別的
    ``repo_root`` 會明確拒絕，不會推測呼叫端想用哪一份 repo。
    因此程式 checkout 與目標 repo 不同時，讀取與版本比對仍指向同一檔案。即使 schema 被弱化，
    ``tools`` 必須包含 ``tools/stage3`` 的信任邊界仍由 loader 程式獨立強制。
    每次載入建立專用輸入快照；同一個 resolved path 只讀一次，解析與 SHA 均使用快照中的原始 bytes。

    Windows L1.5 的可信 pair 對照由呼叫端私下提供；不把實際 COM 名稱寫入
    manifest 或公開映射。正式對照由編排者在 T5B 依原生 COM 實機盤點後建立，
    公開證據只包含去識別端點 ID 與對照檔 SHA-256；T3 測試僅用合成資料。

    import 快照有兩項已知限制：Python 讀取原始檔到建立快照之間存在極短窗口；import
    後才延遲載入的模組不在這次快照的時間界線內。Task 6 必須在 Stage 3 程序啟動後、任何
    A/B 動作之前呼叫 ``verify_frozen``，執行期間不得改動 checkout，並在報告記錄
    ``tools_stage3_tree_sha256``。
    另，時間戳與大小符合來源檔的舊 `.pyc` 可能先於 `.py` 載入。Task 6 啟動時應用
    `python -B` 或 `PYTHONDONTWRITEBYTECODE=1`，並在乾淨 checkout 執行，或先確認
    `tools/stage3` 下沒有 `__pycache__`。
    """
    source = Path(path).resolve()
    root_path = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[2]
    snapshot = _InputSnapshot()
    try:
        source_bytes = snapshot.read_bytes(source)
        doc = json.loads(source_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _invalid(f"cannot read manifest: {exc}") from exc
    manifest_schema_sha256 = _validate_manifest_schema(
        doc, repo_root=root_path, read_bytes=snapshot.read_bytes,
    )
    root = _mapping(doc, "manifest")
    run_id = _string(root.get("run_id"), "run_id")
    layer = _string(root.get("layer"), "layer")
    platform = _string(root.get("platform"), "platform")
    stage = _string(root.get("stage"), "stage")
    exit_item_id = root.get("exit_item_id")
    if exit_item_id is not None and not isinstance(exit_item_id, str):
        raise _invalid("exit_item_id must be a string or null")
    trusted_pair_schema_bytes: bytes | None = None
    trusted_pair_map_schema_sha256: str | None = None
    if exit_item_id == "S3-L15-RELEASE-AB-WINDOWS":
        trusted_pair_schema_path = root_path / "tests" / "acceptance" / "stage3_trusted_pair_map.schema.json"
        trusted_pair_schema_bytes, _trusted_schema_doc = _read_schema_document(
            trusted_pair_schema_path, label="trusted pair map", read_bytes=snapshot.read_bytes,
        )
        trusted_pair_map_schema_sha256 = hashlib.sha256(trusted_pair_schema_bytes).hexdigest()
    contract_revision = _string(root.get("contract_revision"), "contract_revision")
    if not re.fullmatch(r"[0-9a-f]{40}", contract_revision):
        raise _invalid("contract_revision must be 40 lowercase hex characters")
    if layer not in LAYER_ADAPTER or platform not in _PLATFORMS or stage not in _STAGES:
        raise _invalid("unsupported layer, platform, or stage")

    case_list = _frozen(root.get("case_list"), "case_list")
    case_lock = _frozen(root.get("case_lock"), "case_lock")
    _require_canonical_path(root_path, case_list, "tests/acceptance/cases.yaml", "case_list")
    _require_canonical_path(root_path, case_lock, "tests/acceptance/cases.lock.json", "case_lock")
    allowed_differences = _frozen(root.get("allowed_differences"), "allowed_differences")
    candidate_context_value = root.get("candidate_context")
    candidate_context = None if candidate_context_value is None else _frozen(candidate_context_value, "candidate_context")
    if isinstance(candidate_context, FrozenTree):
        raise _invalid("candidate_context must be a frozen file")
    required_cases_lock_value = root.get("required_cases_lock")
    required_cases_lock = None if required_cases_lock_value is None else _frozen(
        required_cases_lock_value, "required_cases_lock"
    )
    if isinstance(required_cases_lock, FrozenTree):
        raise _invalid("required_cases_lock must be a frozen file")
    required_cases_sha256 = root.get("required_cases_sha256")
    exit_catalog_sha256 = root.get("exit_catalog_sha256")
    for name, digest in (("required_cases_sha256", required_cases_sha256),
                         ("exit_catalog_sha256", exit_catalog_sha256)):
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise _invalid(f"{name} must be a lowercase SHA-256 or null")
    required_case_set = _string(root.get("required_case_set"), "required_case_set")
    case_ids = _strings(root.get("case_ids"), "case_ids")
    if not case_ids or len(set(case_ids)) != len(case_ids):
        raise _invalid("case_ids must be non-empty and unique")
    raw_tools = root.get("tools")
    if not isinstance(raw_tools, list) or not raw_tools:
        raise _invalid("tools must be a non-empty array")
    tools = tuple(_frozen(item, f"tools[{i}]", tree_ok=True) for i, item in enumerate(raw_tools))
    if not any(isinstance(item, FrozenTree) and item.path == "tools/stage3" for item in tools):
        raise _invalid("tools must include the tools/stage3 frozen tree")
    acceptance_value = root.get("acceptance")
    acceptance = None if acceptance_value is None else _frozen(acceptance_value, "acceptance")
    if isinstance(acceptance, FrozenTree):
        raise _invalid("acceptance must be a frozen file")
    profile_files = tuple(_frozen(item, f"profile_files[{i}]") for i, item in enumerate(root.get("profile_files", [])))
    for i, item in enumerate(profile_files):
        try:
            actual = snapshot.sha256(_path(root_path, item.path))
        except OSError as exc:
            raise _invalid(f"profile file missing: profile_files[{i}] {item.path}") from exc
        if actual != item.sha256:
            raise _invalid(f"profile sha256 mismatch: {item.path}")

    dut_doc = _mapping(root.get("dut"), "dut")
    pair = dut_doc.get("com_pair")
    if pair is not None:
        pair = _mapping(pair, "dut.com_pair")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in pair.items()):
            raise _invalid("dut.com_pair must map strings to strings")
    image = dut_doc.get("image")
    image_digest = dut_doc.get("image_digest")
    if image is not None and not isinstance(image, str):
        raise _invalid("dut.image must be a string or null")
    if image_digest is not None and not isinstance(image_digest, str):
        raise _invalid("dut.image_digest must be a string or null")
    expected_fp = _mapping(dut_doc.get("expected_fingerprint"), "dut.expected_fingerprint")
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in expected_fp.items()):
        raise _invalid("dut.expected_fingerprint must map strings to strings")
    dut = DutSpec(
        kind=_string(dut_doc.get("kind"), "dut.kind"),
        transport=_string(dut_doc.get("transport"), "dut.transport"),
        com_pair=pair,
        image=image,
        image_digest=image_digest,
        boards=_strings(dut_doc.get("boards"), "dut.boards"),
        reset_argv=_strings(dut_doc.get("reset_argv"), "dut.reset_argv"),
        fingerprint_argv=_strings(dut_doc.get("fingerprint_argv"), "dut.fingerprint_argv"),
        expected_fingerprint=expected_fp,
    )

    auth_doc = root.get("authorization")
    authorization = None
    if auth_doc is not None:
        auth_doc = _mapping(auth_doc, "authorization")
        decision = auth_doc.get("decision_ref")
        if decision is not None and not isinstance(decision, str):
            raise _invalid("authorization.decision_ref must be a string or null")
        if not isinstance(auth_doc.get("allow_destructive"), bool):
            raise _invalid("authorization.allow_destructive must be boolean")
        authorization = Authorization(auth_doc["allow_destructive"], decision,
                                      _strings(auth_doc.get("boards"), "authorization.boards"))
    allowlist = _strings(root.get("test_device_allowlist"), "test_device_allowlist")

    sides_raw = root.get("sides")
    if not isinstance(sides_raw, list) or len(sides_raw) != 2:
        raise _invalid("sides must contain exactly A and B")
    sides: list[AbSide] = []
    for i, side_value in enumerate(sides_raw):
        side = _mapping(side_value, f"sides[{i}]")
        label = "A" if i == 0 else "B"
        role = "baseline" if i == 0 else "candidate"
        if side.get("label") != label or side.get("role") != role:
            raise _invalid(f"side {label} must have role {role}")
        bundle = _path(root_path, _string(side.get("bundle_dir"), f"sides[{i}].bundle_dir")).resolve()
        manifest = _frozen(side.get("release_manifest"), f"sides[{i}].release_manifest")
        if isinstance(manifest, FrozenTree):
            raise _invalid("release_manifest must be a frozen file")
        manifest_path = _path(root_path, manifest.path)
        expected_sidecar = bundle / "release-manifest.json"
        if manifest_path.resolve() != expected_sidecar.resolve():
            raise _invalid(f"side {label} release_manifest is not the bundle root sidecar")
        try:
            manifest_bytes = snapshot.read_bytes(manifest_path)
            manifest_doc = json.loads(manifest_bytes.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise _invalid(f"side {label} release manifest unreadable: {exc}") from exc
        manifest_loaded_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        if manifest_loaded_sha256 != manifest.sha256:
            raise _invalid(
                f"side {label} release manifest sha256 mismatch: "
                f"declared {manifest.sha256} actual {manifest_loaded_sha256}"
            )
        manifest_doc = _mapping(manifest_doc, f"side {label} release manifest")
        _validate_release_component_shape(manifest_doc, stage=stage, label=label)
        cli_exe = _path(root_path, _string(side.get("cli_exe"), f"sides[{i}].cli_exe")).resolve()
        daemon_exe = _path(root_path, _string(side.get("daemon_exe"), f"sides[{i}].daemon_exe")).resolve()
        expected_cli = _release_component_path(bundle, manifest_doc, daemon=False)
        expected_daemon = _release_component_path(bundle, manifest_doc, daemon=True)
        if expected_cli is None or cli_exe != expected_cli.resolve():
            raise _invalid(f"side {label} cli_exe is not the declared CLI component")
        if expected_daemon is None or daemon_exe != expected_daemon.resolve():
            raise _invalid(f"side {label} daemon_exe is not the declared daemon component")
        if platform == "windows" and (cli_exe.suffix.casefold() != ".exe" or daemon_exe.suffix.casefold() != ".exe"):
            raise _invalid(f"side {label} Windows executable component must use .exe")
        sides.append(AbSide(label, role, bundle, manifest, cli_exe, daemon_exe, manifest_loaded_sha256,
                            _strings(side.get("activate_argv"), f"sides[{i}].activate_argv"),
                            _strings(side.get("deactivate_argv"), f"sides[{i}].deactivate_argv"),
                            _deep_freeze(manifest_doc)))

    if (sides[0].bundle_dir.parent.resolve() != sides[1].bundle_dir.parent.resolve()
            or sides[0].bundle_dir.name != "A" or sides[1].bundle_dir.name != "B"):
        raise _invalid("A/B bundle paths must share one version root")

    suite_doc = _mapping(root.get("suite"), "suite")
    def suite_file(key: str) -> FrozenFile | None:
        value = suite_doc.get(key)
        if value is None:
            return None
        item = _frozen(value, f"suite.{key}")
        if isinstance(item, FrozenTree):
            raise _invalid(f"suite.{key} must be a frozen file")
        return item
    tier = suite_doc.get("tier")
    if tier is not None and not isinstance(tier, str):
        raise _invalid("suite.tier must be a string or null")
    suite = SuiteSpec(_string(suite_doc.get("adapter"), "suite.adapter"), _number(suite_doc.get("timeout_s"), "suite.timeout_s"),
                      suite_file("selftest_report"), suite_file("testbed_template"), suite_file("realhw_config_template"),
                      tier, _strings(suite_doc.get("extra_args"), "suite.extra_args"))

    release_doc = _mapping(root.get("release"), "release")
    device_open_probe = release_doc.get("device_open_probe")
    if not isinstance(device_open_probe, bool):
        raise _invalid("release.device_open_probe must be boolean")
    accept_unreadable_holders = release_doc.get("accept_unreadable_holders")
    if not isinstance(accept_unreadable_holders, bool):
        raise _invalid("release.accept_unreadable_holders must be boolean")
    release = ReleaseSpec(_number(release_doc.get("timeout_s"), "release.timeout_s"),
                          _number(release_doc.get("poll_s"), "release.poll_s"),
                          _strings(release_doc.get("endpoints"), "release.endpoints"),
                          _strings(release_doc.get("device_paths"), "release.device_paths"),
                          _strings(release_doc.get("lock_paths"), "release.lock_paths"),
                          device_open_probe,
                          accept_unreadable_holders)
    reverify_doc = _mapping(root.get("reverify"), "reverify")
    if not isinstance(reverify_doc.get("enabled"), bool):
        raise _invalid("reverify.enabled must be boolean")
    reverify = ReverifySpec(reverify_doc["enabled"], _strings(reverify_doc.get("order"), "reverify.order"))

    endpoint = root.get("endpoint")
    failure_retention = root.get("failure_retention")
    if endpoint is not None and not isinstance(endpoint, str):
        raise _invalid("endpoint must be a string or null")
    if failure_retention is not None and not isinstance(failure_retention, str):
        raise _invalid("failure_retention must be a string or null")
    if suite.adapter != LAYER_ADAPTER[layer]:
        raise _invalid("suite adapter does not match layer")
    if stage == "3" and required_case_set != "slice_2.4":
        raise _invalid("Stage 3 requires slice_2.4 case set")
    if layer == "l1.5":
        if dut.kind not in {"local-process", "docker", "physical-substitute"} or suite.selftest_report is None:
            raise _invalid("L1.5 requires a virtual DUT and selftest report")
    else:
        if dut.kind != "hardware" or not dut.boards or not all(side.activate_argv and side.deactivate_argv for side in sides):
            raise _invalid("L2/L3 requires hardware, boards, activate and deactivate argv")
    if dut.kind == "docker" and (not dut.image or not dut.image.strip() or not dut.image_digest):
        raise _invalid("docker DUT requires a non-empty image and image_digest")
    if dut.transport == "com0com-pair" and dut.com_pair is None:
        raise _invalid("com0com-pair requires com_pair")
    if layer == "l2" and suite.testbed_template is None:
        raise _invalid("L2 requires testbed_template")
    if layer == "l3" and (acceptance is None or endpoint is None or suite.realhw_config_template is None
                           or suite.tier is None or failure_retention != "on-demand"):
        raise _invalid("L3 requires acceptance, endpoint, config template, tier, and on-demand failure retention")
    if reverify.enabled and reverify.order != ("B", "A"):
        raise _invalid("reverify order must be B then A")
    if stage in {"1A", "1B"}:
        if _impl(sides[0].manifest_doc, "daemon") != "python" or _impl(sides[1].manifest_doc, "daemon") != "python":
            raise _invalid("Stage 1 requires Python daemons on both sides")
        if _impl(sides[0].manifest_doc, "cli") != "python" or _impl(sides[1].manifest_doc, "cli") != "rust":
            raise _invalid("Stage 1 requires Python CLI on A and Rust CLI on B")
    else:
        if _impl(sides[0].manifest_doc, "daemon") != "python" or _impl(sides[1].manifest_doc, "daemon") != "rust":
            raise _invalid("A/B requires Python daemon on A and Rust daemon on B")
        if _impl(sides[0].manifest_doc, "cli") != _impl(sides[1].manifest_doc, "cli"):
            raise _invalid("Stage 2+ CLI implementation must match")
        if _component(sides[0].manifest_doc, "cli").get("sha256") != _component(sides[1].manifest_doc, "cli").get("sha256"):
            raise _invalid("Stage 2+ CLI sha256 must match")
    if stage == "3":
        candidate = sides[1].manifest_doc
        if candidate.get("stage") != "3" or candidate.get("retirement") != {"python_daemon": "removed"}:
            raise _invalid("Stage 3 candidate requires retirement marker")
    if not _valid_templates(dut, sides, suite):
        raise _invalid("unknown template key")

    case_transport_map_loaded_sha256: str | None = None
    case_transport_map_doc: Mapping[str, Any] | None = None
    if exit_item_id == "S3-L15-RELEASE-AB-WINDOWS":
        settings_doc = _mapping(root.get("platform_settings"), "platform_settings")
        map_ref = _frozen(settings_doc.get("case_transport_map"), "platform_settings.case_transport_map")
        if not isinstance(map_ref, FrozenFile):
            raise _invalid("case_transport_map must be a frozen file")
        try:
            map_bytes = snapshot.read_bytes(_path(root_path, map_ref.path))
        except OSError as exc:
            raise _invalid(f"case transport map is unreadable: {exc}") from exc
        case_transport_map_loaded_sha256 = hashlib.sha256(map_bytes).hexdigest()
        if case_transport_map_loaded_sha256 != map_ref.sha256:
            raise _invalid(
                "case transport map sha256 mismatch: "
                f"declared {map_ref.sha256} actual {case_transport_map_loaded_sha256}"
            )
        case_transport_map_doc = _read_transport_map_bytes(map_bytes)

    candidate_context_loaded_sha256: str | None = None
    candidate_context_doc: Mapping[str, Any] | None = None
    if candidate_context is not None:
        context_path = _path(root_path, candidate_context.path)
        try:
            context_bytes = snapshot.read_bytes(context_path)
        except OSError as exc:
            raise _invalid(f"candidate context is invalid: {exc}") from exc
        candidate_context_loaded_sha256 = hashlib.sha256(context_bytes).hexdigest()
        if candidate_context_loaded_sha256 != candidate_context.sha256:
            raise _invalid("candidate context sha256 mismatch")
        try:
            from tools.stage3 import candidate_context as candidate_context_module

            candidate_context_doc = candidate_context_module.load_run_context_bytes(context_bytes)
        except (UnicodeError, ValueError, KeyError, TypeError, RecursionError) as exc:
            raise _invalid(f"candidate context is invalid: {exc}") from exc

    stage3_source_manifest_loaded_sha256: str | None = None
    stage3_source_manifest_doc: Mapping[str, Any] | None = None
    if stage == "3":
        source_manifest_path = (
            root_path / "docs" / "rust-refactor" / "evidence" / "denominators" / "source-manifest.json"
        )
        try:
            source_manifest_bytes = snapshot.read_bytes(source_manifest_path)
            source_manifest_value = json.loads(source_manifest_bytes.decode("utf-8"))
            stage3_source_manifest_doc = _mapping(source_manifest_value, "Stage 3 source-manifest")
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, HarnessError) as exc:
            raise _invalid(f"Stage 3 source-manifest is invalid: {exc}") from exc
        stage3_source_manifest_loaded_sha256 = hashlib.sha256(source_manifest_bytes).hexdigest()

    manifest = AbManifest(
        schema_version=1,
        run_id=run_id,
        layer=layer,
        platform=platform,
        stage=stage,
        exit_item_id=exit_item_id,
        contract_revision=contract_revision,
        case_list=case_list,
        case_lock=case_lock,
        candidate_context=candidate_context,
        candidate_context_loaded_sha256=candidate_context_loaded_sha256,
        candidate_context_doc=(
            _deep_freeze(dict(candidate_context_doc)) if candidate_context_doc is not None else None
        ),
        required_cases_lock=required_cases_lock,
        required_cases_sha256=required_cases_sha256,
        exit_catalog_sha256=exit_catalog_sha256,
        stage3_source_manifest_loaded_sha256=stage3_source_manifest_loaded_sha256,
        stage3_source_manifest_doc=(
            _deep_freeze(dict(stage3_source_manifest_doc)) if stage3_source_manifest_doc is not None else None
        ),
        required_case_set=required_case_set,
        case_ids=case_ids,
        tools=tools,
        allowed_differences=allowed_differences,
        acceptance=acceptance,
        profile_files=tuple(profile_files),
        seed=root["seed"],
        platform_settings=_deep_freeze(_mapping(root.get("platform_settings"), "platform_settings")),
        case_transport_map_loaded_sha256=case_transport_map_loaded_sha256,
        case_transport_map_doc=(
            _deep_freeze(dict(case_transport_map_doc)) if case_transport_map_doc is not None else None
        ),
        dut=DutSpec(dut.kind, dut.transport, _deep_freeze(dut.com_pair) if dut.com_pair is not None else None,
                    dut.image, dut.image_digest, dut.boards, dut.reset_argv, dut.fingerprint_argv,
                    _deep_freeze(dut.expected_fingerprint)),
        authorization=authorization,
        test_device_allowlist=allowlist,
        sides=(sides[0], sides[1]),
        suite=suite,
        endpoint=endpoint,
        release=release,
        reverify=reverify,
        failure_retention=failure_retention,
        repo_root=root_path,
        source_path=source,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        manifest_schema_sha256=manifest_schema_sha256,
        trusted_pair_map_schema_sha256=trusted_pair_map_schema_sha256,
    )
    if stage == "3":
        _validate_stage3_identity(manifest, root_path, read_bytes=snapshot.read_bytes)
    if exit_item_id == "S3-L15-RELEASE-AB-WINDOWS":
        _validate_windows_l15_transport(
            manifest, root_path, trusted_pair_map=trusted_pair_map,
            trusted_pair_schema_bytes=trusted_pair_schema_bytes,
            read_bytes=snapshot.read_bytes,
        )
    return manifest


def _components(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    value = manifest.get("components")
    return value if isinstance(value, Mapping) else {}


def _component(manifest: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = _components(manifest).get(name)
    return value if isinstance(value, Mapping) else {}


def _impl(manifest: Mapping[str, Any], name: str) -> Any:
    return _component(manifest, name).get("implementation")


def _require_release_file_identity(value: Any, where: str) -> None:
    if not isinstance(value, Mapping):
        raise _invalid(f"{where} must be an object")
    path = value.get("path")
    digest = value.get("sha256")
    if not isinstance(path, str) or not path:
        raise _invalid(f"{where}.path must be a non-empty string")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise _invalid(f"{where}.sha256 must be a lowercase SHA-256")


def _validate_release_component_shape(manifest: Mapping[str, Any], *, stage: str, label: str) -> None:
    """按 stage／角色確認必要的發佈元件都有可驗證身分。"""
    if (stage in {"1A", "1B"} and label == "A"
            and _impl(manifest, "cli") == "python" and _impl(manifest, "daemon") == "python"):
        # 保留 Python 基線對舊 release schema 的相容性，但仍驗證實際選用的 CLI 與 runtime。
        components = manifest.get("components")
        if not isinstance(components, Mapping):
            raise _invalid(f"side {label} release manifest components must be an object")
        _require_release_file_identity(components.get("cli"), f"side {label} components.cli")
        daemon = components.get("daemon")
        if not isinstance(daemon, Mapping) or daemon.get("implementation") != "python":
            raise _invalid(f"side {label} components.daemon must declare the Python implementation")
        _require_release_file_identity(daemon.get("runtime"), f"side {label} components.daemon.runtime")
        return
    components = manifest.get("components")
    if not isinstance(components, Mapping):
        raise _invalid(f"side {label} release manifest components must be an object")
    _require_release_file_identity(components.get("cli"), f"side {label} components.cli")
    daemon = components.get("daemon")
    if not isinstance(daemon, Mapping):
        raise _invalid(f"side {label} components.daemon must be an object")
    daemon_impl = daemon.get("implementation")
    retired_candidate = (
        stage == "3" and label == "B"
        and manifest.get("retirement") == {"python_daemon": "removed"}
    )
    launcher = daemon.get("launcher")
    if daemon_impl == "rust" or launcher is not None:
        _require_release_file_identity(launcher, f"side {label} components.daemon.launcher")
    if retired_candidate:
        # Task 16 的退役候選最小執行身分為 CLI 與 Rust daemon launcher。
        return

    runtime = daemon.get("runtime")
    _require_release_file_identity(runtime, f"side {label} components.daemon.runtime")
    package = daemon.get("package")
    if not isinstance(package, Mapping) or not isinstance(package.get("tree_sha256"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", package["tree_sha256"]
    ):
        raise _invalid(f"side {label} components.daemon.package.tree_sha256 must be a lowercase SHA-256")
    delegate = components.get("python_cli_delegate")
    if (not isinstance(delegate, Mapping)
            or not isinstance(delegate.get("entry"), list)
            or not delegate["entry"]
            or any(not isinstance(item, str) for item in delegate["entry"])
            or not isinstance(delegate.get("tree_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", delegate["tree_sha256"])):
        raise _invalid(f"side {label} components.python_cli_delegate requires entry and tree_sha256")


def _validate_stage3_identity(
    manifest: AbManifest, repo_root: Path, *, read_bytes: ReadBytes | None = None,
) -> None:
    """核對 Stage 3 出口身分、T1 凍結分母與執行前候選 context。"""
    if (manifest.exit_item_id is None or manifest.candidate_context is None
            or manifest.required_cases_lock is None or manifest.required_cases_sha256 is None
            or manifest.exit_catalog_sha256 is None):
        raise _invalid("Stage 3 requires exit identity, candidate context, and required case lock")
    if manifest.required_case_set != "slice_2.4":
        raise _invalid("Stage 3 requires slice_2.4 case set")
    if manifest.required_cases_lock.sha256 != manifest.required_cases_sha256:
        raise _invalid("required_cases_sha256 must match required_cases_lock.sha256")
    expected_lock_path = (Path(repo_root) / "tests" / "acceptance" / "stage3_required_cases.lock.json").resolve()
    if _path(Path(repo_root), manifest.required_cases_lock.path).resolve() != expected_lock_path:
        raise _invalid("required_cases_lock must reference the current Stage 3 exit lock")

    try:
        cases_doc, _case_lock, catalog, lock, _source_manifest = required_cases_module._load_inputs(
            repo_root, read_bytes=read_bytes,
        )
        item = next((row for row in catalog.get("items", [])
                     if isinstance(row, Mapping) and row.get("item_id") == manifest.exit_item_id), None)
        if item is None or item.get("stage") != "3":
            raise _invalid("exit_item_id is not a current Stage 3 exit")
        layer = item.get("layer")
        if not isinstance(layer, str) or layer.casefold() != manifest.layer:
            raise _invalid("exit_item_id does not match layer")
        item_platform = item.get("platform")
        if item_platform not in (manifest.platform, "both"):
            raise _invalid("exit_item_id does not match platform")
        if item.get("required_case_set") != manifest.required_case_set:
            raise _invalid("exit_item_id does not match required_case_set")
        entry = lock["exits"][manifest.exit_item_id][manifest.platform]
        if entry.get("layer", "").casefold() != manifest.layer or entry.get("required_case_set") != manifest.required_case_set:
            raise _invalid("required case lock does not match exit identity")
        required_cases_module.required_for_exit(lock, manifest.exit_item_id, manifest.platform)
    except HarnessError:
        raise
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise _invalid(f"cannot verify current Stage 3 exit identity: {exc}") from exc

    if (manifest.candidate_context_loaded_sha256 != manifest.candidate_context.sha256
            or manifest.candidate_context_doc is None):
        raise _invalid("candidate context loaded identity is unavailable")
    context = manifest.candidate_context_doc
    candidate = manifest.sides[1]
    if context.get("platform") != manifest.platform or context.get("run_id") != manifest.run_id:
        raise _invalid("candidate context platform or run_id does not match manifest")
    if context.get("required_cases_sha256") != manifest.required_cases_lock.sha256:
        raise _invalid("candidate context required_cases_sha256 does not match required case lock")
    if context.get("artifact_id") != candidate.release_manifest.sha256:
        raise _invalid("candidate context artifact_id does not match B release manifest")
    for side in manifest.sides:
        target = str(side.manifest_doc.get("target", "")).casefold()
        if "windows" in target and manifest.platform != "windows":
            raise _invalid(f"side {side.label} release target does not match platform")
        if "linux" in target and manifest.platform != "linux":
            raise _invalid(f"side {side.label} release target does not match platform")


_CASE_TRANSPORTS = frozenset({"windows-native-com", "od08-physical", "docker", "pty", "local-process"})
_TRUSTED_PAIR_MAP_FIELDS = {"schema_version", "exit_item_id", "platform", "selected_pair_id", "pairs"}
_TRUSTED_PAIR_FIELDS = {
    "dut_port", "daemon_port", "dut_side_port_id", "daemon_side_port_id", "decision_ref",
}


def _closed_object(value: Any, required: set[str], optional: set[str], where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid(f"{where} must be an object")
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if extra:
            detail.append("unexpected " + ", ".join(sorted(extra)))
        raise _invalid(f"{where} fields are invalid: {'; '.join(detail)}")
    return value


def _read_transport_map_bytes(raw: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise _invalid(f"case transport map is unreadable: {exc}") from exc
    root = _closed_object(value, {"schema_version", "exit_item_id", "platform", "cases",
                                  "approved_pairs", "physical_substitutes"}, set(), "case transport map")
    if root.get("schema_version") != 1 or isinstance(root.get("schema_version"), bool):
        raise _invalid("case transport map schema_version must be 1")
    if root.get("platform") != "windows" or not isinstance(root.get("exit_item_id"), str):
        raise _invalid("case transport map must identify a Windows exit")
    for section in ("cases", "approved_pairs", "physical_substitutes"):
        if not isinstance(root.get(section), dict):
            raise _invalid(f"case transport map {section} must be an object")
    for case_id, row in root["cases"].items():
        item = _closed_object(row, {"requires_native_com", "runner", "platform", "allowed_transports", "pair_id"},
                              set(), f"case transport map cases.{case_id}")
        if not isinstance(item.get("requires_native_com"), bool):
            raise _invalid(f"case transport map cases.{case_id}.requires_native_com must be boolean")
        if not isinstance(item.get("runner"), str) or not item["runner"] or item.get("platform") != "windows":
            raise _invalid(f"case transport map cases.{case_id} runner/platform is invalid")
        allowed = item.get("allowed_transports")
        if (not isinstance(allowed, list) or not allowed
                or any(not isinstance(transport, str) or transport not in _CASE_TRANSPORTS for transport in allowed)
                or len(set(allowed)) != len(allowed)):
            raise _invalid(f"case transport map cases.{case_id}.allowed_transports is invalid")
        pair_id = item.get("pair_id")
        if pair_id is not None and (not isinstance(pair_id, str) or not pair_id):
            raise _invalid(f"case transport map cases.{case_id}.pair_id must be a string or null")
    for pair_id, pair in root["approved_pairs"].items():
        item = _closed_object(pair, {"dut_side_port_id", "daemon_side_port_id", "decision_ref"}, set(),
                              f"case transport map approved_pairs.{pair_id}")
        if any(not isinstance(item.get(key), str) or not item[key]
               for key in ("dut_side_port_id", "daemon_side_port_id", "decision_ref")):
            raise _invalid(f"case transport map approved_pairs.{pair_id} is incomplete")
        if item["dut_side_port_id"] == item["daemon_side_port_id"]:
            raise _invalid(f"case transport map approved_pairs.{pair_id} endpoints must be distinct")
    for port_id, substitute in root["physical_substitutes"].items():
        item = _closed_object(substitute, {"platform", "decision_ref"}, set(),
                              f"case transport map physical_substitutes.{port_id}")
        if item.get("platform") != "windows" or not isinstance(item.get("decision_ref"), str) or not item["decision_ref"]:
            raise _invalid(f"case transport map physical_substitutes.{port_id} is invalid")
    return root


def _windows_com_identity(value: str) -> str:
    """以 Windows COM 名稱的大小寫不敏感身分比對，並摺疊 Win32 裝置路徑前綴。"""
    candidate = value.strip().replace("/", "\\")
    prefix = "\\\\.\\"
    if candidate.startswith(prefix):
        candidate = candidate[len(prefix):]
    return candidate.casefold()


def _com_pair_allowlist_matches(manifest: AbManifest, actual_pair: Mapping[str, str]) -> bool:
    ports = (actual_pair.get("bridge_side"), actual_pair.get("daemon_side"))
    if any(not isinstance(port, str) or not port for port in ports):
        return False
    identities = tuple(_windows_com_identity(port) for port in ports)
    allowlist = manifest.test_device_allowlist
    if any(not isinstance(port, str) or not port for port in allowlist):
        return False
    allowlist_identities = tuple(_windows_com_identity(port) for port in allowlist)
    return (
        len(identities) == 2
        and len(set(identities)) == 2
        and len(allowlist) == 2
        and len(set(allowlist_identities)) == 2
        and set(allowlist_identities) == set(identities)
    )


def _trusted_pair_map(
    value: Mapping[str, Any], *, repo_root: Path | None = None, schema_bytes: bytes | None = None,
) -> Mapping[str, Any]:
    """驗證呼叫端私下提供的 pair 對照，schema 從同一 repo_root 讀取並凍結。"""
    if repo_root is not None:
        schema_path = Path(repo_root) / "tests" / "acceptance" / "stage3_trusted_pair_map.schema.json"
        _validate_schema_document(value, schema_path, label="trusted pair map", schema_bytes=schema_bytes)
    root = _closed_object(value, _TRUSTED_PAIR_MAP_FIELDS, set(), "trusted_pair_map")
    if (root.get("schema_version") != 1 or isinstance(root.get("schema_version"), bool)
            or root.get("exit_item_id") != "S3-L15-RELEASE-AB-WINDOWS"
            or root.get("platform") != "windows"):
        raise _invalid("trusted_pair_map identity is invalid")
    selected_pair_id = root.get("selected_pair_id")
    pairs = root.get("pairs")
    if not isinstance(selected_pair_id, str) or not selected_pair_id or not isinstance(pairs, Mapping) or not pairs:
        raise _invalid("trusted_pair_map requires selected_pair_id and pairs")
    for pair_id, pair_value in pairs.items():
        pair = _closed_object(pair_value, _TRUSTED_PAIR_FIELDS, set(), f"trusted_pair_map.pairs.{pair_id}")
        if any(not isinstance(pair.get(key), str) or not pair[key] for key in _TRUSTED_PAIR_FIELDS):
            raise _invalid(f"trusted_pair_map.pairs.{pair_id} is incomplete")
        if (_windows_com_identity(pair["dut_port"]) == _windows_com_identity(pair["daemon_port"])
                or pair["dut_side_port_id"] == pair["daemon_side_port_id"]):
            raise _invalid(f"trusted_pair_map.pairs.{pair_id} endpoints must be distinct")
    if selected_pair_id not in pairs:
        raise _invalid("trusted_pair_map selected_pair_id is missing from pairs")
    return root


def _validate_windows_l15_transport(
    manifest: AbManifest,
    repo_root: Path,
    *,
    trusted_pair_map: Mapping[str, Any] | None = None,
    trusted_pair_schema_bytes: bytes | None = None,
    read_bytes: ReadBytes | None = None,
) -> None:
    """驗 Windows L1.5 映射及可信 pair。

    正式 trusted_pair_map 由編排者於 T5B 盤點原生 COM 後建立；實際埠名只留非公開位置，
    公開證據僅留去識別端點 ID 與該對照檔 SHA-256。T3 只測合成對照。
    """
    settings = manifest.platform_settings
    map_value = settings.get("case_transport_map")
    if not isinstance(map_value, Mapping):
        raise _invalid("Windows L1.5 requires case_transport_map")
    map_file = _frozen(dict(map_value), "platform_settings.case_transport_map")
    if not isinstance(map_file, FrozenFile):
        raise _invalid("case_transport_map must be a frozen file")
    if _repo_relative_path(repo_root, map_file.path) is None:
        raise _invalid("case transport map must resolve to a regular path inside repo_root")
    matching_tools = [item for item in manifest.tools if item.path == map_file.path and isinstance(item, FrozenFile)]
    if len(matching_tools) != 1:
        raise _invalid("case_transport_map must appear exactly once in frozen tools")
    if matching_tools[0].sha256 != map_file.sha256:
        raise _invalid("case_transport_map sha256 must match its frozen tool entry")
    if (manifest.case_transport_map_loaded_sha256 != map_file.sha256
            or manifest.case_transport_map_doc is None):
        raise _invalid("case transport map loaded identity is unavailable")
    if not manifest.required_cases_lock or not manifest.exit_item_id:
        raise _invalid("Windows L1.5 requires a Stage 3 required case lock")
    case_transports = settings.get("case_transports")
    if not isinstance(case_transports, Mapping):
        raise _invalid("Windows L1.5 requires case_transports")

    try:
        _cases_doc, _case_lock, _catalog, lock, _source_manifest = required_cases_module._load_inputs(
            repo_root, read_bytes=read_bytes,
        )
        required_ids = required_cases_module.required_for_exit(lock, manifest.exit_item_id, "windows")
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise _invalid(f"cannot read required Windows L1.5 cases: {exc}") from exc
    if set(case_transports) != required_ids:
        raise _invalid("case_transports keys must exactly match required case IDs")
    if manifest.case_ids and not set(manifest.case_ids) <= required_ids:
        raise _invalid("case_ids contain IDs outside the frozen Windows exit")

    mapping = manifest.case_transport_map_doc
    if mapping["exit_item_id"] != manifest.exit_item_id or mapping["platform"] != "windows":
        raise _invalid("case transport map identity does not match manifest")
    cases = mapping["cases"]
    if set(cases) != required_ids:
        raise _invalid("case transport map cases must exactly match required case IDs")

    rows, _sets, _case_doc = _read_cases(repo_root, manifest, read_bytes=read_bytes)
    case_rows = {item.get("case_id"): item for item in rows if isinstance(item, Mapping)}
    approved_pairs = mapping["approved_pairs"]
    physical_substitutes = mapping["physical_substitutes"]
    trusted = (
        _trusted_pair_map(trusted_pair_map, repo_root=repo_root, schema_bytes=trusted_pair_schema_bytes)
        if trusted_pair_map is not None else None
    )
    selected_pair_id = trusted.get("selected_pair_id") if trusted is not None else None
    trusted_pair = trusted["pairs"].get(selected_pair_id) if trusted is not None else None
    if trusted is not None:
        if trusted["exit_item_id"] != manifest.exit_item_id or trusted["platform"] != "windows":
            raise _invalid("trusted_pair_map identity does not match manifest")
        declared_pair = approved_pairs.get(selected_pair_id)
        if not isinstance(declared_pair, Mapping) or not isinstance(trusted_pair, Mapping):
            raise _invalid("trusted_pair_map selected pair is absent from frozen map")
        for trusted_key, declared_key in (
            ("dut_side_port_id", "dut_side_port_id"),
            ("daemon_side_port_id", "daemon_side_port_id"),
            ("decision_ref", "decision_ref"),
        ):
            if trusted_pair.get(trusted_key) != declared_pair.get(declared_key):
                raise _invalid("trusted_pair_map selected pair differs from frozen approval")
    trusted_ports = None
    if trusted_pair is not None:
        trusted_ports = (_windows_com_identity(trusted_pair["dut_port"]),
                         _windows_com_identity(trusted_pair["daemon_port"]))
        if trusted_ports[0] == trusted_ports[1]:
            raise _invalid("trusted_pair_map endpoints must be distinct Windows COM ports")
    if manifest.dut.transport == "com0com-pair":
        actual_pair = manifest.dut.com_pair
        if (not isinstance(actual_pair, Mapping)
                or not _com_pair_allowlist_matches(manifest, actual_pair)):
            raise _invalid("dut.com_pair and test_device_allowlist must contain exactly two distinct COM endpoints")
        if (trusted_ports is not None
                and (_windows_com_identity(actual_pair.get("bridge_side", "")) != trusted_ports[0]
                     or _windows_com_identity(actual_pair.get("daemon_side", "")) != trusted_ports[1])):
            raise _invalid("dut.com_pair endpoints do not match trusted_pair_map")

    for case_id in sorted(required_ids):
        row = cases[case_id]
        case_transport = _closed_object(case_transports[case_id], {"transport", "port_id", "decision_ref"}, set(),
                                        f"case_transports.{case_id}")
        transport = case_transport.get("transport")
        port_id = case_transport.get("port_id")
        decision_ref = case_transport.get("decision_ref")
        if not isinstance(transport, str) or transport not in _CASE_TRANSPORTS:
            raise _invalid(f"case_transports.{case_id}.transport is invalid")
        if (port_id is not None and (not isinstance(port_id, str) or not port_id)
                or decision_ref is not None and (not isinstance(decision_ref, str) or not decision_ref)):
            raise _invalid(f"case_transports.{case_id} port_id/decision_ref is invalid")
        if transport not in row["allowed_transports"]:
            raise _invalid(f"case transport {transport} is not allowed for {case_id}")
        case_row = case_rows.get(case_id)
        if not isinstance(case_row, Mapping) or row["runner"] != case_row.get("suite"):
            raise _invalid(f"case transport map runner does not match case {case_id}")

        if row["requires_native_com"] and transport not in {"windows-native-com", "od08-physical"}:
            raise _invalid(f"native COM case {case_id} requires an approved Windows transport")
        if row["requires_native_com"] and (not port_id or not decision_ref):
            raise _invalid(f"native COM case {case_id} requires port_id and decision_ref")
        if transport == "windows-native-com":
            pair_id = row.get("pair_id")
            pair = approved_pairs.get(pair_id) if isinstance(pair_id, str) else None
            if not isinstance(pair, Mapping):
                raise _invalid(f"native COM case {case_id} has no approved pair")
            actual_pair = manifest.dut.com_pair
            if (manifest.dut.transport != "com0com-pair" or manifest.dut.com_pair is None
                    or any(not isinstance(value, str) or not value for value in actual_pair.values())
                    or not _com_pair_allowlist_matches(manifest, actual_pair)):
                raise _invalid(f"native COM case {case_id} does not match the approved pair")
            if port_id != pair["dut_side_port_id"]:
                raise _invalid(f"native COM case {case_id} port_id does not match approved DUT endpoint")
            auth = manifest.authorization
            if (auth is None or not auth.allow_destructive or auth.decision_ref != pair["decision_ref"]
                    or decision_ref != pair["decision_ref"]):
                raise _invalid(f"native COM case {case_id} lacks approved pair authorization")
            if trusted is not None:
                if pair_id != selected_pair_id:
                    raise _invalid(f"native COM case {case_id} uses a pair other than the trusted selected pair")
                actual_pair = manifest.dut.com_pair
                if (not isinstance(actual_pair, Mapping)
                        or _windows_com_identity(actual_pair.get("bridge_side", "")) != trusted_ports[0]
                        or _windows_com_identity(actual_pair.get("daemon_side", "")) != trusted_ports[1]
                        or not _com_pair_allowlist_matches(manifest, actual_pair)):
                    raise _invalid("dut.com_pair and test_device_allowlist do not match trusted_pair_map")
                if (port_id != trusted_pair["dut_side_port_id"]
                        or decision_ref != trusted_pair["decision_ref"]
                        or auth.decision_ref != trusted_pair["decision_ref"]):
                    raise _invalid(f"native COM case {case_id} does not match trusted pair identity")
        elif transport == "od08-physical":
            substitute = physical_substitutes.get(port_id) if isinstance(port_id, str) else None
            auth = manifest.authorization
            if (not row["requires_native_com"] or manifest.platform != "windows"
                    or manifest.dut.kind != "physical-substitute" or manifest.dut.transport != "physical-usb-uart"
                    or not isinstance(substitute, Mapping) or substitute.get("platform") != "windows"
                    or substitute.get("decision_ref") != decision_ref
                    or not isinstance(decision_ref, str) or not decision_ref.startswith("OD-08-")
                    or auth is None or not auth.allow_destructive or auth.decision_ref != decision_ref
                    or set(auth.boards) != set(manifest.dut.boards)):
                raise _invalid(f"OD-08 physical substitute {case_id} lacks matching authorization")
        elif row["requires_native_com"]:
            raise _invalid(f"native COM case {case_id} has an invalid transport")
        elif port_id is not None or decision_ref is not None:
            raise _invalid(f"non-COM case {case_id} must not declare a port or substitute authorization")


def _valid_templates(dut: DutSpec, sides: Sequence[AbSide], suite: SuiteSpec) -> bool:
    values = [*dut.reset_argv, *dut.fingerprint_argv, *suite.extra_args]
    for side in sides:
        values.extend(side.activate_argv)
        values.extend(side.deactivate_argv)
    for value in values:
        try:
            for _, field, _, _ in Formatter().parse(value):
                if field is not None and field not in TEMPLATE_KEYS:
                    return False
        except (TypeError, ValueError):
            return False
    return True


def _tree_sha256(root: Path, *, runtime: bool = False, read_bytes: ReadBytes | None = None) -> str:
    """計算工具來源樹摘要，排除 __pycache__ 與 *.pyc。"""
    root = Path(root)
    rows: list[tuple[str, str]] = []
    try:
        root_mode = root.lstat().st_mode
    except OSError as exc:
        raise OSError(f"tree directory missing: {root}") from exc
    if not stat.S_ISDIR(root_mode):
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", "non-regular file: .")

    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, dirs, names in os.walk(root, topdown=True, followlinks=False, onerror=raise_walk_error):
        current_path = Path(current)
        for name in list(dirs):
            path = current_path / name
            mode = path.lstat().st_mode
            rel = path.relative_to(root).as_posix()
            if _has_ascii_control(rel):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"ASCII control characters in path: {rel!r}")
            if not stat.S_ISDIR(mode):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"non-regular file: {rel}")
            if name == "__pycache__":
                dirs.remove(name)
        for name in names:
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            if _has_ascii_control(rel):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"ASCII control characters in path: {rel!r}")
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"non-regular file: {rel}")
            if path.suffix == ".pyc":
                continue
            rows.append((rel, _sha256(path, read_bytes)))
    payload = "".join(f"{rel}\0{digest}\n" for rel, digest in sorted(rows, key=lambda item: item[0].encode("utf-8")))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_TOOLS_STAGE3_IMPORT_ROOT = Path(__file__).resolve().parents[2]
try:
    _TOOLS_STAGE3_IMPORT_SHA256: str | None = _tree_sha256(_TOOLS_STAGE3_IMPORT_ROOT / "tools" / "stage3")
except (OSError, HarnessError):
    _TOOLS_STAGE3_IMPORT_SHA256 = None


def _executing_tools_identity() -> tuple[Path, str | None]:
    """回傳本模組 checkout 與 import 快照；monkeypatch 此函式僅供測試使用。"""
    return _TOOLS_STAGE3_IMPORT_ROOT, _TOOLS_STAGE3_IMPORT_SHA256


def _bundle_tree(root: Path, *, runtime: bool = False, read_bytes: ReadBytes | None = None) -> str:
    """計算 bundle 目錄摘要，並依 runtime 規則排除根目錄中繼檔。"""
    root = Path(root)
    rows: list[tuple[str, str]] = []
    try:
        root_mode = root.lstat().st_mode
    except OSError as exc:
        raise OSError(f"bundle directory missing: {root}") from exc
    if not stat.S_ISDIR(root_mode):
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", "non-regular file: .")

    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, dirs, names in os.walk(root, topdown=True, followlinks=False, onerror=raise_walk_error):
        current_path = Path(current)
        for name in list(dirs):
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            if _has_ascii_control(rel):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"ASCII control characters in path: {rel!r}")
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"non-regular file: {rel}")
        for name in names:
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            if _has_ascii_control(rel):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"ASCII control characters in path: {rel!r}")
            if not stat.S_ISREG(path.lstat().st_mode):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"non-regular file: {rel}")
            if runtime and rel in {"release-manifest.json", "SHA256SUMS"}:
                continue
            rows.append((rel, _sha256(path, read_bytes)))
    payload = "".join(f"{rel}\0{digest}\n" for rel, digest in sorted(rows, key=lambda item: item[0].encode("utf-8")))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def bundle_tree_sha256(bundle_dir: Path) -> str:
    return _bundle_tree(Path(bundle_dir))


def runtime_tree_sha256(bundle_dir: Path) -> str:
    return _bundle_tree(Path(bundle_dir), runtime=True)


def _package_root(bundle: Path, components: Mapping[str, Any], component: Mapping[str, Any]) -> Path | None:
    runtime = components.get("daemon")
    if isinstance(runtime, Mapping):
        runtime_doc = runtime.get("runtime")
        if isinstance(runtime_doc, Mapping) and isinstance(runtime_doc.get("path"), str):
            return _path(bundle, runtime_doc["path"]).parent.parent
    entry = component.get("entry")
    if isinstance(entry, (list, tuple)) and entry and isinstance(entry[0], str):
        if "python_cli_delegate" in components and component is components.get("python_cli_delegate"):
            return _path(bundle, entry[0]).parent.parent
        return _path(bundle, entry[0]).parent.parent
    return None


def _python_package_tree_sha256(py_root: Path, *, read_bytes: ReadBytes | None = None) -> str:
    """依 release manifest 契約計算僅含 sw_core/ 的套件樹摘要。"""
    rows = []
    for path in _python_package_tree_members(py_root):
        rel = "sw_core/" + path.relative_to(Path(py_root) / "sw_core").as_posix()
        rows.append((rel, _sha256(path, read_bytes)))
    payload = "".join(f"{rel}\0{digest}\n" for rel, digest in sorted(rows, key=lambda item: item[0].encode("utf-8")))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _python_package_tree_members(py_root: Path) -> list[Path]:
    """列出套件樹身分成員，沿用 release 契約的快取排除與一般檔限制。"""
    package = Path(py_root) / "sw_core"
    members: list[Path] = []
    try:
        mode = package.lstat().st_mode
    except OSError as exc:
        raise OSError(f"Python package directory missing: {package}") from exc
    if not stat.S_ISDIR(mode):
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", "non-regular file: sw_core")

    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, dirs, names in os.walk(package, topdown=True, followlinks=False, onerror=raise_walk_error):
        current_path = Path(current)
        for name in list(dirs):
            path = current_path / name
            rel = "sw_core/" + path.relative_to(package).as_posix()
            if _has_ascii_control(rel):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"ASCII control characters in path: {rel!r}")
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"non-regular file: {rel}")
            if name == "__pycache__":
                dirs.remove(name)
        for name in names:
            path = current_path / name
            rel = "sw_core/" + path.relative_to(package).as_posix()
            if _has_ascii_control(rel):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"ASCII control characters in path: {rel!r}")
            if not stat.S_ISREG(path.lstat().st_mode):
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"non-regular file: {rel}")
            if path.suffix == ".pyc":
                continue
            members.append(path)
    return members


def runtime_digest(manifest_doc: Mapping[str, Any]) -> str:
    components = _components(manifest_doc)
    cli = components.get("cli") if isinstance(components.get("cli"), Mapping) else {}
    daemon = components.get("daemon") if isinstance(components.get("daemon"), Mapping) else {}
    launcher = daemon.get("launcher") if isinstance(daemon.get("launcher"), Mapping) else {}
    runtime = daemon.get("runtime") if isinstance(daemon.get("runtime"), Mapping) else {}
    package = daemon.get("package") if isinstance(daemon.get("package"), Mapping) else {}
    assets = components.get("assets")
    supervisor = components.get("supervisor") if isinstance(components.get("supervisor"), Mapping) else {}
    identity = {
        "cli": cli.get("sha256"),
        "daemon_impl": daemon.get("implementation"),
        "daemon": launcher.get("sha256"),
        "runtime": runtime.get("sha256"),
        "package_tree": package.get("tree_sha256"),
        "assets": _plain_value(assets),
        "supervisor": supervisor.get("definition_sha256"),
    }
    return hashlib.sha256(dumps_compact(identity).encode("utf-8")).hexdigest()


def _declared_files(value: Any, path: tuple[str, ...] = ()) -> list[tuple[str, Mapping[str, Any]]]:
    """列出形狀像檔案宣告的元件；只含具名 digest 的 assets 字串不會成為檔案。"""
    found: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(value, Mapping):
        if "path" in value or "sha256" in value:
            found.append((".".join(path), value))
        else:
            for key, child in value.items():
                found.extend(_declared_files(child, path + (key,)))
    elif isinstance(value, (list, tuple)):
        for i, child in enumerate(value):
            found.extend(_declared_files(child, path + (str(i),)))
    return found


def verify_bundle(
    bundle_dir: Path, manifest_doc: Mapping[str, Any], *, label: str,
    read_bytes: ReadBytes | None = None,
) -> list[str]:
    bundle = Path(bundle_dir).resolve()
    components = _components(manifest_doc)
    violations: list[str] = []
    package_tree_digests: dict[Path, str] = {}
    for component, obj in _declared_files(components):
        raw_path = obj.get("path")
        digest = obj.get("sha256")
        if (not isinstance(raw_path, str) or not raw_path
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            violations.append(f"release component identity invalid: {label} {component}")
            continue
        if _has_ascii_control(raw_path):
            violations.append(f"release component path contains ASCII control characters: {label} {component}")
            continue
        pure = PurePosixPath(raw_path)
        win = PureWindowsPath(raw_path)
        if pure.is_absolute() or win.is_absolute() or ".." in pure.parts or "\\" in raw_path:
            violations.append(f"release component path escapes bundle: {label} {component}")
            continue
        path = bundle.joinpath(*pure.parts)
        if not _inside(bundle, path):
            violations.append(f"release component path escapes bundle: {label} {component}")
            continue
        try:
            mode = path.lstat().st_mode
        except OSError:
            violations.append(f"file missing: {path}")
            continue
        if not stat.S_ISREG(mode):
            violations.append(f"release component non-regular file: {label} {component}")
            continue
        try:
            actual = _sha256(path, read_bytes)
        except OSError:
            violations.append(f"file missing: {path}")
            continue
        if actual != digest:
            violations.append(f"release component sha256 mismatch: {label} {component}")

    daemon = components.get("daemon") if isinstance(components.get("daemon"), Mapping) else {}
    package = daemon.get("package") if isinstance(daemon.get("package"), Mapping) else {}
    delegate = components.get("python_cli_delegate") if isinstance(components.get("python_cli_delegate"), Mapping) else {}
    for component, definition in (("daemon.package", package), ("python_cli_delegate", delegate)):
        expected = definition.get("tree_sha256")
        if not isinstance(expected, str):
            continue
        root = _package_root(bundle, components, definition)
        if root is None:
            violations.append(f"release tree mismatch: {label} {component}")
            continue
        try:
            root_key = root.resolve()
            actual = package_tree_digests.get(root_key)
            if actual is None:
                actual = _python_package_tree_sha256(root, read_bytes=read_bytes)
                package_tree_digests[root_key] = actual
        except (OSError, HarnessError):
            violations.append(f"release tree mismatch: {label} {component}")
            continue
        if actual != expected:
            violations.append(f"release tree mismatch: {label} {component}")
    return violations


def _read_cases(
    repo_root: Path, m: AbManifest, *, read_bytes: ReadBytes | None = None,
) -> tuple[list[dict], Mapping[str, Any], Mapping[str, Any]]:
    try:
        cases_bytes = _read_bytes(_path(repo_root, m.case_list.path), read_bytes)
        lock_bytes = _read_bytes(_path(repo_root, m.case_lock.path), read_bytes)
        cases_doc = required_cases_module.yaml.safe_load(cases_bytes.decode("utf-8"))
        lock = json.loads(lock_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, required_cases_module.yaml.YAMLError, RecursionError) as exc:
        raise _invalid(f"cannot read frozen cases: {exc}") from exc
    if not isinstance(cases_doc, dict) or not isinstance(cases_doc.get("cases"), list):
        raise _invalid("cases.yaml must contain cases list")
    if not isinstance(lock, dict) or not isinstance(lock.get("sets"), dict):
        raise _invalid("cases.lock.json must contain sets")
    return cases_doc["cases"], lock["sets"], cases_doc


def _case_layer_matches(row: Mapping[str, Any], layer: str) -> bool:
    value = row.get("layer")
    return isinstance(value, str) and value.casefold() == layer.casefold()


def required_cases(
    m: AbManifest, *, repo_root: Path, base_revision: str | None = None, source_reader: Any = None,
    read_bytes: ReadBytes | None = None,
) -> tuple[set[str], dict[str, dict]]:
    root = Path(repo_root).resolve()
    if root != m.repo_root:
        raise _invalid(f"manifest repo_root mismatch: loaded {m.repo_root} supplied {root}")
    snapshot = _InputSnapshot(read_bytes)
    reader = snapshot.read_bytes
    if m.stage == "3":
        try:
            for frozen, relative, label in (
                (m.case_list, "tests/acceptance/cases.yaml", "case_list"),
                (m.case_lock, "tests/acceptance/cases.lock.json", "case_lock"),
            ):
                _require_canonical_path(root, frozen, relative, label)
                if hashlib.sha256(reader(_path(root, frozen.path))).hexdigest() != frozen.sha256:
                    raise HarnessError("HARNESS_CASE_SET_INCOMPLETE", f"required cases invalid: {label} sha256 mismatch")
            _validate_stage3_identity(m, root, read_bytes=reader)
            if m.required_cases_lock is None:
                raise _invalid("Stage 3 requires required_cases_lock")
            lock_path = _path(root, m.required_cases_lock.path)
            if hashlib.sha256(reader(lock_path)).hexdigest() != m.required_cases_lock.sha256:
                raise HarnessError("HARNESS_CASE_SET_INCOMPLETE", "required cases invalid: required cases lock sha256 mismatch")
            catalog_path = root / "tests" / "acceptance" / "stage_exits.json"
            if hashlib.sha256(reader(catalog_path)).hexdigest() != m.exit_catalog_sha256:
                raise HarnessError("HARNESS_CASE_SET_INCOMPLETE", "required cases invalid: exit catalog sha256 mismatch")
        except HarnessError:
            raise
        except OSError as exc:
            raise HarnessError("HARNESS_CASE_SET_INCOMPLETE", f"required cases invalid: {exc}") from exc
        errors = required_cases_module.validate_current_lock(
            root, base_revision=base_revision, source_reader=source_reader, read_bytes=reader,
        )
        if errors:
            raise HarnessError("HARNESS_CASE_SET_INCOMPLETE", "required cases invalid: " + "; ".join(errors))

    rows, sets, _cases_doc = _read_cases(root, m, read_bytes=reader)
    by_id = {row.get("case_id"): row for row in rows if isinstance(row, dict) and isinstance(row.get("case_id"), str)}
    if m.stage == "3":
        if not m.exit_item_id or not m.required_cases_lock:
            raise _invalid("Stage 3 requires exit_item_id and required_cases_lock")
        try:
            lock_path = _path(root, m.required_cases_lock.path)
            lock = json.loads(reader(lock_path).decode("utf-8"))
            selected = required_cases_module.required_for_exit(lock, m.exit_item_id, m.platform)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, RecursionError) as exc:
            raise _invalid(f"cannot read Stage 3 required cases: {exc}") from exc
        return selected, by_id
    if m.required_case_set not in sets or not isinstance(sets[m.required_case_set], list):
        raise _invalid(f"unknown required case set: {m.required_case_set}")
    required: set[str] = set()
    for case_id in sets[m.required_case_set]:
        row = by_id.get(case_id)
        if not isinstance(row, dict):
            continue
        platforms = row.get("platforms")
        if _case_layer_matches(row, m.layer) and isinstance(platforms, list) and m.platform in platforms:
            required.add(case_id)
    return required, by_id


def _stage3_lock_errors(
    m: AbManifest, repo_root: Path, *, base_revision: str | None, source_reader: Any,
    read_bytes: ReadBytes | None = None,
) -> list[str]:
    """共用 T1 驗證器重跑目前目錄與 seed；base_revision 僅在呼叫端提供時補做只增比較。"""
    return required_cases_module.validate_current_lock(
        repo_root, base_revision=base_revision, source_reader=source_reader, read_bytes=read_bytes,
    )


def verify_case_coverage(
    m: AbManifest, *, repo_root: Path, base_revision: str | None = None, source_reader: Any = None,
    read_bytes: ReadBytes | None = None,
) -> list[str]:
    root = Path(repo_root).resolve()
    if root != m.repo_root:
        raise _invalid(f"manifest repo_root mismatch: loaded {m.repo_root} supplied {root}")
    snapshot = _InputSnapshot(read_bytes)
    reader = snapshot.read_bytes
    rows, _sets, case_doc = _read_cases(root, m, read_bytes=reader)
    by_id = {row.get("case_id"): row for row in rows if isinstance(row, dict) and isinstance(row.get("case_id"), str)}
    required, _ = required_cases(
        m, repo_root=root, base_revision=base_revision, source_reader=source_reader, read_bytes=reader,
    )
    selected = set(m.case_ids)
    violations = [f"required case missing: {case_id}" for case_id in sorted(required - selected)]
    violations.extend(f"unknown case: {case_id}" for case_id in sorted(selected - by_id.keys()))

    declared = case_doc.get("invariant_ids", []) if isinstance(case_doc, dict) else []
    for invariant in _INVARIANTS:
        if invariant not in declared:
            continue
        covered = any(
            case_id in by_id
            and _case_layer_matches(by_id[case_id], m.layer)
            and isinstance(by_id[case_id].get("platforms"), list)
            and m.platform in by_id[case_id]["platforms"]
            and invariant in (by_id[case_id].get("invariants") or [])
            for case_id in selected
        )
        exists_for_layer = any(
            isinstance(row, dict) and _case_layer_matches(row, m.layer)
            and invariant in (row.get("invariants") or [])
            for row in rows
        )
        if exists_for_layer and not covered:
            violations.append(f"invariant not covered: {invariant}")

    auth = m.authorization
    for case_id in sorted(selected):
        row = by_id.get(case_id)
        if (not isinstance(row, dict) or not _case_layer_matches(row, m.layer)
                or not isinstance(row.get("platforms"), list) or m.platform not in row["platforms"]
                or row.get("destructive") is not True):
            continue
        if auth is None or not auth.allow_destructive or not auth.decision_ref:
            violations.append(f"destructive case without authorization: {case_id}")
    if auth is not None:
        violations.extend(f"authorization board not in dut: {board}" for board in auth.boards if board not in m.dut.boards)
    if m.platform == "windows" and m.dut.transport == "com0com-pair" and not m.test_device_allowlist:
        violations.append("windows com0com instance without test_device_allowlist")
    return violations


def _repo_relative_path(
    root: Path | PureWindowsPath, value: str, *, windows: bool | None = None,
) -> str | None:
    """完成平台原生邊界／symlink 檢查，再回傳 Git 使用的 POSIX 相對路徑。

    ``windows`` 僅供純路徑測試在非 Windows 主機模擬原生語意；正式呼叫依執行平台選擇。
    """
    if not isinstance(value, str) or not value:
        return None
    if _has_ascii_control(value):
        return None
    use_windows = os.name == "nt" if windows is None else windows
    windows_value = PureWindowsPath(value)
    windows_parts = windows_value.parts
    raw_parts = re.split(r"[\\/]", value)
    if any(part in (".", "..") for part in raw_parts) or any(part in (".", "..") for part in windows_parts) or any(
        part in (".", "..") for part in PurePosixPath(value).parts
    ):
        return None

    if use_windows:
        # 純路徑模擬只驗證 Windows 的磁碟、分隔符及詞法邊界；原生執行另做 realpath/symlink 檢查。
        if isinstance(root, PureWindowsPath) or (windows is True and os.name != "nt"):
            root_windows = PureWindowsPath(str(root))
            if (not root_windows.is_absolute() or (windows_value.drive and not windows_value.is_absolute())
                    or (windows_value.root and not windows_value.drive)):
                return None
            candidate_windows = windows_value if windows_value.is_absolute() else root_windows / windows_value
            try:
                relative_windows = candidate_windows.relative_to(root_windows)
            except ValueError:
                return None
            if any(part in ("", ".", "..") for part in relative_windows.parts):
                return None
            return PurePosixPath(*relative_windows.parts).as_posix()

        root_path = Path(root)
        if windows_value.drive and not windows_value.is_absolute():
            return None
        candidate = Path(value) if Path(value).is_absolute() else root_path / Path(value)
        try:
            root_absolute = Path(os.path.abspath(root_path))
            absolute = Path(os.path.abspath(candidate))
            relative = absolute.relative_to(root_absolute)
            absolute.resolve().relative_to(root_absolute.resolve())
        except (OSError, ValueError):
            return None
        pure = PurePosixPath(relative.as_posix())
        if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
            return None
        return pure.as_posix()

    # POSIX 上將 Windows drive/UNC/根路徑視為跨平台偽造；一般反斜線不會單獨造成拒絕。
    if windows_value.drive or (windows_value.root and not Path(value).is_absolute()):
        return None
    candidate = _path(Path(root), value)
    try:
        root_absolute = Path(os.path.abspath(root))
        absolute = Path(os.path.abspath(candidate))
        relative = absolute.relative_to(root_absolute)
        absolute.resolve().relative_to(root_absolute.resolve())
    except (OSError, ValueError):
        return None
    pure = PurePosixPath(relative.as_posix())
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        return None
    return pure.as_posix()


def _frozen_repo_paths(
    m: AbManifest, repo_root: Path,
) -> tuple[set[str], set[str], set[str], list[str]]:
    """分類版本受控檔、版本受控樹與可由 SHA 凍結的執行產物。"""
    files: set[str] = set()
    trees: set[str] = set()
    execution_files: set[str] = set()
    path_errors: list[str] = []

    def add_required_file(item: FrozenFile | None, label: str) -> None:
        if item is None:
            return
        if _has_ascii_control(item.path):
            path_errors.append(f"tool revision path contains ASCII control characters: {item.path!r}")
            return
        relative = _repo_relative_path(repo_root, item.path)
        if relative is None:
            path_errors.append(f"tool revision path outside repo: {label} {item.path}")
            return
        files.add(relative)

    def add_execution_file(item: FrozenFile | None) -> None:
        if item is None:
            return
        if _has_ascii_control(item.path):
            path_errors.append(f"tool revision path contains ASCII control characters: {item.path!r}")
            return
        relative = _repo_relative_path(repo_root, item.path)
        if relative is None:
            try:
                resolved = _path(repo_root, item.path).resolve()
                relative = resolved.relative_to(Path(repo_root).resolve()).as_posix()
            except (OSError, ValueError):
                relative = None
        if relative is not None:
            execution_files.add(relative)

    def add_tree(item: FrozenTree, label: str) -> None:
        if _has_ascii_control(item.path):
            path_errors.append(f"tool revision path contains ASCII control characters: {item.path!r}")
            return
        relative = _repo_relative_path(repo_root, item.path)
        if relative is None:
            path_errors.append(f"tool revision path outside repo: {label} {item.path}")
            return
        trees.add(relative)

    # 即使呼叫端弱化 manifest schema，也必須錨定 loader 的程式樹與實際 schema。
    trees.add("tools/stage3")
    files.add("tests/acceptance/ab_manifest.schema.json")
    if m.exit_item_id == "S3-L15-RELEASE-AB-WINDOWS":
        files.add("tests/acceptance/stage3_trusted_pair_map.schema.json")

    add_required_file(m.case_list, "case_list")
    add_required_file(m.case_lock, "case_lock")
    add_execution_file(FrozenFile(str(m.source_path), m.source_sha256))
    add_execution_file(m.candidate_context)
    add_required_file(m.required_cases_lock, "required_cases_lock")
    add_required_file(m.allowed_differences, "allowed_differences")
    add_required_file(m.acceptance, "acceptance")
    for item in m.profile_files:
        if _has_ascii_control(item.path):
            path_errors.append(f"tool revision path contains ASCII control characters: {item.path!r}")
            continue
        relative = _repo_relative_path(repo_root, item.path)
        if relative is not None:
            files.add(relative)
        else:
            candidate = _path(repo_root, item.path)
            is_absolute = candidate.is_absolute() or PureWindowsPath(item.path).is_absolute()
            if not is_absolute or _inside(repo_root, candidate):
                path_errors.append(f"tool revision path outside repo: profile {item.path}")
    for item in (m.suite.selftest_report, m.suite.testbed_template, m.suite.realhw_config_template):
        add_execution_file(item)
    for item in m.tools:
        if isinstance(item, FrozenFile):
            add_required_file(item, "tool")
        else:
            add_tree(item, "tool tree")
    for side in m.sides:
        add_execution_file(side.release_manifest)
        components = _components(side.manifest_doc)
        for _component, component_ref in _declared_files(components):
            raw_path = component_ref.get("path")
            if isinstance(raw_path, str):
                absolute = side.bundle_dir.joinpath(*PurePosixPath(raw_path).parts)
                add_execution_file(FrozenFile(str(absolute), "0" * 64))
        daemon = components.get("daemon") if isinstance(components.get("daemon"), Mapping) else {}
        package = daemon.get("package") if isinstance(daemon.get("package"), Mapping) else {}
        delegate = (
            components.get("python_cli_delegate")
            if isinstance(components.get("python_cli_delegate"), Mapping) else {}
        )
        for definition in (package, delegate):
            if not isinstance(definition.get("tree_sha256"), str):
                continue
            package_root = _package_root(side.bundle_dir, components, definition)
            if package_root is None:
                continue
            try:
                members = _python_package_tree_members(package_root)
            except (OSError, HarnessError):
                continue
            for member in members:
                add_execution_file(FrozenFile(str(member), "0" * 64))

    if m.stage == "3":
        for relative in (
            "tests/acceptance/stage_exits.json",
            "tests/acceptance/stage3_required_cases.schema.json",
            "docs/rust-refactor/evidence/denominators/source-manifest.json",
        ):
            if _repo_relative_path(repo_root, relative) is None:
                path_errors.append(f"tool revision path outside repo: Stage 3 input {relative}")
            else:
                files.add(relative)
        # contract_revision 凍結整個 contracts tree；tool_revision 必須同時錨定其原始檔位元組。
        trees.add("contracts")
        source_doc = m.stage3_source_manifest_doc
        sources = source_doc.get("sources") if isinstance(source_doc, Mapping) else None
        if isinstance(sources, (list, tuple)):
            evidence_root = "docs/rust-refactor/evidence"
            for source in sources:
                if not isinstance(source, Mapping):
                    continue
                attachment = source.get("attachment")
                if isinstance(attachment, str):
                    relative = _repo_relative_path(repo_root, f"{evidence_root}/{attachment}")
                    if relative is None:
                        path_errors.append(f"tool revision path outside repo: seed attachment {attachment}")
                    else:
                        files.add(relative)

    map_ref = m.platform_settings.get("case_transport_map")
    if isinstance(map_ref, Mapping) and isinstance(map_ref.get("path"), str):
        add_required_file(_frozen(dict(map_ref), "platform_settings.case_transport_map"), "case transport map")

    execution_files -= files
    return files, trees, execution_files, path_errors


def _tool_revision_for_checks(repo_root: Path, run: Runner, tool_revision: str | None) -> str | None:
    revision = tool_revision
    if revision is None:
        try:
            result = run(["git", "-C", str(repo_root), "rev-parse", "--verify", "HEAD"])
        except OSError:
            return None
        if result.rc != 0 or result.timed_out:
            return None
        revision = result.stdout.strip()
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        return None
    try:
        result = run(["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"])
    except OSError:
        return None
    if result.rc != 0 or result.timed_out or result.stdout.strip() != revision:
        return None
    return revision


def _tool_revision_path_inventory(
    *, repo_root: Path, revision: str, run: Runner,
) -> tuple[frozenset[str], frozenset[str]] | None:
    """一次取得 index 與 revision 路徑，供所有身分路徑共用分類。"""
    try:
        indexed = run(["git", "-C", str(repo_root), "ls-files", "-z"], bytes_output=True)
        revision_paths = run([
            "git", "-C", str(repo_root), "ls-tree", "-r", "--name-only", "-z", revision,
        ], bytes_output=True)
    except (OSError, TypeError):
        return None
    if any(result.rc != 0 or result.timed_out for result in (indexed, revision_paths)):
        return None
    try:
        return (
            frozenset(_parse_git_path_list(indexed.stdout)),
            frozenset(_parse_git_path_list(revision_paths.stdout)),
        )
    except (TypeError, UnicodeError, ValueError):
        return None


def _parse_git_path_list(output: bytes) -> tuple[str, ...]:
    """解析 Git 的 NUL 路徑清單並拒絕控制字元或無法對應的 inventory 項目。

    Git 路徑輸出必須以 bytes 擷取，否則文字模式會正規化 `\r`，讓路徑索引失真。
    對含 ASCII 控制字元的 repo 路徑採保守拒絕，避免其在 inventory 中被誤認為另一個名稱。
    """
    if not isinstance(output, bytes):
        raise TypeError("Git NUL path output must be bytes")
    if output == b"":
        return ()
    if not output.endswith(b"\0"):
        raise ValueError("Git path inventory is not NUL terminated")
    entries = output[:-1].split(b"\0")
    paths: list[str] = []
    for entry in entries:
        if not entry:
            raise ValueError("empty Git path entry")
        path = os.fsdecode(entry)
        if any(0xDC80 <= ord(char) <= 0xDCFF for char in path):
            raise ValueError(f"unmappable Git path entry: {path!r}")
        if _has_ascii_control(path):
            raise ValueError(f"Git path contains ASCII control characters: {path!r}")
        if path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/")):
            raise ValueError(f"unmappable Git path entry: {path!r}")
        paths.append(path)
    return tuple(paths)


def _tool_revision_input_errors(
    *, repo_root: Path, relative: str, revision: str, run: Runner,
    map_path: str | None = None, execution_artifact: bool = False,
    path_inventory: tuple[frozenset[str], frozenset[str]] | None = None,
    read_bytes: ReadBytes | None = None,
) -> list[str]:
    """統一分類 repo 內身分路徑，再以指定 revision 的原始 blob 比對內容。"""
    if _has_ascii_control(relative):
        return [f"tool revision path contains ASCII control characters: {relative!r}"]
    if not relative.isascii():
        # Windows 檔案系統的 Unicode 大小寫表無法在 Linux 可靠重現；保守拒絕
        # 才不會把非 ASCII 大小寫別名誤認成未追蹤路徑。ASCII 路徑仍走 icase 查詢。
        return [f"tool revision path contains non-ASCII characters: {relative}"]
    path = repo_root.joinpath(*PurePosixPath(relative).parts)
    prefix = "case transport map tool revision" if relative == map_path else "tool revision"
    requested_identity = relative.replace("\\", "/").casefold()
    if path_inventory is None:
        query = f":(icase,literal){relative}"
        try:
            indexed = run(["git", "-C", str(repo_root), "ls-files", "-z", "--", query], bytes_output=True)
            revision_result = run([
                "git", "-C", str(repo_root), "ls-tree", "-r", "--name-only", "-z", revision,
            ], bytes_output=True)
        except (OSError, TypeError):
            return [f"tool revision unavailable: {relative}"]
        if any(result.rc != 0 or result.timed_out for result in (indexed, revision_result)):
            return [f"tool revision unavailable: {relative}"]
        try:
            index_paths = set(_parse_git_path_list(indexed.stdout))
            revision_members = set(_parse_git_path_list(revision_result.stdout))
        except (TypeError, UnicodeError, ValueError):
            return [f"tool revision unavailable: {relative}"]
    else:
        index_paths, revision_members = path_inventory
    index_matches = {
        item for item in index_paths
        if item.replace("\\", "/").casefold() == requested_identity
    }
    revision_matches = {
        item for item in revision_members
        if item.replace("\\", "/").casefold() == requested_identity
    }
    matches = index_matches | revision_matches
    if len(matches) > 1:
        return [f"tool revision path case ambiguous: {relative}"]
    if matches:
        tracked_path = next(iter(matches))
        if tracked_path != relative:
            return [f"tool revision path case mismatch: {relative} (tracked as {tracked_path})"]
    tracked_in_revision = relative in revision_matches
    if execution_artifact and not matches:
        # 只有 index 與指定 revision 都沒有大小寫等價路徑，才是可豁免的未追蹤執行產物。
        return []
    if not tracked_in_revision:
        return [f"{prefix} mismatch: {relative}"]
    try:
        mode = path.lstat().st_mode
    except OSError:
        return [f"{prefix} mismatch: {relative}"]
    if not stat.S_ISREG(mode):
        return [f"{prefix} mismatch: {relative}"]
    try:
        expected = run([
            "git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet", f"{revision}:{relative}",
        ])
    except OSError:
        return [f"{prefix} unavailable: {relative}"]
    if expected.timed_out:
        return [f"{prefix} unavailable: {relative}"]
    expected_oid = expected.stdout.strip()
    if expected.rc == 1 and not expected_oid:
        return [f"{prefix} mismatch: {relative}"]
    if expected.rc != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", expected_oid):
        return [f"{prefix} unavailable: {relative}"]
    try:
        raw = _read_bytes(path, read_bytes)
    except OSError:
        return [f"{prefix} unavailable: {relative}"]
    object_hash = hashlib.sha256 if len(expected_oid) == 64 else hashlib.sha1
    header = f"blob {len(raw)}\0".encode("ascii")
    actual_oid = object_hash(header + raw).hexdigest()
    if expected_oid != actual_oid:
        return [f"{prefix} mismatch: {relative}"]
    return []


def _tool_revision_tree_errors(
    *, repo_root: Path, tree: str, revision: str, run: Runner,
    path_inventory: tuple[frozenset[str], frozenset[str]] | None = None,
    read_bytes: ReadBytes | None = None,
) -> list[str]:
    """以完整 revision 成員集合和工作樹雙向比較，再逐檔比原始 blob。"""
    root = repo_root.joinpath(*PurePosixPath(tree).parts)
    prefix = f"tool revision tree mismatch: {tree}"
    try:
        mode = root.lstat().st_mode
    except OSError:
        return [f"{prefix} missing tree"]
    if not stat.S_ISDIR(mode):
        return [f"{prefix} non-directory tree"]
    try:
        tree_oid = run([
            "git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet", f"{revision}:{tree}",
        ])
        tree_type = run([
            "git", "-C", str(repo_root), "cat-file", "-t", tree_oid.stdout.strip(),
        ]) if tree_oid.rc == 0 and not tree_oid.timed_out else None
        listed = run([
            "git", "-C", str(repo_root), "ls-tree", "-r", "--name-only", "-z", revision,
            "--", f":(literal){tree}",
        ], bytes_output=True)
    except (OSError, TypeError):
        return [f"tool revision unavailable: {tree}"]
    if (tree_oid.rc != 0 or tree_oid.timed_out
            or not re.fullmatch(r"[0-9a-f]{40,64}", tree_oid.stdout.strip())
            or tree_type is None or tree_type.rc != 0 or tree_type.timed_out or tree_type.stdout.strip() != "tree"
            or listed.rc != 0 or listed.timed_out):
        return [f"tool revision unavailable: {tree}"]
    try:
        listed_paths = _parse_git_path_list(listed.stdout)
    except (TypeError, UnicodeError, ValueError):
        return [f"tool revision unavailable: {tree}"]
    revision_members = {
        item for item in listed_paths
        if "__pycache__" not in PurePosixPath(item).parts and not item.endswith(".pyc")
    }

    working_members: set[str] = set()

    def raise_walk_error(error: OSError) -> None:
        raise error

    try:
        for current, dirs, names in os.walk(root, topdown=True, followlinks=False, onerror=raise_walk_error):
            current_path = Path(current)
            for name in list(dirs):
                directory = current_path / name
                if not stat.S_ISDIR(directory.lstat().st_mode):
                    return [f"{prefix} non-regular file: {directory.relative_to(repo_root).as_posix()}"]
                if name == "__pycache__":
                    dirs.remove(name)
            for name in names:
                path = current_path / name
                if not stat.S_ISREG(path.lstat().st_mode):
                    return [f"{prefix} non-regular file: {path.relative_to(repo_root).as_posix()}"]
                if path.suffix == ".pyc":
                    continue
                relative = path.relative_to(repo_root).as_posix()
                if _has_ascii_control(relative):
                    return [f"tool revision path contains ASCII control characters: {relative!r}"]
                working_members.add(relative)
    except OSError:
        return [f"{prefix} unreadable tree"]

    missing = sorted(revision_members - working_members)
    extra = sorted(working_members - revision_members)
    violations = [f"{prefix} missing {relative}" for relative in missing]
    violations.extend(f"{prefix} extra {relative}" for relative in extra)
    for relative in sorted(revision_members | working_members):
        violations.extend(_tool_revision_input_errors(
            repo_root=repo_root, relative=relative, revision=revision, run=run,
            path_inventory=path_inventory, read_bytes=read_bytes,
        ))
    return violations


def _tool_revision_frozen_errors(
    m: AbManifest, *, repo_root: Path, run: Runner, tool_revision: str | None,
    read_bytes: ReadBytes | None = None,
) -> list[str]:
    """以同一輸入比較器錨定檔案、完整樹與明確豁免的執行產物。"""
    revision = _tool_revision_for_checks(repo_root, run, tool_revision)
    if revision is None:
        return ["tool revision unavailable"]
    files, trees, execution_files, path_errors = _frozen_repo_paths(m, repo_root)
    path_inventory = _tool_revision_path_inventory(repo_root=repo_root, revision=revision, run=run)
    if path_inventory is None:
        return [
            *(f"tool revision unavailable: {relative}" for relative in sorted(files | execution_files)),
            *(f"tool revision unavailable: {tree}" for tree in sorted(trees)),
            *path_errors,
        ]
    map_ref = m.platform_settings.get("case_transport_map")
    map_path = None
    if isinstance(map_ref, Mapping) and isinstance(map_ref.get("path"), str):
        map_path = _repo_relative_path(repo_root, map_ref["path"])
    violations: list[str] = list(path_errors)
    for relative in sorted(files):
        violations.extend(_tool_revision_input_errors(
            repo_root=repo_root, relative=relative, revision=revision, run=run, map_path=map_path,
            path_inventory=path_inventory, read_bytes=read_bytes,
        ))
    for tree in sorted(trees):
        violations.extend(_tool_revision_tree_errors(
            repo_root=repo_root, tree=tree, revision=revision, run=run,
            path_inventory=path_inventory, read_bytes=read_bytes,
        ))
    for relative in sorted(execution_files):
        violations.extend(_tool_revision_input_errors(
            repo_root=repo_root,
            relative=relative,
            revision=revision,
            run=run,
            map_path=map_path,
            execution_artifact=True,
            path_inventory=path_inventory,
            read_bytes=read_bytes,
        ))
    return violations


def _executing_tools_stage3_errors(
    m: AbManifest, executing_tools_root: Path, import_snapshot: str | None,
    *, read_bytes: ReadBytes | None = None,
) -> list[str]:
    """要求 import 快照、目前磁碟樹與已錨定工具樹的摘要三者一致。"""
    declared = [
        item for item in m.tools
        if isinstance(item, FrozenTree) and item.path == "tools/stage3"
    ]
    if len(declared) != 1:
        return ["executing tools/stage3 tree declaration missing or ambiguous"]
    expected = declared[0].tree_sha256
    tree_root = Path(executing_tools_root) / "tools" / "stage3"
    try:
        actual = _tree_sha256(tree_root, read_bytes=read_bytes)
    except (OSError, HarnessError):
        return ["executing tools/stage3 tree unavailable"]
    violations = []
    if import_snapshot is None or actual != import_snapshot:
        violations.append(
            f"executing tools/stage3 import snapshot mismatch: imported {import_snapshot} current {actual}"
        )
    if actual != expected:
        violations.append(f"executing tools/stage3 tree mismatch: expected {expected} actual {actual}")
    return violations


def verify_frozen(
    m: AbManifest, *, repo_root: Path, run: Runner = run_cmd, tool_revision: str | None = None,
    base_revision: str | None = None, source_reader: Any = None,
    trusted_pair_map: Mapping[str, Any] | None = None,
) -> list[str]:
    """重算凍結輸入、bundle 元件及案例涵蓋。

    凍結輸入分類（plan 10 Task 3；Stage 3 另依 §0.4）：

    | 輸入 | 類別與依據 | 路徑規則 | 比對方式／測試 |
    |---|---|---|---|
    | A/B manifest schema | 版本受控；Task 3 loader 契約 | 固定為 repo_root 下 `tests/acceptance/ab_manifest.schema.json` | loader 從此路徑讀取；tool_revision 同路徑原始 blob；`test_manifest_and_trusted_pair_schemas_are_checked_against_tool_revision` |
    | trusted pair map schema | 版本受控；R12-01、BO-P9-01 注入介面 | Windows L1.5 固定為 repo_root 下 `tests/acceptance/stage3_trusted_pair_map.schema.json` | 注入時從此路徑讀取；tool_revision 同路徑原始 blob；同上 |
    | case_list、case_lock | 版本受控；Task 3 案例涵蓋 | 固定為 canonical repo 路徑 | 同路徑原始 blob；`test_tool_revision_rejects_self_hashed_canonical_case_attribute_changes` |
    | required_cases_lock、required_case_set、case_ids、seed | 版本受控；Task 3、R4-01 | 鎖與 seed 附件只能位於 repo_root | 同路徑 blob；case_ids／family／seed 與鎖交叉綁定 |
    | stage_exits、Stage 3 lock schema、seed source-manifest／附件 | 版本受控；Task 3、R4-01 | repo 內相對路徑，不得逃出 repo_root | source-manifest 載入 bytes 解析並保存摘要／文件，執行前比對目前 SHA 與 tool_revision blob；附件同路徑原始 blob；`test_tool_revision_rejects_deleted_contract_tree_file` |
    | Windows L1.5 case_transport_map | 版本受控；R12-01、§0.4 | 必須是 repo_root 內的 regular-file 路徑 | 載入時同一份 bytes 解析並比對 SHA，保存載入摘要／文件；執行前檔案與 tool_revision 重驗 |
    | tools FrozenFile／FrozenTree、contracts 樹 | 版本受控；Task 3、§0.4 | repo 內路徑；loader 獨立要求 `tools/stage3` 樹 | 單檔 blob；樹完整集合雙向比較後逐檔比 bytes；`test_loader_independently_requires_the_stage3_tools_tree_after_schema_weakening` |
    | allowed_differences、acceptance | 版本受控；Task 3 凍結宣告 | 必須在 repo_root 內，外部絕對路徑也拒絕 | 同路徑原始 blob；canonical cases 維持固定路徑 |
    | repo 內 profile_files | 版本受控；Task 3 profile 凍結 | repo_root 內；index／revision 必須有相同拼字 | 載入 SHA、統一 tool_revision blob 比對；`test_repo_profile_staged_new_file_remains_tool_revision_controlled` |
    | repo 外 profile_files | 私有執行產物；Task 3 FrozenFile 絕對路徑 | 僅允許 repo 外絕對路徑；相對逃逸拒絕 | 載入時 SHA 凍結，執行前重驗；`test_absolute_external_profile_is_sha_frozen_and_rechecked` |
    | manifest source bytes、candidate_context、sidecar／bundle、suite selftest／testbed／realhw 樣板 | 執行產物；Task 3、§0.4 | 可在 repo 外；repo 內路徑需查 index 與 revision 的大小寫不敏感等價拼字 | 載入 SHA／交叉綁定與執行前重驗；只有確定未追蹤才豁免；`test_casefold_alias_of_staged_execution_file_is_rejected` |
    | Docker image name／digest | 執行產物；Task 3 DUT 驗證 | 不使用 repo 檔案路徑 | 以 image inspect 比對核准 digest |

    每次驗證建立專用輸入快照；同一個 resolved path 只讀一次，SHA、解析與交叉判定都共用該份 bytes。
    ``tool_revision`` 是唯一可信版本錨點；省略時由 Git 讀取執行時 HEAD，Task 6 可固定傳入。
    單檔以 Git blob header 與快照 bytes 計算 SHA-1／SHA-256，再和同路徑 revision blob 比較；樹使用相同
    排除規則比對 revision 與工作樹的完整成員集合，再逐檔比 bytes。所有 repo 內參與身分判定的路徑
    （FrozenTree 成員、sidecar 元件檔、package／delegate 樹成員與單檔元件）都經同一分類器；它一次
    取得 Git index 與指定 revision 的 bytes 路徑集合，保留 NUL 分隔資料原樣；空白或無法對應的項目一律拒絕。
    repo 內含 ASCII 控制字元（0x00–0x1F、0x7F）的 manifest 路徑、樹成員或 inventory 項目也一律拒絕，
    避免換行正規化使路徑身分含混。檢查 ASCII 路徑及大小寫別名，Git 查詢失敗一律拒絕。
    只有執行產物確認在 index 與 revision 都沒有大小寫等價路徑時才豁免版本比對。Windows 檔案系統
    的 Unicode 大小寫表無法在 Linux 可靠重現，因此 repo 內含非 ASCII 字元的身分路徑一律保守拒絕。
    T1 CI `--base` 治理該 revision 的只增歷史；``base_revision`` 僅為呼叫端提供時的額外檢查。

    import 時建立的 ``tools/stage3`` 快照、目前本模組 checkout 的磁碟樹，以及 manifest 中已和
    ``tool_revision`` 比對的 FrozenTree 必須三者一致。檢查仍有兩項已知限制：Python 讀取原始檔到
    建立 import 快照之間存在極短窗口；import 後才延遲載入的模組不在這次快照的時間界線內。
    Task 6 必須在 Stage 3 程序啟動後、任何 A/B 動作之前呼叫此函式，執行期間不得改動 checkout，
    並在報告記錄該摘要為 ``tools_stage3_tree_sha256``，供 Task 14 對照。

    `trusted_pair_map` 是呼叫端注入的執行期權威對照，不是 repo 凍結檔；它經結構驗證後與公開映射、manifest pair、逐案 ID 及 allowlist 交叉綁定。

    `S3-L15-RELEASE-AB-WINDOWS` 必須由呼叫端另注入非公開可信 COM 對照；缺少時固定回報
    `trusted pair map missing: BLOCKED-OWNER BO-P9-01`。兩端 COM 名稱以大小寫不敏感規則比較，並摺疊
    Win32 裝置命名空間前綴後再比對；去識別 ID 必須不同，allowlist 恰含兩端。實際埠名只存非公開位置，
    公開證據只放去識別 ID 與對照 SHA-256。T3 只用合成資料測試。
    """
    snapshot = _InputSnapshot()
    root = Path(repo_root).resolve()
    if root != m.repo_root:
        return [f"manifest repo_root mismatch: loaded {m.repo_root} supplied {root}"]
    violations: list[str] = []

    def loaded_schema_bytes(relative: str, expected_sha256: str | None) -> bytes | None:
        schema_path = root.joinpath(*PurePosixPath(relative).parts)
        try:
            raw = snapshot.read_bytes(schema_path)
        except OSError:
            violations.append(f"schema sha256 unavailable: {relative}")
            return None
        actual = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is None or actual != expected_sha256:
            violations.append(f"schema sha256 mismatch: {relative}")
        return raw

    loaded_schema_bytes("tests/acceptance/ab_manifest.schema.json", m.manifest_schema_sha256)
    trusted_pair_schema_bytes = None
    if m.exit_item_id == "S3-L15-RELEASE-AB-WINDOWS":
        trusted_pair_schema_bytes = loaded_schema_bytes(
            "tests/acceptance/stage3_trusted_pair_map.schema.json",
            m.trusted_pair_map_schema_sha256,
        )

    try:
        source_sha256 = snapshot.sha256(m.source_path)
    except OSError:
        violations.append(f"file missing: {m.source_path}")
    else:
        if source_sha256 != m.source_sha256:
            violations.append(f"manifest sha256 mismatch: {m.source_path}")

    def check_file(item: FrozenFile, prefix: str, loaded_sha256: str | None = None) -> None:
        path = _path(root, item.path)
        try:
            digest = snapshot.sha256(path)
        except OSError:
            violations.append(f"file missing: {path}")
            return
        if digest != item.sha256 or (loaded_sha256 is not None and digest != loaded_sha256):
            violations.append(f"{prefix} sha256 mismatch: {item.path}")

    check_file(m.case_list, "case_list")
    check_file(m.case_lock, "case_lock")
    check_file(m.allowed_differences, "allowed_differences")
    if m.acceptance is not None:
        check_file(m.acceptance, "acceptance")
    for item in m.profile_files:
        check_file(item, "profile")
    for item in (m.suite.selftest_report, m.suite.testbed_template, m.suite.realhw_config_template):
        if item is not None:
            check_file(item, "suite input")
    for item in m.tools:
        path = _path(root, item.path)
        if isinstance(item, FrozenFile):
            map_ref = m.platform_settings.get("case_transport_map")
            loaded_map_sha256 = (
                m.case_transport_map_loaded_sha256
                if isinstance(map_ref, Mapping) and item.path == map_ref.get("path") else None
            )
            check_file(item, "tool", loaded_sha256=loaded_map_sha256)
        else:
            try:
                actual = _tree_sha256(path, read_bytes=snapshot.read_bytes)
            except (OSError, HarnessError):
                actual = None
            if actual != item.tree_sha256:
                violations.append(f"tool tree mismatch: {item.path}")

    violations.extend(_tool_revision_frozen_errors(
        m, repo_root=root, run=run, tool_revision=tool_revision, read_bytes=snapshot.read_bytes,
    ))
    executing_root, import_snapshot = _executing_tools_identity()
    violations.extend(_executing_tools_stage3_errors(
        m, executing_root, import_snapshot, read_bytes=snapshot.read_bytes,
    ))

    if m.exit_item_id == "S3-L15-RELEASE-AB-WINDOWS":
        if trusted_pair_map is None:
            violations.append("trusted pair map missing: BLOCKED-OWNER BO-P9-01")
        else:
            try:
                _validate_windows_l15_transport(
                    m, root, trusted_pair_map=trusted_pair_map,
                    trusted_pair_schema_bytes=trusted_pair_schema_bytes,
                    read_bytes=snapshot.read_bytes,
                )
            except HarnessError as exc:
                violations.append(f"trusted pair map invalid: {exc.detail}")

    for side in m.sides:
        manifest_path = _path(root, side.release_manifest.path)
        try:
            sidecar_bytes = snapshot.read_bytes(manifest_path)
            current_sidecar_sha256 = hashlib.sha256(sidecar_bytes).hexdigest()
            if not (
                side.release_manifest_loaded_sha256
                == current_sidecar_sha256
                == side.release_manifest.sha256
            ):
                violations.append(f"release manifest sha256 mismatch: side {side.label}")
        except OSError:
            violations.append(f"file missing: {manifest_path}")
        violations.extend(verify_bundle(
            side.bundle_dir, side.manifest_doc, label=f"side {side.label}",
            read_bytes=snapshot.read_bytes,
        ))

    if m.stage == "3":
        source_manifest_path = (
            root / "docs" / "rust-refactor" / "evidence" / "denominators" / "source-manifest.json"
        )
        try:
            source_manifest_bytes = snapshot.read_bytes(source_manifest_path)
            source_manifest_sha256 = hashlib.sha256(source_manifest_bytes).hexdigest()
        except OSError:
            violations.append(f"file missing: {source_manifest_path}")
        else:
            if source_manifest_sha256 != m.stage3_source_manifest_loaded_sha256:
                violations.append("stage3 source-manifest sha256 mismatch")
        if m.candidate_context is not None:
            check_file(
                m.candidate_context,
                "candidate_context",
                loaded_sha256=m.candidate_context_loaded_sha256,
            )
        if m.required_cases_lock is not None:
            check_file(m.required_cases_lock, "required cases lock")
        catalog_path = root / "tests" / "acceptance" / "stage_exits.json"
        try:
            catalog_sha = snapshot.sha256(catalog_path)
        except OSError:
            violations.append(f"file missing: {catalog_path}")
        else:
            if catalog_sha != m.exit_catalog_sha256:
                violations.append(f"exit catalog sha256 mismatch: tests/acceptance/stage_exits.json")
        violations.extend(f"required cases invalid: {detail}" for detail in _stage3_lock_errors(
            m, root, base_revision=base_revision, source_reader=source_reader,
            read_bytes=snapshot.read_bytes,
        ))

    try:
        result = run(["git", "-C", str(root), "rev-parse", "HEAD:contracts"])
    except OSError:
        result = None
    if result is None or result.rc != 0 or result.timed_out:
        violations.append(f"contract_revision unavailable: repo {m.contract_revision}")
    else:
        actual = result.stdout.strip()
        if actual != m.contract_revision:
            violations.append(f"contract_revision mismatch: manifest {m.contract_revision} repo {actual}")

    if m.dut.kind == "docker":
        if not m.dut.image or not m.dut.image.strip() or not m.dut.image_digest:
            violations.append("docker DUT image and image_digest are required for inspect")
        else:
            result = run(["docker", "image", "inspect", "--format", "{{.Id}}", m.dut.image])
            if result.rc != 0 or result.timed_out or result.stdout.strip() != m.dut.image_digest:
                violations.append("dut image digest mismatch")
    try:
        violations.extend(verify_case_coverage(
            m, repo_root=root, base_revision=base_revision, source_reader=source_reader,
            read_bytes=snapshot.read_bytes,
        ))
    except HarnessError as exc:
        violations.append(f"required cases invalid: {exc.detail}")
    return violations


def render_argv(template: Sequence[str], values: Mapping[str, str]) -> list[str]:
    rendered: list[str] = []
    for value in template:
        try:
            fields = [field for _, field, _, _ in Formatter().parse(value) if field is not None]
            if any(field not in TEMPLATE_KEYS for field in fields):
                raise _invalid(f"unknown template key in {value!r}")
            rendered.append(value.format_map(values))
        except (KeyError, ValueError, IndexError, AttributeError, TypeError) as exc:
            raise _invalid(f"cannot render argv template {value!r}: {exc}") from exc
    return rendered
