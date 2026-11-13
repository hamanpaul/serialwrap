#!/usr/bin/env python3
"""Slice 2.4 凍結契約覆蓋判定與出口報告。"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import hashlib
import importlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
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
_CARGO_PREFIX = "cargo:"

# --- 長跑證據（計畫 Task 15 步驟 8；`slice24_exit.sh --longrun <dir>` 匯入）---
# 輸入目錄結構：
#   longrun-manifest.json                      {"schema_version":"slice24-longrun.v1","case_id":"lr-mixed",
#                                               "platforms":{"<p>":{"bundle_tree_sha256":"<pinned bundle>"}}}
#   <p>/release-gate.json                      P2 realhw/acceptance/gate.py candidate 輸出
#   <p>/baseline-release-gate.json             對應 Python 48 h baseline 的 gate 輸出（mode=baseline）
#   <p>/supervision-attest.json                停用自動重啟的紀錄（observer --supervision-attest 同檔）
#   <p>/bench-lock.json                        {"path","acquired_at_utc","released_at_utc"} bench lock 持有紀錄
#   <p>/report.json、events.ndjson、snapshots.ndjson、observer.ndjson、wal/*   gate evidence_hashes 所列檔案
#   windows/excl/probes.ndjson、windows/excl/handoff.ndjson                       Windows 原生 COM 獨占證據
LONGRUN_CASE = "case:lr-mixed"
LONGRUN_SCHEMA = "slice24-longrun.v1"
LONGRUN_IMPORT_SCHEMA = "slice24-longrun-import.v1"
LONGRUN_MANIFEST = "longrun-manifest.json"
LONGRUN_DIRNAME = "longrun"
LONGRUN_RECORD = "longrun-evidence.json"
DEFAULT_ACCEPTANCE = ROOT / "realhw/acceptance/longrun_acceptance.yaml"
_LONGRUN_MIN_DURATION_S = 48 * 3600
_GATE_SCHEMA = "release-gate.v1"
_GATE_NA = "NOT_APPLICABLE_BASELINE"
_GATE_VERDICTS = frozenset({"PASS", "FAIL", "INSUFFICIENT_EVIDENCE", "BLOCKED"})
_GATE_CHECK_RESULTS = frozenset({"PASS", "FAIL", "INSUFFICIENT_EVIDENCE", "BLOCKED", _GATE_NA})
# 必要檢查集合不在本工具另抄：由正式 P2 gate（realhw/acceptance/gate.py）推導（P8X-e038dda-2）。
# 證據本身成立的前提：未通過時整份長跑證據拒收（不是行為 FAIL）。
_LONGRUN_PRECONDITION_CHECKS = ("pinned_cli", "fault_retention_attested", "baseline_compatible")
_LONGRUN_EVIDENCE_FILES = ("report.json", "events.ndjson", "snapshots.ndjson", "observer.ndjson")
_WINDOWS_EXCL_FILES = ("probes.ndjson", "handoff.ndjson")

# --- 本輪候選 bundle（P8X-e038dda-1）---
# `<run_dir>/artifacts/<platform>/candidate-bundle.json` 由 `--record-candidate-bundle <platform>=<bundle 版本目錄>`
# 寫入：{"schema_version","platform","bundle_tree_sha256","files":[[rel, sha256], ...]}。tree 雜湊與 realhw
# deployed_content `tree_digests()[0]` 同算法；判定時由檔案清單重算，並要求 bin/ 內 CLI／daemon 等於本輪
# rust-<platform> 實際受測 artifact，長跑的 pinned bundle、CLI、daemon 映像再逐項等於它才算綁定本輪候選。
CANDIDATE_BUNDLE_SCHEMA = "slice24-candidate-bundle.v1"
CANDIDATE_BUNDLE_RECORD = "candidate-bundle.json"
_BUNDLE_BINARIES = {
    "linux": ("bin/serialwrap", "bin/serialwrapd"),
    "windows": ("bin/serialwrap.exe", "bin/serialwrapd.exe"),
}


def _repo_module(name: str) -> Any:
    """匯入本 repo 的正式模組（realhw.*）；本檔以 script 執行時 sys.path[0] 為 scripts/rust。"""
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module(name)


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
        "parametrized": {},
    }


def _parametrized_entries(value: Mapping[str, Any]) -> dict[str, list[str]]:
    """coverage_map.yaml `parametrized`：參數化基底 nodeid → 必要參數 id 完整清單。"""
    raw = value.get("parametrized") or {}
    if not isinstance(raw, dict):
        raise ValueError("coverage_map.yaml 的 parametrized 必須是 mapping")
    output: dict[str, list[str]] = {}
    for base, params in raw.items():
        if not isinstance(base, str) or "::" not in base or "[" in base:
            raise ValueError(f"parametrized 基底必須是不含參數的 pytest nodeid：{base!r}")
        # pytest 允許空字串參數 id（nodeid 為 `base[]`），故只要求字串、清單非空且不重複。
        if (
            not isinstance(params, list) or not params
            or any(not isinstance(param, str) for param in params)
            or len(set(params)) != len(params)
        ):
            raise ValueError(f"parametrized 參數清單必須是非空且不重複的字串：{base}")
        output[base] = list(params)
    return output


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
    parametrized = _parametrized_entries(map_doc)
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
                "parametrized": {},
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

    for item in output.values():
        referenced = [*item["nodeids"], *item["python_nodeids"]]
        item["parametrized"] = {nodeid: parametrized[nodeid] for nodeid in referenced if nodeid in parametrized}

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


def _read_sha256sums(path: Path) -> set[str] | None:
    """runner `SHA256SUMS` 所列雜湊；檔案不存在或格式錯誤回 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    digests: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, name = line.partition(" ")
        if not _SHA256.fullmatch(digest.lower()) or not name.strip(" *"):
            return None
        digests.add(digest.lower())
    return digests


