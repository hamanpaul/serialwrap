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
from pathlib import Path
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

_COMPARATOR = "HARNESS_COMPARATOR_ERROR"
_SUITE = "HARNESS_SUITE_INFRA_ERROR"
_BASELINE = "HARNESS_BASELINE_INVALID"
_OD09_NOT_FROZEN = "HARNESS_OD09_NOT_FROZEN"

_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
_XFAIL = "XFAIL"
_XPASS = "XPASS"
# P2 gate.py 的列舉（RESULTS／NA）；此處只複製列舉值作驗證，不 import realhw。
_B_RELEASE_RESULTS: frozenset[str] = frozenset({PASS, FAIL, _INSUFFICIENT, "BLOCKED", "NOT_APPLICABLE_BASELINE"})
_A_BASELINE_RESULTS: frozenset[str] = frozenset({PASS, FAIL, _INSUFFICIENT})


# ---------------------------------------------------------------------------
# 變更登記（behavior_changes.yaml）
# ---------------------------------------------------------------------------


def _load_changes(path: Path) -> list[dict[str, Any]]:
    """讀取 `behavior_changes.yaml` 的 `changes` 清單；無法讀取或結構不符一律為比較器錯誤。"""
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
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
        if participation not in _AB_PARTICIPATION:
            raise HarnessError(_COMPARATOR, f"{change_id} 的 ab_participation 無效：{participation!r}")
        if (scope == "both") != (participation == "diff"):
            raise HarnessError(_COMPARATOR, f"{change_id} 的 scope 與 ab_participation 不一致")
        registered[change_id] = participation
    return registered


def case_changes(path: Path, *, scope: str | None = None) -> dict[str, str]:
    """回傳 case_id → change_id（依 `case_ids` 反查）。

    `scope` 為 None 時涵蓋所有條目；編排端建 `registered_case_changes` 時傳 `scope="both"`（P9-T6 `ab_runner` 執行序第 7 步）。
    同一 case_id 掛在不同 change 下為比較器錯誤。
    """
    mapping: dict[str, str] = {}
    for entry in _load_changes(path):
        if scope is not None and entry.get("scope") != scope:
            continue
        change_id = entry.get("id")
        case_ids = entry.get("case_ids") or []
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
        raise ValueError(f"未知的 A/B 層級：{layer!r}")
    if diff is not None:
        safety = list(diff.get("safety_violations") or [])
        if safety:
            return FAIL, None, [f"safety violation: {item}" for item in safety]
    if a_status == INFRA_ERROR or b_status == INFRA_ERROR:
        return INFRA_ERROR, _SUITE, []
    if layer == "l2":
        # L2 的 A／B 狀態只依 compare_l2_reports 逐案判定，不使用 run 層級狀態。
        # 判定表的 l2 列（INCOMPLETE → HARNESS_SUITE_INFRA_ERROR）刻意先於 integrity 列判定：
        # compare_l2_reports 在 INCOMPLETE 時必同時標記 integrity=false，若依列序先判 integrity，
        # 該列對真實輸出永不生效，原因碼會錯報為 HARNESS_COMPARATOR_ERROR。
        return _combine_l2(diff)
    if a_status not in _SUITE_STATUSES or b_status not in _SUITE_STATUSES:
        return INFRA_ERROR, _SUITE, [f"unknown suite status: A={a_status!r} B={b_status!r}"]
    if diff is not None:
        integrity = diff.get("integrity") or {}
        if integrity.get("a") is not True or integrity.get("b") is not True:
            return INFRA_ERROR, _COMPARATOR, ["證據不足：比較器完整性未通過"]
    if layer == "l1.5":
        return _combine_l15(a_status, b_status, diff, registered)
    return _combine_l3(a_status, b_status, diff)


def _combine_l2(diff: Mapping[str, Any] | None) -> tuple[str, str | None, list[str]]:
    if diff is None:
        return INFRA_ERROR, _COMPARATOR, ["缺少 L2 比較結果"]
    verdict = diff.get("verdict")
    if verdict == INCOMPLETE:
        return INFRA_ERROR, _SUITE, ["L2 報告不完整（缺案、重複、未知 verdict 或必要案例 SKIP／ERROR）"]
    integrity = diff.get("integrity") or {}
    if integrity.get("a") is not True or integrity.get("b") is not True:
        return INFRA_ERROR, _COMPARATOR, ["證據不足：L2 比較器完整性未通過"]
    if verdict == PASS:
        return PASS, None, []
    if verdict == FAIL:
        return FAIL, None, []
    return INFRA_ERROR, _COMPARATOR, [f"未知的 L2 判定：{verdict!r}"]


