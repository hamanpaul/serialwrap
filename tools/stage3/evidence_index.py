"""Stage 0～3 出口證據索引與逐平台判定。

受限 FAIL 以最小公開列登錄：除一般的 ``entry_id`` 外，欄位固定為
``result``、``source_visibility``、artifact/platform/item、原始共同輪次與
context SHA、非公開來源 SHA、去識別失敗摘要、核實者 ID 及核實時間。一般
review 附件目前只承諾包含 ``source_hashes`` 與 ``scope_sha256``；細部結構
尚待規格裁決（SPEC-GAP）。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.stage3.jsonio import (
    atomic_write_text,
    dumps_compact,
    dumps_report,
    parse_iso,
    record_id,
    sha256_file,
)
from tools.stage3.reason_codes import HarnessError

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE_EXITS_PATH = REPO_ROOT / "tests" / "acceptance" / "stage_exits.json"
INDEX_PATH = REPO_ROOT / "docs" / "rust-refactor" / "evidence" / "index.json"
L1_LOCK_PATH = REPO_ROOT / "tests" / "acceptance" / "l1_required.lock.json"
REQUIRED_CASES_LOCK_PATH = REPO_ROOT / "tests" / "acceptance" / "stage3_required_cases.lock.json"
ENV_KEYS = (
    "os_build", "cpu", "ram_mb", "filesystem", "av_product", "drivers",
    "board_firmware", "toolchains",
)
STAGES = ("0", "1A", "1B", "2.1", "2.2", "2.3", "2.4", "3")
PLATFORMS = ("linux", "windows")
RESULTS = ("PASS", "FAIL", "XFAIL", "SKIP", "INFRA_ERROR", "INSUFFICIENT", "XPASS")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_RC_TAG = re.compile(r"^v1\.0\.0-rc\.([1-9][0-9]*)$")
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_INDEX_FIELDS = frozenset({
    "candidate_rounds", "current_candidate_round_id", "entries", "legacy_obligations",
    "schema_version", "stage_targets", "target_history",
})
_ENTRY_FIELDS = frozenset({
    "entry_id", "exit_catalog_sha256", "candidate_round_id", "archive_sha256", "item_id",
    "platform", "result", "artifact_id", "runtime_digest", "bundle_tree_sha256",
    "runtime_tree_sha256", "contract_revision", "tool_revision", "case_list_sha256",
    "required_cases_sha256", "l1_set_sha256", "workload_sha256", "environment_id",
    "produced_at", "started_at", "producer", "files", "xfail_cases", "reuse_of",
    "note", "ledger_through", "ledger_records",
})
_RESTRICTED_FIELDS = frozenset({
    "entry_id", "result", "source_visibility", "artifact_id", "platform", "item_id",
    "candidate_round_id", "context_id", "private_source_sha256", "failure_summary",
    "verified_by", "verified_at",
})


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
    release_requirement: str
    required_families: tuple[str, ...] | None
    required_l1_set: str | None


@dataclass(frozen=True)
class EvidenceEntry:
    exit_catalog_sha256: str
    candidate_round_id: str | None
    archive_sha256: str | None
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
    required_cases_sha256: str | None
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


@dataclass(frozen=True)
class RestrictedFailEntry:
    """R8-01 的正式公開 FAIL 列；不攜帶原始證據內容或檔案路徑。"""

    entry_id: str
    result: str
    source_visibility: str
    artifact_id: str
    platform: str
    item_id: str
    candidate_round_id: str
    context_id: str
    private_source_sha256: str
    failure_summary: str
    verified_by: str
    verified_at: str


def _invalid(detail: str) -> HarnessError:
    return HarnessError("HARNESS_MANIFEST_INVALID", detail)


def _ingest_invalid(detail: str) -> HarnessError:
    return HarnessError("HARNESS_EVIDENCE_INGEST_INVALID", detail)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _invalid(f"{label} is not valid UTF-8 JSON: {exc}") from exc


def _relative_public_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and not re.match(r"^[A-Za-z]:", value) and all(
        part not in ("", ".", "..") for part in value.split("/")
    )


def _pointer_valid(pointer: Any) -> bool:
    return (
        isinstance(pointer, dict)
        and set(pointer) == {"path", "sha256"}
        and _relative_public_path(pointer.get("path"))
        and isinstance(pointer.get("sha256"), str)
        and _SHA256.fullmatch(pointer["sha256"]) is not None
    )


def environment_id(env_doc: Mapping[str, Any]) -> str:
    """以固定環境欄位集合計算穩定識別碼。"""
    if not isinstance(env_doc, Mapping) or set(env_doc) != set(ENV_KEYS):
        raise ValueError(f"environment keys must equal {ENV_KEYS!r}")
    return hashlib.sha256(dumps_compact(env_doc).encode("utf-8")).hexdigest()


def load_exit_catalog(path: Path = STAGE_EXITS_PATH) -> dict[str, ExitItem]:
    """載入凍結出口目錄；檢查 ID、stage、platform 與必要欄位。"""
    try:
        doc = _decode_json(Path(path).read_bytes(), "exit catalog")
    except OSError as exc:
        raise _invalid(f"cannot read exit catalog: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema_version", 3) != 3 or not isinstance(doc.get("items"), list):
        raise _invalid("exit catalog must be schema_version 3 with an items array")
    catalog: dict[str, ExitItem] = {}
    required = {
        "item_id", "stage", "platform", "layer", "title", "spec_ref", "owner_blocked",
        "candidate_longrun", "evidence_kind", "required_case_set", "release_requirement",
        "required_families", "required_l1_set",
    }
    allowed = required
    for raw in doc["items"]:
        if not isinstance(raw, dict) or not required.issubset(raw) or set(raw) - allowed:
            raise _invalid("exit catalog item is incomplete")
        item_id = raw["item_id"]
        if not isinstance(item_id, str) or not item_id or item_id in catalog:
            raise _invalid("exit catalog item_id is empty or duplicated")
        if raw["stage"] not in STAGES or raw["platform"] not in (*PLATFORMS, "both"):
            raise _invalid(f"unsupported stage or platform for {item_id}")
        if raw["release_requirement"] not in ("required", "reference"):
            raise _invalid(f"unsupported release requirement for {item_id}")
        families = raw["required_families"]
        if families is not None and (
            not isinstance(families, list) or any(not isinstance(value, str) or not value for value in families)
        ):
            raise _invalid(f"required_families must be a string array or null for {item_id}")
        if raw["required_l1_set"] is not None and raw["layer"] != "L1":
            raise _invalid(f"non-L1 item has required_l1_set: {item_id}")
        if raw["layer"] == "L1" and raw["required_l1_set"] is None:
            raise _invalid(f"L1 item lacks required_l1_set: {item_id}")
        if not isinstance(raw["owner_blocked"], bool) or not isinstance(raw["candidate_longrun"], bool):
            raise _invalid(f"exit catalog flags must be booleans: {item_id}")
        values = dict(raw)
        values["required_families"] = tuple(families) if families is not None else None
        catalog[item_id] = ExitItem(**values)
    return catalog


def _ordinary_entry_errors(row: Any) -> list[str]:
    if not isinstance(row, dict):
        return ["entry must be an object"]
    errors: list[str] = []
    if set(row) != _ENTRY_FIELDS:
        errors.append("ordinary entry fields do not match the closed schema")
        return errors
    if not isinstance(row.get("entry_id"), str) or not re.fullmatch(r"[0-9a-f]{20}", row["entry_id"]):
        errors.append("entry_id must be 20 lowercase hexadecimal characters")
    elif record_id({**{key: value for key, value in row.items() if key != "entry_id"}, "record_id": ""}) != row["entry_id"]:
        errors.append("entry_id does not match the entry content")
    for key in ("exit_catalog_sha256", "artifact_id", "runtime_digest", "case_list_sha256", "environment_id"):
        if not isinstance(row.get(key), str) or not _SHA256.fullmatch(row[key]):
            errors.append(f"{key} must be lowercase SHA-256")
    for key in ("bundle_tree_sha256", "runtime_tree_sha256", "archive_sha256", "required_cases_sha256", "l1_set_sha256", "workload_sha256"):
        value = row.get(key)
        if value is not None and (not isinstance(value, str) or not _SHA256.fullmatch(value)):
            errors.append(f"{key} must be null or lowercase SHA-256")
    for key in ("candidate_round_id",):
        value = row.get(key)
        if value is not None and (not isinstance(value, str) or not _HEX32.fullmatch(value)):
            errors.append(f"{key} must be null or 32 lowercase hexadecimal characters")
    if row.get("platform") not in PLATFORMS or row.get("result") not in RESULTS:
        errors.append("entry platform or result is unsupported")
    for key in ("item_id", "contract_revision", "tool_revision", "producer", "note"):
        if not isinstance(row.get(key), str) or not row[key]:
            errors.append(f"{key} must be a non-empty string")
    if not isinstance(row.get("contract_revision"), str) or not _SHA1.fullmatch(row["contract_revision"]):
        errors.append("contract_revision must be a 40-character Git revision")
    if not isinstance(row.get("tool_revision"), str) or not row["tool_revision"]:
        errors.append("tool_revision must be non-empty")
    for key in ("produced_at", "started_at"):
        try:
            parse_iso(row[key])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{key} must be an ISO timestamp with timezone")
    if not isinstance(row.get("files"), list):
        errors.append("files must be an array")
    else:
        for file_row in row["files"]:
            if (
                not isinstance(file_row, dict)
                or set(file_row) != {"path", "sha256"}
                or not _relative_public_path(file_row.get("path"))
                or not isinstance(file_row.get("sha256"), str)
                or not _SHA256.fullmatch(file_row["sha256"])
            ):
                errors.append("evidence file row is invalid")
                break
    if not isinstance(row.get("xfail_cases"), list) or any(
        not isinstance(value, str) or not value for value in row.get("xfail_cases", [])
    ):
        errors.append("xfail_cases must be an array of non-empty strings")
    for key in ("reuse_of", "ledger_through"):
        if row.get(key) is not None and (not isinstance(row[key], str) or not row[key]):
            errors.append(f"{key} must be null or a non-empty string")
    if row.get("ledger_records") is not None and (
        not isinstance(row["ledger_records"], int) or isinstance(row["ledger_records"], bool) or row["ledger_records"] < 0
    ):
        errors.append("ledger_records must be null or a non-negative integer")
    return errors


def _restricted_entry_errors(row: Any) -> list[str]:
    if not isinstance(row, dict) or set(row) != _RESTRICTED_FIELDS:
        return ["restricted entry fields do not match the closed schema"]
    errors: list[str] = []
    if row.get("result") != "FAIL" or row.get("source_visibility") != "restricted":
        errors.append("restricted entries must be FAIL with restricted visibility")
    if not isinstance(row.get("entry_id"), str) or not re.fullmatch(r"[0-9a-f]{20}", row["entry_id"]):
        errors.append("restricted entry_id is invalid")
    elif record_id({**{key: value for key, value in row.items() if key != "entry_id"}, "record_id": ""}) != row["entry_id"]:
        errors.append("restricted entry_id does not match the entry content")
    if row.get("platform") not in PLATFORMS or not isinstance(row.get("item_id"), str) or not row["item_id"]:
        errors.append("restricted entry item or platform is invalid")
    for key in ("artifact_id", "context_id", "private_source_sha256"):
        if not isinstance(row.get(key), str) or not _SHA256.fullmatch(row[key]):
            errors.append(f"restricted {key} must be lowercase SHA-256")
    if not isinstance(row.get("candidate_round_id"), str) or not _HEX32.fullmatch(row["candidate_round_id"]):
        errors.append("restricted candidate_round_id must be 32 lowercase hexadecimal characters")
    summary = row.get("failure_summary")
    if (
        not isinstance(summary, str) or not summary.strip() or "\\" in summary
        or "FIXTURE_PRIVATE_PAYLOAD" in summary or re.search(r"(?i)(password|token|secret)=", summary)
    ):
        errors.append("restricted failure_summary must be a de-identified summary")
    verifier = row.get("verified_by")
    if not isinstance(verifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", verifier):
        errors.append("restricted verified_by must be a public verifier ID")
    try:
        parse_iso(row["verified_at"])
    except (KeyError, TypeError, ValueError):
        errors.append("restricted verified_at must be an ISO timestamp with timezone")
    return errors


def _entry_dict(entry: EvidenceEntry | RestrictedFailEntry) -> dict[str, Any]:
    if isinstance(entry, RestrictedFailEntry):
        return asdict(entry)
    if not isinstance(entry, EvidenceEntry):
        raise _ingest_invalid("entry must be an EvidenceEntry or RestrictedFailEntry")
    row = asdict(entry)
    row["files"] = [asdict(item) for item in entry.files]
    row["xfail_cases"] = list(entry.xfail_cases)
    return row


def load_index(path: Path = INDEX_PATH) -> dict[str, Any]:
    """讀取並檢查 evidence index 的根結構。"""
    try:
        doc = _decode_json(Path(path).read_bytes(), "evidence index")
    except OSError as exc:
        raise _invalid(f"cannot read evidence index: {exc}") from exc
    if (
        not isinstance(doc, dict)
        or set(doc) != _INDEX_FIELDS
        or doc.get("schema_version") != 2
        or not isinstance(doc.get("entries"), list)
        or not isinstance(doc.get("stage_targets"), dict)
        or not isinstance(doc.get("target_history"), list)
        or not isinstance(doc.get("candidate_rounds"), list)
        or not isinstance(doc.get("legacy_obligations"), dict)
    ):
        raise _invalid("evidence index root is invalid")
    legacy = doc["legacy_obligations"]
    if set(legacy) != {"scope", "resolutions"} or any(
        value is not None and not _pointer_valid(value) for value in legacy.values()
    ):
        raise _invalid("evidence index legacy obligation pointers are invalid")
    seen_entry_ids: set[str] = set()
    for row in doc["entries"]:
        errors = _restricted_entry_errors(row) if isinstance(row, dict) and row.get("source_visibility") == "restricted" else _ordinary_entry_errors(row)
        if errors:
            raise _invalid("evidence index entry is invalid: " + "; ".join(errors))
        entry_id = row["entry_id"]
        if entry_id in seen_entry_ids:
            raise _invalid(f"duplicate evidence entry_id: {entry_id}")
        seen_entry_ids.add(entry_id)
    rounds = doc["candidate_rounds"]
    round_ids: set[str] = set()
    for row in rounds:
        if (
            not isinstance(row, dict)
            or set(row) != {"candidate_round_id", "rc_tag", "set_by", "created_at"}
            or not isinstance(row.get("candidate_round_id"), str)
            or not _HEX32.fullmatch(row["candidate_round_id"])
            or not isinstance(row.get("rc_tag"), str)
            or not _RC_TAG.fullmatch(row["rc_tag"])
            or not isinstance(row.get("set_by"), str)
            or not row["set_by"]
        ):
            raise _invalid("candidate_rounds contains an invalid row")
        try:
            parse_iso(row["created_at"])
        except (TypeError, ValueError):
            raise _invalid("candidate_rounds contains an invalid created_at")
        if row["candidate_round_id"] in round_ids:
            raise _invalid("candidate_rounds contains a duplicate ID")
        round_ids.add(row["candidate_round_id"])
    current_round = doc.get("current_candidate_round_id")
    if current_round is not None and (not isinstance(current_round, str) or current_round not in round_ids):
        raise _invalid("current_candidate_round_id is not registered")
    return doc


def _write_index(path: Path, index: Mapping[str, Any]) -> None:
    atomic_write_text(Path(path), dumps_report(dict(index)))


def check_index_append_only(base_index: Mapping[str, Any], head_index: Mapping[str, Any]) -> list[str]:
    """檢查 base 的每筆 entry 在 head 仍以相同 ID 與完整 JSON 存在。

    entry 順序與物件鍵順序不具身分；陣列順序及其他值均屬身分。此函式不讀 Git、
    不寫檔，也不修改輸入，因此可供本機、PR CI 與其他發布守門共用。
    """
    errors: list[str] = []

    def rows(value: Mapping[str, Any], label: str) -> dict[str, str] | None:
        entries = value.get("entries") if isinstance(value, Mapping) else None
        if not isinstance(entries, list):
            errors.append(f"{label} 缺少 entries 陣列")
            return None
        result: dict[str, str] = {}
        for number, row in enumerate(entries):
            if not isinstance(row, Mapping):
                errors.append(f"{label} entries[{number}] 不是物件")
                continue
            entry_id = row.get("entry_id")
            if not isinstance(entry_id, str) or not entry_id:
                errors.append(f"{label} entries[{number}] 缺少有效 entry_id")
                continue
            if entry_id in result:
                errors.append(f"{label} entries 有重複 entry_id：{entry_id}")
                continue
            try:
                result[entry_id] = dumps_compact(dict(row))
            except (TypeError, ValueError, RecursionError) as exc:
                errors.append(f"{label} entry {entry_id} 不是有效 JSON：{exc}")
        return result

    if not isinstance(base_index, Mapping) or not isinstance(head_index, Mapping):
        return ["base/head index 必須是物件"]
    base_rows = rows(base_index, "base")
    head_rows = rows(head_index, "head")
    if base_rows is None or head_rows is None:
        return errors
    for entry_id, serialized in base_rows.items():
        if entry_id not in head_rows:
            errors.append(f"head 刪除既有 entry：{entry_id}")
        elif head_rows[entry_id] != serialized:
            errors.append(f"head 改寫既有 entry：{entry_id}")
    return errors


def _git_result(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=Path(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )


def _git_text(repo: Path, *args: str) -> str:
    result = _git_result(repo, *args)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git {' '.join(args)} failed ({result.returncode}): {detail}")
    return result.stdout.decode("utf-8", errors="strict").strip()


def _resolve_commit(repo: Path, ref: str) -> str:
    result = _git_result(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if result.returncode:
        raise ValueError(f"無法解析 commit：{ref}")
    commit = result.stdout.decode("ascii", errors="strict").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        raise ValueError(f"Git 回傳不合法 commit ID：{ref}")
    return commit


def _tree_blob(repo: Path, commit: str) -> tuple[str, str] | None:
    result = _git_result(repo, "ls-tree", "-z", "-r", commit, "--", "docs/rust-refactor/evidence/index.json")
    if result.returncode:
        raise ValueError(f"commit 或 tree 物件無法讀取：{commit}")
    if not result.stdout:
        return None
    records = [record for record in result.stdout.split(b"\0") if record]
    exact: list[tuple[str, str]] = []
    for record in records:
        try:
            meta, name = record.split(b"\t", 1)
            mode, kind, oid = meta.split(b" ", 2)
        except ValueError as exc:
            raise ValueError(f"Git 回傳不合法 tree 列：{commit}") from exc
        if name.decode("utf-8", errors="strict") == "docs/rust-refactor/evidence/index.json":
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                raise ValueError(f"證據索引不是一般檔案：{commit}")
            exact.append((mode.decode("ascii", errors="strict"), oid.decode("ascii", errors="strict")))
    if len(exact) > 1:
        raise ValueError(f"Git tree 有重複證據索引路徑：{commit}")
    return exact[0] if exact else None


def _read_git_index(repo: Path, commit: str, *, allow_missing: bool) -> dict[str, Any]:
    # 先列完整 tree；只有成功確認路徑不存在時才可視為首次新增。
    blob = _tree_blob(repo, commit)
    if blob is None:
        if allow_missing:
            return {"entries": []}
        raise ValueError(f"head 缺少證據索引：{commit}")
    result = _git_result(repo, "cat-file", "blob", f"{commit}:docs/rust-refactor/evidence/index.json")
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"索引 blob 無法讀取：{commit}: {detail}")
    try:
        value = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"索引 JSON 無效：{commit}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        raise ValueError(f"索引結構無效：{commit}")
    return value


def check_pr_history_append_only(repo: Path, base_ref: str, head_ref: str) -> bool:
    """逐提交、逐父節點檢查 PR 範圍；回傳範圍內是否有提交改到索引。"""
    root = Path(repo)
    shallow = _git_text(root, "rev-parse", "--is-shallow-repository")
    if shallow == "true":
        raise ValueError("shallow repository 不可驗證")
    base = _resolve_commit(root, base_ref)
    head = _resolve_commit(root, head_ref)
    merge_base = _git_text(root, "merge-base", base, head)
    commits_text = _git_text(root, "rev-list", "--reverse", "--topo-order", f"{merge_base}..{head}")
    commits = commits_text.splitlines() if commits_text else []
    touched = False

    for commit in commits:
        parents_line = _git_text(root, "rev-list", "--parents", "-n", "1", commit)
        parts = parents_line.split()
        if not parts or parts[0] != commit:
            raise ValueError(f"無法確認提交父節點：{commit}")
        parents = parts[1:]
        if not parents:
            raise ValueError(f"範圍內提交沒有可檢查父節點：{commit}")
        current_blob = _tree_blob(root, commit)
        parent_blobs = [_tree_blob(root, parent) for parent in parents]
        if all(blob == current_blob for blob in parent_blobs):
            continue
        touched = True
        current_index = _read_git_index(root, commit, allow_missing=False)
        for parent in parents:
            parent_index = _read_git_index(root, parent, allow_missing=True)
            violations = check_index_append_only(parent_index, current_index)
            if violations:
                raise ValueError("索引只增檢查失敗：" + "; ".join(violations))

    if touched:
        base_index = _read_git_index(root, merge_base, allow_missing=True)
        head_index = _read_git_index(root, head, allow_missing=False)
        violations = check_index_append_only(base_index, head_index)
        if violations:
            raise ValueError("merge-base 至 head 索引只增檢查失敗：" + "; ".join(violations))
    return touched


def main(argv: Sequence[str] | None = None) -> int:
    """PR CI 的薄 CLI；比較規則仍只由純函式實作。"""
    import argparse

    parser = argparse.ArgumentParser(description="Stage 3 證據索引工具")
    parser.add_argument("--check-pr-history", nargs=2, metavar=("BASE", "HEAD"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    if not args.check_pr_history:
        parser.error("必須指定 --check-pr-history BASE HEAD")
    try:
        touched = check_pr_history_append_only(args.repo, *args.check_pr_history)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"evidence index history check failed: {exc}", file=sys.stderr)
        return 1
    state = "checked" if touched else "no index changes in PR commit range"
    print(f"evidence index append-only check: {state}")
    return 0


def _bundle_metadata(bundle_dir: Path, manifest_doc: Mapping[str, Any], platform: str) -> tuple[str, str]:
    from tools.stage3.ab_manifest import bundle_tree_sha256, runtime_tree_sha256, verify_bundle

    violations = verify_bundle(bundle_dir, manifest_doc, label=platform)
    if violations:
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", "; ".join(violations))
    return bundle_tree_sha256(bundle_dir), runtime_tree_sha256(bundle_dir)


def _candidate_round(index: Mapping[str, Any], candidate_round_id: str) -> Mapping[str, Any] | None:
    rounds = index.get("candidate_rounds")
    if not isinstance(rounds, list):
        return None
    return next(
        (row for row in rounds if isinstance(row, Mapping) and row.get("candidate_round_id") == candidate_round_id),
        None,
    )


def _stage3_target_errors(index: Mapping[str, Any]) -> list[str]:
    """驗證 Stage 3 兩平台 target 屬同輪且共用相同凍結契約。"""
    current_id = index.get("current_candidate_round_id")
    round_row = _candidate_round(index, current_id) if isinstance(current_id, str) else None
    targets = index.get("stage_targets", {}).get("3", {})
    if not isinstance(round_row, Mapping) or not isinstance(targets, Mapping):
        return ["both Stage 3 targets must be frozen for the current candidate round"]
    linux = targets.get("linux")
    windows = targets.get("windows")
    if not isinstance(linux, Mapping) or not isinstance(windows, Mapping):
        return ["both Stage 3 targets must be frozen for the current candidate round"]
    errors: list[str] = []
    for platform, target in (("linux", linux), ("windows", windows)):
        if (
            target.get("candidate_round_id") != current_id
            or target.get("rc_tag") != round_row.get("rc_tag")
        ):
            errors.append(f"{platform} target does not match the current candidate round")
    shared_fields = (
        "contract_revision",
        "tool_revision",
        "case_list_sha256",
        "required_cases_sha256",
        "required_cases_lock",
        "exit_catalog_sha256",
        "legacy_scope_sha256",
    )
    for field in shared_fields:
        if linux.get(field) != windows.get(field):
            errors.append(f"Stage 3 targets have mismatched {field}")
    return errors


def begin_candidate_round(index_path: Path, *, rc_tag: str, set_by: str, now: str) -> str:
    """建立新的共同 RC 輪次並立即切換目前輪次；舊紀錄全部保留。"""
    match = _RC_TAG.fullmatch(rc_tag) if isinstance(rc_tag, str) else None
    if match is None or not isinstance(set_by, str) or not set_by:
        raise _invalid("rc_tag or set_by is invalid")
    try:
        parse_iso(now)
    except (TypeError, ValueError) as exc:
        raise _invalid(f"invalid candidate round timestamp: {exc}") from exc
    index = load_index(index_path)
    requested_number = int(match.group(1))
    numbers = [
        int(found.group(1))
        for row in index["candidate_rounds"]
        if isinstance(row, dict)
        and isinstance(row.get("rc_tag"), str)
        and (found := _RC_TAG.fullmatch(row["rc_tag"])) is not None
    ]
    if numbers and requested_number < max(numbers):
        raise _invalid("rc_tag must not move backwards")
    candidate_round_id = uuid.uuid4().hex
    while _candidate_round(index, candidate_round_id) is not None:
        candidate_round_id = uuid.uuid4().hex
    index["candidate_rounds"].append({
        "candidate_round_id": candidate_round_id,
        "rc_tag": rc_tag,
        "set_by": set_by,
        "created_at": now,
    })
    index["current_candidate_round_id"] = candidate_round_id
    _write_index(index_path, index)
    return candidate_round_id


def run_context(index: Mapping[str, Any], *, platform: str, run_id: str) -> dict[str, str]:
    """由目前輪次的兩平台 targets 產生精確的執行身分 context。"""
    if platform not in PLATFORMS or not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise _invalid("platform or run_id is invalid")
    target_errors = _stage3_target_errors(index)
    if target_errors:
        raise _invalid("; ".join(target_errors))
    current_id = index.get("current_candidate_round_id")
    round_row = _candidate_round(index, current_id) if isinstance(current_id, str) else None
    targets = index.get("stage_targets", {}).get("3", {})
    assert isinstance(round_row, Mapping) and isinstance(targets, Mapping)
    target = targets[platform]
    for key in ("archive_sha256", "artifact_id", "required_cases_sha256"):
        if not isinstance(target.get(key), str) or not _SHA256.fullmatch(target[key]):
            raise _invalid(f"Stage 3 {platform} target lacks {key}")
    return {
        "candidate_round_id": str(current_id),
        "rc_tag": str(round_row["rc_tag"]),
        "platform": platform,
        "archive_sha256": str(target["archive_sha256"]),
        "artifact_id": str(target["artifact_id"]),
        "required_cases_sha256": str(target["required_cases_sha256"]),
        "run_id": run_id,
    }


def _catalog_digest() -> str:
    try:
        return _sha256_bytes(STAGE_EXITS_PATH.read_bytes())
    except OSError as exc:
        raise _invalid(f"cannot read current exit catalog: {exc}") from exc


def _index_evidence_root(index_path: Path) -> Path:
    resolved = Path(index_path).resolve()
    if resolved.parent.name == "evidence":
        return resolved.parent
    return resolved.parent / "docs" / "rust-refactor" / "evidence"


def _git_seed_reader(revision: str, repo_path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f"{revision}:{repo_path}"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"cannot read designated seed {revision}:{repo_path}: {detail}")
    return result.stdout


def _prepare_required_cases(evidence_root: Path) -> tuple[dict[str, str], str]:
    """重驗 T1 分母與 seed，並在公開 evidence root 保存原始位元組快照。"""
    from tools.stage3 import required_cases

    errors = required_cases.validate_current_lock(REPO_ROOT)
    if errors:
        raise _invalid("required cases lock is invalid: " + "; ".join(errors))
    lock_bytes = REQUIRED_CASES_LOCK_PATH.read_bytes()
    manifest_bytes = (REPO_ROOT / "docs/rust-refactor/evidence/denominators/source-manifest.json").read_bytes()
    seed_sources = (
        (required_cases.SEED_REVISION, "tests/acceptance/cases.yaml", "denominators/cases.yaml"),
        (required_cases.SEED_REVISION, "tests/acceptance/cases.lock.json", "denominators/cases.lock.json"),
    )
    attachments = [
        (Path("docs/rust-refactor/evidence/denominators/source-manifest.json"), "denominators/source-manifest.json", manifest_bytes),
        (REQUIRED_CASES_LOCK_PATH, "stage3-required-cases.lock.json", lock_bytes),
    ]
    for revision, repo_path, relative in seed_sources:
        try:
            data = _git_seed_reader(revision, repo_path)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise _invalid(f"cannot retrieve designated public seed {repo_path}: {exc}") from exc
        if data is None:
            raise _invalid(f"designated public seed is missing: {repo_path}")
        attachments.append((Path(repo_path), relative, data))
    for source, relative, data in attachments:
        destination = evidence_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.read_bytes() != data:
                raise _invalid(f"frozen required case attachment changed: {relative}")
        else:
            destination.write_bytes(data)
    manifest = _decode_json(manifest_bytes, "required case source manifest")
    seed_errors = required_cases.verify_seed_sources(
        manifest,
        evidence_root=evidence_root,
        source_reader=_git_seed_reader,
    )
    if seed_errors:
        raise _invalid("required case seed is invalid: " + "; ".join(seed_errors))
    digest = _sha256_bytes(lock_bytes)
    return {"path": "stage3-required-cases.lock.json", "sha256": digest}, digest


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
    archive: Path | None = None,
    candidate_round_id: str | None = None,
) -> None:
    """凍結一個 stage/platform target，保留完整 target 歷史。"""
    if stage not in STAGES or platform not in PLATFORMS:
        raise _invalid("unsupported stage or platform")
    if stage == "3" and bundle_dir is None:
        raise _invalid("stage 3 target requires bundle dir")
    if stage == "3" and (archive is None or candidate_round_id is None):
        raise _invalid("stage 3 target requires archive and current candidate round")
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
        manifest_doc = _decode_json(manifest_bytes, "release manifest")
    except OSError as exc:
        raise _invalid(f"cannot read release manifest: {exc}") from exc
    if not isinstance(manifest_doc, dict):
        raise _invalid("release manifest must be an object")
    from tools.stage3.ab_manifest import runtime_digest

    artifact_id = hashlib.sha256(manifest_bytes).hexdigest()
    runtime = runtime_digest(manifest_doc)
    tree_values: dict[str, str] = {}
    if bundle_path is None and stage != "3" and (manifest_path.parent / "SHA256SUMS").is_file():
        # 歷史 target 常只傳 release manifest；以其父目錄做 T3 bundle 核對，
        # 讓跨 Stage reuse 有可比較的 runtime tree 身分。
        bundle_path = manifest_path.parent.resolve()
    if bundle_path is not None:
        tree_values = dict(zip(("bundle_tree_sha256", "runtime_tree_sha256"),
                               _bundle_metadata(bundle_path, manifest_doc, platform)))

    index = load_index(index_path)
    lock_pointer: dict[str, str] | None = None
    required_cases_sha256: str | None = None
    legacy_scope_sha256: str | None = None
    rc_tag: str | None = None
    archive_sha256: str | None = None
    if stage == "3":
        assert candidate_round_id is not None and archive is not None
        if candidate_round_id != index.get("current_candidate_round_id"):
            raise _invalid("stage 3 target must use the current candidate round")
        round_row = _candidate_round(index, candidate_round_id)
        if round_row is None:
            raise _invalid("stage 3 candidate round is not registered")
        rc_tag = str(round_row["rc_tag"])
        targets_for_stage = index.get("stage_targets", {}).get("3", {})
        existing_target = targets_for_stage.get(platform) if isinstance(targets_for_stage, dict) else None
        if isinstance(existing_target, dict) and existing_target.get("candidate_round_id") == candidate_round_id:
            raise _invalid(f"{platform} target is already frozen for this candidate round")
        evidence_root = _index_evidence_root(index_path)
        lock_pointer, required_cases_sha256 = _prepare_required_cases(evidence_root)
        scope_errors = validate_legacy_scope(
            index,
            catalog=load_exit_catalog(),
            evidence_root=evidence_root,
        )
        if scope_errors:
            raise _invalid("legacy obligation scope is invalid: " + "; ".join(scope_errors))
        scope_pointer = index.get("legacy_obligations", {}).get("scope")
        if not _pointer_valid(scope_pointer):
            raise _invalid("stage 3 target requires a frozen legacy obligation scope")
        legacy_scope_sha256 = scope_pointer["sha256"]
        try:
            archive_sha256 = sha256_file(Path(archive))
        except OSError as exc:
            raise _invalid(f"cannot read release archive: {exc}") from exc

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
        "exit_catalog_sha256": _catalog_digest(),
        "required_cases_sha256": required_cases_sha256,
        "required_cases_lock": lock_pointer,
        "candidate_round_id": candidate_round_id,
        "rc_tag": rc_tag,
        "archive_sha256": archive_sha256,
        "legacy_scope_sha256": legacy_scope_sha256,
        "set_by": set_by,
        "set_at": now,
        **tree_values,
    }
    index.setdefault("stage_targets", {}).setdefault(stage, {})[platform] = target
    history_row = {**target, "stage": stage, "platform": platform}
    index.setdefault("target_history", []).append(history_row)
    _write_index(index_path, index)


def add_entry(
    index_path: Path,
    entry: EvidenceEntry | RestrictedFailEntry,
    *,
    catalog: Mapping[str, ExitItem],
) -> None:
    """驗證一筆結果後以原子方式追加至索引。"""
    row = _entry_dict(entry)
    item = catalog.get(entry.item_id)
    if item is None:
        raise HarnessError("HARNESS_EVIDENCE_UNKNOWN_ITEM", entry.item_id)
    restricted = isinstance(entry, RestrictedFailEntry)
    errors = _restricted_entry_errors(row) if restricted else _ordinary_entry_errors(row)
    if errors:
        raise _ingest_invalid("; ".join(errors))
    if row.get("platform") not in PLATFORMS or item.platform not in (row["platform"], "both"):
        raise _ingest_invalid("entry platform does not match item")
    unhashed = {key: value for key, value in row.items() if key != "entry_id"}
    unhashed["record_id"] = ""
    if record_id(unhashed) != row["entry_id"]:
        raise _ingest_invalid("entry_id does not match its content")

    if restricted:
        if item.stage != "3":
            raise _ingest_invalid("restricted FAIL entries are limited to Stage 3 candidate items")
    else:
        if row["result"] in {"PASS", "XFAIL"} and not row["files"]:
            raise _ingest_invalid("PASS and XFAIL require evidence files")
        if row["producer"] == "manual" and item.evidence_kind != "manual-procedure":
            raise _ingest_invalid("manual producer is allowed only for manual-procedure items")
        if item.evidence_kind == "canary-report":
            if row["ledger_records"] is None or (
                row["result"] == "PASS" and row["ledger_through"] is None
            ):
                raise _ingest_invalid("canary report requires ledger version")
        elif row["ledger_through"] is not None or row["ledger_records"] is not None:
            raise _ingest_invalid("non-canary evidence cannot carry a ledger version")
        if row["producer"] == "ingest:junit":
            if not isinstance(row["l1_set_sha256"], str) or not _SHA256.fullmatch(row["l1_set_sha256"]):
                raise _ingest_invalid("junit evidence requires l1_set_sha256")
        elif row["l1_set_sha256"] is not None:
            raise _ingest_invalid("non-junit evidence cannot carry l1_set_sha256")
        if item.required_l1_set and row["producer"] == "ingest:junit" and row["l1_set_sha256"] is None:
            raise _ingest_invalid("L1 junit evidence requires a frozen set digest")
        if item.stage == "3":
            if any(row.get(field) is None for field in ("candidate_round_id", "archive_sha256", "required_cases_sha256")):
                raise _ingest_invalid("Stage 3 entries require round, archive and denominator identity")
        elif any(row.get(field) is not None for field in ("candidate_round_id", "archive_sha256", "required_cases_sha256")):
            raise _ingest_invalid("historical stage entries must not carry Stage 3 round identity")

    index = load_index(index_path)
    entries = index["entries"]
    if any(existing.get("entry_id") == row["entry_id"] for existing in entries):
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


def _a8_substitute_pending(item_id: str, entry: Mapping[str, Any], evidence_root: Path) -> bool:
    """在 BO-P9-19 未裁決前，不把 hash-only／遮罩 CRC 報告算成公開正向證據。"""
    if item_id not in {"S3-L3-P0P1-REMOTE-LINUX", "S3-L3-P0P1-REMOTE-WINDOWS"}:
        return False
    files = entry.get("files")
    if not isinstance(files, list):
        return False
    for file_row in files:
        if not isinstance(file_row, dict):
            continue
        path = _entry_file_path(evidence_root, file_row)
        if path is None:
            continue
        try:
            data = json.loads(path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            continue
        if isinstance(data, dict) and (
            "private_source_sha256" in data
            or "redacted_crc32" in data
            or data.get("payload") == "[redacted]"
        ):
            return True
    return False


def _load_l1_lock(l1_lock: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if l1_lock is not None:
        return l1_lock
    try:
        doc = json.loads(L1_LOCK_PATH.read_bytes().decode("utf-8"))
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
    required_lock: Path | None = None,
    reuse_chain: frozenset[str] = frozenset(),
) -> tuple[str, list[str], list[str]]:
    stage_rows = index.get("stage_targets", {}).get(item.stage, {})
    target = stage_rows.get(platform) if isinstance(stage_rows, dict) else None
    if not isinstance(target, dict):
        return "NO_TARGET", ["no target"], []
    entries = [
        row for row in index.get("entries", [])
        if isinstance(row, dict) and row.get("item_id") == item.item_id and row.get("platform") == platform
    ]
    # 正式 FAIL 對目前 artifact 黏著；先於輪次、目錄角色與一般 entry 新鮮度檢查。
    failures = [
        row for row in entries
        if row.get("result") == "FAIL" and row.get("artifact_id") == target.get("artifact_id")
    ]
    if failures:
        return "FAIL", [f"FAIL: {row.get('entry_id')}" for row in failures], []
    target_errors: list[str] = []
    try:
        if target.get("exit_catalog_sha256") != _catalog_digest():
            target_errors.append("exit catalog changed after target freeze")
    except HarnessError as exc:
        target_errors.append(str(exc))
    if item.stage == "3":
        current_round = index.get("current_candidate_round_id")
        is_od09 = item.item_id == "S3-OD09-ALL-FROZEN"
        if not is_od09 and target.get("candidate_round_id") != current_round:
            target_errors.append("target is not in the current candidate round")

    required_lock_doc: Mapping[str, Any] | None = None
    required_lock_hash: str | None = None
    lock_hash_error: str | None = None
    if item.stage == "3":
        current_lock_path = Path(required_lock) if required_lock is not None else REQUIRED_CASES_LOCK_PATH
        try:
            current_lock_bytes = current_lock_path.read_bytes()
            required_lock_hash = _sha256_bytes(current_lock_bytes)
            loaded_lock = json.loads(current_lock_bytes.decode("utf-8"))
            if isinstance(loaded_lock, dict):
                required_lock_doc = loaded_lock
            else:
                lock_hash_error = "current required cases lock is not an object"
        except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
            lock_hash_error = f"cannot read current required cases lock: {exc}"
        pointer = target.get("required_cases_lock")
        if not _pointer_valid(pointer):
            lock_hash_error = "target required cases lock pointer is invalid"
        else:
            frozen_path = _entry_file_path(evidence_root, pointer)
            if frozen_path is None:
                lock_hash_error = "frozen required cases lock is missing or unsafe"
            else:
                try:
                    frozen_bytes = frozen_path.read_bytes()
                    if _sha256_bytes(frozen_bytes) != pointer["sha256"]:
                        lock_hash_error = "frozen required cases lock hash mismatch"
                except OSError:
                    lock_hash_error = "frozen required cases lock is missing"
        if target.get("required_cases_sha256") != required_lock_hash:
            target_errors.append("required cases changed after target freeze")
        if required_lock_doc is not None and item.required_case_set is not None:
            try:
                required_lock_doc["exits"][item.item_id][platform]
            except (KeyError, TypeError):
                target_errors.append(f"required cases lock lacks {item.item_id}/{platform}")
        if lock_hash_error:
            return "HASH_MISMATCH", [lock_hash_error], []

    current_l1 = _l1_set_sha256(l1_lock or {}, item.required_l1_set, platform) if item.required_l1_set else None
    if item.required_l1_set is not None and current_l1 is None:
        return "MISSING", [f"no frozen l1 set: {item.required_l1_set}"], []
    if not entries:
        return "MISSING", ["no evidence entries"], []

    valid: list[dict[str, Any]] = []
    stale: list[str] = list(target_errors)
    for entry in entries:
        entry_id = str(entry.get("entry_id", ""))
        reasons: list[str] = list(target_errors)
        for field in ("artifact_id", "runtime_digest", "contract_revision", "tool_revision", "exit_catalog_sha256"):
            if entry.get(field) != target.get(field):
                reasons.append(f"stale {field}: {entry_id}")
        if item.required_case_set is not None and entry.get("case_list_sha256") != target.get("case_list_sha256"):
            reasons.append(f"stale case_list_sha256: {entry_id}")
        if item.stage == "3":
            is_od09 = item.item_id == "S3-OD09-ALL-FROZEN"
            if not is_od09 and entry.get("candidate_round_id") != target.get("candidate_round_id"):
                reasons.append(f"stale candidate_round_id: {entry_id}")
            if entry.get("archive_sha256") != target.get("archive_sha256"):
                reasons.append(f"stale archive_sha256: {entry_id}")
            if entry.get("required_cases_sha256") != target.get("required_cases_sha256"):
                reasons.append(f"stale required_cases_sha256: {entry_id}")
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
    a8_pending: list[str] = []
    satisfying: list[str] = []
    for row in successful:
        files_ok, file_reason = _files_status(row, evidence_root)
        if not files_ok:
            hash_mismatch.append(f"{file_reason}: {row.get('entry_id')}")
            continue
        if _a8_substitute_pending(item.item_id, row, evidence_root):
            a8_pending.append(f"BO-P9-19: public substitute verification is not owner-approved: {row.get('entry_id')}")
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
    if a8_pending:
        return "MISSING", a8_pending, []
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
    required_lock: Path | None = None,
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
            required_lock=required_lock,
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
    required_lock: Path | None = None,
) -> tuple[str, list[str]]:
    """依 target、證據身分、檔案雜湊與案例 sentinel 判定單一出口項目。"""
    status, reasons, _ = _item_status_full(
        index,
        item,
        evidence_root=evidence_root,
        cases=cases,
        l1_lock=l1_lock,
        required_lock=required_lock,
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
    required_lock: Path | None = None,
) -> dict[str, Any]:
    """彙整指定 stage 的出口狀態。"""
    if stage not in STAGES:
        raise _invalid(f"unsupported stage: {stage}")
    items: dict[str, Any] = {}
    for item in catalog.values():
        if item.stage != stage:
            continue
        status, reasons, satisfying = _item_status_full(
            index,
            item,
            evidence_root=evidence_root,
            cases=cases,
            l1_lock=l1_lock,
            required_lock=required_lock,
            catalog=catalog,
        )
        items[item.item_id] = {
            "status": status,
            "reasons": reasons,
            "owner_blocked": item.owner_blocked,
            "satisfying_entries": satisfying,
        }
    statuses = [row["status"] for row in items.values()]
    required_statuses = [
        items[item.item_id]["status"]
        for item in catalog.values()
        if item.stage == stage and item.release_requirement == "required"
    ]
    result = (
        "FAIL" if "FAIL" in statuses
        else "BLOCKED" if stage == "3" and _stage3_target_errors(index)
        else "PASS" if required_statuses and all(status == "SATISFIED" for status in required_statuses)
        else "BLOCKED"
    )
    targets = index.get("stage_targets", {}).get(stage, {})
    return {"stage": stage, "targets": dict(targets) if isinstance(targets, dict) else {}, "result": result, "items": items}


def _read_evidence_file(evidence_root: Path, relative: object) -> tuple[bytes | None, str | None]:
    if not _relative_public_path(relative):
        return None, "路徑不是安全的 evidence-relative 路徑"
    root = Path(evidence_root).resolve()
    candidate = root / Path(*str(relative).split("/"))
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if candidate.is_symlink() or not candidate.is_file():
            return None, "附件不是一般檔案"
        return candidate.read_bytes(), None
    except (OSError, ValueError):
        return None, "附件缺失或路徑越界"


def _json_sha256(value: Any) -> str:
    return _sha256_bytes(dumps_compact(value).encode("utf-8"))


def validate_legacy_scope(
    index: Mapping[str, Any],
    *,
    catalog: Mapping[str, ExitItem],
    evidence_root: Path,
) -> list[str]:
    """驗證 S3-E/B5 義務清冊的完整覆蓋、來源快照與具名核定。"""
    errors: list[str] = []
    legacy = index.get("legacy_obligations") if isinstance(index, Mapping) else None
    pointer = legacy.get("scope") if isinstance(legacy, Mapping) else None
    if not _pointer_valid(pointer):
        return ["legacy scope pointer is missing or invalid"]
    raw_scope, read_error = _read_evidence_file(evidence_root, pointer["path"])
    if read_error or raw_scope is None:
        return [f"legacy scope {read_error}"]
    if _sha256_bytes(raw_scope) != pointer["sha256"]:
        return ["legacy scope file hash mismatch"]
    try:
        scope = json.loads(raw_scope.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        return [f"legacy scope is not valid UTF-8 JSON: {exc}"]
    expected_fields = {"schema_version", "exit_catalog_sha256", "sources", "obligations", "review"}
    if not isinstance(scope, dict) or set(scope) != expected_fields or scope.get("schema_version") != 1:
        return ["legacy scope root fields or schema_version are invalid"]
    if scope.get("exit_catalog_sha256") != _catalog_digest():
        errors.append("legacy scope is bound to a different exit catalog")
    if not isinstance(scope.get("sources"), list) or not isinstance(scope.get("obligations"), list):
        return errors + ["legacy scope sources and obligations must be arrays"]

    sources_by_id: dict[str, dict[str, Any]] = {}
    source_hashes: list[dict[str, str]] = []
    for number, source in enumerate(scope["sources"]):
        if not isinstance(source, dict) or set(source) != {"source_id", "version", "snapshot"}:
            errors.append(f"legacy source row {number} has invalid fields")
            continue
        source_id = source.get("source_id")
        snapshot = source.get("snapshot")
        if not isinstance(source_id, str) or not source_id or source_id in sources_by_id:
            errors.append(f"legacy source row {number} has an empty or duplicate source_id")
            continue
        if not isinstance(source.get("version"), str) or not source["version"]:
            errors.append(f"legacy source {source_id} lacks a version")
            continue
        if not isinstance(snapshot, dict) or set(snapshot) != {"path", "sha256"}:
            errors.append(f"legacy source {source_id} snapshot pointer is invalid")
            continue
        if not _relative_public_path(snapshot.get("path")) or not isinstance(snapshot.get("sha256"), str) or not _SHA256.fullmatch(snapshot["sha256"]):
            errors.append(f"legacy source {source_id} snapshot path or SHA is invalid")
            continue
        snapshot_bytes, snapshot_error = _read_evidence_file(evidence_root, snapshot["path"])
        if snapshot_error or snapshot_bytes is None:
            errors.append(f"legacy source {source_id} snapshot {snapshot_error}")
            continue
        actual_hash = _sha256_bytes(snapshot_bytes)
        if actual_hash != snapshot["sha256"]:
            errors.append(f"legacy source {source_id} snapshot hash mismatch")
        sources_by_id[source_id] = source
        source_hashes.append({"source_id": source_id, "sha256": snapshot["sha256"]})

    obligations_by_key: dict[str, dict[str, Any]] = {}
    for number, obligation in enumerate(scope["obligations"]):
        required_fields = {
            "key", "stage", "platform", "item_id", "source_refs", "required_scope",
            "required_scope_sha256", "safety_invariant", "allowed_evidence",
        }
        if not isinstance(obligation, dict) or set(obligation) != required_fields:
            errors.append(f"legacy obligation row {number} has invalid fields")
            continue
        key = obligation.get("key")
        if not isinstance(key, str) or not key or key in obligations_by_key:
            errors.append(f"legacy obligation row {number} has an empty or duplicate key")
            continue
        obligations_by_key[key] = obligation
        if not isinstance(obligation.get("source_refs"), list) or not obligation["source_refs"]:
            errors.append(f"legacy obligation {key} lacks source_refs")
        else:
            for source_ref in obligation["source_refs"]:
                if not isinstance(source_ref, str) or source_ref not in sources_by_id:
                    errors.append(f"legacy obligation {key} has an unresolved source_ref")
        scope_body = obligation.get("required_scope")
        if not isinstance(scope_body, dict) or not isinstance(scope_body.get("case_ids"), list):
            errors.append(f"legacy obligation {key} has invalid required_scope")
        elif _json_sha256(scope_body) != obligation.get("required_scope_sha256"):
            errors.append(f"legacy obligation {key} required_scope_sha256 mismatch")
        if not isinstance(obligation.get("safety_invariant"), bool):
            errors.append(f"legacy obligation {key} safety_invariant must be boolean")
        allowed = obligation.get("allowed_evidence")
        if (
            not isinstance(allowed, dict)
            or set(allowed) != {"kinds", "case_ids"}
            or not isinstance(allowed.get("kinds"), list)
            or not isinstance(allowed.get("case_ids"), list)
        ):
            errors.append(f"legacy obligation {key} has invalid allowed_evidence")
        elif scope_body and allowed["case_ids"] != scope_body.get("case_ids"):
            errors.append(f"legacy obligation {key} allowed_evidence cases differ from required_scope")

    expected: dict[str, tuple[ExitItem, str]] = {}
    for item in catalog.values():
        if item.stage == "3" or item.release_requirement != "required":
            continue
        platforms = PLATFORMS if item.platform == "both" else (item.platform,)
        for platform in platforms:
            expected[f"{item.stage}:{item.item_id}:{platform}"] = (item, platform)
    for key, (item, platform) in expected.items():
        obligation = obligations_by_key.get(key)
        if obligation is None:
            errors.append(f"legacy scope is missing required catalog obligation: {key}")
            continue
        if (
            obligation.get("stage") != item.stage
            or obligation.get("platform") != platform
            or obligation.get("item_id") != item.item_id
        ):
            errors.append(f"legacy obligation {key} does not match its catalog item/platform")
        if item.spec_ref not in obligation.get("source_refs", []):
            errors.append(f"legacy obligation {key} does not cite {item.spec_ref}")
        allowed = obligation.get("allowed_evidence")
        if isinstance(allowed, dict) and allowed.get("kinds") != [item.evidence_kind]:
            errors.append(f"legacy obligation {key} allows the wrong evidence kind")
    if not any(
        row.get("stage") == "legacy" and row.get("item_id") not in catalog
        for row in obligations_by_key.values()
    ):
        errors.append("legacy scope is missing non-catalog source obligations")

    review = scope.get("review")
    review_fields = {"reviewer_id", "approved_at", "decision_ref", "record"}
    if not isinstance(review, dict) or set(review) != review_fields:
        errors.append("legacy scope review record is missing or malformed")
    else:
        if not all(isinstance(review.get(key), str) and review[key] for key in ("reviewer_id", "decision_ref")):
            errors.append("legacy scope review lacks reviewer_id or decision_ref")
        try:
            parse_iso(review["approved_at"])
        except (KeyError, TypeError, ValueError):
            errors.append("legacy scope review approved_at is invalid")
        record = review.get("record")
        if not isinstance(record, dict) or set(record) != {"path", "sha256"} or not _relative_public_path(record.get("path")) or not isinstance(record.get("sha256"), str) or not _SHA256.fullmatch(record["sha256"]):
            errors.append("legacy scope review FrozenFile is invalid")
        else:
            record_bytes, record_error = _read_evidence_file(evidence_root, record["path"])
            if record_error or record_bytes is None:
                errors.append(f"legacy scope review attachment {record_error}")
            elif _sha256_bytes(record_bytes) != record["sha256"]:
                errors.append("legacy scope review attachment hash mismatch")
            else:
                try:
                    record_doc = json.loads(record_bytes.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, RecursionError):
                    record_doc = None
                if not isinstance(record_doc, dict) or not isinstance(record_doc.get("source_hashes"), list):
                    errors.append("legacy review attachment lacks source_hashes")
                else:
                    actual_hashes = sorted(source_hashes, key=lambda row: row["source_id"])
                    declared_hashes = record_doc["source_hashes"]
                    if declared_hashes != actual_hashes:
                        errors.append("legacy review source_hashes do not match the source snapshots")
                    scope_body = {key: value for key, value in scope.items() if key != "review"}
                    if record_doc.get("scope_sha256") != _json_sha256(scope_body):
                        errors.append("legacy review scope_sha256 mismatch")
    return errors


def _resolution_files_valid(files: Any, evidence_root: Path) -> tuple[bool, str | None]:
    if not isinstance(files, list) or not files:
        return False, "resolution must freeze at least one target file"
    return _files_status({"files": files}, evidence_root)


def legacy_status(
    index: Mapping[str, Any],
    *,
    catalog: Mapping[str, ExitItem],
    evidence_root: Path,
    cases: Mapping[str, dict],
    l1_lock: Mapping[str, Any] | None = None,
    required_lock: Path | None = None,
) -> dict[str, Any]:
    """重驗正式遺留義務與核定證據；未裁決的 BO-P9-14／19 保持阻擋。"""
    legacy = index.get("legacy_obligations", {}) if isinstance(index, Mapping) else {}
    pointer = legacy.get("scope") if isinstance(legacy, Mapping) else None
    scope_errors = validate_legacy_scope(index, catalog=catalog, evidence_root=evidence_root)
    obligations: dict[str, Any] = {}
    scope_sha = pointer.get("sha256") if _pointer_valid(pointer) else None
    scope: dict[str, Any] | None = None
    if _pointer_valid(pointer):
        raw, error = _read_evidence_file(evidence_root, pointer["path"])
        if not error and raw is not None:
            try:
                parsed = json.loads(raw.decode("utf-8"))
                scope = parsed if isinstance(parsed, dict) else None
            except (UnicodeDecodeError, ValueError, RecursionError):
                scope = None
    scope_rows = scope.get("obligations", []) if isinstance(scope, dict) else []
    if not isinstance(scope_rows, list):
        scope_rows = []

    resolutions: dict[str, dict[str, Any]] = {}
    resolution_pointer = legacy.get("resolutions") if isinstance(legacy, Mapping) else None
    resolution_error: str | None = None
    if resolution_pointer is not None:
        if not _pointer_valid(resolution_pointer):
            resolution_error = "legacy resolutions pointer is invalid"
        else:
            raw, error = _read_evidence_file(evidence_root, resolution_pointer["path"])
            if error or raw is None:
                resolution_error = f"legacy resolutions {error}"
            elif _sha256_bytes(raw) != resolution_pointer["sha256"]:
                resolution_error = "legacy resolutions file hash mismatch"
            else:
                try:
                    doc = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, RecursionError):
                    doc = None
                if (
                    not isinstance(doc, dict)
                    or set(doc) != {"schema_version", "scope_sha256", "resolutions"}
                    or doc.get("schema_version") != 1
                    or doc.get("scope_sha256") != scope_sha
                    or not isinstance(doc.get("resolutions"), list)
                ):
                    resolution_error = "legacy resolutions schema or scope SHA is invalid"
                else:
                    for row in doc["resolutions"]:
                        if not isinstance(row, dict) or not isinstance(row.get("key"), str) or row["key"] in resolutions:
                            resolution_error = "legacy resolutions contain malformed or duplicate keys"
                            break
                        resolutions[row["key"]] = row

    by_entry_id = {
        row.get("entry_id"): row
        for row in index.get("entries", [])
        if isinstance(row, dict) and isinstance(row.get("entry_id"), str)
    }
    overall = "BLOCKED"
    for obligation in scope_rows:
        if not isinstance(obligation, dict) or not isinstance(obligation.get("key"), str):
            continue
        key = obligation["key"]
        row = resolutions.get(key)
        result = {"status": "BLOCKED", "reasons": [], "entry_ids": [], "decision_ref": None}
        if scope_errors:
            result["reasons"].extend(scope_errors)
        if resolution_error:
            result["reasons"].append(resolution_error)
        if row is None:
            result["reasons"].append("no owner-approved resolution; BO-P9-14")
        elif row.get("method") == "pending" and set(row) == {"key", "method"}:
            result["reasons"].append("resolution is pending; BO-P9-14")
        elif row.get("method") == "retest":
            allowed_keys = {"key", "method", "entry_id", "target_sha256", "files"}
            entry_id = row.get("entry_id")
            entry = by_entry_id.get(entry_id)
            valid = set(row) == allowed_keys and isinstance(entry_id, str) and entry is not None
            if not valid:
                result["reasons"].append("retest resolution lacks a registered entry or exact fields")
            else:
                item_id = obligation.get("item_id")
                platform = obligation.get("platform")
                item = catalog.get(item_id) if isinstance(item_id, str) else None
                if item is None or entry.get("item_id") != item_id or entry.get("platform") != platform:
                    result["reasons"].append("retest entry does not match the catalog obligation")
                else:
                    target = index.get("stage_targets", {}).get(item.stage, {}).get(platform)
                    files_ok, file_reason = _resolution_files_valid(row.get("files"), evidence_root)
                    target_sha = row.get("target_sha256")
                    frozen_target: dict[str, Any] | None = None
                    if files_ok and isinstance(row.get("files"), list):
                        for file_row in row["files"]:
                            if isinstance(file_row, dict):
                                raw_target, read_error = _read_evidence_file(evidence_root, file_row.get("path"))
                                if raw_target is not None and not read_error:
                                    try:
                                        parsed_target = json.loads(raw_target.decode("utf-8"))
                                    except (UnicodeDecodeError, ValueError, RecursionError):
                                        parsed_target = None
                                    if isinstance(parsed_target, dict):
                                        frozen_target = parsed_target
                                        if _sha256_bytes(raw_target) != target_sha:
                                            frozen_target = None
                    if (
                        not files_ok
                        or not isinstance(target_sha, str)
                        or not _SHA256.fullmatch(target_sha)
                        or frozen_target is None
                        or not isinstance(target, dict)
                        or dumps_compact(frozen_target) != dumps_compact(target)
                    ):
                        result["reasons"].append(file_reason or "retest target snapshot does not match the frozen target")
                    else:
                        status, reasons = item_status(
                            index,
                            item,
                            evidence_root=evidence_root,
                            cases=cases,
                            l1_lock=l1_lock,
                            required_lock=required_lock,
                        )
                        result["entry_ids"] = [entry_id]
                        if status == "FAIL":
                            result["status"] = "FAIL"
                            result["reasons"].extend(reasons)
                            overall = "FAIL"
                        elif status == "SATISFIED":
                            result["status"] = "PASS"
                            result["reasons"].append("retest entry and frozen target revalidated")
                        else:
                            result["reasons"].extend(reasons or [f"retest evidence is {status}"])
        elif row.get("method") == "new_od":
            result["decision_ref"] = row.get("decision_ref")
            result["reasons"].append("new OD resolution requires exact owner-named scope and evidence; BO-P9-14")
        else:
            result["reasons"].append("unknown or malformed resolution method")
        if result["status"] == "PASS" and result["decision_ref"] is None:
            # 一筆有效 retest 可覆蓋該義務，但不代表擁有者裁決已解除發布阻擋。
            pass
        obligations[key] = result

    if overall != "FAIL":
        overall = "BLOCKED"
    # A8 的公開替代驗證尚未裁決；即使附件僅含 hash/摘要也不能把 legacy gate 宣稱為 PASS。
    for result in obligations.values():
        if result["status"] != "FAIL" and not any("BO-P9-19" in reason for reason in result["reasons"]):
            result["reasons"].append("public substitute verification is pending owner decision; BO-P9-19")
    return {"result": overall, "scope_sha256": scope_sha, "obligations": obligations}


def render_report(
    index: Mapping[str, Any],
    catalog: Mapping[str, ExitItem],
    *,
    evidence_root: Path,
    cases: Mapping[str, dict],
    l1_lock: Mapping[str, Any] | None = None,
    required_lock: Path | None = None,
) -> str:
    """輸出各 stage 與正式遺留義務清冊的 Markdown 狀態。"""
    lines = ["# Stage 0～3 證據出口", ""]
    for stage in STAGES:
        result = stage_status(
            index,
            stage,
            catalog=catalog,
            evidence_root=evidence_root,
            cases=cases,
            l1_lock=l1_lock,
            required_lock=required_lock,
        )
        lines.extend((f"## Stage {stage} — {result['result']}", "", "| 項目 | 平台 | 狀態 | 說明 |", "|---|---|---|---|"))
        for item_id, row in result["items"].items():
            item = catalog[item_id]
            status = "BLOCKED-OWNER" if item.owner_blocked and row["status"] != "SATISFIED" else row["status"]
            detail = "; ".join(row["reasons"]).replace("|", "\\|")
            lines.append(f"| {item_id} | {item.platform} | {status} | {detail} |")
        lines.append("")
    legacy = legacy_status(
        index,
        catalog=catalog,
        evidence_root=evidence_root,
        cases=cases,
        l1_lock=l1_lock,
        required_lock=required_lock,
    )
    lines.extend((f"## Stage 0～2.4 遺留義務 — {legacy['result']}", "", "| 義務 | 狀態 | entry | 原因 |", "|---|---|---|---|"))
    for key, row in legacy["obligations"].items():
        detail = "; ".join(row["reasons"]).replace("|", "\\|")
        entry_ids = ",".join(row["entry_ids"])
        lines.append(f"| {key} | {row['status']} | {entry_ids} | {detail} |")
    if not legacy["obligations"]:
        lines.append("| — | BLOCKED | — | legacy scope 尚未完整核定 |")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