def _identity_checks(
    results: Sequence[Mapping[str, Any]], run_dir: Path, expected_cli: str | None, expected_daemon: str | None,
    expected_contract_revision: str | None = None,
    expected_cases_yaml_sha256: str | None = None,
    expected_cases_lock_sha256: str | None = None,
    expected_platform: str = "linux",
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
    # 不同平台的 artifact 本來就是不同檔案（Linux ELF／Windows PE），只在同平台內要求一致；
    # 預期雜湊只套用到指定平台，Windows 另對照其原生 runner 的 SHA256SUMS（P8X-24c6b39-5）。
    rust_by_platform: dict[str, list[Mapping[str, Any]]] = {}
    for row in usable:
        if str(row.get("label", "")).startswith("rust-"):
            rust_by_platform.setdefault(str(row.get("platform")), []).append(row)
    for platform, rows in sorted(rust_by_platform.items()):
        for field in ("cli_sha256", "daemon_sha256"):
            values = {row.get(field) for row in rows if row.get(field) is not None}
            if len(values) > 1:
                errors.append(f"同平台（{platform}）rust 結果的 {field} 不一致")
    for field, expected in (("cli_sha256", expected_cli), ("daemon_sha256", expected_daemon)):
        if expected is None:
            continue
        if not _SHA256.fullmatch(expected):
            errors.append(f"預期 {field} 格式無效")
        elif any(row.get(field) != expected for row in rust_by_platform.get(expected_platform, [])):
            errors.append(f"{expected_platform} rust 結果的 {field} 與指定 artifact 不一致")
    windows_rows = rust_by_platform.get("windows", [])
    if windows_rows:
        manifest_path = run_dir / "artifacts" / "windows" / "SHA256SUMS"
        listed = _read_sha256sums(manifest_path)
        if listed is None:
            errors.append("rust-windows 結果缺少可讀的 runner SHA256SUMS（artifacts/windows/SHA256SUMS）")
        else:
            for row in windows_rows:
                for field in ("cli_sha256", "daemon_sha256"):
                    if row.get(field) not in listed:
                        errors.append(f"{row.get('label', 'unknown')}: {field} 不在 runner SHA256SUMS")
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


_WINDOWS_NATIVE_MISSING = "缺少擁有者 Windows 原生 build/link 與實跑證據"


def _collected_nodeids(doc: Mapping[str, Any]) -> list[str]:
    values = doc.get("collected", [])
    return [value for value in values if isinstance(value, str)] if isinstance(values, list) else []


def _expand_nodeid(
    nodeid: str, docs: Sequence[Mapping[str, Any]], declared: Mapping[str, Sequence[str]],
) -> tuple[list[str], str | None]:
    """把參數化基底展開成必要參數節點集合（P8X-24c6b39-3）。

    必要集合＝coverage_map `parametrized` 宣告的完整參數 ∪ 任一受比對結果實際收集到的參數；
    每個節點都要逐項收集並通過，不得只接受任一參數或縮小分母。實際收集到參數化節點、
    卻沒有宣告必要集合時視為證據不足。
    """
    if nodeid.startswith(_CARGO_PREFIX) or "[" in nodeid:
        return [nodeid], None
    prefix = f"{nodeid}["
    variants = sorted({
        collected for doc in docs for collected in _collected_nodeids(doc)
        if collected.startswith(prefix) and collected.endswith("]")
    })
    params = declared.get(nodeid)
    if params is None:
        if variants:
            return [], f"parametrized nodeid lacks declared parameter set: {nodeid}"
        return [nodeid], None
    return list(dict.fromkeys([*(f"{nodeid}[{param}]" for param in params), *variants])), None


def _split_nodeids(nodeids: Sequence[str]) -> tuple[list[str], list[str]]:
    """(Rust L1 的 Cargo 節點, 雙邊黑箱 pytest 節點)；Cargo 節點不可能出現在 Python 結果（P8X-24c6b39-4）。"""
    return (
        [nodeid for nodeid in nodeids if nodeid.startswith(_CARGO_PREFIX)],
        [nodeid for nodeid in nodeids if not nodeid.startswith(_CARGO_PREFIX)],
    )


def _owner_evidence_verdict(item: Mapping[str, Any], evidence: Mapping[str, Any]) -> tuple[str, str]:
    """依已重新驗證的擁有者證據回 ("PASS"|"FAIL"|"BLOCKED-OWNER", 說明)。"""
    platforms = evidence.get("platforms", {}) if isinstance(evidence, Mapping) else {}
    platforms = platforms if isinstance(platforms, Mapping) else {}
    failures: list[str] = []
    blocked: list[str] = []
    passed: list[str] = []
    for platform in item.get("platforms", []):
        entry = platforms.get(platform)
        if not isinstance(entry, Mapping) or entry.get("accepted") is not True:
            errors = entry.get("errors", []) if isinstance(entry, Mapping) else []
            detail = "; ".join(str(error) for error in errors) if errors else "缺少長跑證據"
            blocked.append(f"{platform}: {detail}")
            continue
        verdict = entry.get("release_gate_result")
        if verdict == "FAIL":
            failures.append(f"{platform}: release_gate_result=FAIL")
        elif verdict == "PASS":
            passed.append(platform)
        else:
            blocked.append(f"{platform}: release_gate_result={verdict}")
    if failures:
        return "FAIL", "長跑證據有效但 release gate 判 FAIL：" + "; ".join(failures)
    if blocked:
        return "BLOCKED-OWNER", "長跑證據未成立：" + "; ".join(blocked)
    if not passed:
        return "BLOCKED-OWNER", "長跑證據未涵蓋任何平台"
    return "PASS", "長跑證據已驗證：" + ", ".join(f"{platform}=PASS" for platform in passed)


def _classify_rust_detail(detail: str | None, nodeid: str, failures: list[str], missing: list[str]) -> None:
    if detail and ("outcome is failed" in detail or "outcome is error" in detail or "setup/teardown" in detail):
        failures.append(detail)
    else:
        missing.append(detail or f"Rust 證據不足：{nodeid}")


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
    expected_platform: str = "linux",
    owner_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """依平台逐 nodeid 判定；任何不完整、重複或 BLOCKED-OWNER 結果皆不能成為 PASS。

    擁有者阻擋的 ID 只有在 `owner_evidence` 經重新驗證、所有平台都成立時才解除；
    有效證據判 FAIL 時照實為 FAIL，缺漏或拒收仍維持 BLOCKED-OWNER。
    """
    run_root = run_dir or ROOT
    identity_errors = _identity_checks(
        results,
        run_root,
        expect_cli_sha256,
        expect_daemon_sha256,
        expected_contract_revision,
        expected_cases_yaml_sha256,
        expected_cases_lock_sha256,
        expected_platform,
    )
    incomplete_runs = {
        str(row.get("label", "unknown")): _validate_run(row)
        for row in results
        if _validate_run(row)
    }
    owner_evidence = owner_evidence or {}
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
        "owner_evidence": {},
    }

    for contract_id, item in sorted(contract_universe.items()):
        raw_nodes = item.get("nodeids", [])
        nodeids = [nodeid for nodeid in raw_nodes if isinstance(nodeid, str) and nodeid.strip()] if isinstance(raw_nodes, list) else []
        has_empty_node = isinstance(raw_nodes, list) and any(isinstance(nodeid, str) and not nodeid.strip() for nodeid in raw_nodes)

        owner_verdict: str | None = None
        if _is_owner_blocked(item):
            owner_reason = None
            if contract_id in owner_evidence:
                owner_verdict, owner_reason = _owner_evidence_verdict(item, owner_evidence[contract_id])
                report["owner_evidence"][contract_id] = {"verdict": owner_verdict, "reason": owner_reason}
            if owner_verdict == "FAIL":
                report["failed"].append(contract_id)
                report["missing_reasons"][contract_id] = owner_reason
                continue
            if owner_verdict != "PASS":
                caps = ", ".join(item.get("blocked_caps", []))
                report["blocked_owner"][contract_id] = (
                    owner_reason or item.get("blocked_reason") or f"擁有者證據待補：{caps or '外部資源'}"
                )
                continue

        if not nodeids:
            if owner_verdict == "PASS":
                if identity_errors:
                    report["missing_reasons"][contract_id] = "run identity invalid"
                else:
                    report["covered"].append(contract_id)
                continue
            report["missing_reasons"][contract_id] = (
                "coverage_map contains an empty nodeid" if has_empty_node else "coverage_map has no nodeid"
            )
            continue
        if identity_errors:
            report["missing_reasons"][contract_id] = "run identity invalid"
            continue

        declared = item.get("parametrized") or {}
        _rust_l1_nodes, blackbox_nodes = _split_nodeids(nodeids)
        python_nodes = [nodeid for nodeid in item.get("python_nodeids", []) if isinstance(nodeid, str) and nodeid.strip()]
        needs_ab = not item.get("rust_only") and not item.get("l1_only")
        failures: list[str] = []
        missing_reasons: list[str] = []
        passed_all = True
        for platform in item.get("platforms", ["linux"]):
            platform = str(platform)
            rust_doc = _label_for(results, "rust", platform)
            if rust_doc is None:
                if platform == "windows":
                    report["blocked_owner"].setdefault(contract_id, _WINDOWS_NATIVE_MISSING)
                else:
                    missing_reasons.append(f"缺少 rust-{platform} 結果檔")
                passed_all = False
                continue
            if platform == "windows" and (
                rust_doc.get("native_windows") is not True
                or rust_doc.get("native_build_link") is not True
            ):
                report["blocked_owner"].setdefault(contract_id, _WINDOWS_NATIVE_MISSING)
                passed_all = False
                continue

            # Rust 側：Cargo L1 與雙邊黑箱節點都要在 rust-<platform> 逐項成立。
            rust_required: list[str] = []
            for nodeid in nodeids:
                expanded, error = _expand_nodeid(nodeid, [rust_doc], declared)
                if error:
                    missing_reasons.append(error)
                    passed_all = False
                rust_required.extend(expanded)
            if str(rust_doc.get("label")) in incomplete_runs:
                for nodeid in rust_required:
                    failure = _nodeid_failure(rust_doc, nodeid)
                    if failure:
                        failures.append(failure)
                missing_reasons.append(f"rust-{platform} 結果不完整")
                passed_all = False
                continue
            for nodeid in rust_required:
                ok, detail = _nodeid_passes(rust_doc, nodeid)
                if not ok:
                    _classify_rust_detail(detail, nodeid, failures, missing_reasons)
                    passed_all = False

            if not needs_ab:
                continue
            # Python 側：雙邊黑箱節點做 A/B，contract_tests 只對 Python；Cargo 節點不在 pytest 結果內。
            if not blackbox_nodes and not python_nodes:
                missing_reasons.append(f"缺少 Python 側證據：沒有雙邊黑箱 nodeid 或 contract_tests（{platform}）")
                passed_all = False
                continue
            python_doc = _label_for(results, "python", platform)
            if python_doc is None:
                missing_reasons.append(f"缺少 python-{platform} A/B 結果檔")
                passed_all = False
                continue
            ab_required: list[str] = []
            for nodeid in blackbox_nodes:
                expanded, error = _expand_nodeid(nodeid, [rust_doc, python_doc], declared)
                if error:
                    missing_reasons.append(error)
                    passed_all = False
                ab_required.extend(expanded)
            if str(python_doc.get("label")) in incomplete_runs:
                missing_reasons.append(f"python-{platform} 結果不完整")
                for nodeid in ab_required:
                    rust_ok, _ = _nodeid_passes(rust_doc, nodeid)
                    python_failure = _nodeid_failure(python_doc, nodeid)
                    if rust_ok and python_failure:
                        report["ab_mismatch"].append(contract_id)
                        failures.append(f"Python/Rust A/B 不一致：{python_failure}")
                passed_all = False
                continue
            # ab_required 是雙邊完整必要參數聯集；每個節點兩側都要收集且通過，任一側不成立即阻擋。
            for nodeid in ab_required:
                rust_ok, rust_reason = _nodeid_passes(rust_doc, nodeid)
                python_ok, python_reason = _nodeid_passes(python_doc, nodeid)
                if rust_ok and python_ok:
                    continue
                if not rust_ok and not python_ok:
                    # 雙邊皆未通過（例如只在一側收集、另一側 SKIP）不得漏判（P8X-e038dda-3）。
                    _classify_rust_detail(rust_reason, nodeid, failures, missing_reasons)
                    missing_reasons.append(python_reason or f"Python 證據不足：{nodeid}")
                    passed_all = False
                elif rust_ok and not python_ok:
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
            for base in python_nodes:
                expanded, error = _expand_nodeid(base, [python_doc], declared)
                if error:
                    missing_reasons.append(error)
                    passed_all = False
                for nodeid in expanded:
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
        report["case_verdicts"][contract_id.removeprefix("case:")] = _verdict_for(report, contract_id)
    for contract_id, item in contract_universe.items():
        if "windows" not in item.get("platforms", []):
            continue
        report["windows_verdicts"][contract_id] = _verdict_for(report, contract_id)
    return report