def _combine_l15(
    a_status: str,
    b_status: str,
    diff: Mapping[str, Any] | None,
    registered: Mapping[str, str],
) -> tuple[str, str | None, list[str]]:
    if a_status == FAIL and b_status == FAIL:
        return FAIL, None, []
    if a_status == FAIL and b_status == PASS:
        return BASELINE_INVALID, _BASELINE, []
    if b_status == FAIL:
        return FAIL, None, []
    if diff is None:
        return INFRA_ERROR, _COMPARATOR, ["缺少 L1.5 差分輸出"]

    notes: list[str] = []
    b_cases = diff.get("b_cases") or {}
    for item in diff.get("differences") or []:
        case = str(item.get("case"))
        path = str(item.get("path"))
        change = item.get("registered_change")
        scope = registered.get(change) if isinstance(change, str) else None
        if scope == "absolute_only":
            # rust_only：不參與差分，但 B 側必須通過絕對契約。
            if b_cases.get(case) != PASS:
                notes.append(f"rust_only case failed absolute: {case}")
        elif scope == "diff":
            # both：Python 已先實作，A/B 不應有差。
            notes.append(f"both-scope change differs: {change} {case}")
        else:
            notes.append(f"unregistered difference: {case} {path}")
    if notes:
        return FAIL, None, notes
    return PASS, None, []


def _combine_l3(
    a_status: str,
    b_status: str,
    diff: Mapping[str, Any] | None,
) -> tuple[str, str | None, list[str]]:
    if diff is None:
        return INFRA_ERROR, _COMPARATOR, ["缺少 L3 比較結果"]
    if a_status != PASS or diff.get("a_engine_verdict") != PASS or diff.get("a_baseline_result") != PASS:
        return BASELINE_INVALID, _BASELINE, []
    if diff.get("b_engine_verdict") != PASS:
        # 引擎原始 FAIL 判準保留，即使 suite 或 gate 顯示 PASS。
        return FAIL, None, []
    if b_status == FAIL:
        return INFRA_ERROR, _SUITE, ["suite/engine inconsistent"]
    release = diff.get("b_release_gate_result")
    if release == "BLOCKED":
        return INFRA_ERROR, _OD09_NOT_FROZEN, []
    if release == _INSUFFICIENT or diff.get("b_baseline_compatible") is not True:
        return INFRA_ERROR, _SUITE, []
    if release != PASS:
        return FAIL, None, []
    return PASS, None, []


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
    if not isinstance(results, list):
        results = []
    by_id: dict[str, Mapping[str, Any]] = {}
    duplicates: set[str] = set()
    unknown: set[str] = set()
    for item in results:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            raise HarnessError(_COMPARATOR, f"{label} 報告的結果列缺少 id")
        case_id = item["id"]
        if case_id in by_id:
            duplicates.add(case_id)
        else:
            by_id[case_id] = item
        verdict = _verdict_of(item)
        if verdict not in L2_VERDICTS:
            unknown.add(case_id)
    missing = required - set(by_id)
    skips = {
        case_id
        for case_id in required
        if case_id in by_id and _verdict_of(by_id[case_id]) in _REQUIRED_SKIP_VERDICTS
    }
    # 必要案例集合為空時不得視為完整（與 ab_diff.diff_l15 一致；證據不足不得轉為 PASS）。
    integrity = bool(by_id) and bool(required) and not (missing or duplicates or unknown or skips)
    return {
        "by_id": by_id,
        "missing": sorted(missing),
        "duplicates": sorted(duplicates),
        "unknown": sorted(unknown),
        "required_skips": sorted(skips),
        "integrity": integrity,
    }


def _invariants_of(case_id: str, case: Mapping[str, Any]) -> list[str]:
    """案例的安全不變量清單；結構不符為比較器錯誤（fail closed）。"""
    value = case.get("invariants") or []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise HarnessError(_COMPARATOR, f"案例 {case_id} 的 invariants 結構不符")
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
    if _invariants_of(case_id, case):
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


