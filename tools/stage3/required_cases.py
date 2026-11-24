"""Stage 3 逐出口案例分母與公開種子來源檢查。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

import yaml

SEED_REVISION = "59d8c7f1eb7de1ae2a90cefd6df21ce945d695d2"
SEED_DECISION = "OD-113"
SEED_EXCLUDED = ["lr-mixed"]
SEED_FILES = {
    "tests/acceptance/cases.yaml": "denominators/cases.yaml",
    "tests/acceptance/cases.lock.json": "denominators/cases.lock.json",
}
EVIDENCE_RELATIVE = Path("docs/rust-refactor/evidence")
ACCEPTANCE_RELATIVE = Path("tests/acceptance")
SOURCE_MANIFEST_REPO_PATH = "docs/rust-refactor/evidence/denominators/source-manifest.json"
CASE_ATTR_KEYS = (
    "suite", "layer", "platforms", "expected", "necessity", "daemon_slice", "cli_stage", "stage0_required",
)
# 僅 mirror schema 中 additionalProperties:false 的物件鍵集合；測試會逐一與
# stage3_required_cases.schema.json 的 properties／required 對照，避免兩份契約漂移。
_LOCK_OBJECT_SHAPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "": (
        frozenset({"schema_version", "seed", "exits"}),
        frozenset({"schema_version", "seed", "exits"}),
    ),
    "$defs.Seed": (
        frozenset({"revision", "cases", "case_lock", "decision_ref", "excluded_case_ids"}),
        frozenset({"revision", "cases", "case_lock", "decision_ref", "excluded_case_ids"}),
    ),
    "$defs.FrozenFile": (
        frozenset({"path", "sha256"}),
        frozenset({"path", "sha256"}),
    ),
    "$defs.ExitPlatform": (
        frozenset({"required_case_set", "layer", "case_ids"}),
        frozenset({"required_case_set", "layer", "case_ids"}),
    ),
}
_SOURCE_MANIFEST_OBJECT_SHAPES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "": (
        frozenset({"schema_version", "sources", "initial_exit_platforms"}),
        frozenset({"schema_version", "sources", "initial_exit_platforms"}),
    ),
    "$defs.Source": (
        frozenset({"attachment", "path", "revision", "sha256"}),
        frozenset({"attachment", "path", "revision", "sha256"}),
    ),
    "$defs.InitialExitPlatform": (
        frozenset({"item_id", "platform", "layer"}),
        frozenset({"item_id", "platform", "layer"}),
    ),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXIT_ITEM_ID = re.compile(r"^S3-[A-Z0-9-]+$")
SourceReader = Callable[[str, str], bytes | None]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return None
    return path.as_posix()


def _source_manifest_schema_errors(manifest: Mapping[str, Any]) -> list[str]:
    """精確檢查公開來源 manifest 的物件欄位與初始出口／平台基準。"""
    errors: list[str] = []

    def closed_object(value: Any, schema_path: str, label: str) -> bool:
        if not isinstance(value, Mapping):
            errors.append(f"{label} 必須是 schema 物件")
            return False
        properties, required = _SOURCE_MANIFEST_OBJECT_SHAPES[schema_path]
        actual = set(value)
        missing = required - actual
        unknown = actual - properties
        if missing or unknown:
            details: list[str] = []
            if missing:
                details.append(f"缺少欄位：{', '.join(sorted(missing))}")
            if unknown:
                details.append(f"未知欄位：{', '.join(sorted(unknown))}")
            errors.append(f"{label} 的 schema 欄位集合不符（{'；'.join(details)}）")
        return True

    if not closed_object(manifest, "", "來源 manifest"):
        return errors
    sources = manifest.get("sources")
    if isinstance(sources, list):
        for index, source in enumerate(sources):
            closed_object(source, "$defs.Source", f"來源 manifest sources[{index}]")

    initial = manifest.get("initial_exit_platforms")
    if not isinstance(initial, list):
        errors.append("來源 manifest initial_exit_platforms 必須是陣列")
        return errors
    if not initial:
        errors.append("來源 manifest initial_exit_platforms 不得為空")
        return errors

    identities: list[tuple[str, str, str]] = []
    for index, item in enumerate(initial):
        if not closed_object(item, "$defs.InitialExitPlatform", f"來源 manifest initial_exit_platforms[{index}]"):
            continue
        item_id, platform, layer = item.get("item_id"), item.get("platform"), item.get("layer")
        if not isinstance(item_id, str) or not _EXIT_ITEM_ID.fullmatch(item_id):
            errors.append(f"來源 manifest initial_exit_platforms[{index}] 的 item_id 格式錯誤")
            continue
        if platform not in ("linux", "windows"):
            errors.append(f"來源 manifest initial_exit_platforms[{index}] 的 platform 格式錯誤")
            continue
        if not isinstance(layer, str) or not layer:
            errors.append(f"來源 manifest initial_exit_platforms[{index}] 的 layer 格式錯誤")
            continue
        identities.append((item_id, platform, layer))
    if len(identities) != len(set(identities)):
        errors.append("來源 manifest initial_exit_platforms 不得重複列出出口／平台／layer")
    if identities != sorted(identities):
        errors.append("來源 manifest initial_exit_platforms 必須依出口 ID、平台、layer 排序")
    return errors


def _git_show(repo_root: Path, revision: str, repo_path: str) -> bytes | None:
    """讀 Git blob；只有已確認的「該路徑不在 revision」回傳 None。

    不解析 git 的錯誤訊息（會隨 locale 翻譯）：先把 revision 解析成 commit，
    再以 `ls-tree` 判斷路徑是否存在，最後讀 blob。revision 無法解析、路徑不是
    blob 或讀取失敗一律拋 ValueError，不當成「不存在」。
    """

    def _run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", *argv], cwd=repo_root, capture_output=True, check=False)

    def _err(result: subprocess.CompletedProcess[bytes]) -> str:
        return result.stderr.decode("utf-8", errors="replace").strip()

    resolved = _run(["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"])
    if resolved.returncode != 0:
        raise ValueError(f"無法解析 Git revision {revision}：{_err(resolved)}")
    commit = resolved.stdout.decode("ascii", errors="replace").strip()
    listing = _run(["ls-tree", "-z", commit, "--", repo_path])
    if listing.returncode != 0:
        raise ValueError(f"無法列出 Git 來源 {revision}:{repo_path}：{_err(listing)}")
    if not listing.stdout:
        return None
    meta = listing.stdout.split(b"\0", 1)[0].split(b"\t", 1)[0].split()
    if len(meta) < 2 or meta[1] != b"blob":
        raise ValueError(f"Git 來源 {revision}:{repo_path} 不是檔案")
    blob = _run(["cat-file", "blob", f"{commit}:{repo_path}"])
    if blob.returncode != 0:
        raise ValueError(f"無法讀取 Git 來源 {revision}:{repo_path}：{_err(blob)}")
    return blob.stdout


def verify_seed_sources(
    manifest: Mapping[str, Any],
    *,
    evidence_root: Path,
    source_reader: SourceReader | None = None,
) -> list[str]:
    """核對指定靜態種子的 revision、來源路徑、附件與原始 Git blob SHA。"""
    if not isinstance(manifest, Mapping):
        return ["來源 manifest 必須是物件"]
    errors = _source_manifest_schema_errors(manifest)
    if errors:
        return errors
    if manifest.get("schema_version") != 1:
        return ["來源 manifest 的 schema_version 必須為 1"]
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        return ["來源 manifest 缺少 sources 陣列"]
    if len(sources) != len(SEED_FILES):
        errors.append("來源 manifest 必須完整列出指定靜態種子")
    expected_by_path = {path: attachment for path, attachment in SEED_FILES.items()}
    seen: set[str] = set()
    root = Path(evidence_root)

    for source in sources:
        if not isinstance(source, Mapping):
            errors.append("來源 manifest 項目必須是物件")
            continue
        revision = source.get("revision")
        repo_path = source.get("path")
        attachment = _relative_path(source.get("attachment"))
        if repo_path not in expected_by_path:
            errors.append(f"非指定的靜態種子來源：{repo_path!r}")
            continue
        if repo_path in seen:
            errors.append(f"來源 manifest 重複列出：{repo_path}")
            continue
        seen.add(repo_path)
        if revision != SEED_REVISION:
            errors.append(f"{repo_path} 的 revision 必須為指定種子 revision")
        if attachment != expected_by_path[repo_path]:
            errors.append(f"{repo_path} 的 evidence attachment 路徑不符")
            continue
        declared_sha = source.get("sha256")
        if not isinstance(declared_sha, str) or not _SHA256.fullmatch(declared_sha):
            errors.append(f"{repo_path} 的 sha256 格式錯誤")
            continue
        try:
            attached_bytes = (root / attachment).read_bytes()
        except OSError as exc:
            errors.append(f"無法讀取種子附件 {attachment}: {exc}")
            continue
        attached_sha = _sha256(attached_bytes)
        if attached_sha != declared_sha:
            errors.append(f"種子附件 {attachment} 與 manifest SHA 不符")
        reader = source_reader or (lambda rev, path: _git_show(root.parents[2], rev, path))
        try:
            source_bytes = reader(SEED_REVISION, repo_path)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f"無法重現指定 Git 種子 {repo_path}: {exc}")
            continue
        if source_bytes is None:
            errors.append(f"指定 revision 缺少靜態種子 {repo_path}")
            continue
        source_sha = _sha256(source_bytes)
        if source_sha != declared_sha:
            errors.append(f"{repo_path} 的 Git blob 與 manifest SHA 不符")
        if source_sha != attached_sha:
            errors.append(f"{repo_path} 的 Git blob 與公開附件位元組不符")

    missing = set(expected_by_path) - seen
    if missing:
        errors.append(f"來源 manifest 缺少指定種子：{', '.join(sorted(missing))}")
    return errors


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"無法讀取 JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根節點必須是物件：{path}")
    return value


def _case_maps(cases_doc: Mapping[str, Any], case_lock: Mapping[str, Any]) -> tuple[dict[str, dict], set[str]]:
    cases = cases_doc.get("cases")
    sets = case_lock.get("sets")
    attrs = case_lock.get("case_attrs")
    if not isinstance(cases, list) or not isinstance(sets, dict) or not isinstance(attrs, dict):
        raise ValueError("cases.yaml 或 cases.lock.json 結構不完整")
    if "slice_2.4" not in sets or not isinstance(sets["slice_2.4"], list):
        raise ValueError("cases.lock.json 缺少 slice_2.4 集合")
    case_map: dict[str, dict] = {}
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str):
            raise ValueError("cases.yaml 含無效案例項目")
        case_id = case["case_id"]
        if case_id in case_map:
            raise ValueError(f"cases.yaml 有重複 case_id：{case_id}")
        case_map[case_id] = case
    if len(sets["slice_2.4"]) != len(set(sets["slice_2.4"])):
        raise ValueError("slice_2.4 含重複案例 ID")
    selected = set(sets["slice_2.4"])
    if not selected <= set(case_map):
        raise ValueError("slice_2.4 引用了 cases.yaml 不存在的案例")
    for case_id, case in case_map.items():
        recorded = attrs.get(case_id)
        if not isinstance(recorded, dict):
            raise ValueError(f"cases.lock.json 缺少案例屬性快照：{case_id}")
        expected = {key: case.get(key) for key in CASE_ATTR_KEYS}
        if recorded != expected:
            raise ValueError(f"案例屬性與 cases.lock.json 不符：{case_id}")
    if set(attrs) != set(case_map):
        raise ValueError("cases.lock.json 的 case_attrs 與 cases.yaml 案例集合不同")
    return case_map, selected


def _catalog_exits(catalog: Mapping[str, Any]) -> dict[str, dict]:
    items = catalog.get("items")
    if not isinstance(items, list):
        raise ValueError("stage_exits.json 缺少 items 陣列")
    exits: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("item_id"), str):
            raise ValueError("stage_exits.json 含無效出口項目")
        if item["stage"] != "3" or item.get("required_case_set") is None:
            continue
        item_id = item["item_id"]
        if item_id in exits:
            raise ValueError(f"stage_exits.json 有重複 item_id：{item_id}")
        if item.get("required_case_set") != "slice_2.4":
            raise ValueError(f"Stage 3 案例出口必須使用 slice_2.4：{item_id}")
        exits[item_id] = item
    return exits


def _applicable_platforms(item: Mapping[str, Any]) -> tuple[str, ...]:
    platform = item.get("platform")
    if platform == "both":
        return ("linux", "windows")
    if platform in ("linux", "windows"):
        return (platform,)
    raise ValueError(f"不支援的出口平台：{platform!r}")


def _expected_exit_cases(
    catalog: Mapping[str, Any], cases_doc: Mapping[str, Any], case_lock: Mapping[str, Any],
) -> dict[str, dict[str, dict[str, Any]]]:
    case_map, slice_ids = _case_maps(cases_doc, case_lock)
    expected: dict[str, dict[str, dict[str, Any]]] = {}
    for item_id, item in _catalog_exits(catalog).items():
        layer = item["layer"]
        families = item.get("required_families")
        item_platforms: dict[str, dict[str, Any]] = {}
        for platform in _applicable_platforms(item):
            ids = {
                case_id for case_id in slice_ids
                if case_id not in SEED_EXCLUDED
                and case_map[case_id].get("layer") == layer
                and case_map[case_id].get("necessity") == "required"
                and platform in case_map[case_id].get("platforms", [])
                # L3 的 required_families 僅是目錄資訊；完整分母涵蓋該平台全部適用必要 L3 案例。
                and (layer == "L3" or families is None or case_map[case_id].get("family") in families)
            }
            item_platforms[platform] = {
                "required_case_set": "slice_2.4",
                "layer": layer,
                "case_ids": sorted(ids),
            }
        expected[item_id] = item_platforms
    return expected


def _seed_expected_exit_cases(catalog: Mapping[str, Any], evidence_root: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """只用經核實的固定附件展開初始基準，避免目前 cases／case lock 同時縮減基準。"""
    try:
        cases_doc = yaml.safe_load((evidence_root / SEED_FILES["tests/acceptance/cases.yaml"]).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"無法讀取固定 seed 基準 cases.yaml：{exc}") from exc
    if not isinstance(cases_doc, dict):
        raise ValueError("固定 seed 基準 cases.yaml 根節點必須是物件")
    case_lock = _read_json(evidence_root / SEED_FILES["tests/acceptance/cases.lock.json"])
    return _expected_exit_cases(catalog, cases_doc, case_lock)


def _validate_seed_floor(lock: Mapping[str, Any], seed_expected: Mapping[str, Any]) -> list[str]:
    """確認目前鎖保留 seed 展開的每個初始出口／平台必要 ID。"""
    errors: list[str] = []
    exits = lock.get("exits")
    if not isinstance(exits, dict):
        return ["分母鎖缺少 exits，無法核對固定 seed 基準"]
    for item_id, platforms in seed_expected.items():
        actual_platforms = exits.get(item_id)
        if not isinstance(actual_platforms, dict):
            errors.append(f"分母鎖缺少固定 seed 基準出口：{item_id}")
            continue
        for platform, baseline in platforms.items():
            actual = actual_platforms.get(platform)
            if not isinstance(actual, dict):
                errors.append(f"分母鎖缺少固定 seed 基準平台：{item_id}/{platform}")
                continue
            for key in ("required_case_set", "layer"):
                if actual.get(key) != baseline[key]:
                    errors.append(f"固定 seed 基準出口屬性不符：{item_id}/{platform}/{key}")
            ids = actual.get("case_ids")
            if not isinstance(ids, list) or not all(isinstance(case_id, str) for case_id in ids):
                errors.append(f"固定 seed 基準的 case_ids 格式錯誤：{item_id}/{platform}")
                continue
            missing = set(baseline["case_ids"]) - set(ids)
            if missing:
                errors.append(
                    f"分母鎖小於固定 seed 基準 {item_id}/{platform}：{', '.join(sorted(missing))}"
                )
    return errors


def _initial_exit_platform_errors(
    initial_exit_platforms: list[Mapping[str, Any]],
    expected: Mapping[str, Any],
    candidate: Mapping[str, Any] | None = None,
) -> list[str]:
    """要求目前目錄及候選鎖保留固定 manifest 的每個出口／平台／layer。"""
    errors: list[str] = []
    exits = candidate.get("exits") if isinstance(candidate, Mapping) else None
    for baseline in initial_exit_platforms:
        item_id = baseline["item_id"]
        platform = baseline["platform"]
        layer = baseline["layer"]
        current_entry = expected.get(item_id, {}).get(platform)
        if not isinstance(current_entry, Mapping) or current_entry.get("layer") != layer:
            errors.append(f"目前 stage_exits.json 缺少固定初始出口／平台／layer：{item_id}/{platform}/{layer}")
        if candidate is None:
            continue
        item_lock = exits.get(item_id) if isinstance(exits, Mapping) else None
        locked_entry = item_lock.get(platform) if isinstance(item_lock, Mapping) else None
        if not isinstance(locked_entry, Mapping) or locked_entry.get("layer") != layer:
            errors.append(f"候選分母鎖缺少固定初始出口／平台／layer：{item_id}/{platform}/{layer}")
    return errors


def _case_family_errors(current_cases: Mapping[str, Any], previous_cases: Mapping[str, Any], *, source: str) -> list[str]:
    current = {case["case_id"]: case for case in current_cases.get("cases", []) if isinstance(case, dict)}
    previous = {case["case_id"]: case for case in previous_cases.get("cases", []) if isinstance(case, dict)}
    errors: list[str] = []
    for case_id in sorted(set(current) & set(previous)):
        if current[case_id].get("family") != previous[case_id].get("family"):
            errors.append(f"案例 family 不可重分類（{source}）：{case_id}")
    return errors


def _seed_family_errors(current_cases: Mapping[str, Any], evidence_root: Path) -> list[str]:
    """固定 seed 中既有案例的 family 標籤不可藉重新分類改寫。"""
    try:
        seed_cases = yaml.safe_load(
            (evidence_root / SEED_FILES["tests/acceptance/cases.yaml"]).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"無法讀取固定 seed 案例 family：{exc}") from exc
    if not isinstance(seed_cases, dict):
        raise ValueError("固定 seed 案例 family 根節點必須是物件")
    return _case_family_errors(current_cases, seed_cases, source="固定 seed")


def _lock_schema_errors(lock: Mapping[str, Any]) -> list[str]:
    """以 stdlib 檢查分母鎖的 schema 物件鍵、required 與動態 map 約束。"""
    errors: list[str] = []

    def closed_object(value: Any, schema_path: str, label: str) -> bool:
        if not isinstance(value, Mapping):
            errors.append(f"{label} 必須是 schema 物件")
            return False
        properties, required = _LOCK_OBJECT_SHAPES[schema_path]
        actual = set(value)
        missing = required - actual
        unknown = actual - properties
        if missing or unknown:
            details: list[str] = []
            if missing:
                details.append(f"缺少欄位：{', '.join(sorted(missing))}")
            if unknown:
                details.append(f"未知欄位：{', '.join(sorted(unknown))}")
            errors.append(f"{label} 的 schema 欄位集合不符（{'；'.join(details)}）")
        return True

    closed_object(lock, "", "分母鎖根物件")
    seed = lock.get("seed")
    if "seed" in lock and closed_object(seed, "$defs.Seed", "分母鎖 seed"):
        for name in ("cases", "case_lock"):
            if name in seed:
                closed_object(seed[name], "$defs.FrozenFile", f"分母鎖 seed.{name}")

    exits = lock.get("exits")
    if "exits" not in lock:
        pass  # 根物件的 required 欄位診斷已涵蓋。
    elif not isinstance(exits, Mapping):
        errors.append("分母鎖 exits 必須是 schema 物件")
    elif not exits:
        errors.append("分母鎖 exits 違反 schema minProperties（至少一個出口）")
    else:
        for item_id, platforms in exits.items():
            if not isinstance(item_id, str) or not _EXIT_ITEM_ID.fullmatch(item_id):
                errors.append(f"分母鎖 exits 含不符合 schema propertyNames 的出口：{item_id!r}")
            if not isinstance(platforms, Mapping):
                errors.append(f"分母鎖出口 {item_id!r} 必須是 schema 物件")
                continue
            if not platforms:
                errors.append(f"分母鎖出口 {item_id!r} 違反 schema minProperties（至少一個平台）")
                continue
            for platform, entry in platforms.items():
                if platform not in ("linux", "windows"):
                    errors.append(f"分母鎖出口 {item_id!r} 含不符合 schema propertyNames 的平台：{platform!r}")
                closed_object(entry, "$defs.ExitPlatform", f"分母鎖出口 {item_id!r}/{platform}")
    return errors


def _validate_lock(
    lock: Mapping[str, Any], expected: Mapping[str, Any], *, allow_missing: bool = False,
) -> list[str]:
    errors: list[str] = _lock_schema_errors(lock)
    if lock.get("schema_version") != 1:
        errors.append("分母鎖 schema_version 必須為 1")
    seed = lock.get("seed")
    required_seed = {
        "revision": SEED_REVISION,
        "decision_ref": SEED_DECISION,
        "excluded_case_ids": SEED_EXCLUDED,
        "cases": {"path": "denominators/cases.yaml"},
        "case_lock": {"path": "denominators/cases.lock.json"},
    }
    if not isinstance(seed, dict):
        errors.append("分母鎖缺少 seed 物件")
    else:
        for key, value in required_seed.items():
            if key in ("cases", "case_lock"):
                actual = seed.get(key)
                if not isinstance(actual, dict) or actual.get("path") != value["path"] or not _SHA256.fullmatch(str(actual.get("sha256", ""))):
                    errors.append(f"分母鎖 seed.{key} 路徑或 SHA 格式錯誤")
            elif seed.get(key) != value:
                errors.append(f"分母鎖 seed.{key} 不符指定基準")
    exits = lock.get("exits")
    if not isinstance(exits, dict):
        errors.append("分母鎖缺少 exits 物件")
        return errors
    if (set(exits) - set(expected)) or (not allow_missing and set(expected) - set(exits)):
        errors.append("分母鎖出口集合與目前 stage_exits.json 不同")
    for item_id, platforms in expected.items():
        actual_platforms = exits.get(item_id)
        if actual_platforms is None and allow_missing:
            continue
        if not isinstance(actual_platforms, dict):
            errors.append(f"分母鎖缺少出口：{item_id}")
            continue
        if (set(actual_platforms) - set(platforms)) or (not allow_missing and set(platforms) - set(actual_platforms)):
            errors.append(f"{item_id} 的平台集合與出口目錄不同")
        for platform, expected_entry in platforms.items():
            actual = actual_platforms.get(platform)
            if actual is None and allow_missing:
                continue
            if not isinstance(actual, dict):
                errors.append(f"分母鎖缺少平台：{item_id}/{platform}")
                continue
            for key in ("required_case_set", "layer"):
                if actual.get(key) != expected_entry[key]:
                    errors.append(f"{item_id}/{platform} 的 {key} 與出口目錄不同")
            ids = actual.get("case_ids")
            if not isinstance(ids, list) or not ids or not all(isinstance(case_id, str) and case_id for case_id in ids):
                errors.append(f"{item_id}/{platform} 的 case_ids 必須是非空字串陣列")
                continue
            if ids != sorted(set(ids)):
                errors.append(f"{item_id}/{platform} 的 case_ids 必須排序且唯一")
            current = set(ids)
            wanted = set(expected_entry["case_ids"])
            if current != wanted:
                missing = sorted(wanted - current)
                stale = sorted(current - wanted)
                if missing and not allow_missing:
                    errors.append(f"{item_id}/{platform} 缺少適用案例：{', '.join(missing)}")
                if stale:
                    errors.append(f"{item_id}/{platform} 含不再適用案例：{', '.join(stale)}")
    return errors


def _base_errors(
    current: Mapping[str, Any], current_cases: Mapping[str, Any], *,
    base: str, repo_root: Path, source_reader: SourceReader,
) -> list[str]:
    try:
        raw = source_reader(base, "tests/acceptance/stage3_required_cases.lock.json")
        base_cases_raw = source_reader(base, "tests/acceptance/cases.yaml")
        base_manifest_raw = source_reader(base, SOURCE_MANIFEST_REPO_PATH)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return [f"無法讀取 PR base 的分母鎖：{exc}"]
    errors: list[str] = []
    if base_manifest_raw is not None:
        try:
            current_manifest_raw = (
                repo_root / EVIDENCE_RELATIVE / "denominators" / "source-manifest.json"
            ).read_bytes()
        except OSError as exc:
            errors.append(f"無法讀取目前來源 manifest：{exc}")
        else:
            if current_manifest_raw != base_manifest_raw:
                errors.append("PR base 已有的來源 manifest 不可變更（位元組不相同）")
    if base_cases_raw is not None:
        try:
            base_cases = yaml.safe_load(base_cases_raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
            return [f"PR base 的 cases.yaml 不是有效 YAML：{exc}"]
        if not isinstance(base_cases, dict):
            return ["PR base 的 cases.yaml 根節點必須是物件"]
        errors.extend(_case_family_errors(current_cases, base_cases, source="PR base"))
    if raw is None:
        return errors
    try:
        previous = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        return [f"PR base 的分母鎖不是有效 JSON：{exc}"]
    if not isinstance(previous, dict):
        return ["PR base 的分母鎖根節點必須是物件"]
    errors.extend(_lock_schema_errors(previous))
    if previous.get("seed") != current.get("seed"):
        errors.append("分母鎖 seed 不可在 PR 中變更")
    before = previous.get("exits")
    after = current.get("exits")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return errors + ["PR base 或目前分母鎖缺少 exits"]
    for item_id, old_platforms in before.items():
        new_platforms = after.get(item_id)
        if not isinstance(old_platforms, dict) or not isinstance(new_platforms, dict):
            errors.append(f"既有出口不可刪除：{item_id}")
            continue
        for platform, old_entry in old_platforms.items():
            new_entry = new_platforms.get(platform)
            if not isinstance(old_entry, dict) or not isinstance(new_entry, dict):
                errors.append(f"既有出口平台不可刪除：{item_id}/{platform}")
                continue
            for key in ("required_case_set", "layer"):
                if old_entry.get(key) != new_entry.get(key):
                    errors.append(f"既有出口屬性不可變更：{item_id}/{platform}/{key}")
            old_ids = old_entry.get("case_ids")
            new_ids = new_entry.get("case_ids")
            if not isinstance(old_ids, list) or not isinstance(new_ids, list):
                errors.append(f"既有出口案例清單格式錯誤：{item_id}/{platform}")
            elif not set(old_ids) <= set(new_ids):
                errors.append(f"分母鎖不可刪除既有案例：{item_id}/{platform}")
    return errors


def required_for_exit(lock: Mapping[str, Any], item_id: str, platform: str) -> set[str]:
    """回傳凍結鎖中指定出口／平台的案例 ID 集合。"""
    return set(lock["exits"][item_id][platform]["case_ids"])


def _load_inputs(root: Path) -> tuple[dict, dict, dict, dict, dict]:
    acceptance = root / ACCEPTANCE_RELATIVE
    evidence = root / EVIDENCE_RELATIVE
    try:
        cases_doc = yaml.safe_load((acceptance / "cases.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"無法讀取 cases.yaml：{exc}") from exc
    if not isinstance(cases_doc, dict):
        raise ValueError("cases.yaml 根節點必須是物件")
    case_lock = _read_json(acceptance / "cases.lock.json")
    catalog = _read_json(acceptance / "stage_exits.json")
    lock = _read_json(acceptance / "stage3_required_cases.lock.json")
    manifest = _read_json(evidence / "denominators" / "source-manifest.json")
    return cases_doc, case_lock, catalog, lock, manifest


def _seed_errors(manifest: Mapping[str, Any], root: Path, reader: SourceReader) -> list[str]:
    evidence = root / EVIDENCE_RELATIVE
    errors = verify_seed_sources(manifest, evidence_root=evidence, source_reader=reader)
    lock = _read_json(root / ACCEPTANCE_RELATIVE / "stage3_required_cases.lock.json")
    seed = lock.get("seed")
    if not isinstance(seed, dict):
        return errors + ["分母鎖缺少 seed 物件"]
    by_path = {source.get("path"): source for source in manifest.get("sources", []) if isinstance(source, dict)}
    for key, source_path in (("cases", "tests/acceptance/cases.yaml"), ("case_lock", "tests/acceptance/cases.lock.json")):
        source = by_path.get(source_path)
        entry = seed.get(key)
        if not isinstance(source, dict) or not isinstance(entry, dict):
            errors.append(f"seed.{key} 缺少對應來源 manifest")
            continue
        if entry.get("sha256") != source.get("sha256"):
            errors.append(f"seed.{key} SHA 與來源 manifest 不同")
    return errors


def _write_lock(path: Path, lock: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(lock, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")


def main(
    argv: list[str] | None = None,
    *,
    repo_root: Path | None = None,
    source_reader: SourceReader | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="核對 Stage 3 逐出口案例分母與指定靜態種子")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="只讀核對目前分母鎖")
    mode.add_argument("--add-only", action="store_true", help="只新增目前鎖缺少的出口／平台／案例 ID")
    parser.add_argument("--base", help="PR base revision；檢查其既有鎖只增")
    args = parser.parse_args(argv)
    root = Path(repo_root or Path(__file__).resolve().parents[2]).resolve()
    reader = source_reader or (lambda revision, path: _git_show(root, revision, path))
    try:
        cases_doc, case_lock, catalog, lock, manifest = _load_inputs(root)
        schema_errors = _lock_schema_errors(lock)
        if schema_errors:
            raise ValueError("；".join(schema_errors))
        errors = _seed_errors(manifest, root, reader)
        if errors:
            raise ValueError("；".join(errors))
        expected = _expected_exit_cases(catalog, cases_doc, case_lock)
        seed_expected = _seed_expected_exit_cases(catalog, root / EVIDENCE_RELATIVE)
        lock_path = root / ACCEPTANCE_RELATIVE / "stage3_required_cases.lock.json"
        if args.check:
            check_errors = _initial_exit_platform_errors(manifest["initial_exit_platforms"], expected, lock)
            check_errors.extend(_validate_seed_floor(lock, seed_expected))
            check_errors.extend(_seed_family_errors(cases_doc, root / EVIDENCE_RELATIVE))
            check_errors.extend(_validate_lock(lock, expected))
            if args.base:
                check_errors.extend(_base_errors(
                    lock, cases_doc, base=args.base, repo_root=root, source_reader=reader,
                ))
            if check_errors:
                raise ValueError("；".join(check_errors))
            print("Stage 3 分母鎖、指定 Git 種子與 PR base 只增檢查通過。")
            return 0

        # 先核對舊鎖仍與目前目錄相容，並確認 PR base 的所有權分母未被刪改。
        precheck_errors = _initial_exit_platform_errors(manifest["initial_exit_platforms"], expected)
        precheck_errors.extend(_validate_lock(lock, expected, allow_missing=True))
        if args.base:
            precheck_errors.extend(_base_errors(
                lock, cases_doc, base=args.base, repo_root=root, source_reader=reader,
            ))
        if precheck_errors:
            raise ValueError("；".join(precheck_errors))
        family_errors = _seed_family_errors(cases_doc, root / EVIDENCE_RELATIVE)
        if family_errors:
            raise ValueError("；".join(family_errors))

        # 在記憶體中追加目錄的新出口／平台與案例 ID，再完整核對候選鎖。
        updated = json.loads(json.dumps(lock))
        changed = False
        for item_id, platforms in expected.items():
            item_lock = updated["exits"].setdefault(item_id, {})
            for platform, wanted in platforms.items():
                entry = item_lock.get(platform)
                if entry is None:
                    item_lock[platform] = dict(wanted)
                    changed = True
                    continue
                if entry["required_case_set"] != wanted["required_case_set"] or entry["layer"] != wanted["layer"]:
                    raise ValueError(f"既有出口屬性不可變更：{item_id}/{platform}")
                existing = set(entry["case_ids"])
                desired = set(wanted["case_ids"])
                removed = existing - desired
                if removed:
                    raise ValueError(f"拒絕縮減既有分母 {item_id}/{platform}：{', '.join(sorted(removed))}")
                new_ids = desired - existing
                if new_ids:
                    entry["case_ids"] = sorted(existing | desired)
                    changed = True

        final_errors = _initial_exit_platform_errors(manifest["initial_exit_platforms"], expected, updated)
        final_errors.extend(_validate_seed_floor(updated, seed_expected))
        final_errors.extend(_validate_lock(updated, expected))
        if args.base:
            final_errors.extend(_base_errors(
                updated, cases_doc, base=args.base, repo_root=root, source_reader=reader,
            ))
        if final_errors:
            raise ValueError("；".join(final_errors))

        if changed:
            _write_lock(lock_path, updated)
            print("Stage 3 分母鎖只新增了目前必要的出口、平台或案例 ID。")
        else:
            print("Stage 3 分母鎖已完整，無需更新。")
        return 0
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, subprocess.SubprocessError) as exc:
        print(f"Stage 3 分母鎖檢查失敗：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