def _verdict_for(report: Mapping[str, Any], contract_id: str) -> str:
    if contract_id in report["blocked_owner"]:
        return "BLOCKED-OWNER"
    if contract_id in report["failed"]:
        return "FAIL"
    if contract_id in report["covered"]:
        return "PASS"
    return "EVIDENCE_MISSING"


def _parse_utc(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.timezone.utc)


def _load_json_object(path: Path, errors: list[str], label: str) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        errors.append(f"{label} 缺少或不是一般檔案")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{label} 無法解析：{type(exc).__name__}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{label} 根節點不是 object")
        return None
    return value


def _contracts_tree(repo_root: Path) -> str | None:
    """與 realhw `meta.contract_revision` 同源：`git rev-parse HEAD:contracts`。"""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD:contracts"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (proc.stdout or "").strip()
    return value if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None


@functools.lru_cache(maxsize=None)
def _formal_gate_checks(acceptance_path: str, platform: str, mode: str) -> tuple[tuple[str, str, str], ...]:
    """正式 P2 gate 在該平台／模式必定產出的檢查列 `(id, group, 空輸入結果)`。

    `realhw/acceptance/gate.py` 的每個分支都輸出同一組檢查 ID（只有結果隨輸入改變），
    NOT_APPLICABLE_BASELINE 也只依模式決定；以空輸入跑一次正式判定器即得完整必要集合與適用性，
    不在本工具另抄一份清單，避免與正式 gate 漂移（P8X-e038dda-2）。
    """
    gate = _repo_module("realhw.acceptance.gate")
    with tempfile.TemporaryDirectory(prefix="sw-formal-gate-") as tmp:
        empty = Path(tmp)
        result = gate.evaluate(gate.GateInputs(
            report_dir=empty, case_dir=empty, acceptance_path=Path(acceptance_path),
            platform=platform, mode=mode, observer_path=None, wal_dir=None, baseline_gate_path=None,
        ))
    return tuple((str(check["id"]), str(check["group"]), str(check["result"])) for check in result["checks"])


