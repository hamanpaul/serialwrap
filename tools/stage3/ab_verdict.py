"""A/B 判定（P9-T7）：把 L1.5／L2／L3 的 A、B 兩側狀態與差分合成為單一判定。

本模組只讀取既有工具的輸出，不 import `realhw`、`sw_core` 或任何 Rust crate：
- L1.5：`ab_diff.diff_l15`（P9-T7B）的輸出 dict（`integrity`、`safety_violations`、`differences`、`a_cases`、`b_cases`）。
- L2：TestPilot `report.json`（`{"meta":…,"results":[…]}`）與 P2 `cases.yaml` 的 `expected`、`sentinel`。
- L3：P2 `release-gate.json`（`release-gate.v1`）。門檻與發版判定的唯一來源是 P2 gate 的 `release_gate_result`，
  本模組不重算門檻，也不改寫 `engine_result`（OD-09：引擎原始 FAIL 判準保留）。
- 變更登記：P0 `contracts/behavior_changes.yaml` 的 `scope`、`ab_participation`、`case_ids`。

`combine` 的判定表由上而下第一個符合者勝出；證據不足、缺漏或未知一律不轉為 PASS（§11.3、§11.4）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, time, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from tools.stage3.jsonio import parse_iso
from tools.stage3.reason_codes import HarnessError

L2_VERDICTS: frozenset[str] = frozenset({"PASS", "FAIL", "SKIP", "XFAIL", "XPASS", "ERROR"})

PASS = "PASS"
FAIL = "FAIL"
INFRA_ERROR = "INFRA_ERROR"
BASELINE_INVALID = "BASELINE_INVALID"
INCOMPLETE = "INCOMPLETE"

_LAYERS: frozenset[str] = frozenset({"l1.5", "l2", "l3"})
_SUITE_STATUSES: frozenset[str] = frozenset({PASS, FAIL, INFRA_ERROR})
_AB_PARTICIPATION: frozenset[str] = frozenset({"diff", "absolute_only"})
_REQUIRED_SKIP_VERDICTS: frozenset[str] = frozenset({"SKIP", "ERROR"})
_L2_COMPARABLE_VERDICTS: frozenset[str] = L2_VERDICTS - _REQUIRED_SKIP_VERDICTS

_COMPARATOR = "HARNESS_COMPARATOR_ERROR"
_SUITE = "HARNESS_SUITE_INFRA_ERROR"
_BASELINE = "HARNESS_BASELINE_INVALID"
_OD09_NOT_FROZEN = "HARNESS_OD09_NOT_FROZEN"

_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
_XFAIL = "XFAIL"
_XPASS = "XPASS"
_A_BASELINE_RESULTS: frozenset[str] = frozenset({PASS, FAIL, _INSUFFICIENT})
_GATE_SCHEMA = "release-gate.v1"
_GATE_RESULTS: frozenset[str] = frozenset({
    PASS, FAIL, _INSUFFICIENT, "BLOCKED", "NOT_APPLICABLE_BASELINE",
})
_GATE_REQUIRED_FIELDS: tuple[str, ...] = (
    "schema_version",
    "created_at",
    "platform",
    "mode",
    "engine_result",
    "release_gate_result",
    "baseline_result",
    "acceptance",
    "run",
    "checks",
    "counts",
    "stuck",
    "walscan",
    "evidence_hashes",
    "excl_evidence_hashes",
)


# ---------------------------------------------------------------------------
# 變更登記（behavior_changes.yaml）
# ---------------------------------------------------------------------------


def _load_changes(path: Path) -> list[dict[str, Any]]:
    """讀取 `behavior_changes.yaml` 的 `changes` 清單；無法讀取或結構不符一律為比較器錯誤。"""
    try:
        document = yaml.safe_load(Path(path).read_bytes().decode("utf-8"))
    except (OSError, TypeError, ValueError, UnicodeError, yaml.YAMLError) as exc:
        raise HarnessError(_COMPARATOR, f"無法讀取行為變更登記 {path}: {exc}") from exc
    changes = document.get("changes") if isinstance(document, dict) else None
    if not isinstance(changes, list) or not all(isinstance(item, dict) for item in changes):
        raise HarnessError(_COMPARATOR, f"行為變更登記結構不符：{path}")
    return changes


def load_registered_changes(path: Path) -> dict[str, str]:
    """回傳 change_id → A/B 參與方式（`"diff"` 或 `"absolute_only"`）。

    以 P0 的 `ab_participation` 為準，並核對與 `scope` 一致：`scope == "both"` 必為 `diff`，
    其餘（`rust_only`、`python_when_manifest_present`）必為 `absolute_only`。不一致或重複的 id 為比較器錯誤。
    """
    registered: dict[str, str] = {}
    for entry in _load_changes(path):
        change_id = entry.get("id")
        scope = entry.get("scope")
        participation = entry.get("ab_participation")
        if not isinstance(change_id, str) or change_id in registered:
            raise HarnessError(_COMPARATOR, f"行為變更 id 缺漏或重複：{change_id!r}")
        if scope not in {"both", "rust_only", "python_when_manifest_present"}:
            raise HarnessError(_COMPARATOR, f"{change_id} 的 scope 無效：{scope!r}")
        if participation not in _AB_PARTICIPATION:
            raise HarnessError(_COMPARATOR, f"{change_id} 的 ab_participation 無效：{participation!r}")
        if (scope == "both") != (participation == "diff"):
            raise HarnessError(_COMPARATOR, f"{change_id} 的 scope 與 ab_participation 不一致")
        registered[change_id] = participation
    return registered


def case_changes(path: Path, *, scope: str | None = None) -> dict[str, str]:
    """回傳 case_id → change_id（依 `case_ids` 反查）。

    `scope` 為 None 時涵蓋所有條目，包括 `rust_only`；同一 case_id 掛在不同 change 下為比較器錯誤。
    """
    if scope is not None and scope not in {"both", "rust_only", "python_when_manifest_present"}:
        raise HarnessError(_COMPARATOR, f"scope 參數無效：{scope!r}")
    mapping: dict[str, str] = {}
    for entry in _load_changes(path):
        if scope is not None and entry.get("scope") != scope:
            continue
        change_id = entry.get("id")
        case_ids = entry.get("case_ids")
        if case_ids is None:
            case_ids = []
        if not isinstance(change_id, str) or not isinstance(case_ids, list):
            raise HarnessError(_COMPARATOR, f"行為變更 case_ids 結構不符：{change_id!r}")
        for case_id in case_ids:
            if not isinstance(case_id, str):
                raise HarnessError(_COMPARATOR, f"{change_id} 的 case_id 非字串")
            if mapping.get(case_id, change_id) != change_id:
                raise HarnessError(_COMPARATOR, f"case_id 同時掛在多個變更下：{case_id}")
            mapping[case_id] = change_id
    return mapping


# ---------------------------------------------------------------------------
# 判定表
# ---------------------------------------------------------------------------


def combine(
    layer: str,
    *,
    a_status: str,
    b_status: str,
    diff: Mapping[str, Any] | None,
    registered: Mapping[str, str],
) -> tuple[str, str | None, list[str]]:
    """依判定表合成單一判定，回傳 `(verdict, reason_code, notes)`。

    `diff` 對 L1.5 為 `ab_diff.diff_l15` 輸出、L2 為 `compare_l2_reports` 輸出、L3 為 `compare_l3` 輸出。
    """
    if layer not in _LAYERS:
        raise HarnessError(_COMPARATOR, f"未知的 A/B 層級：{layer!r}")
    if not isinstance(registered, Mapping):
        raise HarnessError(_COMPARATOR, "registered 必須是映射")

    safety, malformed_safety = _checked_text_rows(diff, "safety_violations")
    raw_products, malformed_products = _checked_record_rows(diff, "product_failures", expected_side="b")
    products = collect_product_failures(
        layer,
        diff=dict(diff) if isinstance(diff, Mapping) else {},
        registered=registered,
    )
    _baselines, malformed_baselines = _checked_record_rows(diff, "baseline_failures", expected_side="a")
    shape_errors = []
    if malformed_safety:
        shape_errors.append("safety_violations 結構不符")
    if malformed_products:
        shape_errors.append("product_failures 結構不符")
    if malformed_baselines:
        shape_errors.append("baseline_failures 結構不符")
    shape_errors.extend(_other_shape_errors(diff))
    notes = _unique([*_diagnostic_notes(diff, a_status, b_status), *shape_errors])
    if a_status == INFRA_ERROR or b_status == INFRA_ERROR:
        notes = _unique([*notes, "suite reported INFRA_ERROR"])
    difference_notes: list[str] = []
    difference_errors: list[str] = []
    if layer == "l1.5" and isinstance(diff, Mapping):
        _difference_failures, difference_notes, difference_errors = _l15_difference_findings(
            diff, registered, raw_products,
        )
        shape_errors.extend(difference_errors)
        notes = _unique([*difference_notes, *notes, *difference_errors])
        notes = _unique(notes)
    if safety:
        return FAIL, None, _unique([*(f"safety violation: {item}" for item in safety), *notes])
    if products:
        return FAIL, None, notes

    if a_status == INFRA_ERROR or b_status == INFRA_ERROR:
        return INFRA_ERROR, _SUITE, _unique([*notes, "suite reported INFRA_ERROR"])

    if layer == "l2":
        # L2 只依逐案比較；run 層級狀態／rc 不代表產品失敗。
        if shape_errors:
            return INFRA_ERROR, _COMPARATOR, notes
        return _combine_cases(diff, notes)
    if layer == "l3":
        if diff is None:
            return INFRA_ERROR, _COMPARATOR, _unique(["缺少 L3 比較結果", *notes])
        kind = diff.get("kind")
        if kind == "cases":
            # 1.0.0 非長跑 L3 使用 required cases，且不讀取 release-gate.json。
            if shape_errors:
                return INFRA_ERROR, _COMPARATOR, notes
            return _combine_cases(diff, notes)
        if kind != "longrun":
            return INFRA_ERROR, _COMPARATOR, _unique([f"未知的 L3 比較種類：{kind!r}", *notes])

    if shape_errors:
        return INFRA_ERROR, _COMPARATOR, notes
    if a_status not in _SUITE_STATUSES or b_status not in _SUITE_STATUSES:
        return INFRA_ERROR, _SUITE, _unique([*notes, f"unknown suite status: A={a_status!r} B={b_status!r}"])
    if layer == "l1.5" and isinstance(diff, Mapping):
        differences = diff.get("differences")
        if isinstance(differences, list) and any(
            isinstance(row, Mapping)
            and row.get("verified") is False
            for row in differences
        ):
            return INFRA_ERROR, _COMPARATOR, _unique([*notes, "L1.5 含未核實差異"])
    if layer == "l3" and isinstance(diff, Mapping) and diff.get("kind") == "longrun" and not shape_errors:
        integrity_problems = diff.get("integrity_problems")
        has_gate_problem = isinstance(integrity_problems, Mapping) and any(
            bool(integrity_problems.get(side)) for side in ("a", "b")
        )
        if not has_gate_problem:
            release = diff.get("b_release_gate_result")
            if release == "BLOCKED":
                return INFRA_ERROR, _OD09_NOT_FROZEN, _unique([*notes, "release gate BLOCKED"])
            if release == _INSUFFICIENT or diff.get("b_baseline_compatible") is False:
                return INFRA_ERROR, _SUITE, _unique([*notes, "release gate 證據不足或 baseline 不相容"])
    integrity = diff.get("integrity") if isinstance(diff, Mapping) else None
    if not isinstance(integrity, Mapping) or integrity.get("a") is not True or integrity.get("b") is not True:
        return INFRA_ERROR, _COMPARATOR, _unique([*notes, "證據不足：比較器完整性未通過"])

    if layer == "l1.5":
        baseline_failures = _rows(diff, "baseline_failures")
        if a_status == FAIL and not any(_failure_side(row) == "a" for row in baseline_failures):
            return INFRA_ERROR, _SUITE, _unique([*notes, "suite／原始報告不一致：A status=FAIL，缺少逐案來源"])
        if b_status == FAIL and not any(_failure_side(row) == "b" for row in products):
            return INFRA_ERROR, _SUITE, _unique([*notes, "suite／原始報告不一致：B status=FAIL，缺少逐案來源"])
        return _combine_l15(a_status, b_status, diff, registered, notes)
    return _combine_l3(a_status, b_status, diff, notes)


def _rows(diff: Mapping[str, Any] | None, key: str) -> list[Any]:
    if not isinstance(diff, Mapping):
        return []
    value = diff.get(key)
    return value if isinstance(value, list) else []


def _failure_side(row: Any) -> str | None:
    return row.get("side") if isinstance(row, Mapping) and isinstance(row.get("side"), str) else None


def _l15_difference_findings(
    diff: Mapping[str, Any],
    registered: Mapping[str, str],
    existing_products: list[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """從可信的 T7B 差異建產品 FAIL 記錄；未核實差異僅保留診斷。"""
    differences = diff.get("differences")
    if not isinstance(differences, list):
        return [], [], ["L1.5 differences 結構不符"]
    b_cases = diff.get("b_cases") if isinstance(diff.get("b_cases"), Mapping) else {}
    a_cases = diff.get("a_cases") if isinstance(diff.get("a_cases"), Mapping) else {}
    failures: list[dict[str, Any]] = []
    notes: list[str] = []
    errors: list[str] = []
    for index, item in enumerate(differences):
        if not isinstance(item, Mapping):
            errors.append(f"L1.5 difference[{index}] 列結構不符")
            continue
        case = item.get("case")
        path = item.get("path")
        kind = item.get("kind")
        if (
            not isinstance(case, str) or not case
            or not isinstance(path, str) or not path
            or not isinstance(kind, str) or not kind
        ):
            errors.append(f"L1.5 difference[{index}] 缺少 case、kind 或 path")
            continue
        verified = item.get("verified")
        if not isinstance(verified, bool):
            errors.append(f"L1.5 difference[{index}] verified 缺漏或非布林值")
            continue
        source_refs = item.get("source_refs")
        if not isinstance(source_refs, Mapping):
            errors.append(f"L1.5 difference[{index}] source_refs 結構不符")
            continue
        source_a = source_refs.get("a")
        source_b = source_refs.get("b")
        if source_a is not None and (not isinstance(source_a, str) or not source_a):
            errors.append(f"L1.5 difference[{index}] source_refs.a 結構不符")
            continue
        if source_b is not None and (not isinstance(source_b, str) or not source_b):
            errors.append(f"L1.5 difference[{index}] source_refs.b 結構不符")
            continue
        side, source_ref = ("b", source_b) if source_b is not None else ("a", source_a)
        if not source_ref:
            errors.append(f"L1.5 difference[{index}] 缺少可核對來源")
            continue
        if not verified:
            notes.append(f"unverified difference: {case} {path} (a={source_a or 'null'} b={source_b or 'null'})")
            continue
        if kind == "verdict" and a_cases.get(case) == FAIL and b_cases.get(case) == PASS:
            # 逐案 PASS/FAIL 已分別歸入 product_failures／baseline_failures；
            # A FAIL、B PASS 是基線無效，不是未登錄的 CLI 產品差異。
            continue

        change = item.get("registered_change")
        participation = registered.get(change) if isinstance(change, str) else None
        if participation == "absolute_only":
            if b_cases.get(case) == PASS:
                continue
            reason = f"rust_only case failed absolute: {case}"
        elif participation == "diff":
            reason = f"both-scope change differs: {change} {case}"
        else:
            reason = f"unregistered difference: {case} {path}"

        row = {
            "side": side,
            "case": case,
            "source_ref": source_ref,
            "reason": reason,
            "source_refs": {"a": source_a, "b": source_b},
            "kind": kind,
            "path": path,
        }
        if not any(existing.get("side") == side and existing.get("case") == case for existing in existing_products):
            failures.append(row)
        notes.append(reason)
        notes.append(_record_note("difference source", row))
    return failures, _unique(notes), errors


def _checked_text_rows(diff: Mapping[str, Any] | None, key: str) -> tuple[list[str], bool]:
    if not isinstance(diff, Mapping) or key not in diff:
        return [], False
    value = diff.get(key)
    if not isinstance(value, list):
        return [], True
    rows = [item for item in value if isinstance(item, str) and item]
    return rows, len(rows) != len(value)


def _checked_record_rows(
    diff: Mapping[str, Any] | None,
    key: str,
    *,
    expected_side: str,
) -> tuple[list[Mapping[str, Any]], bool]:
    if not isinstance(diff, Mapping) or key not in diff:
        return [], False
    value = diff.get(key)
    if not isinstance(value, list):
        return [], True
    rows: list[Mapping[str, Any]] = []
    malformed = False
    for item in value:
        if (
            isinstance(item, Mapping)
            and item.get("side") == expected_side
            and isinstance(item.get("case"), str)
            and isinstance(item.get("source_ref"), str)
            and bool(item.get("source_ref"))
            and isinstance(item.get("reason"), str)
        ):
            rows.append(item)
        else:
            malformed = True
    return rows, malformed


def collect_product_failures(
    layer: str,
    *,
    diff: dict[str, Any],
    registered: Mapping[str, str],
) -> list[dict[str, Any]]:
    """回傳報告既有與 T7 判定成立的產品 FAIL 記錄。"""
    if layer not in _LAYERS:
        raise HarnessError(_COMPARATOR, f"未知的 A/B 層級：{layer!r}")
    if not isinstance(diff, dict):
        raise HarnessError(_COMPARATOR, "diff 必須是字典")
    if not isinstance(registered, Mapping):
        raise HarnessError(_COMPARATOR, "registered 必須是映射")
    existing, _malformed = _checked_record_rows(diff, "product_failures", expected_side="b")
    products: list[dict[str, Any]] = [dict(row) for row in existing]
    if layer == "l1.5":
        difference_failures, _notes, _errors = _l15_difference_findings(diff, registered, existing)
        products.extend(difference_failures)
    return products


def _other_shape_errors(diff: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(diff, Mapping):
        return []
    errors: list[str] = []
    if "integrity_problems" in diff and not isinstance(diff.get("integrity_problems"), Mapping):
        errors.append("integrity_problems 結構不符")
    if "unverified_failures" in diff:
        rows = diff.get("unverified_failures")
        if not isinstance(rows, list):
            errors.append("unverified_failures 結構不符")
        elif any(not isinstance(row, Mapping) for row in rows):
            errors.append("unverified_failures 列結構不符")
    for key in ("a_cases", "b_cases"):
        if key in diff and not isinstance(diff.get(key), Mapping):
            errors.append(f"{key} 結構不符")
    if "safety_failure_sources" in diff:
        rows = diff.get("safety_failure_sources")
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            errors.append("safety_failure_sources 結構不符")
    for key in ("missing", "duplicates", "unknown_verdicts", "required_skips"):
        if key not in diff:
            continue
        values = diff.get(key)
        if not isinstance(values, Mapping) or any(not isinstance(values.get(side), list) for side in ("a", "b")):
            errors.append(f"{key} 結構不符")
    return errors


def _unique(notes: list[str]) -> list[str]:
    result: list[str] = []
    for note in notes:
        if note and note not in result:
            result.append(note)
    return result


def _record_note(label: str, row: Any) -> str:
    if not isinstance(row, Mapping):
        return f"{label}: {row}"
    parts = [label]
    side = row.get("side")
    case = row.get("case")
    source_ref = row.get("source_ref")
    reason = row.get("reason")
    if side:
        parts.append(str(side).upper())
    if case:
        parts.append(f"case={case}")
    if source_ref:
        parts.append(f"source={source_ref}")
    if reason:
        parts.append(f"reason={reason}")
    problems = row.get("problems")
    if isinstance(problems, list):
        parts.extend(str(item) for item in problems)
    return ": ".join(parts[:1]) + (" " + " ".join(parts[1:]) if len(parts) > 1 else "")


def _diagnostic_notes(
    diff: Mapping[str, Any] | None,
    a_status: str,
    b_status: str,
    *,
    include_status: bool = True,
) -> list[str]:
    """彙整來源與完整性診斷，讓優先 FAIL 不會遮掉其他問題。"""
    if not isinstance(diff, Mapping):
        return []
    notes: list[str] = []
    safety_rows, malformed_safety = _checked_text_rows(diff, "safety_violations")
    for row in safety_rows:
        notes.append(f"safety violation: {row}")
    if malformed_safety:
        notes.append("safety_violations 結構不符")
    product_rows, malformed_products = _checked_record_rows(diff, "product_failures", expected_side="b")
    for row in product_rows:
        notes.append(_record_note("product failure", row))
    if malformed_products:
        notes.append("product_failures 結構不符")
    baseline_rows, malformed_baselines = _checked_record_rows(diff, "baseline_failures", expected_side="a")
    for row in baseline_rows:
        notes.append(_record_note("baseline failure", row))
    if malformed_baselines:
        notes.append("baseline_failures 結構不符")
    problems = diff.get("integrity_problems")
    if isinstance(problems, Mapping):
        for side in ("a", "b"):
            side_problems = problems.get(side)
            if isinstance(side_problems, list):
                notes.extend(f"{side.upper()} integrity: {item}" for item in side_problems)
            elif side_problems:
                notes.append(f"{side.upper()} integrity: {side_problems}")
    safety_sources = diff.get("safety_failure_sources")
    if isinstance(safety_sources, list):
        for row in safety_sources:
            notes.append(_record_note("safety source", row))
    elif "safety_failure_sources" in diff:
        notes.append("safety_failure_sources 結構不符")
    for field, label in (
        ("missing", "missing required case"),
        ("duplicates", "duplicate case"),
        ("unknown_verdicts", "unknown verdict"),
        ("required_skips", "required case skipped or errored"),
    ):
        value = diff.get(field)
        if isinstance(value, Mapping):
            for side in ("a", "b"):
                items = value.get(side)
                if isinstance(items, list):
                    notes.extend(f"{side.upper()} {label}: {item}" for item in items)
    unverified_rows = diff.get("unverified_failures")
    if isinstance(unverified_rows, list):
        for row in unverified_rows:
            notes.append(_record_note("unverified failure", row))
    elif "unverified_failures" in diff:
        notes.append("unverified_failures 結構不符")
    integrity = diff.get("integrity")
    if isinstance(integrity, Mapping):
        for side in ("a", "b"):
            if integrity.get(side) is False:
                notes.append(f"{side.upper()} integrity=false")
    if include_status:
        if a_status in {FAIL, INFRA_ERROR}:
            notes.append(f"A suite status: {a_status}")
        if b_status in {FAIL, INFRA_ERROR}:
            notes.append(f"B suite status: {b_status}")
    return _unique(notes)


def _combine_cases(
    diff: Mapping[str, Any] | None,
    notes: list[str],
) -> tuple[str, str | None, list[str]]:
    if diff is None:
        return INFRA_ERROR, _COMPARATOR, _unique(["缺少逐案比較結果", *notes])
    verdict = diff.get("verdict")
    if verdict == INCOMPLETE:
        return INFRA_ERROR, _SUITE, _unique([*notes, "逐案報告不完整（缺案、重複、未知 verdict 或必要案例 SKIP／ERROR）"])
    integrity = diff.get("integrity")
    if not isinstance(integrity, Mapping) or integrity.get("a") is not True or integrity.get("b") is not True:
        return INFRA_ERROR, _COMPARATOR, _unique([*notes, "證據不足：逐案比較完整性未通過"])
    if verdict == PASS:
        return PASS, None, notes
    if verdict == FAIL:
        return FAIL, None, _unique([*notes, "逐案比較確認 FAIL"])
    return INFRA_ERROR, _COMPARATOR, _unique([*notes, f"未知的逐案判定：{verdict!r}"])


def _combine_l15(
    a_status: str,
    b_status: str,
    diff: Mapping[str, Any] | None,
    registered: Mapping[str, str],
    diagnostics: list[str],
) -> tuple[str, str | None, list[str]]:
    if a_status == FAIL and b_status == FAIL:
        return FAIL, None, diagnostics
    if a_status == FAIL and b_status == PASS:
        return BASELINE_INVALID, _BASELINE, diagnostics
    if b_status == FAIL:
        return FAIL, None, diagnostics
    if diff is None:
        return INFRA_ERROR, _COMPARATOR, _unique(["缺少 L1.5 差分輸出", *diagnostics])
    return PASS, None, diagnostics


def _combine_l3(
    a_status: str,
    b_status: str,
    diff: Mapping[str, Any] | None,
    diagnostics: list[str],
) -> tuple[str, str | None, list[str]]:
    if diff is None:
        return INFRA_ERROR, _COMPARATOR, _unique(["缺少 L3 比較結果", *diagnostics])
    if a_status != PASS or diff.get("a_engine_verdict") != PASS or diff.get("a_baseline_result") != PASS:
        return BASELINE_INVALID, _BASELINE, diagnostics
    b_engine = diff.get("b_engine_verdict")
    if b_engine == FAIL:
        # 引擎原始 FAIL 判準保留，即使 suite 或 gate 顯示 PASS。
        return FAIL, None, _unique([*diagnostics, "B engine verdict: FAIL"])
    if b_engine != PASS:
        return INFRA_ERROR, _SUITE, _unique([*diagnostics, f"B engine verdict 未知或不足：{b_engine!r}"])
    if b_status == FAIL:
        return INFRA_ERROR, _SUITE, _unique([*diagnostics, "suite/engine inconsistent"])
    release = diff.get("b_release_gate_result")
    if release == "BLOCKED":
        return INFRA_ERROR, _OD09_NOT_FROZEN, _unique([*diagnostics, "release gate BLOCKED"])
    if release == _INSUFFICIENT or diff.get("b_baseline_compatible") is not True:
        return INFRA_ERROR, _SUITE, _unique([*diagnostics, "release gate 證據不足或 baseline 不相容"])
    if release == FAIL:
        return FAIL, None, _unique([*diagnostics, "B release gate result: FAIL"])
    if release != PASS:
        return INFRA_ERROR, _SUITE, _unique([*diagnostics, f"release gate 結果未知：{release!r}"])
    return PASS, None, diagnostics


# ---------------------------------------------------------------------------
# L2：TestPilot report.json 逐案比較
# ---------------------------------------------------------------------------


def _verdict_of(item: Mapping[str, Any] | None) -> str | None:
    if item is None:
        return None
    verdict = item.get("verdict")
    return verdict if isinstance(verdict, str) else None


def _summarize_side(report: Any, required: set[str], label: str) -> dict[str, Any]:
    """完整性檢查（每側獨立）：空報告、缺案、重複、未知 verdict、必要案例 SKIP／ERROR 皆不完整。"""
    results = report.get("results") if isinstance(report, Mapping) else None
    problems: list[str] = []
    if not isinstance(report, Mapping):
        problems.append(f"{label} 報告頂層非物件")
    if not isinstance(results, list):
        problems.append(f"{label} 報告 results 缺漏或非清單")
        results = []
    elif not results:
        problems.append(f"{label} 報告 results 為空")
    by_id: dict[str, Mapping[str, Any]] = {}
    rows: list[tuple[str, Mapping[str, Any], int]] = []
    duplicates: set[str] = set()
    unknown: set[str] = set()
    invalid_rows: list[str] = []
    for index, item in enumerate(results):
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            invalid_rows.append(str(index))
            problems.append(f"{label} 結果列 {index} 缺少字串 id")
            continue
        case_id = item["id"]
        if case_id in by_id:
            duplicates.add(case_id)
        else:
            by_id[case_id] = item
        rows.append((case_id, item, index))
        verdict = _verdict_of(item)
        if verdict not in L2_VERDICTS:
            unknown.add(case_id)
    missing = required - set(by_id)
    skips = {
        case_id
        for case_id in required
        if case_id in by_id and _verdict_of(by_id[case_id]) in _REQUIRED_SKIP_VERDICTS
    }
    if not required:
        problems.append(f"{label} required_case_ids 為空")
    problems.extend(f"{label} 缺少必要案例：{case_id}" for case_id in sorted(missing))
    problems.extend(f"{label} 案例重複：{case_id}" for case_id in sorted(duplicates))
    problems.extend(f"{label} verdict 未知：{case_id}" for case_id in sorted(unknown))
    problems.extend(f"{label} 必要案例 SKIP／ERROR：{case_id}" for case_id in sorted(skips))
    # 必要案例集合為空時不得視為完整（與 ab_diff.diff_l15 一致；證據不足不得轉為 PASS）。
    integrity = bool(by_id) and bool(required) and not (missing or duplicates or unknown or skips or invalid_rows or problems)
    return {
        "by_id": by_id,
        "rows": rows,
        "missing": sorted(missing),
        "duplicates": sorted(duplicates),
        "unknown": sorted(unknown),
        "required_skips": sorted(skips),
        "integrity": integrity,
        "problems": problems,
    }


def _invariants_of(case_id: str, case: Mapping[str, Any]) -> list[str] | None:
    """取得安全不變量；格式不符回 None，由呼叫端記為完整性診斷。"""
    value = case.get("invariants", [])
    if value is None:
        value = []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _parse_expires(value: Any) -> datetime | None:
    """解析哨兵到期時間（UTC）。

    接受 aware datetime、YAML 讀入的 `date` 物件，以及 ISO 字串（完整時間或純日期，如 `2027-01-01`）。
    純日期視為該日 UTC 00:00（保守：當日即視為已到期）。無法解析或 naive datetime 回 None，
    呼叫端一律視為已過期（fail closed）。
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return parse_iso(value)
        except ValueError:
            pass
        try:
            return datetime.combine(date.fromisoformat(value), time.min, tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _sentinel_waiver(case_id: str, case: Mapping[str, Any], reason: Any, now: datetime) -> str | None:
    """B 的 FAIL／XFAIL 是否受哨兵豁免：需 `expected: XFAIL`、非 null 的 sentinel，且結果 reason 含 signature。

    帶安全不變量（`invariants` 非空）的案例不得由哨兵豁免（§11.3、§14.2），一律回 None。
    回傳 `"accepted"`（未到期）、`"expired"`（已到期或到期時間無效）或 None（非豁免）。
    """
    if case.get("expected") != "XFAIL":
        return None
    invariants = _invariants_of(case_id, case)
    if invariants is None or invariants:
        return None
    sentinel = case.get("sentinel")
    if not isinstance(sentinel, Mapping):
        return None
    signature = sentinel.get("signature")
    if not isinstance(signature, str) or not signature or signature not in str(reason or ""):
        return None
    expires = _parse_expires(sentinel.get("expires"))
    if expires is None or expires <= now:
        return "expired"
    return "accepted"


def _case_of(cases: Mapping[str, Any], case_id: str) -> Mapping[str, Any]:
    case = cases.get(case_id)
    return case if isinstance(case, Mapping) else {}


def _evidence_refs(item: Mapping[str, Any]) -> list[str]:
    """整理附加 evidence 引用；主要 FAIL 來源仍是逐案結果列。"""
    evidence = item.get("evidence")
    refs: list[str] = []
    if isinstance(evidence, list):
        for row in evidence:
            if not isinstance(row, Mapping):
                continue
            source_ref = row.get("source_ref")
            if not isinstance(source_ref, str) or not source_ref:
                continue
            normalized = source_ref.replace("\\", "/")
            path = normalized.split("#", 1)[0]
            parsed = PurePosixPath(path)
            if (
                not parsed.is_absolute()
                and not any(part in {"", ".", ".."} for part in parsed.parts)
                and ":" not in (parsed.parts[0] if parsed.parts else "")
            ):
                if normalized not in refs:
                    refs.append(normalized)
    return refs


def _validated_report_ref(value: Any, label: str) -> str:
    """驗證相對側 report_dir 的 POSIX 報告路徑。"""
    if not isinstance(value, str) or not value or "\\" in value:
        raise HarnessError(_COMPARATOR, f"{label} report_ref 必須是相對 POSIX 路徑")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise HarnessError(_COMPARATOR, f"{label} report_ref 必須是安全的相對 POSIX 路徑")
    if ":" in (parsed.parts[0] if parsed.parts else ""):
        raise HarnessError(_COMPARATOR, f"{label} report_ref 不得包含磁碟代號")
    return parsed.as_posix()


def _l2_failure(
    case_id: str,
    item: Mapping[str, Any],
    index: int,
    reason: str,
    report_ref: str,
) -> dict[str, Any]:
    return {
        "side": "b",
        "case": case_id,
        "source_ref": f"{report_ref}#/results/{index}",
        "evidence_refs": _evidence_refs(item),
        "reason": reason,
    }


def _needs_review(
    case_id: str,
    case: Mapping[str, Any],
    a_verdict: str | None,
    b_verdict: str | None,
    registered: Mapping[str, str],
) -> bool:
    """判斷是否需人工審查，包括 XFAIL 案例的兩側結果差異。"""
    if case.get("expected") == "XFAIL":
        # §15.10：XFAIL 案例兩側結果不同一律審查，哨兵轉綠須由外部結果證實並登記。
        if a_verdict not in _L2_COMPARABLE_VERDICTS or b_verdict not in _L2_COMPARABLE_VERDICTS:
            return False
        return a_verdict != b_verdict
    if b_verdict == _XPASS:
        # 既有哨兵未再重現：須確認真修復與行為變更，不得自動刪除哨兵（§11.4）。
        return True
    return b_verdict == PASS and a_verdict == FAIL and case_id not in registered


def compare_l2_reports(
    a_report: Mapping[str, Any],
    b_report: Mapping[str, Any],
    *,
    required_case_ids: set[str],
    cases: Mapping[str, Mapping[str, Any]],
    registered_case_changes: Mapping[str, str],
    now: str,
    a_report_ref: str,
    b_report_ref: str,
) -> dict[str, Any]:
    """逐案比較兩側 L2 報告；先完整性、後判定。

    TestPilot 逐案結果沒有明確安全或 invariant verdict，所以 L2 的
    `safety_violations` 固定為空；`cases.yaml` 的覆蓋清單不是執行結果證據。
    產品失敗主來源指向原始結果列，附加 evidence 引用另列於 `evidence_refs`。
    """
    if not isinstance(required_case_ids, (set, frozenset)) or not all(isinstance(item, str) for item in required_case_ids):
        raise HarnessError(_COMPARATOR, "required_case_ids 必須是字串集合")
    if not isinstance(cases, Mapping) or not isinstance(registered_case_changes, Mapping):
        raise HarnessError(_COMPARATOR, "cases 與 registered_case_changes 必須是映射")
    a_report_ref = _validated_report_ref(a_report_ref, "A")
    b_report_ref = _validated_report_ref(b_report_ref, "B")
    required = set(required_case_ids)
    try:
        now_dt = parse_iso(now)
    except (TypeError, ValueError) as exc:
        raise HarnessError(_COMPARATOR, f"now 不是有效的 aware ISO 時間：{exc}") from exc
    a_side = _summarize_side(a_report, required, "A")
    b_side = _summarize_side(b_report, required, "B")

    b_failures: list[str] = []
    xfail_accepted: list[str] = []
    expired_sentinels: list[str] = []
    needs_review: list[dict[str, str]] = []
    safety_violations: list[str] = []
    product_failures: list[dict[str, Any]] = []
    b_result_index: dict[str, tuple[Mapping[str, Any], int]] = {}
    for case_id, b_item, index in sorted(b_side["rows"], key=lambda row: (row[0], row[2])):
        b_result_index.setdefault(case_id, (b_item, index))
        b_verdict = _verdict_of(b_item)
        a_verdict = _verdict_of(a_side["by_id"].get(case_id))
        case = _case_of(cases, case_id)
        if b_verdict in (FAIL, _XFAIL):
            waiver = _sentinel_waiver(case_id, case, b_item.get("reason"), now_dt)
            if waiver == "accepted":
                xfail_accepted.append(case_id)
            elif waiver == "expired":
                if case_id not in expired_sentinels:
                    expired_sentinels.append(case_id)
                product_failures.append(_l2_failure(case_id, b_item, index, "sentinel expired", b_report_ref))
            else:
                if case_id not in b_failures:
                    b_failures.append(case_id)
                product_failures.append(_l2_failure(
                    case_id, b_item, index, str(b_item.get("reason") or b_verdict), b_report_ref,
                ))
        if _needs_review(case_id, case, a_verdict, b_verdict, registered_case_changes):
            review = {"case": case_id, "a": a_verdict or "MISSING", "b": b_verdict or "MISSING"}
            if review not in needs_review:
                needs_review.append(review)
                review_item, review_index = b_result_index[case_id]
                product_failures.append(_l2_failure(
                    case_id,
                    review_item,
                    review_index,
                    "unregistered behavior change needs review",
                    b_report_ref,
                ))

    integrity = {"a": a_side["integrity"], "b": b_side["integrity"]}
    if product_failures or safety_violations:
        verdict = FAIL
    elif not (a_side["integrity"] and b_side["integrity"]):
        verdict = INCOMPLETE
    else:
        verdict = PASS
    return {
        "verdict": verdict,
        "missing": {"a": a_side["missing"], "b": b_side["missing"]},
        "duplicates": {"a": a_side["duplicates"], "b": b_side["duplicates"]},
        "unknown_verdicts": {"a": a_side["unknown"], "b": b_side["unknown"]},
        "required_skips": {"a": a_side["required_skips"], "b": b_side["required_skips"]},
        "b_failures": b_failures,
        "xfail_accepted": xfail_accepted,
        "expired_sentinels": expired_sentinels,
        "needs_review": needs_review,
        "safety_violations": safety_violations,
        "product_failures": product_failures,
        "integrity_problems": {"a": a_side["problems"], "b": b_side["problems"]},
        "integrity": integrity,
    }


# ---------------------------------------------------------------------------
# L3：release-gate.json 讀取（只讀 JSON，不重算門檻）
# ---------------------------------------------------------------------------


def _read_gate(path: Path, label: str) -> tuple[dict[str, Any] | None, str | None]:
    """以位元組讀取 gate；來源損毀留作完整性診斷，不轉成產品失敗。"""
    try:
        document = json.loads(Path(path).read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        return None, f"{label} release-gate.json 無法讀取：{exc}"
    if not isinstance(document, dict):
        return None, f"{label} release-gate.json 頂層非物件"
    return document, None


def _engine_verdict(gate: Mapping[str, Any], label: str) -> str:
    engine = gate.get("engine_result")
    verdict = engine.get("verdict") if isinstance(engine, Mapping) else None
    if not isinstance(verdict, str):
        raise HarnessError(_COMPARATOR, f"{label} 側 engine_result.verdict 缺漏")
    return verdict


def _revision_of(gate: Mapping[str, Any]) -> str | int | None:
    """取得有效的 acceptance revision；格式錯誤由呼叫端記入完整性診斷。"""
    acceptance = gate.get("acceptance")
    revision = acceptance.get("revision") if isinstance(acceptance, Mapping) else None
    if isinstance(revision, bool) or not isinstance(revision, (str, int)) or revision == "":
        return None
    return revision


def _gate_problems(gate: Mapping[str, Any], label: str) -> list[str]:
    """驗證 P2 release-gate.v1 的實際輸出欄位與必要型別。"""
    problems: list[str] = []
    for key in _GATE_REQUIRED_FIELDS:
        if key not in gate:
            problems.append(f"{label} release-gate.json 缺少必要欄位：{key}")
    if gate.get("schema_version") != _GATE_SCHEMA:
        problems.append(f"{label} release-gate.json schema_version 無效：{gate.get('schema_version')!r}")
    for key in ("created_at", "platform", "mode"):
        value = gate.get(key)
        if not isinstance(value, str) or not value:
            problems.append(f"{label} release-gate.json {key} 缺漏或格式錯誤")
    if not isinstance(gate.get("engine_result"), Mapping):
        problems.append(f"{label} release-gate.json engine_result 缺漏或非物件")
    release_result = gate.get("release_gate_result")
    if not isinstance(release_result, str) or release_result not in _GATE_RESULTS:
        problems.append(f"{label} release-gate.json release_gate_result 無效：{release_result!r}")
    baseline_result = gate.get("baseline_result")
    mode = gate.get("mode")
    if mode == "candidate" and baseline_result is None:
        pass
    elif not isinstance(baseline_result, str) or baseline_result not in _A_BASELINE_RESULTS:
        problems.append(f"{label} release-gate.json baseline_result 無效：{baseline_result!r}")
    for key in ("acceptance", "run", "counts", "stuck", "walscan", "evidence_hashes", "excl_evidence_hashes"):
        if not isinstance(gate.get(key), Mapping):
            problems.append(f"{label} release-gate.json {key} 缺漏或非物件")
    return problems


def _checks_of(gate: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], list[str]]:
    checks = gate.get("checks")
    if not isinstance(checks, list):
        return [], ["checks 缺漏或非清單"]
    rows: list[Mapping[str, Any]] = []
    problems: list[str] = []
    for index, check in enumerate(checks):
        if not isinstance(check, Mapping):
            problems.append(f"checks[{index}] 非物件")
            continue
        valid = True
        for key in ("group", "id", "result"):
            value = check.get(key)
            if not isinstance(value, str) or not value:
                problems.append(f"checks[{index}].{key} 缺漏或非字串")
                valid = False
        result = check.get("result")
        if isinstance(result, str) and result not in _GATE_RESULTS:
            problems.append(f"checks[{index}].result 未知：{result!r}")
            valid = False
        if valid:
            rows.append(check)
    return rows, problems


def _evidence_intact(checks: list[Mapping[str, Any]], mode: str) -> bool:
    """證據完整：candidate 僅接受 PASS；baseline 另接受 P2 明定的不適用值。"""
    results = [check.get("result") for check in checks if check.get("group") == "evidence"]
    return bool(results) and all(
        result == PASS or (mode == "baseline" and result == "NOT_APPLICABLE_BASELINE")
        for result in results
    )


def _summarized_gate_result(checks: list[Mapping[str, Any]], mode: str) -> str:
    """複製 P2 ``_summarize`` 的優先序，不自行推導其他發版門檻。"""
    results = {check.get("result") for check in checks}
    if FAIL in results:
        return FAIL
    if _INSUFFICIENT in results:
        return _INSUFFICIENT
    if "BLOCKED" in results:
        return "BLOCKED"
    return PASS if mode == "candidate" else "NOT_APPLICABLE_BASELINE"


def _gate_side_verified(gate: Mapping[str, Any] | None, expected_mode: str) -> bool:
    return gate is not None and gate.get("schema_version") == _GATE_SCHEMA and gate.get("mode") == expected_mode


def _l3_safety_sources(
    a_gate: Mapping[str, Any] | None,
    b_gate: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    for side, gate in (("a", a_gate), ("b", b_gate)):
        checks = gate.get("checks") if gate is not None else None
        if not isinstance(checks, list):
            continue
        for index, check in enumerate(checks):
            if (
                isinstance(check, Mapping)
                and isinstance(check.get("id"), str)
                and bool(check.get("id"))
                and check.get("group") == "safety"
                and check.get("result") == FAIL
            ):
                sources.append({
                    "side": side,
                    "case": check["id"],
                    "source_ref": f"release-gate.json#/checks/{index}",
                    "reason": "safety check FAIL",
                })
    return sources


def _l3_safety_failures(a_gate: Mapping[str, Any] | None, b_gate: Mapping[str, Any] | None) -> list[str]:
    return [f"{row['side'].upper()}:{row['case']}" for row in _l3_safety_sources(a_gate, b_gate)]


def _l3_baseline_failures(gate: Mapping[str, Any] | None) -> list[dict[str, str]]:
    if gate is None:
        return []
    failures: list[dict[str, str]] = []
    engine = gate.get("engine_result")
    engine_verdict = engine.get("verdict") if isinstance(engine, Mapping) else None
    baseline_result = gate.get("baseline_result")
    if engine_verdict == FAIL:
        failures.append({
            "side": "a",
            "case": "engine_result",
            "source_ref": "release-gate.json#/engine_result/verdict",
            "reason": "baseline engine verdict is FAIL",
        })
    if baseline_result == FAIL:
        failures.append({
            "side": "a",
            "case": "baseline_result",
            "source_ref": "release-gate.json#/baseline_result",
            "reason": "baseline result is FAIL",
        })
    return failures


def _l3_product_failures(gate: Mapping[str, Any] | None) -> list[dict[str, str]]:
    if gate is None:
        return []
    failures: list[dict[str, str]] = []
    engine = gate.get("engine_result")
    engine_verdict = engine.get("verdict") if isinstance(engine, Mapping) else None
    release_result = gate.get("release_gate_result")
    if engine_verdict == FAIL:
        failures.append({
            "side": "b",
            "case": "engine_result",
            "source_ref": "release-gate.json#/engine_result/verdict",
            "reason": "candidate engine verdict is FAIL",
        })
    if release_result == FAIL:
        failures.append({
            "side": "b",
            "case": "release_gate_result",
            "source_ref": "release-gate.json#/release_gate_result",
            "reason": "candidate release gate result is FAIL",
        })
    return failures


def _preserve_l3_findings_or_raise(
    a_gate: Mapping[str, Any] | None,
    b_gate: Mapping[str, Any] | None,
    detail: str,
    *,
    a_valid: bool,
    b_valid: bool,
    problems: Mapping[str, list[str]],
) -> dict[str, Any]:
    """Comparator mismatch 時仍回傳先前可核實的產品／安全 FAIL。"""
    trusted_a = a_gate if a_valid else None
    trusted_b = b_gate if b_valid else None
    safety_sources = _l3_safety_sources(trusted_a, trusted_b)
    safety = [f"{row['side'].upper()}:{row['case']}" for row in safety_sources]
    products = _l3_product_failures(trusted_b)
    if not safety and not products:
        raise HarnessError(_COMPARATOR, detail)
    a_checks, _ = _checks_of(trusted_a or {})
    b_checks, _ = _checks_of(trusted_b or {})
    compatible = [check for check in b_checks if check.get("id") == "baseline_compatible"]
    failure_problems = {
        "a": _unique([*problems.get("a", []), detail]),
        "b": _unique([*problems.get("b", []), detail]),
    }
    return {
        "kind": "longrun",
        "a_mode": a_gate.get("mode") if a_gate else None,
        "b_mode": b_gate.get("mode") if b_gate else None,
        "a_engine_verdict": _raw_engine_verdict(trusted_a),
        "a_baseline_result": trusted_a.get("baseline_result") if trusted_a else None,
        "b_engine_verdict": _raw_engine_verdict(trusted_b),
        "b_release_gate_result": trusted_b.get("release_gate_result") if trusted_b else None,
        "b_baseline_compatible": compatible[0].get("result") == PASS if len(compatible) == 1 else None,
        "acceptance_revision": _revision_of(trusted_a) if trusted_a else None,
        "safety_violations": safety,
        "safety_failure_sources": safety_sources,
        "baseline_failures": _l3_baseline_failures(trusted_a),
        "product_failures": products,
        "integrity_problems": failure_problems,
        "integrity": {"a": False, "b": False},
    }


def _raw_engine_verdict(gate: Mapping[str, Any] | None) -> str | None:
    engine = gate.get("engine_result") if gate is not None else None
    verdict = engine.get("verdict") if isinstance(engine, Mapping) else None
    return verdict if isinstance(verdict, str) else None


def compare_l3(a_gate_path: Path, b_gate_path: Path) -> dict[str, Any]:
    """讀取 A（baseline）、B（candidate）兩側 release-gate.json，回傳供 `combine("l3", …)` 使用的摘要。

    依 P2 `release-gate.v1` 核對必要欄位與每筆 check；先驗證側別 mode，再收集該側 FAIL。
    """
    try:
        a_gate_path = Path(a_gate_path)
        b_gate_path = Path(b_gate_path)
    except (TypeError, ValueError) as exc:
        raise HarnessError(_COMPARATOR, f"gate 路徑參數無效：{exc}") from exc
    a_gate, a_error = _read_gate(a_gate_path, "A")
    b_gate, b_error = _read_gate(b_gate_path, "B")
    problems: dict[str, list[str]] = {"a": [], "b": []}
    if a_error:
        problems["a"].append(a_error)
    if b_error:
        problems["b"].append(b_error)
    if a_gate is not None:
        problems["a"].extend(_gate_problems(a_gate, "A"))
    if b_gate is not None:
        problems["b"].extend(_gate_problems(b_gate, "B"))

    a_mode = a_gate.get("mode") if a_gate else None
    b_mode = b_gate.get("mode") if b_gate else None
    a_valid = _gate_side_verified(a_gate, "baseline")
    b_valid = _gate_side_verified(b_gate, "candidate")
    mode_mismatch = (a_gate is not None and a_mode != "baseline") or (b_gate is not None and b_mode != "candidate")
    if a_gate is not None and a_mode != "baseline":
        problems["a"].append(f"A mode 應為 baseline，實際為 {a_mode!r}")
    if b_gate is not None and b_mode != "candidate":
        problems["b"].append(f"B mode 應為 candidate，實際為 {b_mode!r}")
    if mode_mismatch:
        return _preserve_l3_findings_or_raise(
            a_gate,
            b_gate,
            f"模式不符：A={a_mode!r} B={b_mode!r}",
            a_valid=a_valid,
            b_valid=b_valid,
            problems=problems,
        )

    a_revision = _revision_of(a_gate) if a_gate else None
    b_revision = _revision_of(b_gate) if b_gate else None
    if a_gate is not None and a_revision is None:
        problems["a"].append("acceptance.revision 缺漏或格式錯誤")
    if b_gate is not None and b_revision is None:
        problems["b"].append("acceptance.revision 缺漏或格式錯誤")
    if a_revision is not None and b_revision is not None and a_revision != b_revision:
        return _preserve_l3_findings_or_raise(
            a_gate,
            b_gate,
            f"acceptance.revision 不同：A={a_revision} B={b_revision}",
            a_valid=a_valid,
            b_valid=b_valid,
            problems=problems,
        )

    a_platform = a_gate.get("platform") if a_gate else None
    b_platform = b_gate.get("platform") if b_gate else None
    if a_gate is not None and (not isinstance(a_platform, str) or not a_platform):
        problems["a"].append("platform 缺漏或格式錯誤")
    if b_gate is not None and (not isinstance(b_platform, str) or not b_platform):
        problems["b"].append("platform 缺漏或格式錯誤")
    if isinstance(a_platform, str) and isinstance(b_platform, str) and a_platform != b_platform:
        return _preserve_l3_findings_or_raise(
            a_gate,
            b_gate,
            "兩側 platform 不同",
            a_valid=a_valid,
            b_valid=b_valid,
            problems=problems,
        )

    a_checks, a_check_problems = _checks_of(a_gate or {})
    b_checks, b_check_problems = _checks_of(b_gate or {})
    problems["a"].extend(f"A {item}" for item in a_check_problems)
    problems["b"].extend(f"B {item}" for item in b_check_problems)
    for side, gate, checks, check_problems, mode in (
        ("a", a_gate, a_checks, a_check_problems, "baseline"),
        ("b", b_gate, b_checks, b_check_problems, "candidate"),
    ):
        if gate is not None and not check_problems:
            summarized = _summarized_gate_result(checks, mode)
            reported = gate.get("release_gate_result")
            if reported != summarized:
                problems[side].append(
                    f"{side.upper()} release_gate_result 與 checks 彙總不一致："
                    f"reported={reported!r} summarized={summarized!r}"
                )
    trusted_a = a_gate if a_valid else None
    trusted_b = b_gate if b_valid else None
    safety_sources = _l3_safety_sources(trusted_a, trusted_b)
    safety = [f"{row['side'].upper()}:{row['case']}" for row in safety_sources]

    # B 側必須恰有一筆 baseline_compatible 檢查；重複或缺漏不以任意一筆掩蓋。
    compatible_checks = [check for check in b_checks if check.get("id") == "baseline_compatible"]
    if b_gate is not None and not b_check_problems and len(compatible_checks) != 1:
        return _preserve_l3_findings_or_raise(
            a_gate,
            b_gate,
            f"B 側 baseline_compatible 檢查必須恰好一筆，實際 {len(compatible_checks)} 筆",
            a_valid=a_valid,
            b_valid=b_valid,
            problems=problems,
        )
    if b_gate is not None and b_check_problems:
        problems["b"].append("無法完整驗證 baseline_compatible 檢查")

    try:
        a_engine = _engine_verdict(a_gate, "A") if a_gate is not None else None
        b_engine = _engine_verdict(b_gate, "B") if b_gate is not None else None
    except HarnessError as exc:
        return _preserve_l3_findings_or_raise(
            a_gate,
            b_gate,
            exc.detail,
            a_valid=a_valid,
            b_valid=b_valid,
            problems=problems,
        )
    a_baseline_result = a_gate.get("baseline_result") if a_gate else None
    b_release_gate_result = b_gate.get("release_gate_result") if b_gate else None
    for label, gate, checks in (("A", a_gate, a_checks), ("B", b_gate, b_checks)):
        if gate is not None and not any(check.get("group") == "evidence" for check in checks):
            problems[label.lower()].append("缺少 evidence checks")

    product_failures = _l3_product_failures(trusted_b)
    baseline_failures = _l3_baseline_failures(trusted_a)

    a_integrity = (
        a_gate is not None
        and not problems["a"]
        and _evidence_intact(a_checks, "baseline")
        and isinstance(a_engine, str)
    )
    b_integrity = (
        b_gate is not None
        and not problems["b"]
        and _evidence_intact(b_checks, "candidate")
        and isinstance(b_engine, str)
        and len(compatible_checks) == 1
    )
    return {
        "kind": "longrun",
        "a_mode": a_mode,
        "b_mode": b_mode,
        "a_engine_verdict": a_engine,
        "a_baseline_result": a_baseline_result,
        "b_engine_verdict": b_engine,
        "b_release_gate_result": b_release_gate_result,
        "b_baseline_compatible": compatible_checks[0].get("result") == PASS if compatible_checks else None,
        "acceptance_revision": a_revision,
        "safety_violations": safety,
        "safety_failure_sources": safety_sources,
        "baseline_failures": baseline_failures,
        "product_failures": product_failures,
        "integrity_problems": problems,
        "integrity": {"a": a_integrity, "b": b_integrity},
    }


__all__ = [
    "L2_VERDICTS",
    "case_changes",
    "combine",
    "collect_product_failures",
    "compare_l2_reports",
    "compare_l3",
    "load_registered_changes",
]
