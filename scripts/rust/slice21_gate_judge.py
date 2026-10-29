#!/usr/bin/env python3
"""Slice 2.1 出口 gate 判定器與報告組裝（P5-T22；計畫 06 Task 22「判定規則」）。

純函式 `judge(report, expect)` 重算每個 section 的 `status`／`l1_verdict` 與整體 `verdict`，不沿用輸入值：

- 測試類 section（`TEST_SECTIONS`）：`required_set` 非 null、`l1_results` 指向存在的 `l1-results.v1`，且
  P2 唯一實作 `tests/acceptance/l1_judge.py` 的 `judge(...)` 為 PASS（R5-F3：取代計數門檻），
  另核對 section `exit`／計數與 `l1_results` 一致；`FAIL` → FAIL；其餘不足 → EVIDENCE_MISSING。
- 非測試類 section：`required_set` 為 null，`exit == 0` 且明確帶 `section_verdict == "PASS"` 才 PASS（缺即 EVIDENCE_MISSING）；
  `section_verdict == "FAIL"` → FAIL；未執行（exit null）或證據缺 → EVIDENCE_MISSING。`case_uptake` 另以
  `slice_breaker.evaluate` 判定斷路器：tripped／OD-02 → FAIL、證據不足 → EVIDENCE_MISSING。
- parity 列 `OWNER-DECISION-PENDING` 須帶存在於報告 `pending_decisions` 的 `decision` id，否則 INCOMPLETE。
- 只有 `ALLOWED_MISSING`（`windows_cap_windows_com`→BO-S21-01）可在 EVIDENCE_MISSING 下不阻擋。
- `verdict`：任何 FAIL → FAIL；否則非豁免 EVIDENCE_MISSING、identity 欄位缺漏、dry-run、#104 parity 列
  INCOMPLETE／無標記 → INCOMPLETE；全部 PASS → READY_FOR_OWNER_EVIDENCE。本判定器永不輸出 PASS 作為 Slice 出口。

CLI（由 `scripts/rust/slice21_gate.sh` 呼叫）：
- `record-section`：寫入一個 section 片段（計數一律由 `l1_results` 彙總）。
- `assemble`：組裝 report.json（identity、BLOCKED-OWNER、parity、case_uptake）並以 `judge` 判定後寫回。
- `judge`：對既有 report.json 重新判定。

只用 stdlib＋PyYAML；L1 判定與承接率分別依路徑載入 `tests/acceptance/l1_judge.py`、`scripts/rust/slice_breaker.py`。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import platform as _platform
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
L1_JUDGE_PATH = ROOT / "tests" / "acceptance" / "l1_judge.py"
SLICE_BREAKER_PATH = ROOT / "scripts" / "rust" / "slice_breaker.py"

# 測試類 section → 平台（required_set 由報告提供；對應表見 tests/acceptance/slice21_gate_expect.yaml）。
TEST_SECTIONS: dict[str, str] = {
    "linux_l1": "linux",
    "linux_cap_posix_pty": "linux",
    "windows_native_l1": "windows",
    "windows_cap_windows_com": "windows",
    "blackbox_python": "linux",
    "blackbox_rust": "linux",
    "existing_suites_rust": "linux",
    "pytest_full": "linux",
}
NON_TEST_SECTIONS: tuple[str, ...] = (
    "l15_ab", "event_latency", "wal_s1w_perf", "cfg_boundary", "policy_check", "case_uptake",
)
SECTION_ORDER: tuple[str, ...] = (
    "cfg_boundary", "linux_l1", "linux_cap_posix_pty", "windows_native_l1", "windows_cap_windows_com",
    "blackbox_python", "blackbox_rust", "existing_suites_rust", "l15_ab", "event_latency", "wal_s1w_perf",
    "case_uptake", "pytest_full", "policy_check",
)
# 計畫判定規則：只有此 section 可在 EVIDENCE_MISSING 下不阻擋（不接受報告自行宣告的豁免）。
ALLOWED_MISSING: dict[str, str] = {"windows_cap_windows_com": "BO-S21-01"}
IDENTITY_REQUIRED: tuple[str, ...] = (
    "cli_path", "cli_sha256", "daemon_implementation", "daemon_version", "daemon_artifact_id", "daemon_sha256",
    "contracts_revision", "cases_lock_sha256", "freeze_file_sha256", "tool_revision", "platform",
)
TOOL_REVISION_REQUIRED: tuple[str, ...] = ("uart_stdio_bridge_tree_sha256", "runner_sha256")
PLATFORM_REQUIRED: tuple[str, ...] = ("os", "kernel", "arch")
PARITY_STATUSES = frozenset({"PASS", "BLOCKED-OWNER", "OWNER-DECISION-PENDING", "INCOMPLETE"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SKIP_OUTCOMES = frozenset({"skipped", "xfailed"})


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入 {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_L1 = None


def _l1_judge():
    global _L1
    if _L1 is None:
        _L1 = _load_module("l1_judge", L1_JUDGE_PATH)
    return _L1


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- 計數彙總

def counts_from_l1(l1: Mapping[str, Any]) -> dict[str, int]:
    """section 計數欄的唯一來源：由 l1-results 逐列彙總（ignored＝能力類 skip，即 libtest ignored）。"""
    rows = [r for r in (l1.get("results") or []) if isinstance(r, Mapping)]
    return {
        "passed": sum(1 for r in rows if r.get("outcome") == "passed"),
        "failed": sum(1 for r in rows if r.get("outcome") in ("failed", "error")),
        "skipped": sum(1 for r in rows if r.get("outcome") in _SKIP_OUTCOMES),
        "deselected": len(l1.get("deselected") or []),
        "ignored": sum(1 for r in rows if r.get("skip_kind") == "capability"),
    }


def _passed_ids(l1: Mapping[str, Any]) -> set[str]:
    rows = [r for r in (l1.get("results") or []) if isinstance(r, Mapping)]
    by_id: dict[str, list[str]] = {}
    for r in rows:
        by_id.setdefault(str(r.get("nodeid")), []).append(str(r.get("outcome")))
    return {nid for nid, outs in by_id.items() if len(outs) == 1 and outs[0] == "passed"}


# --------------------------------------------------------------------------- section 判定

def _empty_l1_verdict(reason: str) -> dict[str, Any]:
    return {"verdict": "INSUFFICIENT", "reasons": [reason], "missing": [], "skipped_required": [],
            "deselected_required": [], "duplicates": [], "not_run": [], "required_sha256": ""}


def _judge_test_section(sec: Mapping[str, Any], expect: Mapping[str, Any], platform: str,
                        passed_pool: set[str]) -> tuple[str, dict[str, Any], list[str]]:
    reasons: list[str] = []
    set_id = sec.get("required_set")
    if not set_id:
        return "EVIDENCE_MISSING", _empty_l1_verdict("required_set 為 null"), ["測試類 section 缺 required_set"]
    lock = expect.get("lock") or {}
    l1j = _l1_judge()
    set_ids = [str(set_id)]
    extra_cfg = expect.get("additional_required_sets") or {}
    set_ids += [str(s) for s in (extra_cfg.get(sec.get("name")) or [])]
    set_ids += [str(s) for s in (sec.get("additional_required_sets") or []) if str(s) not in set_ids]
    required: list[str] = []
    for sid in set_ids:
        try:
            for nid in l1j.required_for(lock, sid, platform):
                if nid not in required:
                    required.append(nid)
        except (KeyError, TypeError):
            return ("EVIDENCE_MISSING", _empty_l1_verdict(f"set 不存在：{sid}/{platform}"),
                    [f"必要集合 {sid}（{platform}）不在 l1_required.lock.json"])
    rel = sec.get("l1_results")
    if not rel:
        return "EVIDENCE_MISSING", _empty_l1_verdict("l1_results 為 null"), ["l1_results 缺失"]
    path = pathlib.Path(rel)
    if not path.is_absolute():
        path = pathlib.Path(expect.get("base_dir") or ".") / path
    try:
        l1 = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "EVIDENCE_MISSING", _empty_l1_verdict(f"l1_results 無法讀取：{exc}"), [f"l1_results 無法讀取：{rel}"]
    verdict = dict(l1j.judge(l1, required, platform=platform))
    if verdict.get("verdict") != "INVALID":
        passed_pool.update(_passed_ids(l1))
    if verdict.get("verdict") == "FAIL":
        return "FAIL", verdict, list(verdict.get("reasons") or [])
    # 附加檢查（如 existing_suites_rust 的三次 func-test/runner.py --case）：任一非 0 → FAIL，未執行 → 證據不足。
    extra_missing: list[str] = []
    for chk in sec.get("extra_checks") or []:
        code = chk.get("exit") if isinstance(chk, Mapping) else None
        label = chk.get("name") if isinstance(chk, Mapping) else repr(chk)
        if not isinstance(code, int) or isinstance(code, bool):
            extra_missing.append(f"附加檢查未執行：{label}")
        elif code != 0:
            return "FAIL", verdict, [f"附加檢查失敗：{label} exit {code}"]
    reasons.extend(extra_missing)
    if verdict.get("verdict") != "PASS":
        return "EVIDENCE_MISSING", verdict, list(verdict.get("reasons") or [])
    want = counts_from_l1(l1)
    got = {k: sec.get(k) for k in want}
    if got != want:
        reasons.append(f"section 計數與 l1_results 不一致：section {got}、l1_results {want}")
    if sec.get("exit") != l1.get("exit_code") or sec.get("exit") != 0:
        reasons.append(f"section exit {sec.get('exit')!r} 與 l1_results exit_code {l1.get('exit_code')!r} 不一致或非 0")
    if reasons:
        return "EVIDENCE_MISSING", verdict, reasons
    return "PASS", verdict, []


def _judge_non_test_section(sec: Mapping[str, Any]) -> tuple[str, list[str]]:
    if sec.get("required_set") is not None:
        return "EVIDENCE_MISSING", ["非測試類 section 的 required_set 必須為 null"]
    # 不預設放行：缺 section_verdict 即證據不足（審查 b7cd852 #3）。
    sv = sec.get("section_verdict")
    exit_code = sec.get("exit")
    if sv == "FAIL":
        return "FAIL", [f"section 自身判定 FAIL（exit {exit_code!r}）"] + list(sec.get("reasons") or [])
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return "EVIDENCE_MISSING", ["未執行（exit 為 null）"] + list(sec.get("reasons") or [])
    if exit_code != 0:
        return "FAIL", [f"exit {exit_code}"] + list(sec.get("reasons") or [])
    if sv != "PASS":
        return "EVIDENCE_MISSING", [f"section 自身判定 {sv!r}"] + list(sec.get("reasons") or [])
    return "PASS", []


EVLAT_TOOL_PATH = ROOT / "scripts" / "rust" / "event_latency_measure.py"
EVLAT_CONTRACT_PATH = ROOT / "contracts" / "resource_limits.yaml"


def event_latency_ref() -> dict[str, Any]:
    """event_latency 證據須對上的工具／契約身分（現行工作樹）：工具與契約 sha256、契約 method_revision。"""
    ref: dict[str, Any] = {"tool_sha256": None, "contract_sha256": None, "method_revision": None}
    if EVLAT_TOOL_PATH.is_file():
        ref["tool_sha256"] = sha256_file(EVLAT_TOOL_PATH)
    if EVLAT_CONTRACT_PATH.is_file():
        ref["contract_sha256"] = sha256_file(EVLAT_CONTRACT_PATH)
        try:
            ref["method_revision"] = ((_load_yaml(EVLAT_CONTRACT_PATH) or {}).get("event_latency") or {}).get("method_revision")
        except Exception:  # noqa: BLE001 — 讀不到即 None，核對時判不符
            pass
    return ref


def judge_event_latency_evidence(sec: Mapping[str, Any], report: Mapping[str, Any],
                                 expect: Mapping[str, Any]) -> tuple[str | None, list[str]]:
    """方法審閱 [High]（2026-10-29）：event_latency section 不得只看 gate 片段的 section_verdict——須讀取 `evidence`
    指向的量測 JSON，以其 verdict 判定，並核對其 identity 與本 run 一致：daemon_sha256＝報告 identity、
    artifact_source_commit＝報告 git_revision（建置失敗留下的舊 binary 不得綁到目前 HEAD）、artifact_build_run_id 與
    run_id＝報告 run_id、tool_sha256／contract_sha256／method_revision＝現行工具與契約。

    回傳 (覆寫狀態或 None, 原因)：證據 FAIL → "FAIL"；缺檔、無法讀取、身分不符、verdict 非 PASS → "EVIDENCE_MISSING"；
    全部相符且 PASS → None（沿用片段判定）。"""
    rel = sec.get("evidence")
    if not rel:
        return "EVIDENCE_MISSING", ["event_latency 未附量測證據（evidence 為 null）"]
    path = pathlib.Path(str(rel))
    if not path.is_absolute():
        path = pathlib.Path(expect.get("base_dir") or ".") / path
    try:
        ev = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "EVIDENCE_MISSING", [f"event_latency 量測證據無法讀取：{rel}（{exc}）"]
    if not isinstance(ev, Mapping):
        return "EVIDENCE_MISSING", [f"event_latency 量測證據格式錯誤：{rel}"]
    verdict = ev.get("verdict")
    if verdict == "FAIL":
        return "FAIL", [f"event_latency 量測證據 FAIL：{'；'.join(map(str, ev.get('reasons') or [])) or '—'}"]
    ident = ev.get("identity") if isinstance(ev.get("identity"), Mapping) else {}
    rep_ident = report.get("identity") if isinstance(report.get("identity"), Mapping) else {}
    ref = expect.get("event_latency_ref") or event_latency_ref()
    want = {
        "daemon_sha256": rep_ident.get("daemon_sha256"),
        "artifact_source_commit": report.get("git_revision"),
        "artifact_build_run_id": report.get("run_id"),
        "run_id": report.get("run_id"),
        "tool_sha256": ref.get("tool_sha256"),
        "contract_sha256": ref.get("contract_sha256"),
        "method_revision": ref.get("method_revision"),
    }
    reasons = [f"event_latency 證據 identity.{k}={ident.get(k)!r} 與本 run 不符（應為 {v!r}）"
               for k, v in want.items() if not v or ident.get(k) != v]
    if verdict != "PASS":
        reasons.append(f"event_latency 量測證據 verdict={verdict!r}：{'；'.join(map(str, ev.get('reasons') or [])) or '—'}")
    return ("EVIDENCE_MISSING", reasons) if reasons else (None, [])


# --------------------------------------------------------------------------- identity／承接率／parity

def identity_problems(identity: Any, daemon_path: Any) -> tuple[list[str], list[str]]:
    """回傳 (缺漏→INCOMPLETE, 不符→FAIL)。"""
    missing: list[str] = []
    mismatch: list[str] = []
    if not isinstance(identity, Mapping):
        return ["identity 缺失"], []
    for key in IDENTITY_REQUIRED:
        if key not in identity or identity.get(key) in (None, ""):
            missing.append(f"identity.{key}")
    tool = identity.get("tool_revision")
    if isinstance(tool, Mapping):
        missing += [f"identity.tool_revision.{k}" for k in TOOL_REVISION_REQUIRED if not tool.get(k)]
    plat = identity.get("platform")
    if isinstance(plat, Mapping):
        missing += [f"identity.platform.{k}" for k in PLATFORM_REQUIRED if not plat.get(k)]
    for key in ("cli_sha256", "daemon_sha256", "cases_lock_sha256", "freeze_file_sha256"):
        val = identity.get(key)
        if val not in (None, "") and not (isinstance(val, str) and _SHA256_RE.match(val)):
            mismatch.append(f"identity.{key} 不是 64 位小寫 hex")
    build = identity.get("build")
    if isinstance(build, Mapping):
        # 方法審閱 [High]：Linux release 建置失敗即 FAIL（受測 artifact 不是本 run 的產物）；建置當下 sha256 須等於報告 artifact。
        if build.get("linux_exit") not in (0, None):
            mismatch.append(f"Linux release 建置失敗（exit {build.get('linux_exit')}），受測 artifact 不是本 run 產物")
        at_build = build.get("daemon_sha256_at_build")
        if at_build and identity.get("daemon_sha256") and at_build != identity.get("daemon_sha256"):
            mismatch.append("identity.daemon_sha256 與建置當下記錄的 sha256 不符")
        elif build.get("linux_exit") == 0 and not at_build:
            missing.append("identity.build.daemon_sha256_at_build")
    if daemon_path:
        p = pathlib.Path(daemon_path)
        if not p.is_file():
            missing.append(f"daemon artifact 不存在：{p}")
        elif identity.get("daemon_sha256") and sha256_file(p) != identity.get("daemon_sha256"):
            mismatch.append(f"identity.daemon_sha256 與 {p} 實際 sha256 不符")
    return missing, mismatch


def case_uptake(freeze: Mapping[str, Any] | None, case_verdicts: Mapping[str, Any] | None) -> dict[str, Any]:
    """有效 PASS 只計 gate 判定為 PASS 的 slice_2.1 案例；同一案例全部 verdict 皆 PASS 才計入（Task 0b）。"""
    freeze = freeze or {}
    ids = [str(x) for x in (freeze.get("required_case_ids") or [])]
    denominator = int(freeze.get("denominator") or 0)
    verdicts = case_verdicts or {}
    passed = []
    for cid in ids:
        v = verdicts.get(cid)
        vs = v if isinstance(v, list) else [v]
        if vs and all(x == "PASS" for x in vs):
            passed.append(cid)
    rate = (len(passed) / denominator) if denominator else 0.0
    window = freeze.get("window") or {}
    return {"denominator": denominator, "valid_pass": len(passed), "rate": rate,
            "window_start": window.get("start"), "pass_case_ids": passed}


# --------------------------------------------------------------------------- case_verdicts 彙整（OD-60／OWNER-BATCH #8）

_VERDICT_RANK = {"PASS": 0, "EVIDENCE_MISSING": 1, "FAIL": 2}
L15_TEST_PREFIX = "tests/acceptance/l15/test_slice21_l15.py::test_case["
L15_LABELS = ("python", "rust")


def worst_verdict(verdicts: Sequence[str]) -> str:
    """最差判定：FAIL ＞ EVIDENCE_MISSING ＞ PASS；空串列（缺證據）即 EVIDENCE_MISSING；未知值視為 EVIDENCE_MISSING。"""
    if not verdicts:
        return "EVIDENCE_MISSING"
    return max((v if v in _VERDICT_RANK else "EVIDENCE_MISSING" for v in verdicts), key=_VERDICT_RANK.__getitem__)


def _outcome_verdict(outcomes: Sequence[str]) -> str:
    """單一 nodeid 的 outcome → 判定：恰一列 passed → PASS；failed／error → FAIL；其餘（skip、重複列、缺）→ EVIDENCE_MISSING。"""
    if any(o in ("failed", "error") for o in outcomes):
        return "FAIL"
    return "PASS" if list(outcomes) == ["passed"] else "EVIDENCE_MISSING"


def aggregate_case_verdicts(required_ids: Sequence[str], *, l15_rows: Sequence[Mapping[str, Any]],
                            func_checks: Mapping[str, Any], excluded: Sequence[Mapping[str, Any]],
                            section_rows: Mapping[str, Sequence[Mapping[str, Any]]],
                            section_status: Mapping[str, str] | None = None) -> tuple[dict[str, str], dict[str, list]]:
    """依 Task 20／21 的逐案證據彙整 `case_verdicts`（純函式；rulings/p5.md OWNER-BATCH #8、OD-60）。

    證據來源：
    - l15_ab：`test_slice21_l15.py::test_case[<label>-…-<case>]` 的 python 與 rust 兩列（A/B 兩邊皆為該案證據）；
    - 移出 l15_ab 者（`l15_ab_excluded`）：`covered_by` 各 nodeid 在指定 section 的 outcome，`covered_by_sections` 的 section 判定；
    - func-test：`existing_suites_rust` 的 `func-test:<case>` runner exit（0 → PASS，非 0 → FAIL）。
    每案取所有證據中最差判定，無任何證據即 EVIDENCE_MISSING。回傳 (case_id → 判定, case_id → 證據明細)。"""
    by_case: dict[str, dict[str, list[str]]] = {}
    for row in l15_rows:
        nid = str(row.get("nodeid") or "")
        if not nid.startswith(L15_TEST_PREFIX) or not nid.endswith("]"):
            continue
        params = nid[len(L15_TEST_PREFIX):-1]
        label = params.split("-", 1)[0]
        for cid in required_ids:
            if params.endswith(f"-{cid}") and label in L15_LABELS:
                by_case.setdefault(cid, {}).setdefault(label, []).append(str(row.get("outcome")))
    pools: dict[str, dict[str, list[str]]] = {}
    for sec, rows in section_rows.items():
        pool = pools.setdefault(sec, {})
        for row in rows:
            pool.setdefault(str(row.get("nodeid")), []).append(str(row.get("outcome")))
    excl = {str(r.get("case_id")): r for r in excluded}
    verdicts: dict[str, str] = {}
    detail: dict[str, list] = {}
    for cid in required_ids:
        evidence: list[dict[str, Any]] = []
        if cid in by_case:
            for label in L15_LABELS:
                outs = by_case[cid].get(label, [])
                evidence.append({"source": f"l15_ab:{label}", "outcomes": outs, "verdict": _outcome_verdict(outs)})
        if cid in excl:
            row = excl[cid]
            for cov in row.get("covered_by") or []:
                outs = pools.get(str(cov.get("section")), {}).get(str(cov.get("nodeid")), [])
                evidence.append({"source": f"{cov.get('section')}:{cov.get('nodeid')}", "decision": row.get("decision"),
                                 "outcomes": outs, "verdict": _outcome_verdict(outs)})
            for name in row.get("covered_by_sections") or []:
                st = (section_status or {}).get(str(name))
                evidence.append({"source": f"section:{name}", "decision": row.get("decision"),
                                 "verdict": st if st in _VERDICT_RANK else "EVIDENCE_MISSING"})
        if cid in func_checks:
            code = func_checks[cid]
            ok = isinstance(code, int) and not isinstance(code, bool)
            evidence.append({"source": f"func-test:{cid}", "exit": code,
                             "verdict": ("PASS" if code == 0 else "FAIL") if ok else "EVIDENCE_MISSING"})
        verdicts[cid] = worst_verdict([e["verdict"] for e in evidence])
        detail[cid] = evidence
    return verdicts, detail


def append_breaker_snapshot(path: pathlib.Path, freeze: Mapping[str, Any], case_verdicts: Mapping[str, Any], *,
                            run_id: str, today: str | None = None) -> dict[str, Any]:
    """OD-60：gate 每次正式實跑附加一筆斷路器快照（`slice_breaker` 格式＋`passed`／`denominator` 使 rate 可驗證）。"""
    up = case_uptake(freeze, case_verdicts)
    body = json.dumps(dict(sorted((str(k), v) for k, v in case_verdicts.items())), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))
    snap = {"date": today or dt.datetime.now(dt.timezone.utc).date().isoformat(), "rate": up["rate"],
            "passed": up["valid_pass"], "denominator": up["denominator"], "run_id": run_id,
            "results_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(snap, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return snap


def judge_breaker(freeze: Mapping[str, Any] | None, breaker: Mapping[str, Any] | None) -> tuple[str, list[str], Any]:
    """承接率斷路器（審查 b7cd852 #1）：以 `slice_breaker.evaluate` 判定，回 (status, reasons, evaluate 結果)。

    tripped 或 od02_resubmit → FAIL；insufficient_evidence、輸入缺漏或格式錯誤 → EVIDENCE_MISSING；其餘 PASS。
    `breaker` 須含 `snapshots`（每週快照）、`today`（YYYY-MM-DD）與 `stage2_estimate_weeks`（OD-02 輸入）。
    """
    if not isinstance(breaker, Mapping):
        return "EVIDENCE_MISSING", ["斷路器輸入缺漏（每週快照／判定日／stage2_estimate_weeks）"], None
    missing = [k for k in ("snapshots", "today", "stage2_estimate_weeks") if breaker.get(k) is None]
    if missing or not isinstance(freeze, Mapping):
        return "EVIDENCE_MISSING", [f"斷路器輸入缺漏：{missing or ['freeze']}"], None
    sb = _load_module("slice_breaker", SLICE_BREAKER_PATH)
    try:
        sb._check_snapshot_denominators(list(breaker["snapshots"]), dict(freeze))
        res = sb.evaluate(dict(freeze), list(breaker["snapshots"]), today=str(breaker["today"]),
                          stage2_estimate_weeks=breaker["stage2_estimate_weeks"])
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        return "EVIDENCE_MISSING", [f"斷路器輸入錯誤：{exc}"], None
    if res["tripped"] or res["od02_resubmit"]:
        reasons = [r for r in (res.get("reason"),) if r]
        if res["od02_resubmit"]:
            reasons.append("Stage 2 已耗週數＋剩餘預估超過 OD-02 24 週上限")
        return "FAIL", reasons, res
    if res["insufficient_evidence"]:
        return "EVIDENCE_MISSING", [str(res.get("reason"))], res
    return "PASS", [], res


def parity_rows_from_doc(text: str) -> list[dict[str, str]]:
    """自 parity-amendments 文件抽出「承接 Phase/Task」含 P5 的列（§1 以 `#N`、§2 以稽核 ID 為 id）。"""
    rows: list[dict[str, str]] = []
    sec = None
    for line in text.splitlines():
        if line.startswith("## §1"):
            sec = 1
        elif line.startswith("## §2"):
            sec = 2
        elif line.startswith("### §2.1") or line.startswith("## §3"):
            sec = None
        if not sec or not line.startswith("| ") or line.startswith(("| #", "| 稽核 ID", "|---")):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        take = cells[4] if sec == 1 and len(cells) > 4 else cells[2]
        if "P5" not in take:
            continue
        rid = f"#{cells[0]}" if sec == 1 else cells[0]
        rows.append({"id": rid, "take": take})
    return rows


def judge_parity(parity: Mapping[str, Any], passed_pool: set[str], bo_ids: set[str],
                 decision_ids: set[str] | None = None) -> dict[str, Any]:
    status_doc = parity.get("status") or {}
    status_rows = {str(r.get("id")): r for r in (status_doc.get("rows") or []) if isinstance(r, Mapping)}
    source_rows = parity.get("source_rows")
    problems: list[str] = []
    if source_rows is None:
        problems.append("parity 來源文件不可得，無法核對逐列標記")
        ids = list(status_rows)
        takes: dict[str, str] = {}
    else:
        ids = [r["id"] for r in source_rows]
        takes = {r["id"]: r.get("take", "") for r in source_rows}
        extra = sorted(set(status_rows) - set(ids))
        if extra:
            problems.append(f"狀態檔含來源文件沒有的列：{extra}")
    actual_sha = parity.get("source_sha256_actual")
    if actual_sha is not None and actual_sha != status_doc.get("source_sha256"):
        problems.append("來源文件 sha256 與狀態檔 source_sha256 不符，須重新核對")
    out_rows: list[dict[str, Any]] = []
    for rid in ids:
        row = status_rows.get(rid)
        reasons: list[str] = []
        claimed = row.get("status") if row else None
        tests = [str(t) for t in (row.get("tests") or [])] if row else []
        if row is None:
            status = "INCOMPLETE"
            reasons.append("無標記")
        elif claimed not in PARITY_STATUSES:
            status = "INCOMPLETE"
            reasons.append(f"未知狀態 {claimed!r}")
        elif claimed == "PASS":
            if not tests:
                status = "INCOMPLETE"
                reasons.append("PASS 未附測試 nodeid")
            else:
                unverified = [t for t in tests if t not in passed_pool]
                status = "INCOMPLETE" if unverified else "PASS"
                if unverified:
                    reasons.append(f"本次 L1 證據未見 passed：{unverified}")
        elif claimed == "BLOCKED-OWNER":
            bo = row.get("bo")
            status = "BLOCKED-OWNER" if bo in bo_ids else "INCOMPLETE"
            if status != "BLOCKED-OWNER":
                reasons.append(f"BLOCKED-OWNER 未對應有效 BO 項目：{bo!r}")
        elif claimed == "OWNER-DECISION-PENDING":
            # 須對應報告 pending_decisions 中的裁決項，擁有者才看得到（審查 4920d90 #1）。
            decision = row.get("decision")
            status = claimed if decision and decision in (decision_ids or set()) else "INCOMPLETE"
            if status != claimed:
                reasons.append(f"OWNER-DECISION-PENDING 未對應 pending_decisions 項目：{decision!r}")
        else:
            status = claimed
        if row and row.get("reason") and status != "PASS":
            reasons.append(str(row["reason"]))
        out_rows.append({"id": rid, "take": takes.get(rid, ""), "status": status, "claimed": claimed,
                         "tests": tests, "reasons": reasons})
    summary: dict[str, int] = {}
    for r in out_rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    incomplete = bool(problems) or any(r["status"] == "INCOMPLETE" for r in out_rows)
    return {"source": str(status_doc.get("source", "")), "status_file": str(parity.get("status_file", "")),
            "evaluated": True, "problems": problems, "summary": summary, "incomplete": incomplete,
            "rows": out_rows}


# --------------------------------------------------------------------------- 主判定

def judge(report: Mapping[str, Any], expect: Mapping[str, Any]) -> dict[str, Any]:
    """依計畫 Task 22 判定規則重算 section 與 verdict（純函式；不寫檔）。"""
    passed_pool: set[str] = set()
    section_passed: dict[str, set[str]] = {}
    by_name: dict[str, Mapping[str, Any]] = {}
    for sec in report.get("sections") or []:
        if isinstance(sec, Mapping) and sec.get("name") not in by_name:
            by_name[str(sec.get("name"))] = sec
    out_sections: list[dict[str, Any]] = []
    verdict_reasons: list[str] = []
    names = [n for n in SECTION_ORDER if n in by_name] + [n for n in by_name if n not in SECTION_ORDER]
    names += [n for n in SECTION_ORDER if n not in by_name]
    for name in names:
        sec = dict(by_name.get(name) or {"name": name, "command": "", "exit": None, "passed": 0, "failed": 0,
                                          "skipped": 0, "deselected": 0, "ignored": 0, "required_set": None,
                                          "l1_results": None, "allowed_missing_bo": None, "log": ""})
        if name not in by_name:
            reasons = ["section 未出現在報告中"]
            status, l1v = "EVIDENCE_MISSING", _empty_l1_verdict("section 缺失")
        elif name in TEST_SECTIONS:
            pool: set[str] = set()
            status, l1v, reasons = _judge_test_section(sec, expect, TEST_SECTIONS[name], pool)
            section_passed[name] = pool
            passed_pool |= pool
        elif name in NON_TEST_SECTIONS:
            status, reasons = _judge_non_test_section(sec)
            l1v = {}
            if name == "event_latency" and status != "FAIL":
                ev_status, ev_reasons = judge_event_latency_evidence(sec, report, expect)
                if ev_status is not None:
                    status, reasons = ev_status, list(reasons) + ev_reasons
        else:
            status, l1v, reasons = "EVIDENCE_MISSING", {}, [f"未知 section：{name}"]
        sec["status"] = status
        sec["l1_verdict"] = l1v
        sec["reasons"] = reasons
        sec["allowed_missing_bo"] = ALLOWED_MISSING.get(name)
        out_sections.append(sec)

    # OD-40：移出 l15_ab 的情境改由逐名列出的 pytest 測試承擔；每筆須在指定 section 恰為 passed，否則 l15_ab 不得 PASS。
    # OD-58：另可由整個 section 承擔（`covered_by_sections`，如 s21-event-latency → event_latency），該 section 須為 PASS。
    status_by_name = {s["name"]: s["status"] for s in out_sections}
    for sec in out_sections:
        if sec["name"] != "l15_ab" or sec["status"] != "PASS":
            continue
        for row in expect.get("l15_ab_excluded") or []:
            absent = [f"{c.get('section')}:{c.get('nodeid')}" for c in (row.get("covered_by") or [])
                      if c.get("nodeid") not in section_passed.get(str(c.get("section")), set())]
            if absent or not row.get("covered_by"):
                sec["status"] = "EVIDENCE_MISSING"
                sec["reasons"] = list(sec["reasons"]) + [
                    f"{row.get('case_id')} 依 {row.get('decision')} 移出 l15_ab，承擔的 pytest 未見 passed：{absent or '（未列）'}"]
            not_pass = [str(n) for n in (row.get("covered_by_sections") or []) if status_by_name.get(str(n)) != "PASS"]
            if not_pass:
                sec["status"] = "EVIDENCE_MISSING"
                sec["reasons"] = list(sec["reasons"]) + [
                    f"{row.get('case_id')} 依 {row.get('decision')} 移出 l15_ab，承擔的 section 非 PASS：{not_pass}"]

    missing_id, mismatch_id = identity_problems(report.get("identity"), expect.get("daemon_path"))
    uptake = case_uptake(expect.get("freeze"), expect.get("case_verdicts"))
    b_status, b_reasons, b_res = judge_breaker(expect.get("freeze"), expect.get("breaker"))
    uptake["breaker"] = b_res
    for sec in out_sections:
        if sec["name"] == "case_uptake" and sec["status"] == "PASS" and b_status != "PASS":
            sec["status"] = b_status
            sec["reasons"] = list(sec["reasons"]) + b_reasons
    parity_cfg = expect.get("parity")
    bo_ids = {str(b.get("id")) for b in (report.get("blocked_owner") or []) if isinstance(b, Mapping)}
    bo_ids |= set(expect.get("bo_ids") or [])
    decision_ids = {str(d.get("id")) for d in (report.get("pending_decisions") or []) if isinstance(d, Mapping)}
    parity = (judge_parity(parity_cfg, passed_pool, bo_ids, decision_ids) if parity_cfg
              else {"evaluated": False, "rows": []})

    failed = [s["name"] for s in out_sections if s["status"] == "FAIL"]
    missing = [s["name"] for s in out_sections
               if s["status"] == "EVIDENCE_MISSING" and s["name"] not in ALLOWED_MISSING]
    if failed:
        verdict_reasons.append(f"FAIL section：{failed}")
    if mismatch_id:
        verdict_reasons.extend(mismatch_id)
    if missing:
        verdict_reasons.append(f"EVIDENCE_MISSING section：{missing}")
    if missing_id:
        verdict_reasons.append(f"identity 缺漏：{missing_id}")
    if report.get("dry_run"):
        verdict_reasons.append("--dry-run 永不 READY")
    if parity.get("evaluated") and parity.get("incomplete"):
        verdict_reasons.append(f"#104 parity_amendments 未完成：{parity.get('summary')}；{parity.get('problems')}")
    pre = report.get("preflight")
    if isinstance(pre, Mapping) and pre.get("event_latency_frozen") is False:
        verdict_reasons.append("§5.3 event_latency 未凍結（INSUFFICIENT_EVIDENCE）")

    if failed or mismatch_id:
        verdict = "FAIL"
    elif (missing or missing_id or report.get("dry_run")
          or (parity.get("evaluated") and parity.get("incomplete"))
          or (isinstance(pre, Mapping) and pre.get("event_latency_frozen") is False)):
        verdict = "INCOMPLETE"
    else:
        verdict = "READY_FOR_OWNER_EVIDENCE"
    return {"verdict": verdict, "verdict_reasons": verdict_reasons, "sections": out_sections,
            "case_uptake": uptake, "parity_amendments": parity,
            "identity_problems": {"missing": missing_id, "mismatch": mismatch_id}}


# --------------------------------------------------------------------------- CLI：片段、組裝、判定

def _load_yaml(path: pathlib.Path) -> Any:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def cmd_record_section(args: argparse.Namespace) -> int:
    run_dir = pathlib.Path(args.run_dir)
    frag = {
        "name": args.name, "status": "EVIDENCE_MISSING", "command": args.command,
        "exit": None if args.exit in (None, "", "null") else int(args.exit),
        "passed": 0, "failed": 0, "skipped": 0, "deselected": 0, "ignored": 0,
        "required_set": args.required_set or None, "l1_results": args.l1_results or None, "l1_verdict": {},
        "allowed_missing_bo": ALLOWED_MISSING.get(args.name), "log": args.log or "",
    }
    if args.section_verdict:
        frag["section_verdict"] = args.section_verdict
    if args.reason:
        frag["reasons"] = list(args.reason)
    if args.evidence:
        frag["evidence"] = args.evidence
    if args.extra_check:
        checks = []
        for item in args.extra_check:
            label, _, code = item.rpartition("=")
            checks.append({"name": label, "exit": int(code) if code.lstrip("-").isdigit() else None})
        frag["extra_checks"] = checks
    if frag["l1_results"]:
        p = run_dir / frag["l1_results"]
        try:
            frag.update(counts_from_l1(json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            pass
    out = run_dir / "sections" / f"{args.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(frag, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    return 0


def _parity_source(cfg: Mapping[str, Any]) -> tuple[str | None, str]:
    rel = str(cfg.get("source") or "")
    p = ROOT / rel
    if rel and p.is_file():
        return p.read_text(encoding="utf-8"), f"worktree:{rel}"
    ref = cfg.get("source_ref")
    if ref and rel:
        text = _git("show", f"{ref}:{rel}")
        if text is not None:
            return text, f"git:{ref}:{rel}"
    return None, "unavailable"


def build_identity(args: argparse.Namespace) -> dict[str, Any]:
    # 取不到的欄位一律為 null（不得填 0 或空字串）；judge 將 null 視為缺漏 → INCOMPLETE。
    ident: dict[str, Any] = {key: None for key in IDENTITY_REQUIRED}
    ident.update({"daemon_implementation": "rust", "device": os.environ.get("SW_TEST_COM_PAIR") or None})
    if args.cli_path:
        ident["cli_path"] = args.cli_path
        if pathlib.Path(args.cli_path).is_file():
            ident["cli_sha256"] = sha256_file(pathlib.Path(args.cli_path))
    if args.daemon_path and pathlib.Path(args.daemon_path).is_file():
        ident["daemon_sha256"] = sha256_file(pathlib.Path(args.daemon_path))
        ident["daemon_artifact_id"] = f"{pathlib.Path(args.daemon_path).name}@{ident['daemon_sha256'][:12]}"
    if args.daemon_version:
        ident["daemon_version"] = args.daemon_version
    # 方法審閱 [High]：release 建置結果（Linux／Windows 分開）與建置成功當下的 daemon sha256。
    build = {"linux_exit": getattr(args, "build_linux_exit", None), "windows_exit": getattr(args, "build_windows_exit", None),
             "daemon_sha256_at_build": getattr(args, "built_daemon_sha256", None) or None}
    if any(v is not None for v in build.values()):
        ident["build"] = build
    contracts = _git("rev-parse", "HEAD:contracts")
    if contracts:
        ident["contracts_revision"] = contracts.strip()
    for key, rel in (("cases_lock_sha256", "tests/acceptance/cases.lock.json"),
                     ("freeze_file_sha256", "tests/acceptance/breaker/slice_2.1.json")):
        if (ROOT / rel).is_file():
            ident[key] = sha256_file(ROOT / rel)
    tool: dict[str, Any] = {key: None for key in TOOL_REVISION_REQUIRED}
    runner = ROOT / "func-test" / "runner.py"
    if runner.is_file():
        tool["runner_sha256"] = sha256_file(runner)
    try:
        sys.path.insert(0, str(ROOT))
        from sw_core.release_manifest import tree_sha256  # 僅組裝 identity 用

        tool["uart_stdio_bridge_tree_sha256"] = tree_sha256(str(ROOT / "tools" / "uart_stdio_bridge" / "src"))
    except Exception:  # noqa: BLE001 — 取不到即缺漏，由 judge 判 INCOMPLETE
        pass
    ident["tool_revision"] = tool
    uname = _platform.uname()
    ident["platform"] = {"os": uname.system.lower(), "kernel": uname.release, "arch": uname.machine}
    return ident


def breaker_inputs(expect_yaml: Mapping[str, Any], today: str | None = None) -> dict[str, Any]:
    """組 `judge_breaker` 的輸入：快照檔（不存在即 None → 證據缺）、判定日（預設今日 UTC）、OD-02 剩餘預估週數。"""
    snaps = None
    rel = expect_yaml.get("breaker_snapshots_file")
    if rel and (ROOT / rel).is_file():
        snaps = [json.loads(line) for line in (ROOT / rel).read_text(encoding="utf-8").splitlines() if line.strip()]
    return {"snapshots": snaps, "today": today or dt.datetime.now(dt.timezone.utc).date().isoformat(),
            "stage2_estimate_weeks": expect_yaml.get("stage2_estimate_weeks")}


def cmd_assemble(args: argparse.Namespace) -> int:
    run_dir = pathlib.Path(args.run_dir)
    expect_yaml = _load_yaml(pathlib.Path(args.expect))
    bo_doc = _load_yaml(ROOT / expect_yaml["blocked_owner_file"])
    sections = []
    frag_dir = run_dir / "sections"
    for name in SECTION_ORDER:
        p = frag_dir / f"{name}.json"
        if p.is_file():
            sections.append(json.loads(p.read_text(encoding="utf-8")))
    case_verdicts = {}
    if args.case_verdicts and pathlib.Path(args.case_verdicts).is_file():
        case_verdicts = json.loads(pathlib.Path(args.case_verdicts).read_text(encoding="utf-8"))
    blocked = []
    for item in bo_doc["items"]:
        evid = None
        blocked.append({"id": item["id"], "item": item["item"], "case_ids": list(item.get("case_ids") or []),
                        "status": "BLOCKED-OWNER",
                        "procedure": f"docs/rust-refactor/slice21-owner-runbook.md#{item['id'].lower()}",
                        "evidence": evid})
    report: dict[str, Any] = {
        "schema_version": 1, "run_id": args.run_id,
        "started_at": args.started_at or dt.datetime.now(dt.timezone.utc).isoformat(),
        "git_revision": (_git("rev-parse", "HEAD") or "unknown").strip(), "slice": "2.1",
        "dry_run": bool(args.dry_run),
        "identity": build_identity(args), "sections": sections,
        "pending_decisions": list(expect_yaml.get("pending_decisions") or []),
        "blocked_owner": blocked,
    }
    if args.preflight and pathlib.Path(args.preflight).is_file():
        report["preflight"] = json.loads(pathlib.Path(args.preflight).read_text(encoding="utf-8"))
    if getattr(args, "append_breaker_snapshot", False) and not args.dry_run and expect_yaml.get("breaker_snapshots_file"):
        # OD-60：每次正式實跑附加快照（判定前附加，斷路器以含本次的快照判定）。
        freeze_mod = _load_module("freeze_slice_cases", ROOT / "scripts" / "rust" / "freeze_slice_cases.py")
        snap = append_breaker_snapshot(ROOT / expect_yaml["breaker_snapshots_file"],
                                       freeze_mod.load_effective_freeze(ROOT / expect_yaml["freeze_file"]),
                                       case_verdicts, run_id=args.run_id)
        print(f"slice21_gate_judge: 斷路器快照已附加 {snap}")
    expect, src_where = build_expect(expect_yaml, base_dir=run_dir, case_verdicts=case_verdicts,
                                     daemon_path=args.daemon_path or None, bo_ids=[b["id"] for b in blocked])
    out = judge(report, expect)
    report["sections"] = out["sections"]
    report["case_uptake"] = out["case_uptake"]
    par = out["parity_amendments"]
    par["source_location"] = src_where
    report["parity_amendments"] = par
    report["verdict"] = out["verdict"]
    report["verdict_reasons"] = out["verdict_reasons"]
    report["identity_problems"] = out["identity_problems"]
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"slice21_gate_judge: verdict={report['verdict']} report={run_dir / 'report.json'}")
    for r in report["verdict_reasons"]:
        print(f"  - {r}")
    return 0


def build_expect(expect_yaml: Mapping[str, Any], *, base_dir: pathlib.Path, case_verdicts: Mapping[str, Any],
                 daemon_path: str | None, bo_ids: Sequence[str]) -> tuple[dict[str, Any], str]:
    """`assemble` 與 `judge` 共用的 expect 建構（sol-backfill p5 #8）：鎖、凍結檔、斷路器輸入與 #104 parity
    狀態／來源一律由檔案重新載入並驗證；parity 狀態檔或來源不可得時仍帶入（judge_parity 判 INCOMPLETE），
    不得以「未提供 parity」略過必要條件。回傳 (expect, parity 來源位置)。"""
    freeze_path = ROOT / expect_yaml["freeze_file"]
    # 凍結檔＋擁有者逐名核准的修訂（OD-34 起）；修訂鏈不一致即 ValueError（工具錯誤 exit 2，不得放行）。
    freeze_mod = _load_module("freeze_slice_cases", ROOT / "scripts" / "rust" / "freeze_slice_cases.py")
    freeze = freeze_mod.load_effective_freeze(freeze_path) if freeze_path.is_file() else None
    pcfg = expect_yaml.get("parity") or {}
    status_rel = str(pcfg.get("status_file", "") or "")
    status_file = ROOT / status_rel
    status_doc = _load_yaml(status_file) if status_rel and status_file.is_file() else {}
    src_text, src_where = _parity_source(status_doc or {})
    parity_in = {
        "status": status_doc or {},
        "status_file": status_rel,
        "source_rows": parity_rows_from_doc(src_text) if src_text is not None else None,
        "source_sha256_actual": hashlib.sha256(src_text.encode("utf-8")).hexdigest() if src_text is not None else None,
    }
    lock = json.loads((ROOT / expect_yaml["lock_file"]).read_text(encoding="utf-8"))
    expect = {"lock": lock, "base_dir": base_dir, "freeze": freeze, "case_verdicts": dict(case_verdicts),
              "daemon_path": daemon_path, "parity": parity_in,
              "additional_required_sets": expect_yaml.get("additional_required_sets") or {},
              "bo_ids": list(bo_ids), "breaker": breaker_inputs(expect_yaml),
              "l15_ab_excluded": list(expect_yaml.get("l15_ab_excluded") or [])}
    return expect, src_where


def _load_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cmd_case_verdicts(args: argparse.Namespace) -> int:
    """自 run 目錄的 section 片段與逐案證據彙整 `case_verdicts.json`（＋`case_verdicts.detail.json`）。"""
    run_dir = pathlib.Path(args.run_dir)
    expect_yaml = _load_yaml(pathlib.Path(args.expect))
    freeze_mod = _load_module("freeze_slice_cases", ROOT / "scripts" / "rust" / "freeze_slice_cases.py")
    freeze = freeze_mod.load_effective_freeze(ROOT / expect_yaml["freeze_file"])
    frags = {p.stem: (_load_json(p) or {}) for p in sorted((run_dir / "sections").glob("*.json"))}

    def rows_of(rel: Any) -> list[dict]:
        doc = _load_json(run_dir / str(rel)) if rel else None
        return [r for r in ((doc or {}).get("results") or []) if isinstance(r, dict)] if isinstance(doc, dict) else []

    l15 = frags.get("l15_ab") or {}
    func_checks: dict[str, Any] = {}
    for chk in (frags.get("existing_suites_rust") or {}).get("extra_checks") or []:
        name = str(chk.get("name") or "")
        if name.startswith("func-test:"):
            func_checks[name[len("func-test:"):]] = chk.get("exit")
    section_rows = {name: rows_of(frag.get("l1_results")) for name, frag in frags.items() if frag.get("l1_results")}
    section_status: dict[str, str] = {}
    ev = frags.get("event_latency") or {}
    if ev.get("evidence"):
        doc = _load_json(run_dir / str(ev["evidence"])) or {}
        section_status["event_latency"] = {"PASS": "PASS", "FAIL": "FAIL"}.get(str(doc.get("verdict")), "EVIDENCE_MISSING")
    verdicts, detail = aggregate_case_verdicts(
        [str(x) for x in freeze.get("required_case_ids") or []], l15_rows=rows_of(l15.get("evidence")),
        func_checks=func_checks, excluded=list(expect_yaml.get("l15_ab_excluded") or []),
        section_rows=section_rows, section_status=section_status)
    (run_dir / "case_verdicts.json").write_text(json.dumps(verdicts, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                                                encoding="utf-8")
    (run_dir / "case_verdicts.detail.json").write_text(
        json.dumps(detail, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    summary: dict[str, int] = {}
    for v in verdicts.values():
        summary[v] = summary.get(v, 0) + 1
    print(f"slice21_gate_judge: case_verdicts {summary} → {run_dir / 'case_verdicts.json'}")
    return 0


def cmd_judge(args: argparse.Namespace) -> int:
    report = json.loads(pathlib.Path(args.report).read_text(encoding="utf-8"))
    expect_yaml = _load_yaml(pathlib.Path(args.expect))
    bo_ids = [str(b.get("id")) for b in (report.get("blocked_owner") or []) if isinstance(b, Mapping)]
    case_verdicts = {}
    if args.case_verdicts and pathlib.Path(args.case_verdicts).is_file():
        case_verdicts = json.loads(pathlib.Path(args.case_verdicts).read_text(encoding="utf-8"))
    expect, _ = build_expect(expect_yaml, base_dir=pathlib.Path(args.report).parent, case_verdicts=case_verdicts,
                             daemon_path=args.daemon_path, bo_ids=bo_ids)
    out = judge(report, expect)
    print(json.dumps({"verdict": out["verdict"], "verdict_reasons": out["verdict_reasons"],
                      "sections": {s["name"]: s["status"] for s in out["sections"]}}, ensure_ascii=False, indent=1))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record-section")
    r.add_argument("--run-dir", required=True)
    r.add_argument("--name", required=True, choices=SECTION_ORDER)
    r.add_argument("--command", default="")
    r.add_argument("--exit", default=None)
    r.add_argument("--log", default="")
    r.add_argument("--required-set", default=None)
    r.add_argument("--l1-results", default=None)
    r.add_argument("--section-verdict", default=None, choices=("PASS", "FAIL", "EVIDENCE_MISSING"))
    r.add_argument("--reason", action="append", default=[])
    r.add_argument("--evidence", default=None)
    r.add_argument("--extra-check", action="append", default=[], help="<名稱>=<exit>，可重複")
    a = sub.add_parser("assemble")
    a.add_argument("--run-dir", required=True)
    a.add_argument("--run-id", required=True)
    a.add_argument("--expect", default=str(ROOT / "tests" / "acceptance" / "slice21_gate_expect.yaml"))
    a.add_argument("--started-at", default=None)
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--cli-path", default=None)
    a.add_argument("--daemon-path", default=None)
    a.add_argument("--daemon-version", default=None)
    a.add_argument("--case-verdicts", default=None)
    a.add_argument("--preflight", default=None)
    a.add_argument("--build-linux-exit", type=int, default=None)
    a.add_argument("--build-windows-exit", type=int, default=None)
    a.add_argument("--built-daemon-sha256", default=None)
    a.add_argument("--append-breaker-snapshot", action="store_true",
                   help="正式實跑（非 dry-run）時依 case_verdicts 附加一筆斷路器快照（OD-60）")
    c = sub.add_parser("case-verdicts")
    c.add_argument("--run-dir", required=True)
    c.add_argument("--expect", default=str(ROOT / "tests" / "acceptance" / "slice21_gate_expect.yaml"))
    j = sub.add_parser("judge")
    j.add_argument("--report", required=True)
    j.add_argument("--expect", default=str(ROOT / "tests" / "acceptance" / "slice21_gate_expect.yaml"))
    j.add_argument("--daemon-path", default=None)
    j.add_argument("--case-verdicts", default=None)
    args = p.parse_args(argv)
    try:
        if args.cmd == "record-section":
            return cmd_record_section(args)
        if args.cmd == "assemble":
            return cmd_assemble(args)
        if args.cmd == "case-verdicts":
            return cmd_case_verdicts(args)
        return cmd_judge(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"slice21_gate_judge: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