def _formal_summary(checks: Sequence[Mapping[str, Any]], mode: str) -> str | None:
    """以正式 gate 的彙總規則重算（candidate：`_summarize`；baseline：`_baseline_summary`）；無法重算回 None。"""
    try:
        gate = _repo_module("realhw.acceptance.gate")
        return gate._summarize(list(checks), True) if mode == "candidate" else gate._baseline_summary(list(checks))
    except Exception:  # noqa: BLE001 - 無法重算即不得接受彙總
        return None


def _gate_checks(doc: Mapping[str, Any], label: str, errors: list[str]) -> list[dict[str, Any]]:
    checks = doc.get("checks")
    if not isinstance(checks, list) or any(
        not isinstance(check, dict) or not isinstance(check.get("id"), str)
        or not isinstance(check.get("group"), str) or check.get("result") not in _GATE_CHECK_RESULTS
        for check in checks
    ):
        errors.append(f"{label} checks 格式無效")
        return []
    return checks


def _formal_check_errors(
    label: str, checks: Sequence[Mapping[str, Any]], acceptance_path: Path, platform: str, mode: str,
) -> list[str]:
    """檢查列必須恰為正式 gate 的完整集合：ID 唯一、無缺無多、group 相同、NOT_APPLICABLE_BASELINE 適用性一致。"""
    try:
        formal = _formal_gate_checks(str(acceptance_path), platform, mode)
    except Exception as exc:  # noqa: BLE001 - 正式判定器失敗即無法證明必要集合，證據不得成立
        return [f"無法以正式 P2 gate 推導 {platform} {mode} 必要檢查集合：{type(exc).__name__}: {exc}"]
    errors: list[str] = []
    formal_by_id = {check_id: (group, result) for check_id, group, result in formal}
    seen: set[str] = set()
    for check in checks:
        check_id = check["id"]
        if check_id in seen:
            errors.append(f"{label} 檢查 {check_id} 重複")
            continue
        seen.add(check_id)
        if check_id not in formal_by_id:
            errors.append(f"{label} 含正式 gate 未定義的檢查 {check_id}")
            continue
        group, formal_result = formal_by_id[check_id]
        if check.get("group") != group:
            errors.append(f"{label} 檢查 {check_id} group={check.get('group')!r} 與正式 gate（{group}）不同")
        if formal_result == _GATE_NA and check.get("result") != _GATE_NA:
            errors.append(f"{label} 檢查 {check_id}={check.get('result')}，正式 gate 在 {mode} 模式只判 {_GATE_NA}")
        elif formal_result != _GATE_NA and check.get("result") == _GATE_NA:
            errors.append(f"{label} 檢查 {check_id}={_GATE_NA} 不適用 {mode} 模式")
    for check_id, _, _ in formal:
        if check_id not in seen:
            errors.append(f"{label} 缺少正式 gate 必要檢查 {check_id}")
    return errors


def _check_hashed_file(path: Path, expected: object, label: str, errors: list[str]) -> None:
    if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
        errors.append(f"{label} 在 gate evidence 雜湊清單中缺少或格式無效")
    elif path.is_symlink() or not path.is_file():
        errors.append(f"{label} 缺檔")
    elif _sha256(path) != expected:
        errors.append(f"{label} 雜湊與 gate 記錄不符")


