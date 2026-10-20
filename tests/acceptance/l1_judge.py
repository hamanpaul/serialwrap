"""L1 必要案例集合與完整性判定（P2-T13，R5-F3；§11.3「必要案例集合事先凍結」、§11.4「證據不足不得 PASS」）。

全計畫唯一的 L1 完整性判定實作：P3～P9 以 `importlib.util.spec_from_file_location` 依路徑載入本檔，不得另寫。
只用 stdlib；不得 import `sw_core`。

判定結果 verdict 取最嚴重者：INVALID > FAIL > INSUFFICIENT > PASS。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

L1_VERDICTS = ("PASS", "FAIL", "INSUFFICIENT", "INVALID")
FORMAT = "l1-results.v1"
_RANK = {"PASS": 0, "INSUFFICIENT": 1, "FAIL": 2, "INVALID": 3}
_FAIL_OUTCOMES = frozenset({"failed", "error", "xpassed"})
_SKIP_OUTCOMES = frozenset({"skipped", "xfailed"})
_PASS_OUTCOMES = frozenset({"passed", "xpassed"})
_OUTPUT_PATH_FLAGS = frozenset({"--junitxml", "--out"})
# 與 tests/acceptance/l1_results.schema.json 同義的 stdlib 結構檢查（本檔不得 import jsonschema；
# schema 檔仍由 check_evidence_index.py 以 jsonschema 另行驗證，兩者應給出相同結論）。
_TOP_REQUIRED = (
    "schema_version", "format", "runner", "platform", "argv", "markexpr", "counts", "resources",
    "not_collected_platform", "core_portable_platform_skips", "collected", "deselected", "results",
    "exit_code", "finished", "deployed_content",
)
_OUTCOMES = frozenset({"passed", "failed", "skipped", "error", "xfailed", "xpassed"})
_SKIP_KINDS = frozenset({"platform", "capability", "other"})
_PHASE_VALUES = frozenset({"passed", "failed", "skipped"})
_TALLY_KEYS = frozenset({"passed", "failed", "skipped"})
_ROW_KEYS = frozenset({"nodeid", "outcome", "skip_kind", "phases"})
_PHASE_KEYS = frozenset({"setup", "call", "teardown"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def set_sha256(nodeids: Sequence[str]) -> str:
    """必要集合的指紋（排序後 JSON 陣列的 sha256）。"""
    return hashlib.sha256(json.dumps(sorted(nodeids), separators=(",", ":")).encode()).hexdigest()


def required_for(lock: Mapping[str, Any], set_id: str, platform: str) -> list[str]:
    """回傳 lock 中 `sets[set_id][platform].nodeids`；set 或平台不存在時拋 KeyError（呼叫端轉 INSUFFICIENT）。"""
    return list(lock["sets"][set_id][platform]["nodeids"])


def normalize_argv(argv: Sequence[str]) -> list[str]:
    """移除輸出路徑參數（`--junitxml=<p>`、`--junitxml <p>`、`--out <p>`），其餘逐字保留、不重排。"""
    out: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("--junitxml="):
            continue
        if token in _OUTPUT_PATH_FLAGS:
            skip_next = True
            continue
        out.append(token)
    return out


def _report_argv(report: Mapping[str, Any]) -> list[str]:
    argv = [str(x) for x in (report.get("argv") or [])]
    # pytest 報告的 argv[0] 是 sys.argv 的程式入口（`python3 -m pytest` 的 __main__.py），不屬 selector。
    if report.get("runner", "pytest") == "pytest" and argv:
        argv = argv[1:]
    return normalize_argv(argv)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _tally_ok(value: Any) -> bool:
    return isinstance(value, Mapping) and set(value) == _TALLY_KEYS and all(
        _is_int(value[k]) and value[k] >= 0 for k in _TALLY_KEYS
    )


def _deployed_side_ok(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("schema_version") != "deployed-content.v1":
        return False
    if not isinstance(value.get("error"), (str, type(None))):
        return False
    for key in ("bundle_tree_sha256", "release_manifest_sha256"):
        digest = value.get(key)
        if digest is not None and not (isinstance(digest, str) and _SHA256_RE.match(digest)):
            return False
    return "bundle_tree_sha256" in value and "release_manifest_sha256" in value


def _row_errors(row: Any) -> list[str]:
    if not isinstance(row, Mapping) or set(row) != _ROW_KEYS:
        return ["results 列需恰含 nodeid／outcome／skip_kind／phases"]
    errs: list[str] = []
    if not isinstance(row["nodeid"], str) or not row["nodeid"]:
        errs.append("results 列 nodeid 須為非空字串")
    if row["outcome"] not in _OUTCOMES:
        errs.append(f"未知 outcome：{row['outcome']!r}")
    if row["skip_kind"] is not None and row["skip_kind"] not in _SKIP_KINDS:
        errs.append(f"未知 skip_kind：{row['skip_kind']!r}")
    phases = row["phases"]
    if not isinstance(phases, Mapping) or set(phases) != _PHASE_KEYS:
        errs.append("phases 需恰含 setup／call／teardown")
    elif any(v is not None and v not in _PHASE_VALUES for v in phases.values()):
        errs.append("phases 值需為 passed／failed／skipped／null")
    return errs


def _schema_errors(report: Mapping[str, Any]) -> list[str]:
    """依 l1_results.schema.json 的結構檢查（缺欄、型別、列舉、deployed_content 形狀）。"""
    errors = [f"缺必填欄位：{k}" for k in _TOP_REQUIRED if k not in report]
    if report.get("schema_version") != 2 or not _is_int(report.get("schema_version")):
        errors.append("schema_version 必須為 2")
    if report.get("runner") not in ("pytest", "cargo"):
        errors.append("runner 必須為 pytest 或 cargo")
    if report.get("platform") not in ("linux", "windows"):
        errors.append("platform 必須為 linux 或 windows")
    if not _is_str_list(report.get("argv")):
        errors.append("argv 必須為字串陣列")
    if not isinstance(report.get("markexpr"), str):
        errors.append("markexpr 必須為字串")
    for key in ("counts", "resources"):
        table = report.get(key)
        if not isinstance(table, Mapping) or not all(_tally_ok(v) for v in table.values()):
            errors.append(f"{key} 必須為 {{名稱: {{passed,failed,skipped}}}}")
    for key in ("not_collected_platform", "core_portable_platform_skips", "collected", "deselected"):
        if not _is_str_list(report.get(key)):
            errors.append(f"{key} 必須為字串陣列")
    if not isinstance(report.get("results"), list):
        errors.append("results 必須為陣列")
    else:
        for row in report["results"]:
            errors.extend(_row_errors(row))
    if not _is_int(report.get("exit_code")):
        errors.append("exit_code 必須為整數")
    if not isinstance(report.get("finished"), bool):
        errors.append("finished 必須為布林值")
    deployed = report.get("deployed_content")
    if deployed is not None:
        if not isinstance(deployed, Mapping) or not {"start", "end"} <= set(deployed):
            errors.append("deployed_content 須為 null 或含 start／end 的物件")
        elif not (_deployed_side_ok(deployed["start"]) and _deployed_side_ok(deployed["end"])):
            errors.append("deployed_content.start／end 形狀不符 deployed-content.v1")
    return errors


def _consistency_errors(results: Sequence[Mapping[str, Any]]) -> list[str]:
    """outcome 與 phases 必須一致：passed 只能來自三個 phase 全數 passed（防止未執行的 call 被記為 passed）。"""
    errs: list[str] = []
    for row in results:
        if row.get("outcome") == "passed":
            phases = row.get("phases") or {}
            if any(phases.get(k) != "passed" for k in ("setup", "call", "teardown")):
                errs.append(f"outcome passed 與 phases 不一致：{row.get('nodeid')}")
    return errs


def _dupes(items: Sequence[str]) -> list[str]:
    seen: dict[str, int] = {}
    for item in items:
        seen[item] = seen.get(item, 0) + 1
    return sorted(nid for nid, count in seen.items() if count > 1)


def _unique(items: Sequence[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def judge(
    report: Mapping[str, Any],
    required: Sequence[str],
    *,
    platform: str,
    junit: Mapping[str, int] | None = None,
    selector: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """依 R5-F3 判定規則評估一份 `l1-results.v1` 報告。

    junit：`{"tests","failures","errors","skipped"}`，與報告逐列交叉核對（不同次執行即 INVALID）。
    selector：lock 中該 set／平台的 selector；argv 不符即 INVALID。
    """
    invalid: list[str] = []
    insufficient: list[str] = []
    fail_rows: list[str] = []

    if not isinstance(report, Mapping):
        invalid.append("report 不是物件")
        report = {}
    if report.get("format") != FORMAT:
        invalid.append(f"format 不是 {FORMAT}：{report.get('format')!r}")
    if report.get("platform") != platform:
        invalid.append(f"platform 不符：報告 {report.get('platform')!r}、參數 {platform!r}")
    invalid.extend(f"schema: {err}" for err in _schema_errors(report))
    if not _schema_errors(report):
        invalid.extend(_consistency_errors(report["results"]))

    physical_uart = (report.get("resources") or {}).get("physical_uart") or {}
    if (isinstance(physical_uart, Mapping)
            and any(isinstance(value, int) and value > 0 for value in physical_uart.values())
            and report.get("uart_kind") != "native"):
        insufficient.append("uart_kind_not_native")

    collected = [str(x) for x in (report.get("collected") or [])]
    deselected = [str(x) for x in (report.get("deselected") or [])]
    results = [row for row in (report.get("results") or []) if isinstance(row, Mapping)]
    result_ids = [str(row.get("nodeid")) for row in results]
    required_list = list(required)

    counts = {
        "collected": len(collected),
        "executed": len(results),
        "passed": sum(1 for row in results if row.get("outcome") in _PASS_OUTCOMES),
        "skipped": sum(1 for row in results if row.get("outcome") in _SKIP_OUTCOMES),
        "failed": sum(1 for row in results if row.get("outcome") in ("failed", "error")),
    }

    if junit is not None:
        outcomes = [row.get("outcome") for row in results]
        expect = {
            "tests": len(results),
            "failures": outcomes.count("failed"),
            "errors": outcomes.count("error"),
            "skipped": sum(1 for outcome in outcomes if outcome in _SKIP_OUTCOMES),
        }
        got = {key: junit.get(key) for key in ("tests", "failures", "errors", "skipped")}
        if got != expect:
            invalid.append(f"junit/l1-results mismatch：junit {got}、report {expect}")

    if selector is not None:
        want = normalize_argv([str(x) for x in (selector.get("argv") or [])])
        if _report_argv(report) != want:
            invalid.append(f"selector mismatch：報告 {_report_argv(report)}、selector {want}")

    # 2. 任何列（必要與否）失敗 → FAIL
    for row in results:
        if row.get("outcome") in _FAIL_OUTCOMES:
            fail_rows.append(str(row.get("nodeid")))
    failed = _unique(fail_rows)

    # 3. 執行完成與退出碼
    if report.get("finished") is not True:
        insufficient.append("runner not finished")
    exit_code = report.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0 and not fail_rows:
        insufficient.append(f"runner exit {exit_code}")

    # 4. 重複 nodeid
    duplicates = _unique(_dupes(collected) + _dupes(result_ids))
    if duplicates:
        insufficient.append("duplicate nodeid")

    # 5. 必要集合為空
    if not required_list:
        insufficient.append("no required set")

    # 6. 必要案例逐一檢查
    missing: list[str] = []
    deselected_required: list[str] = []
    not_run: list[str] = []
    skipped_required: list[dict[str, Any]] = []
    collected_set = set(collected)
    deselected_set = set(deselected)
    result_by_id: dict[str, list[Mapping[str, Any]]] = {}
    for row in results:
        result_by_id.setdefault(str(row.get("nodeid")), []).append(row)
    for nodeid in required_list:
        if nodeid in deselected_set:
            deselected_required.append(nodeid)
            continue
        if nodeid not in collected_set:
            missing.append(nodeid)
            continue
        rows = result_by_id.get(nodeid, [])
        if not rows:
            not_run.append(nodeid)
            continue
        for row in rows:
            if row.get("outcome") in _SKIP_OUTCOMES:
                skipped_required.append(
                    {"nodeid": nodeid, "outcome": row.get("outcome"), "skip_kind": row.get("skip_kind")}
                )
    if missing or deselected_required or not_run or skipped_required:
        insufficient.append("required case not satisfied")

    # 7. core_portable 平台 SKIP 閘門
    cp_skips = [str(x) for x in (report.get("core_portable_platform_skips") or [])]
    if cp_skips:
        insufficient.append("core_portable platform skip")

    # 8. 結果與收集一致（中途停止或報告被裁剪）
    if set(result_ids) != (collected_set - deselected_set):
        insufficient.append("results do not match collection")

    extra = [nid for nid in _unique(result_ids) if nid not in set(required_list)]

    reasons: list[str] = []
    if invalid:
        reasons.extend(invalid)
    if fail_rows:
        reasons.append(f"failed: {failed}")
    reasons.extend(insufficient)
    if missing:
        reasons.append(f"missing: {missing}")
    if deselected_required:
        reasons.append(f"deselected required: {deselected_required}")
    if not_run:
        reasons.append(f"not run: {not_run}")
    if skipped_required:
        reasons.append(f"required skipped: {[s['nodeid'] for s in skipped_required]}")
    if cp_skips:
        reasons.append(f"core_portable platform skips: {cp_skips}")

    if invalid:
        verdict = "INVALID"
    elif fail_rows:
        verdict = "FAIL"
    elif insufficient:
        verdict = "INSUFFICIENT"
    else:
        verdict = "PASS"
    return {
        "verdict": verdict,
        "reasons": reasons,
        "missing": missing,
        "deselected_required": deselected_required,
        "not_run": not_run,
        "skipped_required": skipped_required,
        "duplicates": duplicates,
        "failed": failed,
        "extra": extra,
        "required_sha256": set_sha256(required_list),
        "counts": counts,
    }
