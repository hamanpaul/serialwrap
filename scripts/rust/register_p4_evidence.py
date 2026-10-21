#!/usr/bin/env python3
"""依凍結 P4 對照表與實際測試報告登錄逐案例證據。"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

import yaml


ROOT = Path(__file__).resolve().parents[2]
CASE_MAP = ROOT / "tests" / "acceptance" / "p4_case_map.yaml"
EVIDENCE_DIR = ROOT / "target" / "p4-evidence"
L1_LOCK = ROOT / "tests" / "acceptance" / "l1_required.lock.json"
L1_JUDGE = ROOT / "tests" / "acceptance" / "l1_judge.py"
PRODUCER = "register_p4_evidence.py"
BLOCKED_LAYERS = {"L2", "L3", "manual"}
# 這些案例須由 judge_spike2 寫入；其中包含 owner／硬體證據，不得由一般 L1 報告代替。
JUDGE_SPIKE2_CASES = {
    "win-task-logon-trigger",
    "win-task-single-instance",
    "win-task-restart-on-failure",
    "win-task-72h",
    "p1-rst-daemon",
    "rm-live-e2e",
}
LIST_LINE = re.compile(r"^(.+?):\s+test\s*$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _head(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("無法從指定 repo 取得 git HEAD")
    return commit


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _load_case_map(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list):
        raise ValueError(f"P4 對照表缺少 cases 陣列：{path}")
    return [case for case in cases if isinstance(case, dict)]


def _node_matches(actual: str, expected: str) -> bool:
    return actual == expected or actual.startswith(expected + "[")


def _pytest_module_candidates(classname: str) -> list[str]:
    parts = classname.split(".") if classname else []
    candidates: list[str] = []
    for end in range(len(parts), 0, -1):
        prefix = parts[:end]
        joined = "/".join(prefix)
        if prefix[0] == "tests":
            candidates.append(joined + ".py")
        elif prefix[0] == "contract_blackbox":
            candidates.append("tests/" + joined + ".py")
        elif "contract_blackbox" in prefix:
            candidates.append("tests/" + joined + ".py")
        elif prefix[0].startswith("test_"):
            candidates.extend(("tests/contract_blackbox/" + joined + ".py", "tests/" + joined + ".py"))
        else:
            candidates.append(joined + ".py")
    return candidates


def _pytest_node_path(classname: str, name: str, source_root: Path | None = None) -> str:
    parts = classname.split(".") if classname else []
    if source_root is not None:
        for module_path in _pytest_module_candidates(classname):
            if (source_root / module_path).is_file():
                module_parts = module_path[:-3].split("/")
                class_parts = parts[len(module_parts) :]
                suffix = "".join(f"::{part}" for part in class_parts)
                return f"{module_path}{suffix}::{name}"
    if parts and parts[0] == "tests":
        module_path = "/".join(parts) + ".py"
    elif parts and parts[0] == "contract_blackbox":
        module_path = "tests/" + "/".join(parts) + ".py"
    elif parts and parts[0].startswith("test_"):
        parent = "tests/contract_blackbox" if "contract_blackbox" in classname else "tests"
        module_path = parent + "/" + "/".join(parts) + ".py"
    else:
        module_path = "/".join(parts) + ".py"
    return f"{module_path}::{name}"


def _junit_rows(path: Path | None, source_root: Path | None = None) -> dict[str, list[str]] | None:
    if path is None or not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    rows: dict[str, list[str]] = {}
    for testcase in root.findall(".//testcase"):
        nodeid = _pytest_node_path(testcase.get("classname", ""), testcase.get("name", ""), source_root)
        if testcase.find("failure") is not None or testcase.find("error") is not None:
            outcome = "failed"
        elif testcase.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        rows.setdefault(nodeid, []).append(outcome)
    return rows


def _report_rows(path: Path, platform: str, runner: str) -> tuple[dict[str, Any] | None, str | None]:
    report = _read_json(path)
    if report is None:
        return None, f"報告不存在或無法解析：{path}"
    if report.get("format") != "l1-results.v1":
        return report, "報告格式不是 l1-results.v1"
    if report.get("platform") != platform or report.get("runner") != runner:
        return report, f"報告 platform/runner 不符（需 {platform}/{runner}）"
    if report.get("finished") is not True:
        return report, "L1 報告未完整結束"
    if not isinstance(report.get("collected"), list) or not isinstance(report.get("results"), list):
        return report, "L1 報告缺少 collected/results 陣列"
    return report, None


def _load_l1_judge() -> Any | None:
    if not L1_JUDGE.is_file():
        return None
    spec = importlib.util.spec_from_file_location("p4_l1_judge", L1_JUDGE)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def _junit_counts(path: Path | None) -> dict[str, int] | None:
    if path is None or not path.is_file():
        return None
    try:
        root = ET.parse(path).getroot()
        suite = root if root.tag == "testsuite" else root.find("testsuite")
        if suite is None:
            return None
        return {key: int(suite.get(key, "0")) for key in ("tests", "failures", "errors", "skipped")}
    except (OSError, ET.ParseError, ValueError):
        return None


def _matching_outcomes(values: Iterable[object], expected: str, *, cargo: bool = False) -> list[str]:
    found: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.removeprefix("cargo:") if cargo else value
        if _node_matches(candidate, expected):
            found.append(value)
    return found


def _l1_outcome(
    report_path: Path,
    platform: str,
    runner: str,
    expected: str,
    *,
    junit_path: Path | None = None,
    source_root: Path | None = None,
) -> tuple[bool, str | None]:
    report, invalid = _report_rows(report_path, platform, runner)
    if report is None or invalid:
        return False, invalid or "L1 報告不可用"
    judge = _load_l1_judge()
    if judge is None:
        return False, "BLOCKED-PREREQ：缺 tests/acceptance/l1_judge.py，拒絕以其他邏輯替代 L1 判定"
    try:
        lock = json.loads(L1_LOCK.read_text(encoding="utf-8"))
        set_id = "s1b_20groups" if runner == "pytest" else "s1b_cargo"
        selector = lock["sets"][set_id][platform]["selector"]
        required = judge.required_for(lock, set_id, platform)
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return False, f"L1 凍結集合不可用：{type(exc).__name__}"
    junit = _junit_counts(junit_path) if runner == "pytest" else None
    if runner == "pytest" and junit is None:
        return False, f"pytest junit 不存在或無法解析：{junit_path}"
    verdict = judge.judge(report, required, platform=platform, junit=junit, selector=selector)
    if verdict.get("verdict") != "PASS":
        reasons = verdict.get("reasons") or []
        return False, f"l1_judge {verdict.get('verdict', 'INVALID')}: {'; '.join(map(str, reasons))}"

    cargo = runner == "cargo"
    collected = _matching_outcomes(report["collected"], expected, cargo=cargo)
    results = [
        row
        for row in report["results"]
        if isinstance(row, dict)
        and isinstance(row.get("nodeid"), str)
        and _node_matches(row["nodeid"].removeprefix("cargo:") if cargo else row["nodeid"], expected)
    ]
    if not collected or not results:
        return False, "必要測試未出現在 L1 collected/results"
    if len(collected) != len(set(collected)):
        return False, "L1 collected 含重複測試節點"
    result_ids = [str(row["nodeid"]) for row in results]
    if len(result_ids) != len(set(result_ids)):
        return False, "L1 報告含重複測試節點"
    if set(collected) != set(result_ids):
        return False, "L1 collected 與 results 節點不一致"
    if any(row.get("outcome") != "passed" for row in results):
        return False, "L1 測試結果不是全數 passed"

    if junit_path is not None:
        junit = _junit_rows(junit_path, source_root)
        if junit is None:
            return False, f"pytest junit 不存在或無法解析：{junit_path}"
        matched = [
            outcome
            for nodeid, outcomes in junit.items()
            if _node_matches(nodeid, expected)
            for outcome in outcomes
        ]
        if not matched:
            return False, "必要測試未出現在 junit.xml"
        if len(matched) != len(collected):
            return False, "junit 與 L1 報告的必要測試筆數不一致"
        if any(outcome != "passed" for outcome in matched):
            return False, "junit 必要測試不是全數 passed"
    return True, None


def _rust_manifest(cli: Path, commit: str) -> tuple[dict[str, Any] | None, str | None]:
    """驗證 Rust CLI 位於標示受測 HEAD 的 bundle，且 manifest SHA 對應實際執行檔。"""
    if not cli.is_file():
        return None, "Rust CLI artifact 不存在"
    candidates = [cli.parent.parent / "release-manifest.json"]
    pointer = cli.parent / "current-release.json"
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_text(encoding="utf-8"))
            directory = Path(data["dir"])
            if not directory.is_absolute():
                directory = cli.parent / directory
            candidates.insert(0, directory / "release-manifest.json")
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return None, "current-release.json 無法解析"
    manifest_path = next((path for path in candidates if path.is_file()), None)
    if manifest_path is None:
        return None, "Rust CLI bundle 缺少 release-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "Rust CLI release-manifest.json 無法解析"
    if not isinstance(manifest, dict):
        return None, "Rust CLI release manifest 根節點不是物件"
    component = (manifest.get("components") or {}).get("cli")
    if not isinstance(component, dict) or component.get("implementation") != "rust":
        return None, "release manifest 未標示 Rust CLI"
    if manifest.get("git_revision") != commit:
        return None, "release manifest git_revision 不等於受測 HEAD"
    if component.get("git_revision") not in (None, commit):
        return None, "CLI component git_revision 不等於受測 HEAD"
    relative = component.get("path")
    if not isinstance(relative, str) or Path(relative).is_absolute():
        return None, "release manifest CLI path 無效"
    root = manifest_path.parent.resolve()
    manifest_cli = (root / relative).resolve()
    try:
        manifest_cli.relative_to(root)
    except ValueError:
        return None, "release manifest CLI path 超出 bundle"
    if manifest_cli != cli.resolve():
        return None, "release manifest CLI path 與受測 artifact 不符"
    actual_sha = _sha256(cli)
    if component.get("sha256") != actual_sha:
        return None, "release manifest CLI sha256 與實際 artifact 不符"
    return {"path": manifest_path, "sha256": _sha256(manifest_path), "cli_sha256": actual_sha}, None


def _deployed_cli_problem(report_path: Path, bundle: dict[str, Any]) -> str | None:
    report = _read_json(report_path)
    deployed = report.get("deployed_content") if report else None
    if not isinstance(deployed, dict):
        return "L1 報告缺少 deployed_content"
    for side in ("start", "end"):
        measured = deployed.get(side)
        if not isinstance(measured, dict) or measured.get("error") is not None:
            return f"L1 報告 {side} deployed_content 不完整"
        if measured.get("cli_sha256") != bundle["cli_sha256"]:
            return f"L1 報告 {side} CLI sha256 與 bundle 不符"
        if measured.get("release_manifest_sha256") != bundle["sha256"]:
            return f"L1 報告 {side} release manifest sha256 與 bundle 不符"
    return None


def _expression_path(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expression_path(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _marker_case_ids(node: ast.AST) -> set[str]:
    values: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or _expression_path(child.func) != "pytest.mark.case":
            continue
        if child.args and isinstance(child.args[0], ast.Constant) and isinstance(child.args[0].value, str):
            values.add(child.args[0].value)
    return values


def _case_marker_matches(source_root: Path, nodeid: str, case_id: str) -> tuple[bool, str | None]:
    path_text, separator, test_name = nodeid.partition("::")
    if not separator:
        return False, "pytest nodeid 缺少 ::test 名稱"
    source = source_root / path_text
    try:
        module = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    except (OSError, SyntaxError, UnicodeError) as exc:
        return False, f"無法讀取 pytest 測試來源：{type(exc).__name__}"

    test_parts = test_name.split("::")
    function_name = test_parts[-1].split("[", 1)[0]
    class_name = test_parts[-2] if len(test_parts) > 1 else None
    if class_name is None:
        scope: ast.Module | ast.ClassDef = module
    else:
        classes = [node for node in module.body if isinstance(node, ast.ClassDef) and node.name == class_name]
        if len(classes) != 1:
            return False, "pytest 測試類別不存在或不唯一"
        scope = classes[0]
    functions = [
        node
        for node in scope.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    ]
    if len(functions) != 1:
        return False, "pytest 測試函式不存在"

    module_markers: set[str] = set()
    class_markers: set[str] = set()
    for statement in module.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
                module_markers.update(_marker_case_ids(statement.value))
        if isinstance(statement, ast.ClassDef) and statement.name == class_name:
            for decorator in statement.decorator_list:
                class_markers.update(_marker_case_ids(decorator))
            for member in statement.body:
                if isinstance(member, (ast.Assign, ast.AnnAssign)):
                    targets = member.targets if isinstance(member, ast.Assign) else [member.target]
                    if any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
                        class_markers.update(_marker_case_ids(member.value))

    for function in functions:
        function_markers: set[str] = set()
        for decorator in function.decorator_list:
            function_markers.update(_marker_case_ids(decorator))
        for statement in function.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                if any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
                    function_markers.update(_marker_case_ids(statement.value))
        if case_id in module_markers | class_markers | function_markers:
            return True, None
    return False, f"@pytest.mark.case({case_id!r}) 不存在於測試或其所屬層級"


def _test_exe_parts(nodeid: str) -> tuple[str, str, str] | None:
    parts = nodeid.split("::")
    if len(parts) < 3:
        return None
    crate, target = parts[0], parts[1]
    test_name = "::".join(parts[2:])
    if not crate.startswith("sw-"):
        return None
    return crate, target, test_name


def _listed_tests(path: Path, platform: str, win_runner: Path) -> set[str] | None:
    argv = [str(path), "--list", "--format", "terse"]
    if platform == "windows":
        argv = [str(win_runner), str(path), "--list", "--format", "terse"]
    env = os.environ.copy()
    for name in ("SW_TEST_COM_PAIR", "SW_TEST_CAPS", "SW_TEST_SERIALWRAPD"):
        env.pop(name, None)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=90, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    names: set[str] = set()
    for line in result.stdout.splitlines():
        matched = LIST_LINE.match(line.strip())
        if matched:
            names.add(matched.group(1))
    return names


def _cargo_executable(
    cargo_target_dir: Path,
    platform: str,
    nodeid: str,
    win_runner: Path,
    cache: dict[tuple[str, str, str, str], Path | None],
) -> tuple[Path | None, str | None]:
    parts = _test_exe_parts(nodeid)
    if parts is None:
        return None, "Cargo nodeid 格式無法解析"
    crate, target, test_name = parts
    cache_key = (platform, crate, target, test_name)
    if cache_key in cache:
        path = cache[cache_key]
        return (path, None) if path is not None else (None, "找不到唯一包含必要測試的 Cargo test executable")

    platform_dir = cargo_target_dir / "debug" / "deps"
    if platform == "windows":
        platform_dir = cargo_target_dir / "x86_64-pc-windows-gnu" / "debug" / "deps"
    candidates = [
        path
        for path in sorted(platform_dir.glob(target + "-*"))
        if path.is_file()
        and (path.suffix.lower() == ".exe" if platform == "windows" else os.access(path, os.X_OK))
    ]
    matches = [
        path
        for path in candidates
        if test_name in (_listed_tests(path, platform, win_runner) or set())
    ]
    resolved = matches[0] if len(matches) == 1 else None
    cache[cache_key] = resolved
    if resolved is None:
        return None, "找不到唯一包含必要測試的 Cargo test executable"
    return resolved, None


def _artifact(path: Path | None) -> tuple[str | None, str | None]:
    if path is None or not path.is_file():
        return None, None
    return str(path.resolve()), _sha256(path)


def _record(
    case_id: str,
    round_name: str,
    platform: str,
    commit: str,
    artifact_path: Path | None,
    result: str,
    test_id: str,
    source: str,
    reason: str | None,
) -> dict[str, Any]:
    actual_path, digest = _artifact(artifact_path)
    return {
        "case_id": case_id,
        "round": round_name,
        "platform": platform,
        "commit": commit,
        "artifact_path": actual_path,
        "artifact_sha256": digest,
        "result": result,
        "test_id": test_id,
        "source": source,
        "producer": PRODUCER,
        **({"reason": reason} if reason else {}),
    }


def _pytest_artifact(args: argparse.Namespace, platform: str, round_name: str) -> Path | None:
    value = getattr(args, f"{platform}_cli") if round_name == "rust" else args.python_cli
    return Path(value).resolve() if value else None


def _cargo_records(
    case: dict[str, Any],
    nodeid: str,
    platform: str,
    commit: str,
    args: argparse.Namespace,
    executable_cache: dict[tuple[str, str, str, str], Path | None],
) -> list[dict[str, Any]]:
    case_id = str(case["case_id"])
    report_path = args.l1_dir / platform / "cargo" / "resource-report.json"
    mapped = f"cargo:{nodeid}"
    passed, reason = _l1_outcome(report_path, platform, "cargo", nodeid)
    report = _read_json(report_path)
    exe: Path | None = None
    if report is not None:
        matching = [
            str(row.get("nodeid"))
            for row in report.get("results", [])
            if isinstance(row, dict)
            and isinstance(row.get("nodeid"), str)
            and _node_matches(row["nodeid"].removeprefix("cargo:"), nodeid)
        ]
        if len(matching) == 1:
            exe, exe_reason = _cargo_executable(
                args.cargo_target_dir,
                platform,
                matching[0].removeprefix("cargo:"),
                args.windows_runner,
                executable_cache,
            )
            if exe_reason:
                passed = False
                reason = exe_reason
        else:
            passed = False
            reason = "必要 Cargo 節點缺失或重複"
    return [
        _record(
            case_id,
            "rust",
            platform,
            commit,
            exe,
            "PASS" if passed and exe is not None else "FAIL",
            mapped,
            str(report_path),
            reason if not passed or exe is None else None,
        )
    ]


def _pytest_records(
    case: dict[str, Any],
    nodeid: str,
    platform: str,
    commit: str,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    case_id = str(case["case_id"])
    report_dir = args.l1_dir / platform / "20groups"
    report_path = report_dir / "resource-report.json"
    junit_path = report_dir / "junit.xml"
    marked, marker_error = _case_marker_matches(args.source_root, nodeid, case_id)
    passed, reason = _l1_outcome(
        report_path,
        platform,
        "pytest",
        nodeid,
        junit_path=junit_path,
        source_root=args.source_root,
    )
    if not marked:
        passed = False
        reason = marker_error
    rust_artifact = _pytest_artifact(args, platform, "rust")
    if rust_artifact is None or not rust_artifact.is_file():
        passed = False
        reason = "Rust CLI artifact 不存在"
    else:
        bundle, bundle_error = _rust_manifest(rust_artifact, commit)
        if bundle_error:
            passed = False
            reason = bundle_error
        elif passed:
            attestation_error = _deployed_cli_problem(report_path, bundle or {})
            if attestation_error:
                passed = False
                reason = attestation_error
    records = [
        _record(
            case_id,
            "rust",
            platform,
            commit,
            rust_artifact,
            "PASS" if passed else "FAIL",
            nodeid,
            str(report_path),
            reason,
        )
    ]

    python_junit_path = getattr(args, f"{platform}_python_junit")
    if python_junit_path:
        python_junit = Path(python_junit_path)
        junit = _junit_rows(python_junit, args.source_root)
        outcomes = [] if junit is None else [
            outcome
            for actual, values in junit.items()
            if _node_matches(actual, nodeid)
            for outcome in values
        ]
        skips = case.get("skip_registered") or {}
        if not outcomes and ("python" in skips or platform in skips):
            return records
        python_artifact = _pytest_artifact(args, platform, "python")
        python_pass = marked and bool(outcomes) and all(outcome == "passed" for outcome in outcomes)
        python_result = "PASS" if python_pass else "FAIL"
        if marked and outcomes and all(outcome == "skipped" for outcome in outcomes):
            if "python" in skips or platform in skips:
                python_result = "SKIP"
        if python_artifact is None or not python_artifact.is_file():
            python_result = "FAIL"
        python_reason = None if python_result == "PASS" else marker_error or (
            "Python junit 缺少必要測試或結果不是全數 passed"
        )
        records.append(
            _record(
                case_id,
                "python",
                platform,
                commit,
                python_artifact,
                python_result,
                nodeid,
                str(python_junit),
                python_reason,
            )
        )
    return records


def _unit_python_records(
    case: dict[str, Any], nodeid: str, platform: str, commit: str, args: argparse.Namespace
) -> list[dict[str, Any]]:
    junit_value = getattr(args, f"{platform}_unit_junit")
    if not junit_value:
        outcomes: list[str] = []
        junit_path = None
    else:
        junit_path = Path(junit_value)
        rows = _junit_rows(junit_path, args.source_root)
        outcomes = [] if rows is None else [
            outcome for actual, values in rows.items() if _node_matches(actual, nodeid) for outcome in values
        ]
    skips = case.get("skip_registered") or {}
    if not outcomes and ("python" in skips or platform in skips):
        return []
    marked, marker_error = _case_marker_matches(args.source_root, nodeid, str(case["case_id"]))
    python_artifact = _pytest_artifact(args, platform, "python")
    passed = (
        marked
        and bool(outcomes)
        and all(outcome == "passed" for outcome in outcomes)
        and python_artifact is not None
        and python_artifact.is_file()
    )
    reason = None if passed else marker_error or "完整 pytest junit 缺少必要測試、結果未通過或 Python CLI artifact 不存在"
    result = "PASS" if passed else "FAIL"
    if marked and outcomes and all(outcome == "skipped" for outcome in outcomes) and (
        "python" in skips or platform in skips
    ):
        result = "SKIP"
        reason = "Python round 已於 p4_case_map.skip_registered 登記跳過"
    return [
        _record(
            str(case["case_id"]),
            "python",
            platform,
            commit,
            python_artifact,
            result,
            nodeid,
            str(junit_path),
            reason,
        )
    ]


def _is_owner_case(case: dict[str, Any]) -> bool:
    return (
        str(case.get("case_id")) in JUDGE_SPIKE2_CASES
        or case.get("evidence_status") == "BLOCKED-OWNER"
        or case.get("layer") in BLOCKED_LAYERS
    )


def _merge_records(path: Path, case_id: str, generated: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    existing: list[dict[str, Any]] = []
    preserved_fields: dict[str, Any] = {}
    if path.exists():
        data = _read_json(path)
        if data is None or not isinstance(data.get("records"), list):
            raise ValueError(f"既有證據檔格式錯誤，拒絕覆寫：{path}")
        if data.get("case_id") != case_id or any(not isinstance(record, dict) for record in data["records"]):
            raise ValueError(f"既有證據檔案例 ID 或 records 項目格式錯誤，拒絕覆寫：{path}")
        existing = list(data["records"])
        preserved_fields = {key: value for key, value in data.items() if key not in {"case_id", "records"}}
    merged = list(existing)
    appended = 0
    for record in generated:
        if record not in merged:
            merged.append(record)
            appended += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**preserved_fields, "case_id": case_id, "records": merged}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return merged, appended


def register(args: argparse.Namespace) -> dict[str, Any]:
    commit = _head(args.repo_root)
    cases = _load_case_map(args.case_map)
    generated_cases = 0
    generated_records = 0
    failed_records = 0
    skipped_cases: list[str] = []
    executable_cache: dict[tuple[str, str, str, str], Path | None] = {}

    for case in cases:
        case_id = str(case.get("case_id", ""))
        if not case_id or _is_owner_case(case):
            if case_id:
                skipped_cases.append(case_id)
            continue
        records: list[dict[str, Any]] = []
        for platform in case.get("platforms", []):
            for nodeid in case.get("tests", []):
                node = str(nodeid)
                if node.startswith("tests/contract_blackbox/") and "::" in node:
                    records.extend(_pytest_records(case, node, platform, commit, args))
                elif node.startswith("sw-") and "::" in node:
                    records.extend(_cargo_records(case, node, platform, commit, args, executable_cache))
                elif node.startswith("tests/") and "::" in node:
                    records.extend(_unit_python_records(case, node, platform, commit, args))

        if records:
            merged, appended = _merge_records(args.out_dir / f"{case_id}.json", case_id, records)
            generated_cases += 1
            generated_records += appended
            failed_records += sum(
                record.get("commit") == commit and record.get("result") == "FAIL"
                for record in merged
            )

    return {
        "commit": commit,
        "case_files_written": generated_cases,
        "records_written": generated_records,
        "fail_records": failed_records,
        "owner_or_judge_cases_skipped": sorted(skipped_cases),
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--case-map", type=Path, default=CASE_MAP)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--l1-dir", type=Path, default=EVIDENCE_DIR / "l1")
    parser.add_argument("--out-dir", type=Path, default=EVIDENCE_DIR)
    parser.add_argument("--cargo-target-dir", type=Path, default=ROOT / "target")
    parser.add_argument("--windows-runner", type=Path, default=ROOT / "scripts" / "rust" / "win-runner.sh")
    parser.add_argument("--linux-cli", help="Linux Rust CLI bundle 執行檔")
    parser.add_argument("--windows-cli", help="Windows Rust CLI bundle 執行檔")
    parser.add_argument("--python-cli", default=str(ROOT / "serialwrap.py"), help="Python 基線 CLI 執行檔")
    parser.add_argument("--linux-python-junit", help="Linux Python A 輪 junit.xml")
    parser.add_argument("--windows-python-junit", help="Windows Python A 輪 junit.xml")
    parser.add_argument("--linux-unit-junit", help="Linux 完整 pytest junit.xml")
    parser.add_argument("--windows-unit-junit", help="Windows 完整 pytest junit.xml")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        args.repo_root = args.repo_root.resolve()
        args.case_map = args.case_map.resolve()
        args.source_root = args.source_root.resolve()
        args.l1_dir = args.l1_dir.resolve()
        args.out_dir = args.out_dir.resolve()
        args.cargo_target_dir = args.cargo_target_dir.resolve()
        args.windows_runner = args.windows_runner.resolve()
        for name in ("linux_cli", "windows_cli", "python_cli", "linux_python_junit", "windows_python_junit", "linux_unit_junit", "windows_unit_junit"):
            value = getattr(args, name)
            if value:
                setattr(args, name, str(Path(value).resolve()))
        summary = register(args)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"register_p4_evidence: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 1 if summary["fail_records"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
