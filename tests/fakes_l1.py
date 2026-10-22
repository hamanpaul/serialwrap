"""P9-T14B 本 Task 自有替身：L1 凍結集合、`l1-results.v1` 報告與一致的 junit XML（R5-F3）。

- `lock(sets)`：仿 P2 `tests/acceptance/l1_required.lock.json` 結構，`sets.<set>.<platform>.{selector,nodeids}`。
- `row()` / `report()`：仿 P2 `tests/acceptance/l1_results.schema.json`（schema_version 2、format `l1-results.v1`）的報告。
- `full_pass()`：每個凍結 nodeid 恰一列 `passed`、`exit_code` 0、`finished` true。
- `junit_xml()` / `write_report_dir()`：與報告逐列一致的 junit XML（`junit.xml`）與 `resource-report.json`。
- `l1_judge_module()`：以路徑載入 P2 `tests/acceptance/l1_judge.py`（唯一判定實作，本替身不重寫判定）。

本檔不 import `sw_core`、不 import `tools.stage3`。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping, Sequence
from xml.sax.saxutils import quoteattr

REPO_ROOT = Path(__file__).resolve().parents[1]
L1_JUDGE_PATH = REPO_ROOT / "tests" / "acceptance" / "l1_judge.py"

RUNNER_ENTRY = "/x/pytest/__main__.py"
DEFAULT_ARGV: tuple[str, ...] = ("-q", "tests")
MARKEXPR: Mapping[str, str] = {
    "linux": "core_portable or posix_only",
    "windows": "core_portable or windows_native",
}


def l1_judge_module() -> ModuleType:
    """載入 P2 l1_judge.py（唯一判定實作）；不得在本替身內重寫判定邏輯。"""
    spec = importlib.util.spec_from_file_location("p2_l1_judge_for_t14b", L1_JUDGE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {L1_JUDGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lock(sets: Mapping[str, Mapping[str, Sequence[str]]], *, argv: Sequence[str] = DEFAULT_ARGV) -> dict[str, Any]:
    """建立最小 L1 凍結集合：`{"schema_version":1,"sets":{set:{platform:{"selector":…,"nodeids":[…]}}}}`。

    selector.argv 不含 runner 入口（與 P2 lock 相同），故報告 argv 須為 `[RUNNER_ENTRY, *argv]`。
    """
    out: dict[str, Any] = {}
    for set_id, per_platform in sets.items():
        out[set_id] = {}
        for platform, nodeids in per_platform.items():
            out[set_id][platform] = {
                "selector": {"argv": list(argv), "markexpr": MARKEXPR[platform], "paths": ["tests"]},
                "nodeids": list(nodeids),
            }
    return {"schema_version": 1, "sets": out}


def row(nodeid: str, outcome: str = "passed", skip_kind: str | None = None) -> dict[str, Any]:
    """一列 results；phases 與 outcome 一致（passed 必為三段 passed，見 P2 `_consistency_errors`）。"""
    phases = {"setup": "passed", "call": "passed", "teardown": "passed"}
    if outcome == "failed":
        phases["call"] = "failed"
    elif outcome == "error":
        phases["setup"] = "failed"
        phases["call"] = None
        phases["teardown"] = None
    elif outcome in ("skipped", "xfailed"):
        phases["call"] = "skipped"
        if skip_kind is None:
            skip_kind = "other"
    return {"nodeid": nodeid, "outcome": outcome, "skip_kind": skip_kind, "phases": phases}


def _unique(items: Sequence[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def report(
    rows: Sequence[Mapping[str, Any]],
    *,
    platform: str = "linux",
    exit_code: int = 0,
    finished: bool = True,
    collected: Sequence[str] | None = None,
    deselected: Sequence[str] = (),
    argv: Sequence[str] | None = None,
    core_portable_platform_skips: Sequence[str] = (),
    deployed_content: Any = None,
) -> dict[str, Any]:
    """`l1-results.v1` 報告。collected 預設＝結果列 nodeid ∪ deselected（與 P2 判定的「結果與收集一致」相符）。"""
    results = [dict(r) for r in rows]
    result_ids = [r["nodeid"] for r in results]
    if collected is None:
        collected = _unique(result_ids) + [d for d in deselected if d not in result_ids]
    return {
        "schema_version": 2,
        "format": "l1-results.v1",
        "runner": "pytest",
        "platform": platform,
        "argv": list(argv) if argv is not None else [RUNNER_ENTRY, *DEFAULT_ARGV],
        "markexpr": MARKEXPR[platform],
        "counts": {},
        "resources": {},
        "uart_kind": "native",
        "not_collected_platform": [],
        "core_portable_platform_skips": list(core_portable_platform_skips),
        "collected": list(collected),
        "deselected": list(deselected),
        "results": results,
        "exit_code": exit_code,
        "finished": finished,
        "deployed_content": deployed_content,
    }


def full_pass(lock_doc: Mapping[str, Any], set_id: str, platform: str, *, deployed_content: Any = None) -> dict[str, Any]:
    """每個凍結 nodeid 恰一列 passed、exit_code 0、finished true。"""
    nodeids = lock_doc["sets"][set_id][platform]["nodeids"]
    return report([row(n) for n in nodeids], platform=platform, deployed_content=deployed_content)


def junit_xml(
    rep: Mapping[str, Any],
    *,
    tests: int | None = None,
    failures: int | None = None,
    errors: int | None = None,
    skipped: int | None = None,
) -> str:
    """與報告逐列一致的 junit XML；可覆寫計數以模擬「兩檔非同一次執行」。"""
    rows = list(rep["results"])
    counts = {
        "tests": len(rows),
        "failures": sum(1 for r in rows if r["outcome"] == "failed"),
        "errors": sum(1 for r in rows if r["outcome"] == "error"),
        "skipped": sum(1 for r in rows if r["outcome"] in ("skipped", "xfailed")),
    }
    for key, value in (("tests", tests), ("failures", failures), ("errors", errors), ("skipped", skipped)):
        if value is not None:
            counts[key] = value
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<testsuites tests="{counts["tests"]}" failures="{counts["failures"]}" '
        f'errors="{counts["errors"]}" skipped="{counts["skipped"]}">',
        f'<testsuite name="pytest" timestamp="2026-11-01T00:00:00+00:00" '
        f'tests="{counts["tests"]}" failures="{counts["failures"]}" '
        f'errors="{counts["errors"]}" skipped="{counts["skipped"]}">',
    ]
    for r in rows:
        classname, _, name = str(r["nodeid"]).partition("::")
        lines.append(f"<testcase classname={quoteattr(classname)} name={quoteattr(name or classname)}>")
        if r["outcome"] == "failed":
            lines.append('<failure message="fake failure"/>')
        elif r["outcome"] == "error":
            lines.append('<error message="fake error"/>')
        elif r["outcome"] in ("skipped", "xfailed"):
            lines.append('<skipped message="fake skip"/>')
        lines.append("</testcase>")
    lines.extend(("</testsuite>", "</testsuites>"))
    return "\n".join(lines) + "\n"


def write_report_dir(
    dir_path: Path,
    rep: Mapping[str, Any],
    *,
    tests: int | None = None,
    failures: int | None = None,
    errors: int | None = None,
    skipped: int | None = None,
) -> Path:
    """寫出 `resource-report.json`（SW_RESOURCE_REPORT v2 形狀）與同目錄 `junit.xml`；回傳 junit 路徑。"""
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "resource-report.json").write_text(
        json.dumps(rep, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8"
    )
    junit = dir_path / "junit.xml"
    junit.write_text(junit_xml(rep, tests=tests, failures=failures, errors=errors, skipped=skipped), encoding="utf-8")
    return junit
