"""Stage 0～3 出口證據索引與逐平台判定。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from tools.stage3.jsonio import atomic_write_text, dumps_compact, dumps_report, parse_iso, sha256_file
from tools.stage3.reason_codes import HarnessError

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE_EXITS_PATH = REPO_ROOT / "tests" / "acceptance" / "stage_exits.json"
INDEX_PATH = REPO_ROOT / "docs" / "rust-refactor" / "evidence" / "index.json"
L1_LOCK_PATH = REPO_ROOT / "tests" / "acceptance" / "l1_required.lock.json"
ENV_KEYS = (
    "os_build", "cpu", "ram_mb", "filesystem", "av_product", "drivers",
    "board_firmware", "toolchains",
)
STAGES = ("0", "1A", "1B", "2.1", "2.2", "2.3", "2.4", "3")
PLATFORMS = ("linux", "windows")
RESULTS = ("PASS", "FAIL", "XFAIL", "SKIP", "INFRA_ERROR", "INSUFFICIENT", "XPASS")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class FrozenFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class ExitItem:
    item_id: str
    stage: str
    platform: str
    layer: str
    title: str
    spec_ref: str
    owner_blocked: bool
    candidate_longrun: bool
    evidence_kind: str
    required_case_set: str | None
    required_l1_set: str | None


@dataclass(frozen=True)
class EvidenceEntry:
    entry_id: str
    item_id: str
    platform: str
    result: str
    artifact_id: str
    runtime_digest: str
    bundle_tree_sha256: str | None
    runtime_tree_sha256: str | None
    contract_revision: str
    tool_revision: str
    case_list_sha256: str
    l1_set_sha256: str | None
    workload_sha256: str | None
    environment_id: str
    produced_at: str
    started_at: str
    producer: str
    files: tuple[FrozenFile, ...]
    xfail_cases: tuple[str, ...]
    reuse_of: str | None
    note: str
    ledger_through: str | None
    ledger_records: int | None


def _invalid(detail: str) -> HarnessError:
    return HarnessError("HARNESS_MANIFEST_INVALID", detail)


def _ingest_invalid(detail: str) -> HarnessError:
    return HarnessError("HARNESS_EVIDENCE_INGEST_INVALID", detail)


def environment_id(env_doc: Mapping[str, Any]) -> str:
    """以固定環境欄位集合計算穩定識別碼。"""
    if not isinstance(env_doc, Mapping) or set(env_doc) != set(ENV_KEYS):
        raise ValueError(f"environment keys must equal {ENV_KEYS!r}")
    return hashlib.sha256(dumps_compact(env_doc).encode("utf-8")).hexdigest()


def load_exit_catalog(path: Path = STAGE_EXITS_PATH) -> dict[str, ExitItem]:
    """載入凍結出口目錄；檢查 ID、stage、platform 與必要欄位。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _invalid(f"cannot read exit catalog: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema_version") != 3 or not isinstance(doc.get("items"), list):
        raise _invalid("exit catalog must be schema_version 3 with an items array")
    catalog: dict[str, ExitItem] = {}
    required = {
        "item_id", "stage", "platform", "layer", "title", "spec_ref", "owner_blocked",
        "candidate_longrun", "evidence_kind", "required_case_set", "required_l1_set",
    }
    for raw in doc["items"]:
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise _invalid("exit catalog item is incomplete")
        item_id = raw["item_id"]
        if not isinstance(item_id, str) or not item_id or item_id in catalog:
            raise _invalid("exit catalog item_id is empty or duplicated")
        if raw["stage"] not in STAGES or raw["platform"] not in (*PLATFORMS, "both"):
            raise _invalid(f"unsupported stage or platform for {item_id}")
        if raw["required_l1_set"] is not None and raw["layer"] != "L1":
            raise _invalid(f"non-L1 item has required_l1_set: {item_id}")
        if raw["layer"] == "L1" and raw["required_l1_set"] is None:
            raise _invalid(f"L1 item lacks required_l1_set: {item_id}")
        if not isinstance(raw["owner_blocked"], bool) or not isinstance(raw["candidate_longrun"], bool):
            raise _invalid(f"exit catalog flags must be booleans: {item_id}")
        catalog[item_id] = ExitItem(**{key: raw[key] for key in required})
    return catalog