def _validate_longrun_platform(
    root: Path, platform: str, pinned_bundle: object, expected: Mapping[str, str | None],
    acceptance_path: Path = DEFAULT_ACCEPTANCE,
) -> tuple[list[str], str | None, dict[str, Any]]:
    """驗證單一平台的長跑證據；回 (錯誤清單, release_gate_result, 長跑受測身分)。"""
    errors: list[str] = []
    identity: dict[str, Any] = {"bundle_tree_sha256": None, "cli_sha256": None, "daemon_sha256": []}
    if root.is_symlink() or not root.is_dir():
        return [f"缺少 {platform}/ 長跑證據目錄"], None, identity
    if not isinstance(pinned_bundle, str) or not _SHA256.fullmatch(pinned_bundle):
        errors.append(f"{LONGRUN_MANIFEST} 缺少 {platform} 的 pinned bundle_tree_sha256")
        pinned_bundle = None

    candidate = _load_json_object(root / "release-gate.json", errors, "release-gate.json")
    verdict: str | None = None
    candidate_run: Mapping[str, Any] = {}
    if candidate is not None:
        if candidate.get("schema_version") != _GATE_SCHEMA:
            errors.append("release-gate.json schema_version 不是 release-gate.v1")
        if candidate.get("mode") != "candidate" or candidate.get("platform") != platform:
            errors.append(f"release-gate.json 不是 {platform} candidate 判定")
        verdict = candidate.get("release_gate_result") if isinstance(candidate.get("release_gate_result"), str) else None
        checks = _gate_checks(candidate, "release-gate.json", errors)
        errors.extend(_formal_check_errors("release-gate.json", checks, acceptance_path, platform, "candidate"))
        by_id = {check["id"]: check.get("result") for check in checks}
        if verdict not in _GATE_VERDICTS:
            errors.append(f"release-gate.json release_gate_result 無效：{verdict!r}")
        elif _formal_summary(checks, "candidate") != verdict:
            errors.append("release-gate.json release_gate_result 與 checks 彙總不一致")
        for check_id in _LONGRUN_PRECONDITION_CHECKS:
            if check_id in by_id and by_id[check_id] != "PASS":
                errors.append(f"release-gate.json 前提檢查 {check_id}={by_id[check_id]}（證據不成立）")
        if not isinstance(candidate.get("engine_result"), dict):
            errors.append("release-gate.json 缺少 engine_result")
        acceptance = candidate.get("acceptance") if isinstance(candidate.get("acceptance"), dict) else {}
        if acceptance.get("sha256") != expected.get("acceptance_sha256"):
            errors.append("release-gate.json acceptance.sha256 與目前 longrun_acceptance.yaml 不同")
        candidate_run = candidate.get("run") if isinstance(candidate.get("run"), dict) else {}
        if expected.get("contract_revision") is None or candidate_run.get("contract_revision") != expected["contract_revision"]:
            errors.append("release-gate.json run.contract_revision 與目前 contracts 樹不同（不得跨候選版本）")
        if candidate_run.get("cases_yaml_sha256") != expected.get("cases_yaml_sha256"):
            errors.append("release-gate.json run.cases_yaml_sha256 與目前 cases.yaml 不同")
        duration = candidate_run.get("duration_s")
        if not isinstance(duration, (int, float)) or duration < _LONGRUN_MIN_DURATION_S:
            errors.append(f"release-gate.json run.duration_s 未達 48 小時：{duration!r}")
        cli_identity = candidate_run.get("cli_identity") if isinstance(candidate_run.get("cli_identity"), dict) else {}
        cli_sha = cli_identity.get("sha256")
        if cli_identity.get("pinned") is not True or not isinstance(cli_sha, str) or not _SHA256.fullmatch(cli_sha):
            errors.append("release-gate.json run.cli_identity 未證明 pinned CLI")
        else:
            identity["cli_sha256"] = cli_sha
        identity["bundle_tree_sha256"] = pinned_bundle
        deployed = candidate_run.get("deployed_content") if isinstance(candidate_run.get("deployed_content"), dict) else {}
        for edge in ("start", "end"):
            snapshot = deployed.get(edge) if isinstance(deployed.get(edge), dict) else {}
            if snapshot.get("error") is not None or not snapshot:
                errors.append(f"release-gate.json deployed_content.{edge} 量測失敗或缺少")
            elif snapshot.get("bundle_tree_sha256") != pinned_bundle:
                errors.append(f"release-gate.json deployed_content.{edge}.bundle_tree_sha256 與 pinned bundle 不符")
            elif snapshot.get("cli_sha256") != cli_sha:
                errors.append(f"release-gate.json deployed_content.{edge}.cli_sha256 與 pinned CLI 不符")
        live = deployed.get("daemon_live")
        if not isinstance(live, list) or not live or any(
            not isinstance(entry, dict) or entry.get("in_install") is not True or entry.get("error") is not None
            or not isinstance(entry.get("image_sha256"), str) or not _SHA256.fullmatch(entry["image_sha256"])
            for entry in live
        ):
            errors.append("release-gate.json deployed_content.daemon_live 未證明受測 daemon 位於 pinned bundle")
        else:
            identity["daemon_sha256"] = sorted({entry["image_sha256"] for entry in live})
        hashes = candidate.get("evidence_hashes") if isinstance(candidate.get("evidence_hashes"), dict) else {}
        for name in _LONGRUN_EVIDENCE_FILES:
            _check_hashed_file(root / name, hashes.get(name), name, errors)
        wal = hashes.get("wal")
        if not isinstance(wal, dict) or not wal:
            errors.append("release-gate.json evidence_hashes.wal 缺少 WAL 雜湊")
        else:
            for name, digest in sorted(wal.items()):
                if not isinstance(name, str) or Path(name).name != name:
                    errors.append(f"wal/{name} 名稱無效")
                    continue
                _check_hashed_file(root / "wal" / name, digest, f"wal/{name}", errors)
        if platform == "windows":
            excl = candidate.get("excl_evidence_hashes") if isinstance(candidate.get("excl_evidence_hashes"), dict) else {}
            for name in _WINDOWS_EXCL_FILES:
                _check_hashed_file(root / "excl" / name, excl.get(name), f"excl/{name}", errors)

    baseline = _load_json_object(root / "baseline-release-gate.json", errors, "baseline-release-gate.json")
    if baseline is not None and candidate is not None:
        base_run = baseline.get("run") if isinstance(baseline.get("run"), dict) else {}
        base_acceptance = baseline.get("acceptance") if isinstance(baseline.get("acceptance"), dict) else {}
        cand_acceptance = candidate.get("acceptance") if isinstance(candidate.get("acceptance"), dict) else {}
        if baseline.get("schema_version") != _GATE_SCHEMA or baseline.get("mode") != "baseline" or baseline.get("platform") != platform:
            errors.append(f"baseline-release-gate.json 不是 {platform} 的 Python baseline 判定")
        if baseline.get("baseline_result") != "PASS":
            errors.append(f"baseline-release-gate.json baseline_result={baseline.get('baseline_result')!r}")
        base_checks = _gate_checks(baseline, "baseline-release-gate.json", errors)
        errors.extend(_formal_check_errors(
            "baseline-release-gate.json", base_checks, acceptance_path, platform, "baseline",
        ))
        if _formal_summary(base_checks, "baseline") != baseline.get("baseline_result"):
            errors.append("baseline-release-gate.json baseline_result 與 checks 彙總不一致")
        base_duration = base_run.get("duration_s")
        if not isinstance(base_duration, (int, float)) or base_duration < _LONGRUN_MIN_DURATION_S:
            errors.append(f"baseline-release-gate.json run.duration_s 未達 48 小時：{base_duration!r}")
        if base_acceptance.get("workload_contract_sha256") != cand_acceptance.get("workload_contract_sha256"):
            errors.append("baseline-release-gate.json workload_contract_sha256 與 candidate 不同")
        if base_run.get("engine_files_sha256") != candidate_run.get("engine_files_sha256"):
            errors.append("baseline-release-gate.json engine_files_sha256 與 candidate 不同")

    attest = _load_json_object(root / "supervision-attest.json", errors, "supervision-attest.json")
    if attest is not None:
        if attest.get("auto_restart_disabled") is not True:
            errors.append("supervision-attest.json auto_restart_disabled 不是 true")
        header: Any = None
        try:
            first = (root / "observer.ndjson").read_text(encoding="utf-8").splitlines()[:1]
            header = json.loads(first[0]) if first else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            header = None
        supervision = header.get("supervision") if isinstance(header, dict) and header.get("kind") == "header" else None
        wanted = {key: attest.get(key) for key in ("mode", "auto_restart_disabled", "evidence")}
        if not isinstance(supervision, dict) or {key: supervision.get(key) for key in wanted} != wanted:
            errors.append("observer.ndjson header 的 supervision 與 supervision-attest.json 不符")

    lock = _load_json_object(root / "bench-lock.json", errors, "bench-lock.json")
    if lock is not None:
        acquired = _parse_utc(lock.get("acquired_at_utc"))
        released = _parse_utc(lock.get("released_at_utc"))
        started = _parse_utc(candidate_run.get("started_at_utc"))
        ended = _parse_utc(candidate_run.get("ended_at_utc"))
        if not isinstance(lock.get("path"), str) or not lock["path"].strip():
            errors.append("bench-lock.json 缺少 path")
        if acquired is None or released is None or started is None or ended is None:
            errors.append("bench-lock.json 或 candidate run 缺少可解析的 UTC 時間")
        elif not (acquired <= started and ended <= released):
            errors.append("bench-lock.json 持有區間未涵蓋整個長跑區間")
    return list(dict.fromkeys(errors)), verdict, identity


