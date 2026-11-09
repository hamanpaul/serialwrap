#!/usr/bin/env python3
"""Slice 2.4 凍結契約覆蓋判定與出口報告。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "tests/acceptance/cases.yaml"
DEFAULT_CASES_LOCK = ROOT / "tests/acceptance/cases.lock.json"
DEFAULT_MAP = ROOT / "contracts/coverage_map.yaml"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VALID_OUTCOMES = frozenset({"passed", "failed", "skipped", "xfailed", "error"})
_VALID_PHASES = frozenset({"setup", "call", "teardown"})
_OWNER_CAPS = frozenset({
    "physical_uart", "windows_com", "supervisor_runtime", "interactive_desktop", "longrun_bench",
})


def _read_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"YAML 根節點不是 mapping：{path}")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根節點不是 object：{path}")
    return value


def _extract_error_code_ids(path: Path) -> list[str]:
    return [
        f"error_code:{row['code']}"
        for row in _read_yaml(path).get("codes", [])
        if isinstance(row, dict) and row.get("reachable") is True and isinstance(row.get("code"), str)
    ]


def _extract_last_error_ids(path: Path) -> list[str]:
    return [
        f"last_error:{row['value']}"
        for row in _read_yaml(path).get("values", [])
        if isinstance(row, dict) and row.get("reachable") is True and isinstance(row.get("value"), str)
    ]


def _extract_rpc_method_ids(path: Path) -> list[str]:
    methods = _read_json(path).get("x-methods", {})
    return [f"rpc_method:{name}" for name in methods] if isinstance(methods, dict) else []


def _extract_cli_command_ids(path: Path) -> list[str]:
    output = []
    for group in _read_json(path).get("x-groups", []):
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            continue
        for command in group.get("subcommands") or []:
            if isinstance(command, dict) and isinstance(command.get("name"), str):
                output.append(f"cli_cmd:{group['name']} {command['name']}")
    return output


def _extract_behavior_change_ids(path: Path) -> list[str]:
    return [
        f"bc:{row['id']}"
        for row in _read_yaml(path).get("changes", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    ]


def _extract_case_ids(path: Path) -> list[str]:
    sets = _read_json(path).get("sets", {})
    values = sets.get("slice_2.4", []) if isinstance(sets, dict) else []
    return [f"case:{value}" for value in values] if isinstance(values, list) else []


def _extract_map_ids(path: Path) -> list[str]:
    if not path.exists():
        return []
    return list(_mapping_entries(_read_yaml(path)))


def _extract_if_present(path: Path, contract_id: str) -> list[str]:
    return [contract_id] if path.is_file() else []


EXTRACTORS: dict[str, Callable[[Path], list[str]]] = {
    "error_codes.yaml": _extract_error_code_ids,
    "session_last_errors.yaml": _extract_last_error_ids,
    "rpc_schema.json": _extract_rpc_method_ids,
    "cli_schema.json": _extract_cli_command_ids,
    "behavior_changes.yaml": _extract_behavior_change_ids,
    "wal_record.schema.json": lambda path: _extract_if_present(path, "schema:wal_record"),
    "state_file.schema.json": lambda path: _extract_if_present(path, "schema:state_file"),
    "cases.lock.json": _extract_case_ids,
    "coverage_map.yaml": _extract_map_ids,
}


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _nodeids(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))


def _phase_number(value: object) -> int:
    matched = re.fullmatch(r"P(\d+)", str(value or ""))
    return int(matched.group(1)) if matched else -1


def _phase_for_refs(refs: Iterable[str], changes: Mapping[str, Mapping[str, Any]]) -> str:
    phases: set[str] = set()
    for change_id in refs:
        change = changes.get(change_id)
        if change:
            phases.update(str(value) for value in change.get("phases", []) if re.fullmatch(r"P\d+", str(value)))
    if not phases:
        return "P0"
    return max(phases, key=_phase_number)


def _phase_for_slice(value: object) -> str:
    matched = re.fullmatch(r"2\.(\d+)", str(value or ""))
    if not matched:
        return "P0"
    slice_number = int(matched.group(1))
    return f"P{slice_number + 4}" if 1 <= slice_number <= 4 else "P0"


def _contract_test_nodes(entry: Mapping[str, Any]) -> list[str]:
    return _nodeids(entry.get("contract_tests"))


def _case_nodes(case: Mapping[str, Any]) -> list[str]:
    values = case.get("evidence", [])
    if not isinstance(values, list):
        return []
    return _nodeids([value for value in values if isinstance(value, str) and ("::" in value or value.startswith("cargo:"))])


def _mapping_entries(value: Mapping[str, Any]) -> dict[str, Any]:
    entries = value.get("entries", value.get("coverage_map", {}))
    if entries is None:
        return {}
    if not isinstance(entries, dict):
        raise ValueError("coverage_map.yaml 的 entries 必須是 mapping")
    return {str(key): row for key, row in entries.items()}


def _map_row(value: object) -> dict[str, Any]:
    if isinstance(value, list):
        return {"nodeids": _nodeids(value)}
    if isinstance(value, dict):
        row = dict(value)
        row["nodeids"] = _nodeids(row.get("nodeids"))
        return row
    raise ValueError("coverage_map 每個 ID 必須是 nodeid 清單或 mapping")


def _add(
    output: dict[str, dict[str, Any]],
    contract_id: str,
    *,
    kind: str,
    owner_phase: str = "P0",
    platforms: Sequence[str] = ("linux", "windows"),
    nodeids: Sequence[str] = (),
    python_nodeids: Sequence[str] = (),
    rust_only: bool = False,
    l1_only: bool = False,
    blocked_caps: Sequence[str] = (),
    blocked_reason: str | None = None,
    status: str | None = None,
) -> None:
    output[contract_id] = {
        "kind": kind,
        "owner_phase": owner_phase,
        "platforms": list(dict.fromkeys(platforms)),
        "nodeids": list(dict.fromkeys(nodeids)),
        "python_nodeids": list(dict.fromkeys(python_nodeids)),
        "rust_only": rust_only,
        "l1_only": l1_only,
        "blocked_caps": list(dict.fromkeys(blocked_caps)),
        "blocked_reason": blocked_reason,
        "status": status,
    }


def _platforms_from_scope(value: object, default: Sequence[str] = ("linux", "windows")) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    result = []
    for platform in value:
        if platform in {"posix", "linux"}:
            result.append("linux")
        elif platform == "windows":
            result.append("windows")
    return list(dict.fromkeys(result)) or list(default)


def _contract_revision(contracts_dir: Path) -> str:
    paths = (
        contracts_dir / "error_codes.yaml",
        contracts_dir / "session_last_errors.yaml",
        contracts_dir / "behavior_changes.yaml",
        contracts_dir / "rpc_schema.json",
        contracts_dir / "cli_schema.json",
        contracts_dir / "wal_record.schema.json",
        contracts_dir / "state_file.schema.json",
        contracts_dir / "coverage_map.yaml",
    )
    material = "".join(f"{path.name}:{_sha256(path) or 'missing'}\n" for path in paths)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def universe(contracts_dir: Path, cases: Path, lock: Path, cmap: Path) -> dict[str, dict[str, Any]]:
    """從契約、凍結案例與宣告式 map 建立 ID 集合；未知對應保持空白。"""
    errors = _read_yaml(contracts_dir / "error_codes.yaml")
    last_errors = _read_yaml(contracts_dir / "session_last_errors.yaml")
    behavior = _read_yaml(contracts_dir / "behavior_changes.yaml")
    rpc = _read_json(contracts_dir / "rpc_schema.json")
    cli = _read_json(contracts_dir / "cli_schema.json")
    cases_doc = _read_yaml(cases)
    lock_doc = _read_json(lock)
    map_doc = _read_yaml(cmap) if cmap.exists() else {"entries": {}}
    map_entries = _mapping_entries(map_doc)
    source_paths = {
        "error_codes.yaml": contracts_dir / "error_codes.yaml",
        "session_last_errors.yaml": contracts_dir / "session_last_errors.yaml",
        "rpc_schema.json": contracts_dir / "rpc_schema.json",
        "cli_schema.json": contracts_dir / "cli_schema.json",
        "behavior_changes.yaml": contracts_dir / "behavior_changes.yaml",
        "wal_record.schema.json": contracts_dir / "wal_record.schema.json",
        "state_file.schema.json": contracts_dir / "state_file.schema.json",
        "cases.lock.json": lock,
        "coverage_map.yaml": cmap,
    }
    extracted_ids = {
        contract_id
        for source_name, extractor in EXTRACTORS.items()
        for contract_id in extractor(source_paths[source_name])
    }

    case_rows = cases_doc.get("cases", [])
    if not isinstance(case_rows, list):
        raise ValueError("cases.yaml 的 cases 必須是 list")
    cases_by_id = {
        str(row["case_id"]): row for row in case_rows
        if isinstance(row, dict) and isinstance(row.get("case_id"), str)
    }
    changes = {
        str(row["id"]): row for row in behavior.get("changes", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }

    def nodes_for_changes(refs: Iterable[str]) -> list[str]:
        wanted = set(refs)
        nodeids: list[str] = []
        for case in cases_by_id.values():
            if wanted.intersection(str(value) for value in case.get("behavior_changes", [])):
                nodeids.extend(_case_nodes(case))
        return list(dict.fromkeys(nodeids))

    output: dict[str, dict[str, Any]] = {}

    for entry in errors.get("codes", []):
        if not isinstance(entry, dict) or entry.get("reachable") is not True:
            continue
        code = str(entry.get("code", ""))
        refs = entry.get("behavior_change", [])
        if isinstance(refs, str):
            refs = [refs]
        refs = [str(item) for item in refs] if isinstance(refs, list) else []
        scope = entry.get("scope", {})
        output_id = f"error_code:{code}"
        _add(
            output, output_id, kind="error_code", owner_phase=_phase_for_refs(refs, changes),
            platforms=_platforms_from_scope(scope.get("platforms") if isinstance(scope, dict) else None),
            nodeids=nodes_for_changes(refs), python_nodeids=_contract_test_nodes(entry),
            status=str(entry.get("status", "")),
        )

    for entry in last_errors.get("values", []):
        if not isinstance(entry, dict) or entry.get("reachable") is not True:
            continue
        value = str(entry.get("value", ""))
        output_id = f"last_error:{value}"
        _add(
            output, output_id, kind="last_error", owner_phase="P0",
            python_nodeids=_contract_test_nodes(entry), status=str(entry.get("status", "")) or None,
        )

    for method in (rpc.get("x-methods") or {}):
        output_id = f"rpc_method:{method}"
        related_refs: list[str] = []
        for entry in errors.get("codes", []):
            scope = entry.get("scope", {}) if isinstance(entry, dict) else {}
            methods = scope.get("methods", []) if isinstance(scope, dict) else []
            if method in methods:
                refs = entry.get("behavior_change", [])
                related_refs.extend([refs] if isinstance(refs, str) else refs if isinstance(refs, list) else [])
        _add(
            output, output_id, kind="rpc_method", owner_phase=_phase_for_refs(related_refs, changes),
            nodeids=nodes_for_changes(str(value) for value in related_refs),
        )

    for group in cli.get("x-groups", []):
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            continue
        for subcommand in group.get("subcommands") or []:
            if isinstance(subcommand, dict) and isinstance(subcommand.get("name"), str):
                stage = str(group.get("stage", ""))
                owner = "P3" if "1a" in stage else "P4" if "1b" in stage else "P0"
                linked = subcommand.get("rpc")
                method_id = f"rpc_method:{linked.get('method')}" if isinstance(linked, dict) and linked.get("method") else ""
                method_entry = output.get(method_id, {})
                _add(
                    output, f"cli_cmd:{group['name']} {subcommand['name']}", kind="cli_cmd",
                    owner_phase=method_entry.get("owner_phase", owner),
                    nodeids=method_entry.get("nodeids", []),
                )

    for entry in behavior.get("changes", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        case_nodes: list[str] = []
        for case_id in entry.get("case_ids", []):
            case = cases_by_id.get(str(case_id))
            if case:
                case_nodes.extend(_case_nodes(case))
        phases = [str(value) for value in entry.get("phases", []) if re.fullmatch(r"P\d+", str(value))]
        owner = max(phases, key=_phase_number) if phases else "P0"
        _add(output, f"bc:{entry['id']}", kind="bc", owner_phase=owner, nodeids=case_nodes, status=str(entry.get("status", "")))

    _add(output, "schema:wal_record", kind="schema", owner_phase="P0")
    _add(output, "schema:state_file", kind="schema", owner_phase="P0")

    lock_ids = lock_doc.get("sets", {}).get("slice_2.4", [])
    if not isinstance(lock_ids, list):
        raise ValueError("cases.lock.json sets.slice_2.4 必須是 list")
    for case_id in lock_ids:
        case_id = str(case_id)
        case = cases_by_id.get(case_id)
        if case is None:
            _add(output, f"case:{case_id}", kind="case", owner_phase="P8", nodeids=())
            continue
        caps = [str(cap) for cap in case.get("capabilities", []) if str(cap) in _OWNER_CAPS]
        layer = str(case.get("layer", ""))
        needs_owner = layer in {"L2", "L3", "manual"} or bool(caps)
        owner_reason = None
        if needs_owner:
            owner_reason = "需要擁有者實機、Windows 原生或長時間執行證據"
        _add(
            output, f"case:{case_id}", kind="case",
            owner_phase=_phase_for_slice(case.get("daemon_slice")),
            platforms=_platforms_from_scope(case.get("platforms")),
            nodeids=_case_nodes(case),
            rust_only=case.get("suite") == "rust-l1",
            blocked_caps=caps,
            blocked_reason=owner_reason,
            status=layer,
        )

    for contract_id, raw_entry in map_entries.items():
        row = _map_row(raw_entry)
        current = output.get(contract_id)
        if current is None:
            kind = contract_id.partition(":")[0]
            current = {
                "kind": kind,
                "owner_phase": "P8",
                "platforms": ["linux", "windows"],
                "nodeids": [],
                "python_nodeids": [],
                "rust_only": False,
                "l1_only": False,
                "blocked_caps": [],
                "blocked_reason": None,
                "status": None,
            }
            output[contract_id] = current
        current["nodeids"] = list(dict.fromkeys([*current["nodeids"], *row.get("nodeids", [])]))
        if isinstance(row.get("owner_phase"), str) and row["owner_phase"].strip():
            current["owner_phase"] = row["owner_phase"].strip()
        if isinstance(row.get("platforms"), list) and row["platforms"]:
            current["platforms"] = list(dict.fromkeys(str(value) for value in row["platforms"]))
        for key in ("rust_only", "l1_only"):
            if key in row:
                current[key] = row[key] is True
        if isinstance(row.get("blocked_caps"), list):
            current["blocked_caps"] = list(dict.fromkeys([*current["blocked_caps"], *row["blocked_caps"]]))
        if isinstance(row.get("blocked_reason"), str):
            current["blocked_reason"] = row["blocked_reason"]
        # 對非案例契約，coverage_map 是 Rust nodeid 的權威來源；空清單明確
        # 表示尚未交付映射，不能用從相鄰案例推導出的 nodeid 代替。
        if current["kind"] != "case" and "nodeids" in row and not row["nodeids"]:
            current["nodeids"] = []

    unextracted = set(output) - extracted_ids
    if unextracted:
        raise ValueError(f"EXTRACTORS 未列出 universe ID：{', '.join(sorted(unextracted))}")

    return output


def load_results(run_dir: Path) -> list[dict[str, Any]]:
    """只載入單一 run directory 的 results-*.json。"""
    if not run_dir.exists():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("results-*.json")):
        try:
            value = _read_json(path)
            value.setdefault("_source_path", str(path))
            results.append(value)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            results.append({"label": path.stem.removeprefix("results-"), "_load_error": str(exc), "_source_path": str(path)})
    return results


def _resolve_path(value: object, run_dir: Path) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    for candidate in (ROOT / path, run_dir / path):
        if candidate.exists():
            return candidate
    return ROOT / path


def _identity_checks(
    results: Sequence[Mapping[str, Any]], run_dir: Path, expected_cli: str | None, expected_daemon: str | None,
    expected_contract_revision: str | None = None,
    expected_cases_yaml_sha256: str | None = None,
    expected_cases_lock_sha256: str | None = None,
) -> list[str]:
    errors: list[str] = []
    usable = [row for row in results if not row.get("_load_error")]
    labels = [str(row.get("label", "")) for row in usable]
    duplicate_labels = sorted({label for label in labels if labels.count(label) > 1})
    if duplicate_labels:
        errors.append("run directory 內 label 重複：" + ", ".join(duplicate_labels))
    for row in results:
        if row.get("_load_error"):
            errors.append(f"{row.get('label', 'unknown')}: 結果檔無法讀取：{row['_load_error']}")
        for message in row.get("identity_errors", []) if isinstance(row.get("identity_errors", []), list) else []:
            errors.append(f"{row.get('label', 'unknown')}: {message}")
    for field in ("contract_revision", "cases_yaml_sha256", "cases_lock_sha256"):
        values = {row.get(field) for row in usable}
        if len(values) > 1:
            errors.append(f"run directory 內 {field} 不一致")
    expected_sources = {
        "contract_revision": expected_contract_revision,
        "cases_yaml_sha256": expected_cases_yaml_sha256,
        "cases_lock_sha256": expected_cases_lock_sha256,
    }
    rust_rows = [row for row in usable if str(row.get("label", "")).startswith("rust-")]
    for field, expected in (("cli_sha256", expected_cli), ("daemon_sha256", expected_daemon)):
        values = {row.get(field) for row in rust_rows if row.get(field) is not None}
        if len(values) > 1:
            errors.append(f"rust 結果的 {field} 不一致")
        if expected is not None:
            if not _SHA256.fullmatch(expected):
                errors.append(f"預期 {field} 格式無效")
            elif any(row.get(field) != expected for row in rust_rows):
                errors.append(f"rust 結果的 {field} 與指定 artifact 不一致")
    for row in usable:
        label = str(row.get("label", "unknown"))
        if not isinstance(row.get("finished"), bool) or not isinstance(row.get("exit_code"), int):
            errors.append(f"{label}: 缺少 finished／exit_code 身分欄位")
        for field in ("cases_yaml_sha256", "cases_lock_sha256"):
            value = row.get(field)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                errors.append(f"{label}: {field} 格式無效")
        revision = row.get("contract_revision")
        if not isinstance(revision, str) or not _SHA256.fullmatch(revision):
            errors.append(f"{label}: contract_revision 格式無效")
        for field, expected in expected_sources.items():
            if expected is not None and row.get(field) != expected:
                errors.append(f"{label}: {field} 與目前來源不同")
        if row.get("platform") not in {"linux", "windows"}:
            errors.append(f"{label}: platform 欄位無效")
        elif str(row.get("label", "")).endswith(("-linux", "-windows")):
            label_platform = str(row["label"]).rsplit("-", 1)[-1]
            if row.get("platform") != label_platform:
                errors.append(f"{label}: platform 與 label 不一致")
        for field in ("cli_sha256", "daemon_sha256"):
            value = row.get(field)
            if value is not None and (not isinstance(value, str) or not _SHA256.fullmatch(value)):
                errors.append(f"{label}: {field} 格式無效")
            executable = _resolve_path(row.get(field.removesuffix("_sha256") + "_exe"), run_dir)
            if label.startswith("rust-") and (executable is None or not executable.is_file() or value is None):
                errors.append(f"{label}: 缺少可驗證的 {field.removesuffix('_sha256')} artifact")
            elif executable is not None and executable.exists() and value is not None:
                actual = _sha256(executable)
                if actual != value:
                    errors.append(f"{label}: {field} 與 artifact 實際雜湊不同")
        rows = row.get("results", [])
        if isinstance(rows, list):
            for item in rows:
                if not isinstance(item, dict):
                    continue
                for evidence in item.get("evidence", []) if isinstance(item.get("evidence", []), list) else []:
                    if not isinstance(evidence, dict):
                        continue
                    path = _resolve_path(evidence.get("path"), run_dir)
                    if path is None or not path.is_file() or _sha256(path) != evidence.get("sha256"):
                        errors.append(f"{label}: evidence 雜湊不符或檔案不存在：{evidence.get('path')}")
    return list(dict.fromkeys(errors))


def _validate_run(row: Mapping[str, Any]) -> list[str]:
    label = str(row.get("label", "unknown"))
    if row.get("_load_error"):
        return [f"結果檔載入失敗：{row['_load_error']}"]
    reasons: list[str] = []
    if row.get("finished") is not True:
        reasons.append("finished 不為 true")
    if row.get("exit_code") != 0:
        reasons.append(f"exit_code={row.get('exit_code')!r}")
    collected = row.get("collected")
    if not isinstance(collected, list) or any(not isinstance(nodeid, str) or not nodeid.strip() for nodeid in collected):
        reasons.append("collected 欄位無效或包含空 nodeid")
        collected = []
    if len(collected) != len(set(collected)):
        reasons.append("collected 含重複 nodeid")
    collected_set = set(collected)
    test_rows = row.get("results")
    if not isinstance(test_rows, list):
        reasons.append("results 欄位不是 list")
        return reasons
    seen: set[tuple[str, str]] = set()
    for test in test_rows:
        if not isinstance(test, dict):
            reasons.append("results 含非 object 列")
            continue
        nodeid = test.get("nodeid")
        phase = test.get("phase")
        outcome = test.get("outcome")
        if not isinstance(nodeid, str) or not nodeid.strip():
            reasons.append("results 含空 nodeid")
            continue
        if nodeid not in collected_set:
            reasons.append(f"results nodeid 未收集：{nodeid}")
        if phase not in _VALID_PHASES or outcome not in _VALID_OUTCOMES:
            reasons.append(f"{nodeid}: phase／outcome 無效")
            continue
        key = (nodeid, str(phase))
        if key in seen:
            reasons.append(f"{nodeid}: 同 phase 有重複結果")
        seen.add(key)
    return list(dict.fromkeys(reasons))


def _rows_for(doc: Mapping[str, Any], nodeid: str) -> list[Mapping[str, Any]]:
    rows = doc.get("results", [])
    return [row for row in rows if isinstance(row, dict) and row.get("nodeid") == nodeid] if isinstance(rows, list) else []


def _nodeid_passes(doc: Mapping[str, Any], nodeid: str) -> tuple[bool, str | None]:
    rows = _rows_for(doc, nodeid)
    if nodeid not in set(doc.get("collected", [])):
        return False, f"nodeid not collected: {nodeid}"
    calls = [row for row in rows if row.get("phase") == "call"]
    if len(calls) != 1:
        return False, f"nodeid must have exactly one call result: {nodeid}"
    if calls[0].get("outcome") != "passed":
        return False, f"nodeid call outcome is {calls[0].get('outcome')}: {nodeid}"
    if any(row.get("phase") in {"setup", "teardown"} and row.get("outcome") == "error" for row in rows):
        return False, f"setup/teardown error: {nodeid}"
    if any(row.get("phase") in {"setup", "teardown"} and row.get("outcome") == "failed" for row in rows):
        return False, f"setup/teardown failed: {nodeid}"
    return True, None


def _nodeid_failure(doc: Mapping[str, Any], nodeid: str) -> str | None:
    for row in _rows_for(doc, nodeid):
        if row.get("outcome") not in {"failed", "error"}:
            continue
        phase = str(row.get("phase", "call"))
        return f"{phase} outcome is {row['outcome']}: {nodeid}"
    return None


def _label_for(results: Sequence[Mapping[str, Any]], prefix: str, platform: str) -> Mapping[str, Any] | None:
    label = f"{prefix}-{platform}"
    return next((row for row in results if row.get("label") == label), None)


def _normalize_phase(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "P0"


def _is_owner_blocked(item: Mapping[str, Any]) -> bool:
    if item.get("blocked_reason"):
        return True
    if any(cap in _OWNER_CAPS for cap in item.get("blocked_caps", [])):
        return True
    return item.get("kind") == "case" and item.get("status") in {"L2", "L3", "manual"}


def evaluate(
    contract_universe: dict[str, dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    run_dir: Path | None = None,
    expect_cli_sha256: str | None = None,
    expect_daemon_sha256: str | None = None,
    expected_contract_revision: str | None = None,
    expected_cases_yaml_sha256: str | None = None,
    expected_cases_lock_sha256: str | None = None,
) -> dict[str, Any]:
    """依平台逐 nodeid 判定；任何不完整、重複或 BLOCKED-OWNER 結果皆不能成為 PASS。"""
    run_root = run_dir or ROOT
    identity_errors = _identity_checks(
        results,
        run_root,
        expect_cli_sha256,
        expect_daemon_sha256,
        expected_contract_revision,
        expected_cases_yaml_sha256,
        expected_cases_lock_sha256,
    )
    incomplete_runs = {
        str(row.get("label", "unknown")): _validate_run(row)
        for row in results
        if _validate_run(row)
    }
    by_label = {str(row.get("label", "")): row for row in results if row.get("label")}
    report: dict[str, Any] = {
        "covered": [],
        "missing": {},
        "missing_reasons": {},
        "blocked_owner": {},
        "failed": [],
        "ab_mismatch": [],
        "identity_errors": identity_errors,
        "incomplete_runs": incomplete_runs,
        "case_verdicts": {},
        "windows_verdicts": {},
    }

    for contract_id, item in sorted(contract_universe.items()):
        reason: str | None = None
        if _is_owner_blocked(item):
            caps = ", ".join(item.get("blocked_caps", []))
            report["blocked_owner"][contract_id] = item.get("blocked_reason") or f"擁有者證據待補：{caps or '外部資源'}"
            reason = report["blocked_owner"][contract_id]

        nodeids = [nodeid for nodeid in item.get("nodeids", []) if isinstance(nodeid, str) and nodeid.strip()]
        raw_nodes = item.get("nodeids", [])
        if not nodeids:
            if isinstance(raw_nodes, list) and any(isinstance(nodeid, str) and not nodeid.strip() for nodeid in raw_nodes):
                reason = "coverage_map contains an empty nodeid"
            else:
                reason = reason or "coverage_map has no nodeid"
        if reason and contract_id not in report["blocked_owner"]:
            report["missing_reasons"][contract_id] = reason

        if _is_owner_blocked(item):
            continue
        if not nodeids:
            continue
        if identity_errors:
            report["missing_reasons"][contract_id] = "run identity invalid"
            continue

        failures: list[str] = []
        missing_reasons: list[str] = []
        passed_all = True
        for platform in item.get("platforms", ["linux"]):
            platform = str(platform)
            rust_doc = _label_for(results, "rust", platform)
            if rust_doc is None:
                if platform == "windows":
                    report["blocked_owner"].setdefault(contract_id, "缺少擁有者 Windows 原生 build/link 與實跑證據")
                    passed_all = False
                    continue
                missing_reasons.append(f"缺少 rust-{platform} 結果檔")
                passed_all = False
                continue
            if platform == "windows" and (
                rust_doc.get("native_windows") is not True
                or rust_doc.get("native_build_link") is not True
            ):
                report["blocked_owner"].setdefault(
                    contract_id,
                    "缺少擁有者 Windows 原生 build/link 與實跑證據",
                )
                passed_all = False
                continue
            if str(rust_doc.get("label")) in incomplete_runs:
                for nodeid in nodeids:
                    failure = _nodeid_failure(rust_doc, nodeid)
                    if failure:
                        failures.append(failure)
                missing_reasons.append(f"rust-{platform} 結果不完整")
                passed_all = False
                continue
            for nodeid in nodeids:
                ok, detail = _nodeid_passes(rust_doc, nodeid)
                if ok:
                    continue
                if detail and "outcome is failed" in detail:
                    failures.append(detail)
                elif detail and ("outcome is error" in detail or "setup/teardown" in detail):
                    failures.append(detail)
                else:
                    missing_reasons.append(detail or f"Rust 證據不足：{nodeid}")
                passed_all = False

            python_nodes = [nodeid for nodeid in item.get("python_nodeids", []) if isinstance(nodeid, str) and nodeid.strip()]
            python_doc = _label_for(results, "python", platform)
            if not item.get("rust_only") and not item.get("l1_only"):
                if python_doc is None:
                    missing_reasons.append(f"缺少 python-{platform} A/B 結果檔")
                    passed_all = False
                elif str(python_doc.get("label")) in incomplete_runs:
                    missing_reasons.append(f"python-{platform} 結果不完整")
                    for nodeid in nodeids:
                        rust_ok, _ = _nodeid_passes(rust_doc, nodeid)
                        python_failure = _nodeid_failure(python_doc, nodeid)
                        if rust_ok and python_failure:
                            report["ab_mismatch"].append(contract_id)
                            failures.append(f"Python/Rust A/B 不一致：{python_failure}")
                    passed_all = False
                else:
                    for nodeid in nodeids:
                        rust_ok, rust_reason = _nodeid_passes(rust_doc, nodeid)
                        python_ok, python_reason = _nodeid_passes(python_doc, nodeid)
                        if rust_ok and not python_ok:
                            python_failure = _nodeid_failure(python_doc, nodeid)
                            if python_failure:
                                report["ab_mismatch"].append(contract_id)
                                failures.append(f"Python/Rust A/B 不一致：{python_failure}")
                            else:
                                missing_reasons.append(python_reason or f"Python 證據不足：{nodeid}")
                            passed_all = False
                        elif python_ok and not rust_ok:
                            rust_failure = _nodeid_failure(rust_doc, nodeid)
                            if rust_failure:
                                report["ab_mismatch"].append(contract_id)
                                failures.append(f"Python/Rust A/B 不一致：{rust_failure}")
                            else:
                                missing_reasons.append(rust_reason or f"Rust 證據不足：{nodeid}")
                            passed_all = False
                    for nodeid in python_nodes:
                        ok, detail = _nodeid_passes(python_doc, nodeid)
                        if not ok:
                            missing_reasons.append(detail or f"Python contract_test 未通過：{nodeid}")
                            passed_all = False

        if failures:
            report["failed"].append(contract_id)
            report["missing_reasons"][contract_id] = "; ".join(dict.fromkeys(failures))
        elif missing_reasons:
            report["missing_reasons"][contract_id] = "; ".join(dict.fromkeys(missing_reasons))
        elif passed_all:
            report["covered"].append(contract_id)

    report["failed"] = list(dict.fromkeys(report["failed"]))
    report["ab_mismatch"] = list(dict.fromkeys(report["ab_mismatch"]))
    for contract_id, reason in report["missing_reasons"].items():
        item = contract_universe.get(contract_id, {})
        owner = _normalize_phase(item.get("owner_phase"))
        report["missing"].setdefault(owner, []).append(contract_id)
    for owner in report["missing"]:
        report["missing"][owner] = sorted(set(report["missing"][owner]))

    for contract_id, item in contract_universe.items():
        if item.get("kind") != "case":
            continue
        if contract_id in report["blocked_owner"]:
            verdict = "BLOCKED-OWNER"
        elif contract_id in report["failed"]:
            verdict = "FAIL"
        elif contract_id in report["covered"]:
            verdict = "PASS"
        else:
            verdict = "EVIDENCE_MISSING"
        report["case_verdicts"][contract_id.removeprefix("case:")] = verdict
    for contract_id, item in contract_universe.items():
        if "windows" not in item.get("platforms", []):
            continue
        if contract_id in report["blocked_owner"]:
            verdict = "BLOCKED-OWNER"
        elif contract_id in report["failed"]:
            verdict = "FAIL"
        elif contract_id in report["covered"]:
            verdict = "PASS"
        else:
            verdict = "EVIDENCE_MISSING"
        report["windows_verdicts"][contract_id] = verdict
    return report


def _case_rows(cases_path: Path, lock_path: Path) -> list[dict[str, Any]]:
    cases_doc = _read_yaml(cases_path)
    lock_doc = _read_json(lock_path)
    by_id = {row["case_id"]: row for row in cases_doc.get("cases", []) if isinstance(row, dict) and "case_id" in row}
    rows = []
    for case_id in lock_doc.get("sets", {}).get("slice_2.4", []):
        case = by_id.get(case_id)
        rows.append(case if case is not None else {"case_id": case_id, "platforms": []})
    return rows


def _t9_result_rows(run_info: Mapping[str, Any]) -> list[str]:
    """收集 T9 記錄測項的執行結果；只供待裁決欄，不進覆蓋判定。"""
    observations: list[str] = []
    result_docs = run_info.get("results", [])
    if not isinstance(result_docs, list):
        return observations
    for document in result_docs:
        if not isinstance(document, dict):
            continue
        label = str(document.get("label", "unknown"))
        rows = document.get("results", [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            nodeid = row.get("nodeid")
            if not isinstance(nodeid, str) or "endpoint_reopen_after_flasher_set_excl_recorded" not in nodeid:
                continue
            phase = str(row.get("phase", "unknown"))
            outcome = str(row.get("outcome", "unknown"))
            observations.append(f"{label} {phase}={outcome}")
    return sorted(set(observations))


def render_gate_report(report: Mapping[str, Any], cases: Sequence[Mapping[str, Any]], run_info: Mapping[str, Any]) -> str:
    """建立供擁有者審閱的逐案 Slice 2.4 gate 摘要。"""
    run_id = str(run_info.get("run_id", "未指定"))
    lines = [
        "# Slice 2.4 凍結契約覆蓋報告",
        "",
        f"- run：`{run_id}`",
        f"- 產生時間：`{run_info.get('finished_at', '未指定')}`",
        "- 覆蓋判定：PASS 只代表本報告列出的對應 nodeid、平台及身分檢查全數成立。",
        "",
        "## 凍結案例",
        "",
        "| case_id | 平台 | 狀態 | 證據／待辦 |",
        "|---|---|---|---|",
    ]
    verdicts = report.get("case_verdicts", {})
    missing_reasons = report.get("missing_reasons", {})
    blocked = report.get("blocked_owner", {})
    failed = set(report.get("failed", []))
    for case in sorted(cases, key=lambda row: str(row.get("case_id", ""))):
        case_id = str(case.get("case_id", ""))
        contract_id = f"case:{case_id}"
        status = verdicts.get(case_id, "EVIDENCE_MISSING")
        detail = blocked.get(contract_id) or missing_reasons.get(contract_id) or ("測試與身分證據已通過" if status == "PASS" else "失敗證據" if contract_id in failed else "尚無有效結果")
        platforms = ", ".join(str(value) for value in case.get("platforms", []))
        lines.append(f"| `{case_id}` | {platforms} | **{status}** | {detail} |")

    frozen_case_ids = {str(case.get("case_id", "")) for case in cases}
    supplemental_cases = sorted(
        (contract_id, reason)
        for contract_id, reason in blocked.items()
        if contract_id.startswith("case:") and contract_id.removeprefix("case:") not in frozen_case_ids
    )
    if supplemental_cases:
        lines.extend(["", "## 凍結分母外的 BLOCKED-OWNER 案例", ""])
        for contract_id, reason in supplemental_cases:
            lines.append(f"- `{contract_id.removeprefix('case:')}`：**BLOCKED-OWNER**；{reason}。")

    lines.extend([
        "",
        "## Windows 原生項目",
        "",
    ])
    windows_cases = [case for case in cases if "windows" in case.get("platforms", [])]
    if windows_cases:
        for case in sorted(windows_cases, key=lambda row: str(row.get("case_id", ""))):
            case_id = str(case.get("case_id", ""))
            status = verdicts.get(case_id, "EVIDENCE_MISSING")
            reason = blocked.get(f"case:{case_id}") or missing_reasons.get(f"case:{case_id}") or "缺少擁有者 Windows 原生 build/link 與實跑證據"
            if status == "PASS":
                reason = "已提供符合身分的 Windows 原生結果"
            lines.append(f"- `{case_id}`：**{status}**；{reason}。")
    else:
        lines.append("- 本次凍結案例沒有 Windows 平台項目。")
    windows_verdicts = report.get("windows_verdicts", {})
    if isinstance(windows_verdicts, dict):
        windows_contracts = [(contract_id, status) for contract_id, status in sorted(windows_verdicts.items()) if not contract_id.startswith("case:")]
        if windows_contracts:
            counts = {status: sum(value == status for _, value in windows_verdicts.items()) for status in ("PASS", "FAIL", "EVIDENCE_MISSING", "BLOCKED-OWNER")}
            lines.extend([
                "",
                "Windows 全部契約 ID（案例狀態見上表）：" + ", ".join(f"{status}={counts[status]}" for status in counts),
            ])
            for contract_id, status in windows_contracts:
                detail = blocked.get(contract_id) or missing_reasons.get(contract_id) or ("符合身分的 Windows 原生結果已通過" if status == "PASS" else "測項失敗" if status == "FAIL" else "尚無有效 Windows 原生結果")
                lines.append(f"- `{contract_id}`：**{status}**；{detail}。")

    lines.extend([
        "",
        "## 待裁決",
        "",
        "| 項目 | 裁決狀態 | 結果記錄 | 說明 |",
        "|---|---|---|---|",
        "",
        "## 已知 Low",
        "",
        "- `P8X-9e51abd-1`：交接凍結後 RX 記帳與 Python 的差異列為已知 Low；於 Slice 2.4 出口再檢視是否更新 RX 記帳，不推進 readiness、不 TX。",
        "",
        "## 判定彙總",
        "",
        f"- covered：{len(report.get('covered', []))}",
        f"- missing IDs：{sum(len(items) for items in report.get('missing', {}).values())}",
        f"- failed IDs：{len(report.get('failed', []))}",
        f"- BLOCKED-OWNER：{len(report.get('blocked_owner', {}))}",
        f"- A/B mismatch：{len(report.get('ab_mismatch', []))}",
        f"- identity errors：{len(report.get('identity_errors', []))}",
        f"- incomplete runs：{len(report.get('incomplete_runs', {}))}",
        "",
    ])
    t9_results = _t9_result_rows(run_info)
    recorded = "; ".join(t9_results) if t9_results else "EVIDENCE_MISSING：沒有結果檔含此測項 nodeid"
    t9_row = (
        "| `endpoint_reopen_after_flasher_set_excl_recorded` | 待裁決 | "
        f"{recorded} | 只記錄測項執行結果，不把它轉成行為 PASS／FAIL；依 S2 T9 由擁有者裁決。 |"
    )
    pending_end = lines.index("## 已知 Low")
    lines.insert(pending_end - 1, t9_row)
    return "\n".join(lines)


def _parse_pairs(values: Sequence[str], *, comma_values: bool = False) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for raw in values:
        key, separator, value = raw.partition("=")
        if not separator or not key:
            raise ValueError(f"無效的 ID 指定：{raw}")
        output[key] = [part.strip() for part in value.split(",")] if comma_values else [value.strip()]
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True, help="單一輪次的結果檔目錄")
    parser.add_argument("--contracts-dir", type=Path, default=ROOT / "contracts")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--cases-lock", type=Path, default=DEFAULT_CASES_LOCK)
    parser.add_argument("--map-file", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--json", type=Path, help="JSON 報告輸出路徑")
    parser.add_argument("--report", type=Path, help="Markdown gate 報告輸出路徑")
    parser.add_argument("--only", action="append", default=[], help="只判定指定 ID，可重複")
    parser.add_argument("--map", action="append", default=[], help="測試用 map 覆寫：ID=nodeid,nodeid")
    parser.add_argument("--platforms", action="append", default=[], help="測試用平台覆寫：ID=linux,windows")
    parser.add_argument("--expect-cli-sha256")
    parser.add_argument("--expect-daemon-sha256")
    args = parser.parse_args(argv)

    try:
        all_ids = universe(args.contracts_dir, args.cases, args.cases_lock, args.map_file)
        selected = set(args.only) if args.only else set(all_ids)
        unknown = selected - set(all_ids)
        for contract_id in unknown:
            kind = contract_id.partition(":")[0]
            all_ids[contract_id] = {
                "kind": kind, "owner_phase": "P8", "platforms": ["linux"], "nodeids": [],
                "python_nodeids": [], "rust_only": False, "l1_only": False, "blocked_caps": [],
                "blocked_reason": None, "status": None,
            }
        ids = {contract_id: all_ids[contract_id] for contract_id in selected if contract_id in all_ids}
        map_overrides = _parse_pairs(args.map, comma_values=True)
        platform_overrides = _parse_pairs(args.platforms, comma_values=True)
        for contract_id, nodeids in map_overrides.items():
            if contract_id not in ids:
                ids[contract_id] = all_ids.get(contract_id, {
                    "kind": contract_id.partition(":")[0], "owner_phase": "P8", "platforms": ["linux"],
                    "nodeids": [], "python_nodeids": [], "rust_only": False, "l1_only": False,
                    "blocked_caps": [], "blocked_reason": None, "status": None,
                })
            ids[contract_id] = dict(ids[contract_id])
            ids[contract_id]["nodeids"] = nodeids
        for contract_id, platforms in platform_overrides.items():
            if contract_id not in ids:
                continue
            ids[contract_id] = dict(ids[contract_id])
            ids[contract_id]["platforms"] = platforms
        result_rows = load_results(args.run_dir)
        expected_sources = {
            "contract_revision": _contract_revision(args.contracts_dir),
            "cases_yaml_sha256": _sha256(args.cases),
            "cases_lock_sha256": _sha256(args.cases_lock),
        }
        if any(value is None for value in expected_sources.values()):
            raise ValueError("無法計算目前 contract/cases/lock 身分雜湊")
        report = evaluate(
            ids, result_rows, run_dir=args.run_dir,
            expect_cli_sha256=args.expect_cli_sha256,
            expect_daemon_sha256=args.expect_daemon_sha256,
            expected_contract_revision=expected_sources["contract_revision"],
            expected_cases_yaml_sha256=expected_sources["cases_yaml_sha256"],
            expected_cases_lock_sha256=expected_sources["cases_lock_sha256"],
        )
        report["run_dir"] = str(args.run_dir)
        body = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(body, encoding="utf-8")
        else:
            sys.stdout.write(body)
        if args.report:
            cases = _case_rows(args.cases, args.cases_lock)
            markdown = render_gate_report(report, cases, {"run_id": args.run_dir.name, "results": result_rows})
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(markdown, encoding="utf-8")
        hard_errors = bool(report["missing"] or report["failed"] or report["ab_mismatch"] or report["identity_errors"] or report["incomplete_runs"])
        return 1 if hard_errors else 0
    except (OSError, ValueError, yaml.YAMLError, json.JSONDecodeError) as exc:
        print(f"contract_coverage: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