def _entry_dict(entry: EvidenceEntry) -> dict[str, Any]:
    if not isinstance(entry, EvidenceEntry):
        raise _ingest_invalid("entry must be an EvidenceEntry")
    row = asdict(entry)
    row["files"] = [asdict(item) for item in entry.files]
    row["xfail_cases"] = list(entry.xfail_cases)
    return row


def load_index(path: Path = INDEX_PATH) -> dict[str, Any]:
    """讀取並檢查 evidence index 的根結構。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _invalid(f"cannot read evidence index: {exc}") from exc
    if (
        not isinstance(doc, dict)
        or doc.get("schema_version") != 2
        or not isinstance(doc.get("entries"), list)
        or not isinstance(doc.get("stage_targets"), dict)
        or not isinstance(doc.get("target_history"), list)
    ):
        raise _invalid("evidence index root is invalid")
    if any(not isinstance(row, dict) for row in doc["entries"]):
        raise _invalid("evidence index entries must be objects")
    return doc


def _write_index(path: Path, index: Mapping[str, Any]) -> None:
    atomic_write_text(Path(path), dumps_report(dict(index)))


def _bundle_metadata(bundle_dir: Path, manifest_doc: Mapping[str, Any], platform: str) -> tuple[str, str]:
    from tools.stage3.ab_manifest import bundle_tree_sha256, runtime_tree_sha256, verify_bundle

    violations = verify_bundle(bundle_dir, manifest_doc, label=platform)
    if violations:
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", "; ".join(violations))
    return bundle_tree_sha256(bundle_dir), runtime_tree_sha256(bundle_dir)


def set_target(
    index_path: Path,
    *,
    stage: str,
    platform: str,
    release_manifest: Path,
    contract_revision: str,
    tool_revision: str,
    case_list_sha256: str,
    set_by: str,
    now: str,
    bundle_dir: Path | None = None,
) -> None:
    """凍結一個 stage/platform target，保留完整 target 歷史。"""
    if stage not in STAGES or platform not in PLATFORMS:
        raise _invalid("unsupported stage or platform")
    if stage == "3" and bundle_dir is None:
        raise _invalid("stage 3 target requires bundle dir")
    try:
        parse_iso(now)
    except (TypeError, ValueError) as exc:
        raise _invalid(f"invalid target timestamp: {exc}") from exc
    if not all(isinstance(value, str) and value for value in (contract_revision, tool_revision, set_by)):
        raise _invalid("target revisions and set_by must be non-empty strings")
    if not isinstance(case_list_sha256, str) or not _SHA256.fullmatch(case_list_sha256):
        raise _invalid("case_list_sha256 must be lowercase SHA-256")

    manifest_path = Path(release_manifest).resolve()
    bundle_path = Path(bundle_dir).resolve() if bundle_dir is not None else None
    if bundle_path is not None and manifest_path != (bundle_path / "release-manifest.json").resolve():
        raise _invalid("release manifest must be the bundle root release-manifest.json")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest_doc = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _invalid(f"cannot read release manifest: {exc}") from exc
    if not isinstance(manifest_doc, dict):
        raise _invalid("release manifest must be an object")
    from tools.stage3.ab_manifest import runtime_digest

    artifact_id = hashlib.sha256(manifest_bytes).hexdigest()
    runtime = runtime_digest(manifest_doc)
    tree_values: dict[str, str] = {}
    if bundle_path is not None:
        tree_values = dict(zip(("bundle_tree_sha256", "runtime_tree_sha256"),
                               _bundle_metadata(bundle_path, manifest_doc, platform)))

    index = load_index(index_path)
    history = index["target_history"]
    for previous in history:
        previous_target = previous.get("target", previous)
        if (
            previous.get("stage") == stage
            and previous.get("platform") == platform
            and previous_target.get("artifact_id") == artifact_id
            and previous_target.get("bundle_tree_sha256") is not None
            and tree_values
            and previous_target.get("bundle_tree_sha256") != tree_values["bundle_tree_sha256"]
        ):
            raise HarnessError(
                "HARNESS_ARTIFACT_MISMATCH",
                f"bundle tree changed under same manifest: {platform}",
            )

    target: dict[str, Any] = {
        "artifact_id": artifact_id,
        "runtime_digest": runtime,
        "contract_revision": contract_revision,
        "tool_revision": tool_revision,
        "case_list_sha256": case_list_sha256,
        "set_by": set_by,
        "set_at": now,
        **tree_values,
    }
    index.setdefault("stage_targets", {}).setdefault(stage, {})[platform] = target
    history_row = {**target, "stage": stage, "platform": platform}
    index.setdefault("target_history", []).append(history_row)
    _write_index(index_path, index)


def add_entry(index_path: Path, entry: EvidenceEntry, *, catalog: Mapping[str, ExitItem]) -> None:
    """驗證一筆結果後以原子方式追加至索引。"""
    row = _entry_dict(entry)
    item = catalog.get(entry.item_id)
    if item is None:
        raise HarnessError("HARNESS_EVIDENCE_UNKNOWN_ITEM", entry.item_id)
    if not isinstance(entry.entry_id, str) or not entry.entry_id:
        raise _ingest_invalid("entry_id is required")
    if entry.platform not in PLATFORMS or item.platform not in (entry.platform, "both"):
        raise _ingest_invalid("entry platform does not match item")
    if entry.result not in RESULTS:
        raise _ingest_invalid("unsupported evidence result")
    if entry.result in {"PASS", "XFAIL"} and not entry.files:
        raise _ingest_invalid("PASS and XFAIL require evidence files")
    if entry.producer == "manual" and item.evidence_kind != "manual-procedure":
        raise _ingest_invalid("manual producer is allowed only for manual-procedure items")
    if item.evidence_kind == "canary-report":
        if entry.ledger_records is None or (
            entry.result == "PASS" and entry.ledger_through is None
        ):
            raise _ingest_invalid("canary report requires ledger version")
        if entry.ledger_records < 0:
            raise _ingest_invalid("ledger_records must be non-negative")
    elif entry.ledger_through is not None or entry.ledger_records is not None:
        raise _ingest_invalid("non-canary evidence cannot carry a ledger version")
    if entry.producer == "ingest:junit":
        if not isinstance(entry.l1_set_sha256, str) or not _SHA256.fullmatch(entry.l1_set_sha256):
            raise _ingest_invalid("junit evidence requires l1_set_sha256")
    elif entry.l1_set_sha256 is not None:
        raise _ingest_invalid("non-junit evidence cannot carry l1_set_sha256")
    if item.required_l1_set and entry.producer == "ingest:junit" and entry.l1_set_sha256 is None:
        raise _ingest_invalid("L1 junit evidence requires a frozen set digest")
    for stamp in (entry.produced_at, entry.started_at):
        try:
            parse_iso(stamp)
        except (TypeError, ValueError) as exc:
            raise _ingest_invalid(f"invalid evidence timestamp: {exc}") from exc
    for file in entry.files:
        if not isinstance(file.path, str) or not file.path or not isinstance(file.sha256, str) or not _SHA256.fullmatch(file.sha256):
            raise _ingest_invalid("evidence file path or sha256 is invalid")
    if any(not isinstance(case, str) or not case for case in entry.xfail_cases):
        raise _ingest_invalid("xfail_cases must contain non-empty strings")

    index = load_index(index_path)
    entries = index["entries"]
    if any(existing.get("entry_id") == entry.entry_id for existing in entries):
        raise HarnessError("HARNESS_EVIDENCE_DUPLICATE", entry.entry_id)
    entries.append(row)
    _write_index(index_path, index)


def _entry_file_path(evidence_root: Path, file_row: Mapping[str, Any]) -> Path | None:
    rel = file_row.get("path")
    if not isinstance(rel, str) or not rel:
        return None
    rel_path = Path(rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        return None
    root = Path(evidence_root).resolve()
    # 測試與早期索引呼叫端會傳 repo root；正式 ingest 傳 evidence root。
    candidates = [(root, rel_path)]
    if (root / "evidence").is_dir():
        candidates.append((root / "evidence", rel_path))
    # 反向情形：entry 路徑以 evidence 目錄名開頭（相對索引所在的上層目錄記錄，如 `evidence/<file>`），
    # 而呼叫端傳的是該 evidence 目錄本身；去掉開頭目錄名後仍限定在 evidence root 之內解析。
    if len(rel_path.parts) > 1 and rel_path.parts[0] == root.name:
        candidates.append((root, Path(*rel_path.parts[1:])))
    for candidate_root, candidate_rel in candidates:
        candidate = candidate_root / candidate_rel
        try:
            candidate.resolve(strict=True).relative_to(candidate_root)
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        except (OSError, ValueError):
            continue
    return None


def _files_status(entry: Mapping[str, Any], evidence_root: Path) -> tuple[bool, str | None]:
    files = entry.get("files")
    if not isinstance(files, list) or not files:
        return False, "no evidence files"
    for file_row in files:
        if not isinstance(file_row, dict):
            return False, "invalid evidence file"
        path = _entry_file_path(evidence_root, file_row)
        if path is None:
            return False, f"evidence file missing or unsafe: {file_row.get('path', '')}"
        try:
            if sha256_file(path) != file_row.get("sha256"):
                return False, f"evidence file hash mismatch: {file_row.get('path', '')}"
        except OSError:
            return False, f"evidence file missing: {file_row.get('path', '')}"
    return True, None


def _load_l1_lock(l1_lock: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if l1_lock is not None:
        return l1_lock
    try:
        doc = json.loads(L1_LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _l1_set_sha256(lock: Mapping[str, Any], set_id: str, platform: str) -> str | None:
    try:
        nodeids = lock["sets"][set_id][platform]["nodeids"]
    except (KeyError, TypeError):
        return None
    if not isinstance(nodeids, list) or any(not isinstance(node, str) for node in nodeids):
        return None
    payload = json.dumps(sorted(nodeids), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _xfail_valid(entry: Mapping[str, Any], cases: Mapping[str, dict]) -> bool:
    xfail_cases = entry.get("xfail_cases")
    if not isinstance(xfail_cases, list) or not xfail_cases:
        return False
    try:
        produced = parse_iso(entry["produced_at"])
    except (KeyError, TypeError, ValueError):
        return False
    for case_id in xfail_cases:
        row = cases.get(case_id)
        sentinel = row.get("sentinel") if isinstance(row, dict) else None
        if not isinstance(row, dict) or row.get("expected") != "XFAIL" or not isinstance(sentinel, dict):
            return False
        expires = sentinel.get("expires")
        try:
            if produced >= parse_iso(expires):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _reuse_ok(
    entry: Mapping[str, Any],
    by_id: Mapping[str, Mapping[str, Any]],
    evidence_root: Path,
    *,
    index: Mapping[str, Any],
    catalog: Mapping[str, ExitItem],
    cases: Mapping[str, dict],
    l1_lock: Mapping[str, Any] | None,
    visiting: frozenset[str] = frozenset(),
) -> tuple[bool, str | None]:
    source_id = entry.get("reuse_of")
    if source_id is None:
        return True, None
    current_id = entry.get("entry_id")
    if source_id in visiting or source_id == current_id:
        return False, "reuse cycle"
    source = by_id.get(source_id)
    if source is None:
        return False, "reused entry missing"
    if source.get("result") != "PASS":
        return False, "reused entry not PASS"
    source_item = catalog.get(str(source.get("item_id", "")))
    source_platform = source.get("platform")
    if source_item is None or not isinstance(source_platform, str):
        return False, "reused entry item or platform invalid"
    files_ok, _ = _files_status(source, evidence_root)
    if not files_ok:
        return False, "reused entry files invalid"
    identity_fields = (
        "runtime_digest", "runtime_tree_sha256", "contract_revision", "tool_revision",
        "case_list_sha256", "workload_sha256", "environment_id",
    )
    identity_error: str | None = None
    for field in identity_fields:
        left, right = entry.get(field), source.get(field)
        if field == "runtime_tree_sha256" and (left is None or right is None):
            identity_error = f"reuse identity differs: {field}"
            break
        if left != right:
            identity_error = f"reuse identity differs: {field}"
            break
    source_l1 = _load_l1_lock(l1_lock) if source_item.required_l1_set is not None else None
    source_status, source_reasons, satisfying = _platform_status(
        index,
        source_item,
        source_platform,
        evidence_root=evidence_root,
        cases=cases,
        l1_lock=source_l1,
        catalog=catalog,
        reuse_chain=visiting | {str(source_id), str(current_id)},
    )
    if source_status != "SATISFIED" or str(source_id) not in satisfying:
        if any("reuse cycle" in reason for reason in source_reasons):
            return False, "reuse cycle"
        if identity_error is not None:
            return False, identity_error
        return False, "reused entry not PASS"
    if identity_error is not None:
        return False, identity_error
    return True, None


def _entry_order(row: Mapping[str, Any]) -> tuple[str, int, str, str]:
    records = row.get("ledger_records")
    if not isinstance(records, int) or isinstance(records, bool):
        records = -1
    return (
        str(row.get("ledger_through") or ""),
        records,
        str(row.get("produced_at") or ""),
        str(row.get("entry_id") or ""),
    )


def _platform_status(
    index: Mapping[str, Any],
    item: ExitItem,
    platform: str,
    *,
    evidence_root: Path,
    cases: Mapping[str, dict],
    l1_lock: Mapping[str, Any] | None,
    catalog: Mapping[str, ExitItem],
    reuse_chain: frozenset[str] = frozenset(),
) -> tuple[str, list[str], list[str]]:
    stage_rows = index.get("stage_targets", {}).get(item.stage, {})
    target = stage_rows.get(platform) if isinstance(stage_rows, dict) else None
    if not isinstance(target, dict):
        return "NO_TARGET", ["no target"], []
    current_l1 = _l1_set_sha256(l1_lock or {}, item.required_l1_set, platform) if item.required_l1_set else None
    if item.required_l1_set is not None and current_l1 is None:
        return "MISSING", [f"no frozen l1 set: {item.required_l1_set}"], []
    entries = [
        row for row in index.get("entries", [])
        if row.get("item_id") == item.item_id and row.get("platform") == platform
    ]
    if not entries:
        return "MISSING", ["no evidence entries"], []

    valid: list[dict[str, Any]] = []
    stale: list[str] = []
    for entry in entries:
        entry_id = str(entry.get("entry_id", ""))
        reasons: list[str] = []
        for field in ("artifact_id", "contract_revision", "tool_revision"):
            if entry.get(field) != target.get(field):
                reasons.append(f"stale {field}: {entry_id}")
        if item.required_case_set is not None and entry.get("case_list_sha256") != target.get("case_list_sha256"):
            reasons.append(f"stale case_list_sha256: {entry_id}")
        if target.get("bundle_tree_sha256") is not None and (
            entry.get("bundle_tree_sha256") is None
            or entry.get("runtime_tree_sha256") is None
            or entry.get("bundle_tree_sha256") != target.get("bundle_tree_sha256")
            or entry.get("runtime_tree_sha256") != target.get("runtime_tree_sha256")
        ):
            reasons.append(f"bundle tree differs from target: {entry_id}")
        if item.required_l1_set is not None:
            if entry.get("l1_set_sha256") != current_l1:
                reasons.append(f"l1 required set changed: {entry_id}")
        if reasons:
            stale.extend(reasons)
        else:
            valid.append(entry)

    if not valid:
        return ("STALE", stale or ["no current evidence entries"], []) if entries else ("MISSING", [], [])

    # 同 artifact 的 FAIL 永遠黏著；累計型目前報告規則不能抹除已證實的失敗。
    if any(row.get("result") == "FAIL" for row in valid):
        return "FAIL", [f"FAIL: {row.get('entry_id')}" for row in valid if row.get("result") == "FAIL"], []

    effective = valid
    if item.evidence_kind == "canary-report":
        latest = max(valid, key=_entry_order)
        effective = [latest]
        if latest.get("result") != "PASS":
            earlier_passes = [row for row in valid if row.get("result") == "PASS"]
            reasons = [f"latest canary report {latest.get('entry_id')} is {latest.get('result')}"]
            if earlier_passes:
                ids = ",".join(str(row.get("entry_id")) for row in sorted(earlier_passes, key=_entry_order))
                reasons = [f"superseded: {reasons[0]}; earlier PASS {ids} no longer current"]
            return "MISSING", reasons, []

    successful = [
        row for row in effective
        if row.get("result") == "PASS"
        or (row.get("result") == "XFAIL" and _xfail_valid(row, cases))
    ]
    if not successful:
        return "MISSING", [f"result {row.get('result')} is not sufficient: {row.get('entry_id')}" for row in effective], []

    by_id = {str(row.get("entry_id")): row for row in index.get("entries", []) if isinstance(row, dict)}
    stale_reuse: list[str] = []
    hash_mismatch: list[str] = []
    satisfying: list[str] = []
    for row in successful:
        files_ok, file_reason = _files_status(row, evidence_root)
        if not files_ok:
            hash_mismatch.append(f"{file_reason}: {row.get('entry_id')}")
            continue
        reuse_ok, reuse_reason = _reuse_ok(
            row,
            by_id,
            evidence_root,
            index=index,
            catalog=catalog,
            cases=cases,
            l1_lock=l1_lock,
            visiting=reuse_chain,
        )
        if not reuse_ok:
            stale_reuse.append(str(reuse_reason))
            continue
        satisfying.append(str(row.get("entry_id")))
    if hash_mismatch:
        return "HASH_MISMATCH", hash_mismatch, []
    if satisfying:
        return "SATISFIED", [], satisfying
    if stale_reuse:
        return "STALE", stale_reuse, []
    return "MISSING", [f"no passing evidence for {item.item_id}"], []


def _item_status_full(
    index: Mapping[str, Any],
    item: ExitItem,
    *,
    evidence_root: Path,
    cases: Mapping[str, dict],
    l1_lock: Mapping[str, Any] | None,
    catalog: Mapping[str, ExitItem] | None = None,
) -> tuple[str, list[str], list[str]]:
    resolved_catalog = catalog if catalog is not None else load_exit_catalog()
    item_l1 = _load_l1_lock(l1_lock) if item.required_l1_set is not None else None
    platforms = PLATFORMS if item.platform == "both" else (item.platform,)
    results: list[tuple[str, str, list[str], list[str]]] = []
    for platform in platforms:
        status, reasons, satisfying = _platform_status(
            index,
            item,
            platform,
            evidence_root=Path(evidence_root),
            cases=cases,
            l1_lock=item_l1,
            catalog=resolved_catalog,
        )
        results.append((platform, status, reasons, satisfying))
    for platform, status, reasons, _ in results:
        if status == "FAIL":
            return "FAIL", [f"{platform}: {reason}" for reason in reasons], []
    if all(status == "SATISFIED" for _, status, _, _ in results):
        return "SATISFIED", [], [entry for _, _, _, entries in results for entry in entries]
    priority = {"HASH_MISMATCH": 5, "STALE": 4, "NO_TARGET": 3, "MISSING": 2, "SATISFIED": 1}
    status = max((row[1] for row in results), key=lambda value: priority.get(value, 0))
    reasons = [f"{platform}: {reason}" for platform, _, entries, _ in results for reason in entries]
    return status, reasons, []


def item_status(
    index: Mapping[str, Any],
    item: ExitItem,
    *,
    evidence_root: Path,
    cases: Mapping[str, dict],
    l1_lock: Mapping[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """依 target、證據身分、檔案雜湊與案例 sentinel 判定單一出口項目。"""
    status, reasons, _ = _item_status_full(
        index, item, evidence_root=evidence_root, cases=cases, l1_lock=l1_lock
    )
    return status, reasons


def stage_status(
    index: Mapping[str, Any],
    stage: str,
    *,
    catalog: Mapping[str, ExitItem],
    evidence_root: Path,
    cases: Mapping[str, dict],
    l1_lock: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """彙整指定 stage 的出口狀態。"""
    if stage not in STAGES:
        raise _invalid(f"unsupported stage: {stage}")
    items: dict[str, Any] = {}
    for item in catalog.values():
        if item.stage != stage:
            continue
        status, reasons, satisfying = _item_status_full(
            index, item, evidence_root=evidence_root, cases=cases, l1_lock=l1_lock, catalog=catalog
        )
        items[item.item_id] = {
            "status": status,
            "reasons": reasons,
            "owner_blocked": item.owner_blocked,
            "satisfying_entries": satisfying,
        }
    statuses = [row["status"] for row in items.values()]
    result = "FAIL" if "FAIL" in statuses else "PASS" if statuses and all(s == "SATISFIED" for s in statuses) else "BLOCKED"
    targets = index.get("stage_targets", {}).get(stage, {})
    return {"stage": stage, "targets": dict(targets) if isinstance(targets, dict) else {}, "result": result, "items": items}


def render_report(
    index: Mapping[str, Any],
    catalog: Mapping[str, ExitItem],
    *,
    evidence_root: Path,
    cases: Mapping[str, dict],
    l1_lock: Mapping[str, Any] | None = None,
) -> str:
    """輸出各 stage 的 Markdown 出口表。"""
    lines = ["# Stage 0～3 證據出口", ""]
    for stage in STAGES:
        result = stage_status(
            index, stage, catalog=catalog, evidence_root=evidence_root, cases=cases, l1_lock=l1_lock
        )
        lines.extend((f"## Stage {stage} — {result['result']}", "", "| 項目 | 平台 | 狀態 | 說明 |", "|---|---|---|---|"))
        for item_id, row in result["items"].items():
            item = catalog[item_id]
            status = "BLOCKED-OWNER" if item.owner_blocked and row["status"] != "SATISFIED" else row["status"]
            detail = "; ".join(row["reasons"]).replace("|", "\\|")
            lines.append(f"| {item_id} | {item.platform} | {status} | {detail} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