def validate_longrun(source: Path, *, platforms: Sequence[str] | None = None, repo_root: Path = ROOT) -> dict[str, Any]:
    """驗證長跑證據目錄；只有每個平台都通過全部檢查時 `accepted` 才為 true。"""
    if platforms is None:
        platforms = _longrun_platforms(repo_root / "tests/acceptance/cases.yaml")
    acceptance_path = repo_root / "realhw/acceptance/longrun_acceptance.yaml"
    expected = {
        "acceptance_sha256": _sha256(acceptance_path),
        "cases_yaml_sha256": _sha256(repo_root / "tests/acceptance/cases.yaml"),
        "contract_revision": _contracts_tree(repo_root),
    }
    record: dict[str, Any] = {
        "schema_version": LONGRUN_IMPORT_SCHEMA,
        "case_id": LONGRUN_CASE.removeprefix("case:"),
        "source": str(source),
        "accepted": False,
        "errors": [],
        "platforms": {},
    }
    manifest = _load_json_object(source / LONGRUN_MANIFEST, record["errors"], LONGRUN_MANIFEST)
    manifest_platforms: Mapping[str, Any] = {}
    if manifest is not None:
        if manifest.get("schema_version") != LONGRUN_SCHEMA:
            record["errors"].append(f"{LONGRUN_MANIFEST} schema_version 不是 {LONGRUN_SCHEMA}")
        if manifest.get("case_id") != record["case_id"]:
            record["errors"].append(f"{LONGRUN_MANIFEST} case_id 不是 {record['case_id']}")
        if isinstance(manifest.get("platforms"), dict):
            manifest_platforms = manifest["platforms"]
        else:
            record["errors"].append(f"{LONGRUN_MANIFEST} platforms 必須是 mapping")
    for platform in platforms:
        entry = manifest_platforms.get(platform)
        bundle = entry.get("bundle_tree_sha256") if isinstance(entry, dict) else None
        errors, verdict, identity = _validate_longrun_platform(
            source / platform, platform, bundle, expected, acceptance_path,
        )
        errors = [*record["errors"], *errors]
        record["platforms"][platform] = {
            "accepted": not errors, "release_gate_result": verdict, "errors": errors, "identity": identity,
        }
    record["accepted"] = not record["errors"] and bool(record["platforms"]) and all(
        entry["accepted"] for entry in record["platforms"].values()
    )
    return record


def _longrun_platforms(cases_path: Path) -> list[str]:
    for case in _read_yaml(cases_path).get("cases", []):
        if isinstance(case, dict) and case.get("case_id") == LONGRUN_CASE.removeprefix("case:"):
            return _platforms_from_scope(case.get("platforms"))
    return ["linux", "windows"]


def _copy_evidence_tree(source: Path, destination: Path) -> list[str]:
    """只複製一般檔案；遇到 symlink 拒收，避免把目錄外內容帶進 run directory。"""
    errors: list[str] = []
    for directory, dirnames, filenames in os.walk(source, followlinks=False):
        current = Path(directory)
        for name in [*dirnames, *filenames]:
            if (current / name).is_symlink():
                errors.append(f"{(current / name).relative_to(source)} 是 symlink，拒絕匯入")
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            path = current / name
            if path.is_symlink() or not path.is_file():
                continue
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    return errors