def _needs_review(
    case_id: str,
    case: Mapping[str, Any],
    a_verdict: str | None,
    b_verdict: str | None,
    registered: Mapping[str, str],
) -> bool:
    """判斷是否需人工審查，包括 XFAIL 案例的兩側結果差異。"""
    if b_verdict == _XPASS:
        # 既有哨兵未再重現：須確認真修復與行為變更，不得自動刪除哨兵（§11.4）。
        return True
    if case.get("expected") == "XFAIL":
        # §15.10：XFAIL 案例兩側結果不同一律審查，哨兵轉綠須由外部結果證實並登記。
        return a_verdict != b_verdict
    return b_verdict == PASS and a_verdict == FAIL and case_id not in registered


def compare_l2_reports(
    a_report: Mapping[str, Any],
    b_report: Mapping[str, Any],
    *,
    required_case_ids: set[str],
    cases: Mapping[str, Mapping[str, Any]],
    registered_case_changes: Mapping[str, str],
    now: str,
) -> dict[str, Any]:
    """逐案比較兩側 L2 報告；先完整性、後判定。

    B 側 FAIL／XFAIL 的案例若帶安全不變量（`invariants` 非空），每個不變量產生 `B:<case>:<invariant>` 列入
    `safety_violations`，由 `combine` 第一列直接判 FAIL（即使其他必要案例不完整）。此類案例不得由哨兵豁免。
    A 側不在此判定安全（plan rule 3）。
    """
    required = set(required_case_ids)
    now_dt = parse_iso(now)
    a_side = _summarize_side(a_report, required, "A")
    b_side = _summarize_side(b_report, required, "B")

    b_failures: list[str] = []
    xfail_accepted: list[str] = []
    expired_sentinels: list[str] = []
    needs_review: list[dict[str, str]] = []
    safety_violations: list[str] = []
    for case_id in sorted(b_side["by_id"]):
        b_item = b_side["by_id"][case_id]
        b_verdict = _verdict_of(b_item)
        a_verdict = _verdict_of(a_side["by_id"].get(case_id))
        case = _case_of(cases, case_id)
        if b_verdict in (FAIL, _XFAIL):
            safety_violations.extend(f"B:{case_id}:{inv}" for inv in _invariants_of(case_id, case))
            waiver = _sentinel_waiver(case_id, case, b_item.get("reason"), now_dt)
            if waiver == "accepted":
                xfail_accepted.append(case_id)
            elif waiver == "expired":
                expired_sentinels.append(case_id)
            else:
                b_failures.append(case_id)
        if _needs_review(case_id, case, a_verdict, b_verdict, registered_case_changes):
            needs_review.append({"case": case_id, "a": a_verdict or "MISSING", "b": b_verdict or "MISSING"})

    integrity = {"a": a_side["integrity"], "b": b_side["integrity"]}
    if not (a_side["integrity"] and b_side["integrity"]):
        verdict = INCOMPLETE
    elif b_failures or expired_sentinels or needs_review or safety_violations:
        verdict = FAIL
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
        "integrity": integrity,
    }


# ---------------------------------------------------------------------------
# L3：release-gate.json 讀取（只讀 JSON，不重算門檻）
# ---------------------------------------------------------------------------