def import_longrun(source: Path, run_dir: Path) -> dict[str, Any]:
    """把長跑證據複製進 run directory 後重新驗證副本，並寫入匯入紀錄。"""
    destination = run_dir / LONGRUN_DIRNAME
    if destination.exists() or (run_dir / LONGRUN_RECORD).exists():
        raise ValueError(f"{destination} 或 {LONGRUN_RECORD} 已存在，拒絕混合不同輪次的長跑證據")
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"長跑證據目錄不存在：{source}")
    destination.mkdir(parents=True)
    copy_errors = _copy_evidence_tree(source, destination)
    record = validate_longrun(destination)
    record["source"] = str(source)
    if copy_errors:
        record["errors"] = [*record["errors"], *copy_errors]
        record["accepted"] = False
        for entry in record["platforms"].values():
            entry["errors"] = [*entry["errors"], *copy_errors]
            entry["accepted"] = False
    (run_dir / LONGRUN_RECORD).write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8",
    )
    return record


def _candidate_bundle_identity(
    run_dir: Path, platform: str, results: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """讀本輪候選 bundle 紀錄並重算 tree 雜湊；bin/ 內 CLI／daemon 必須等於 rust-<platform> 實際受測 artifact。"""
    errors: list[str] = []
    label = f"artifacts/{platform}/{CANDIDATE_BUNDLE_RECORD}"
    rust_doc = _label_for(results, "rust", platform)
    if rust_doc is None:
        errors.append(f"本輪缺少 rust-{platform} 受測結果，長跑無法綁定本輪候選 artifact")
    record = _load_json_object(run_dir / "artifacts" / platform / CANDIDATE_BUNDLE_RECORD, errors, label)
    if record is None:
        errors.append(f"本輪未記錄 {platform} 候選 bundle（slice24_exit.sh --candidate-bundle／Windows runner bundle/）")
        return None, errors
    files = record.get("files")
    entries: dict[str, str] = {}
    lines = bytearray()
    previous: bytes | None = None
    valid = isinstance(files, list) and bool(files)
    for entry in files if valid else []:
        if not (
            isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str) and entry[0]
            and isinstance(entry[1], str) and _SHA256.fullmatch(entry[1])
        ):
            valid = False
            break
        key = entry[0].encode("utf-8")
        if previous is not None and key <= previous:
            valid = False
            break
        previous = key
        entries[entry[0]] = entry[1]
        lines.extend(f"{entry[0]}\0{entry[1]}\n".encode("utf-8"))
    if record.get("schema_version") != CANDIDATE_BUNDLE_SCHEMA or record.get("platform") != platform or not valid:
        errors.append(f"{label} 格式無效")
        return None, errors
    tree = hashlib.sha256(bytes(lines)).hexdigest()
    if record.get("bundle_tree_sha256") != tree:
        errors.append(f"{label} bundle_tree_sha256 與檔案清單重算結果不同")
        return None, errors
    cli_name, daemon_name = _BUNDLE_BINARIES[platform]
    identity = {"bundle_tree_sha256": tree, "cli_sha256": entries.get(cli_name), "daemon_sha256": entries.get(daemon_name)}
    for key, name in (("cli_sha256", cli_name), ("daemon_sha256", daemon_name)):
        if identity[key] is None:
            errors.append(f"{label} 缺少 {name}")
        elif rust_doc is not None and rust_doc.get(key) != identity[key]:
            errors.append(f"本輪候選 bundle 的 {name} 與 rust-{platform} 實際受測 artifact 不同")
    return identity, errors


def bind_longrun_candidate(
    record: dict[str, Any], run_dir: Path, results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """判定時把長跑的 pinned bundle、CLI、daemon 映像與本輪候選交叉核對（P8X-e038dda-1）。

    只比 contracts 樹／cases／門檻檔不足以證明是同一候選；任何一項不同或本輪缺少候選紀錄，該平台證據即不成立。
    """
    for platform, entry in record.get("platforms", {}).items():
        if not isinstance(entry, dict):
            continue
        candidate, errors = _candidate_bundle_identity(run_dir, str(platform), results)
        longrun = entry.get("identity") if isinstance(entry.get("identity"), dict) else {}
        if candidate is not None:
            if longrun.get("bundle_tree_sha256") != candidate["bundle_tree_sha256"]:
                errors.append("長跑 pinned bundle_tree_sha256 與本輪候選 bundle 不同")
            if candidate["cli_sha256"] is not None and longrun.get("cli_sha256") != candidate["cli_sha256"]:
                errors.append("長跑 pinned CLI 與本輪候選 CLI 不同")
            daemons = longrun.get("daemon_sha256") if isinstance(longrun.get("daemon_sha256"), list) else []
            if candidate["daemon_sha256"] is not None and (
                not daemons or any(digest != candidate["daemon_sha256"] for digest in daemons)
            ):
                errors.append("長跑受測 daemon 映像與本輪候選 daemon 不同")
        entry["candidate"] = candidate
        if errors:
            entry["accepted"] = False
            entry["errors"] = [*entry.get("errors", []), *errors]
    record["accepted"] = record.get("accepted") is True and all(
        isinstance(entry, dict) and entry.get("accepted") is True for entry in record.get("platforms", {}).values()
    )
    return record


def record_candidate_bundle(platform: str, bundle_root: Path, run_dir: Path) -> dict[str, Any]:
    """記錄本輪候選 bundle 版本目錄的檔案清單與 tree 雜湊；只收一般檔案，不覆寫既有紀錄。"""
    if platform not in _BUNDLE_BINARIES:
        raise ValueError(f"未知平台：{platform}")
    destination = run_dir / "artifacts" / platform / CANDIDATE_BUNDLE_RECORD
    if destination.exists():
        raise ValueError(f"{destination} 已存在，拒絕覆寫")
    if bundle_root.is_symlink() or not bundle_root.is_dir():
        raise ValueError(f"候選 bundle 不是一般目錄：{bundle_root}")
    root = bundle_root.resolve()
    if not (root / "release-manifest.json").is_file():
        raise ValueError(f"候選 bundle 缺少 release-manifest.json：{root}")
    entries: list[tuple[str, str]] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in [*dirnames, *filenames]:
            path = current / name
            rel = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ValueError(f"候選 bundle 含非一般檔案：{rel}")
            digest = _sha256(path)
            if digest is None:
                raise ValueError(f"候選 bundle 檔案無法讀取：{rel}")
            entries.append((rel, digest))
    entries.sort(key=lambda item: item[0].encode("utf-8"))
    tree = hashlib.sha256(b"".join(f"{rel}\0{digest}\n".encode("utf-8") for rel, digest in entries)).hexdigest()
    measured = _repo_module("realhw.deployed_content").tree_digests(root)[0]
    if measured != tree:
        raise ValueError("候選 bundle tree 雜湊與 realhw deployed_content 量測不一致")
    names = {rel for rel, _ in entries}
    for name in _BUNDLE_BINARIES[platform]:
        if name not in names:
            raise ValueError(f"候選 bundle 缺少 {name}")
    record = {
        "schema_version": CANDIDATE_BUNDLE_SCHEMA,
        "platform": platform,
        "bundle_tree_sha256": tree,
        "files": [[rel, digest] for rel, digest in entries],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8",
    )
    return record


def load_owner_evidence(
    run_dir: Path, results: Sequence[Mapping[str, Any]] = (),
) -> dict[str, dict[str, Any]]:
    """判定時重新驗證 run directory 內的擁有者證據副本並綁定本輪候選；沒有副本時不提供任何解除。"""
    destination = run_dir / LONGRUN_DIRNAME
    if not destination.exists():
        return {}
    record = validate_longrun(destination)
    imported = run_dir / LONGRUN_RECORD
    try:
        recorded = json.loads(imported.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        recorded = None
    recorded_ok = isinstance(recorded, dict) and recorded.get("accepted") is True
    if record["accepted"] and not recorded_ok:
        record["accepted"] = False
        for entry in record["platforms"].values():
            entry["accepted"] = False
            entry["errors"] = [*entry["errors"], f"{LONGRUN_RECORD} 缺少或匯入時未通過驗證"]
    return {LONGRUN_CASE: bind_longrun_candidate(record, run_dir, results)}


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


def _longrun_report_lines(report: Mapping[str, Any]) -> list[str]:
    owner = report.get("owner_evidence", {})
    entry = owner.get(LONGRUN_CASE) if isinstance(owner, Mapping) else None
    if not isinstance(entry, Mapping):
        return ["- 未匯入長跑證據（`slice24_exit.sh --longrun <dir>`）；`lr-mixed` 維持 **BLOCKED-OWNER**。"]
    return [f"- 判定：**{entry.get('verdict')}**；{entry.get('reason')}。"]


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
        "## T6 SPEC-GAP 4：FLASHING／RECOVERING 下的 file.* 閘門",
        "",
        "- 結案依據：依 2026-11-12 擁有者裁決第 9 點，以 Rust L1 節點及 Python／Rust 程式路徑對照登記。",
        "- Rust L1 nodeid：`cargo:sw-core::file_transfer_session::non_ready_states_rejected_with_session_zero_tx`（`crates/sw-core/tests/file_transfer_session.rs:451-470`；逐案測試同時覆蓋 file.push、file.pull、RECOVERING、FLASHING 與零 TX）。",
        "- Rust 閘門：`crates/sw-core/src/session_core/file_xfer.rs:104-125`，`file_transfer_admit` 在進入 OpRunner 前依狀態拒絕傳輸。",
        "- Python 對照：`origin/main:sw_core/service.py:1451-1466,1489-1501` 將兩個 RPC 送入 `SessionManager.file_push`／`file_pull`；`origin/main:sw_core/session_manager.py:8312-8323,8415-8424` 在非 READY 與 recovering 狀態拒絕。",
        "- 狀態：**以 Rust L1＋程式對照結案**；本列不宣稱已取得 Python 對跑或 Windows 原生證據。",
    ])

    lines.extend([
        "",
        "## 待裁決",
        "",
        "| 項目 | 裁決狀態 | 結果記錄 | 說明 |",
        "|---|---|---|---|",
        "",
        "## 長跑證據（lr-mixed）",
        "",
        *_longrun_report_lines(report),
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
    parser.add_argument(
        "--expect-platform", choices=("linux", "windows"), default="linux",
        help="--expect-*-sha256 適用的平台；其他平台的 artifact 另以各自 runner manifest 核對",
    )
    parser.add_argument(
        "--import-longrun", type=Path,
        help="驗證長跑證據目錄並匯入 <run-dir>/longrun/；全部成立 exit 0，拒收 exit 1",
    )
    parser.add_argument(
        "--record-candidate-bundle", metavar="PLATFORM=DIR",
        help="記錄本輪候選 bundle 版本目錄（含 release-manifest.json）到 <run-dir>/artifacts/<platform>/",
    )
    args = parser.parse_args(argv)

    if args.record_candidate_bundle is not None:
        platform, _, bundle = args.record_candidate_bundle.partition("=")
        try:
            if not bundle:
                raise ValueError("--record-candidate-bundle 需為 PLATFORM=DIR")
            record = record_candidate_bundle(platform, Path(bundle), args.run_dir)
        except (OSError, ValueError) as exc:
            print(f"contract_coverage: 候選 bundle 記錄失敗：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print(f"candidate-bundle {platform}: bundle_tree_sha256={record['bundle_tree_sha256']} files={len(record['files'])}")
        return 0

    if args.import_longrun is not None:
        try:
            args.run_dir.mkdir(parents=True, exist_ok=True)
            record = import_longrun(args.import_longrun, args.run_dir)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            print(f"contract_coverage: 長跑證據匯入失敗：{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        for platform, entry in sorted(record["platforms"].items()):
            state = "ACCEPTED" if entry["accepted"] else "REJECTED"
            print(f"longrun {platform}: {state} release_gate_result={entry['release_gate_result']}")
            for error in entry["errors"]:
                print(f"  - {error}")
        return 0 if record["accepted"] else 1

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
            expected_platform=args.expect_platform,
            owner_evidence=load_owner_evidence(args.run_dir, result_rows),
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