def _read_gate(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise HarnessError(_COMPARATOR, f"無法讀取 {label} 側 release-gate.json: {exc}") from exc
    if not isinstance(document, dict):
        raise HarnessError(_COMPARATOR, f"{label} 側 release-gate.json 頂層非物件")
    return document


def _engine_verdict(gate: Mapping[str, Any], label: str) -> str:
    engine = gate.get("engine_result")
    verdict = engine.get("verdict") if isinstance(engine, Mapping) else None
    if not isinstance(verdict, str):
        raise HarnessError(_COMPARATOR, f"{label} 側 engine_result.verdict 缺漏")
    return verdict


def _revision_of(gate: Mapping[str, Any], label: str) -> str | int:
    """`acceptance.revision` 為 P2 契約的字串（如 `"2026-10-15.1"`）；整數亦接受，bool 與空字串拒絕。"""
    acceptance = gate.get("acceptance")
    revision = acceptance.get("revision") if isinstance(acceptance, Mapping) else None
    if isinstance(revision, bool) or not isinstance(revision, (str, int)) or revision == "":
        raise HarnessError(_COMPARATOR, f"{label} 側 acceptance.revision 缺漏")
    return revision


def _enum_of(gate: Mapping[str, Any], key: str, allowed: frozenset[str], label: str) -> str:
    """列舉欄位必須落在 P2 gate 的列舉內；缺漏或未知為比較器錯誤，不得當成產品 FAIL。"""
    value = gate.get(key)
    if not isinstance(value, str) or value not in allowed:
        raise HarnessError(_COMPARATOR, f"{label} 側 {key} 不在允許列舉內：{value!r}")
    return value


def _checks_of(gate: Mapping[str, Any], label: str) -> list[Mapping[str, Any]]:
    checks = gate.get("checks")
    if not isinstance(checks, list) or not all(isinstance(check, Mapping) for check in checks):
        raise HarnessError(_COMPARATOR, f"{label} 側 checks 結構不符")
    return checks


def _evidence_intact(checks: list[Mapping[str, Any]]) -> bool:
    """證據完整：至少一筆 evidence 檢查，且無任何 evidence 檢查為 INSUFFICIENT_EVIDENCE。"""
    results = [check.get("result") for check in checks if check.get("group") == "evidence"]
    return bool(results) and all(result != _INSUFFICIENT for result in results)


def compare_l3(a_gate_path: Path, b_gate_path: Path) -> dict[str, Any]:
    """讀取 A（baseline）、B（candidate）兩側 release-gate.json，回傳供 `combine("l3", …)` 使用的摘要。

    兩側 `mode`、`acceptance.revision`、`platform` 不符為比較器錯誤。
    """
    a_gate = _read_gate(a_gate_path, "A")
    b_gate = _read_gate(b_gate_path, "B")
    if a_gate.get("mode") != "baseline" or b_gate.get("mode") != "candidate":
        raise HarnessError(
            _COMPARATOR,
            f"模式不符：A={a_gate.get('mode')!r} B={b_gate.get('mode')!r}",
        )
    a_revision = _revision_of(a_gate, "A")
    b_revision = _revision_of(b_gate, "B")
    if a_revision != b_revision:
        raise HarnessError(_COMPARATOR, f"acceptance.revision 不同：A={a_revision} B={b_revision}")
    if a_gate.get("platform") != b_gate.get("platform"):
        raise HarnessError(_COMPARATOR, "兩側 platform 不同")

    a_checks = _checks_of(a_gate, "A")
    b_checks = _checks_of(b_gate, "B")
    safety = [
        f"{side}:{check.get('id')}"
        for side, checks in (("A", a_checks), ("B", b_checks))
        for check in checks
        if check.get("group") == "safety" and check.get("result") == FAIL
    ]
    a_baseline_result = _enum_of(a_gate, "baseline_result", _A_BASELINE_RESULTS, "A")
    b_release_gate_result = _enum_of(b_gate, "release_gate_result", _B_RELEASE_RESULTS, "B")
    # B 側必須恰有一筆 baseline_compatible 檢查；重複或缺漏代表 gate 輸出結構不符，不得取 any() 掩蓋。
    compatible_checks = [check for check in b_checks if check.get("id") == "baseline_compatible"]
    if len(compatible_checks) != 1:
        raise HarnessError(
            _COMPARATOR,
            f"B 側 baseline_compatible 檢查必須恰好一筆，實際 {len(compatible_checks)} 筆",
        )
    return {
        "a_mode": a_gate["mode"],
        "b_mode": b_gate["mode"],
        "a_engine_verdict": _engine_verdict(a_gate, "A"),
        "a_baseline_result": a_baseline_result,
        "b_engine_verdict": _engine_verdict(b_gate, "B"),
        "b_release_gate_result": b_release_gate_result,
        "b_baseline_compatible": compatible_checks[0].get("result") == PASS,
        "acceptance_revision": a_revision,
        "safety_violations": safety,
        "integrity": {"a": _evidence_intact(a_checks), "b": _evidence_intact(b_checks)},
    }


__all__ = [
    "L2_VERDICTS",
    "case_changes",
    "combine",
    "compare_l2_reports",
    "compare_l3",
    "load_registered_changes",
]
