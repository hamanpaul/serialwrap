#!/usr/bin/env python3
"""Stage 1A gate 判定器（P3-T18）。

彙整 A/B 比較報告、Windows 測試報告、L1.5 功能測試報告、spike3 Windows WAL 證據與 L1 完整性報告，
對照 cases.lock.json、l1_required.lock.json 與 coverage_1a.json，
判定 Stage 1A 是否滿足 IMPLEMENTED 或 VERIFIED 出口門檻。

S1X 規則（計畫 04 Task 18、§3 B3；owner_gated.py 為擁有者機台列的共用登記）：
- 擁有者機台相依列（`cap_*` 與登記列）在 cargo 報告中為 skipped → 列為 BLOCKED-OWNER（不論是否仍在鎖內），整體至多 IMPLEMENTED。
- 登記列在 selector 涵蓋且本平台會編譯卻整列缺席於報告 → evidence-insufficient（FAIL）。
- pytest JUnit 必須帶 provenance：HEAD＝受測 revision、SW_REQUIRE_CARGO=1、argv 涵蓋 tests/ 且不縮小範圍；否則整份拒收。
- s1_pytest_full（linux，OD-17）：`<l1>/linux/pytest-full/resource-report.json` 與 `--pytest-junit-report` 對帳；缺檔、集合不存在、INSUFFICIENT 或 INVALID → evidence-insufficient，FAIL → FAIL；不新增放行規則。
- spike3（`scripts/stage0/spike3_win_wal.py`）證據依 `spike3_evidence.schema.json` 驗證、case_id 相符、git_head＝受測 HEAD、git_clean、verdict PASS 與 binary_integrity 成立才 PASS；防毒產品名與狀態只是資訊欄位（OD-19），不參與判定。
- Windows COM（OD-15、計畫 04 §3 B1）：可執行 gate。`--windows-com` 缺席、目錄無 `windows-com.json` 或任何驗證不符 → evidence-insufficient（整體 FAIL）；明示 `windows_com: FAIL` → FAIL。不再列為 BLOCKED-OWNER。
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import math
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import owner_gated  # noqa: E402  同目錄登記模組（擁有者機台相依 cargo 列）


def _normalize_case_id(cid: str) -> str:
    """從 JUnit case_id 提取模組與函式名 (例如 test_bb_x.py::test_y)。"""
    if "::" in cid:
        cls, name = cid.split("::", 1)
    else:
        cls, name = "", cid

    name_clean = name.split("[")[0].strip()

    parts = cls.split(".")
    mod_part = None
    for p in parts:
        if p.startswith("test_"):
            mod_part = p
            break
    if mod_part:
        return f"{mod_part}.py::{name_clean}"

    if cls:
        if cls.endswith(".py"):
            return f"{cls}::{name_clean}"
        return f"{cls}::{name_clean}"
    return name_clean


def _l15_case_rows(
    case_id: str,
    meta: dict[str, Any],
    reports: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    platforms = meta.get("platforms")
    allowed_platforms = set(platforms) if isinstance(platforms, list) and platforms else None
    rows: list[dict[str, Any]] = []
    for platform, report in reports:
        if allowed_platforms is not None and platform not in allowed_platforms:
            continue
        results = report.get("results")
        if isinstance(results, list):
            rows.extend(
                {**row, "platform": platform} for row in results
                if isinstance(row, dict) and row.get("case_id") == case_id
            )
    return rows


def _l15_platforms(meta: dict[str, Any]) -> list[str] | None:
    """案例宣告的平台清單（`meta.platforms`）；未宣告回 None，不套用逐平台覆蓋要求。"""
    platforms = meta.get("platforms")
    return [str(p) for p in platforms] if isinstance(platforms, list) and platforms else None


def _l15_row_na_only(row: dict[str, Any]) -> bool:
    """列的唯一缺口是「平台不適用的 not_applicable 段」（其餘條件皆成立）：不阻擋 gate，但也不計為 PASS。"""
    return (
        row.get("verdict") == "INSUFFICIENT_EVIDENCE"
        and row.get("na_only_gap") is True
        and bool(row.get("not_applicable_steps"))
    )


def _l15_case_status(rows: list[dict[str, Any]], platforms: list[str] | None = None) -> str:
    """L15 案例的 gate 狀態（計畫 04 §5c）。

    - 無列 → evidence-insufficient。
    - 傳入 `platforms`（案例宣告的平台）時，每個宣告平台都必須至少有一列（即該平台的報告確實含此案例）；
      缺任一平台 → evidence-insufficient。Linux 列不得替代 Windows 列（S1X：Windows L1.5 producer 須獨立產出）。
      僅 not_applicable 缺口的列仍計入「該平台有列」，其 PASS 與否由下列規則判定。
    - 排除「僅因 not_applicable 段缺口」的列後：任一列 FAIL（或未知 verdict）→ FAIL；
      任一列 INSUFFICIENT_EVIDENCE／SKIP → evidence-insufficient（不得記為 FAIL）。
    - 其餘列全部 PASS 且 gate_eligible，且至少一列 → PASS；否則 evidence-insufficient。
      例如 l15-wal-interference：POSIX 列僅缺 segment (c)（windows 專屬）時，必須由 Windows 列 PASS＋eligible 才成立。
    """
    if not rows:
        return "evidence-insufficient"
    if platforms:
        present = {row.get("platform") for row in rows}
        if any(platform not in present for platform in platforms):
            return "evidence-insufficient"
    blocking = [row for row in rows if not _l15_row_na_only(row)]
    if any(row.get("verdict") not in ("PASS", "INSUFFICIENT_EVIDENCE", "SKIP") for row in blocking):
        return "FAIL"
    if any(row.get("verdict") != "PASS" for row in blocking):
        return "evidence-insufficient"
    if not blocking or any(row.get("gate_eligible") is not True for row in blocking):
        return "evidence-insufficient"
    return "PASS"


def _is_rust_pytest_case(case_id: str) -> bool:
    path, separator, _ = case_id.partition("::")
    return bool(separator) and Path(path).name.startswith("test_rust_") and path.endswith(".py")


PYTEST_PROVENANCE_KEYS = ("HEAD", "SW_REQUIRE_CARGO", "argv", "git_clean", "effective")
# 縮小 pytest 範圍或提早終止的參數；JUnit 若帶其中之一，即非「完整 tests/」執行，整份拒收。
PYTEST_FORBIDDEN_TOKENS = frozenset({
    "-k", "-m", "-x", "--exitfirst", "--lf", "--last-failed", "--ff", "--failed-first",
    "--co", "--collect-only", "--deselect", "--ignore", "--ignore-glob", "--maxfail",
})
PYTEST_FORBIDDEN_PREFIXES = ("-k", "-m", "--deselect=", "--ignore=", "--ignore-glob=", "--maxfail=")
TESTS_TREE_TOKENS = ("tests", "tests/", "./tests", "./tests/")

SPIKE3_CASES = (
    "win-wal-av-rotate", "win-wal-av-unlink", "win-wal-av-reader-handle", "win-wal-f12-progress",
)
SPIKE3_SCHEMA_REL = Path("scripts/stage0/spike3_evidence.schema.json")
# OD-19 (2) 要求寫入延遲有上限；此值須由 owner 裁決後設定，目前不得自行推定。
OD19_AVSIM_WRITE_LATENCY_BOUND_MS: float | None = None


def _git_head(repo_root: Path) -> str | None:
    """受測 revision＝repo_root 的 HEAD；非 git 或無 HEAD 回 None（呼叫端 fail-closed）。"""
    try:
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True,
                             text=True, check=False)
    except OSError:
        return None
    head = res.stdout.strip()
    return head if res.returncode == 0 and head else None


def _junit_provenance_errors(root: ET.Element, expected_head: str | None) -> list[str]:
    """驗證 pytest JUnit 的 provenance properties（由 scripts/rust/junit_provenance.py 寫入）。"""
    props: dict[str, str] = {}
    for prop in root.iter("property"):
        name = prop.get("name")
        if name in PYTEST_PROVENANCE_KEYS and name not in props:
            props[name] = prop.get("value", "")
    errs: list[str] = []
    if not expected_head:
        errs.append("無法取得受測 HEAD（repo_root 非 git 或無 HEAD），pytest JUnit 無法對帳")
    if "HEAD" not in props:
        errs.append("pytest JUnit 缺 HEAD provenance")
    elif expected_head and props["HEAD"] != expected_head:
        errs.append(f"pytest JUnit HEAD 與受測 HEAD 不符：{props['HEAD']!r}")
    if props.get("SW_REQUIRE_CARGO") != "1":
        errs.append(f"pytest JUnit 缺 SW_REQUIRE_CARGO=1 provenance（實得 {props.get('SW_REQUIRE_CARGO')!r}）")
    if props.get("git_clean") != "1":
        errs.append(f"pytest JUnit 非乾淨工作樹（git_clean={props.get('git_clean')!r}）：正式證據須於 commit 後、無未提交或未追蹤變更時執行")
    argv: list[str] | None = None
    if "argv" in props:
        try:
            parsed = json.loads(props["argv"])
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
            argv = parsed
    if argv is None:
        errs.append("pytest JUnit argv provenance 缺漏或格式不符（須為 JSON 字串陣列）")
    else:
        if not any(tok in TESTS_TREE_TOKENS for tok in argv):
            errs.append(f"pytest JUnit argv 未涵蓋 tests/：{argv}")
        bad = [tok for tok in argv if tok in PYTEST_FORBIDDEN_TOKENS or tok.startswith(PYTEST_FORBIDDEN_PREFIXES)]
        if bad:
            errs.append(f"pytest JUnit argv 含縮小範圍或提早終止的參數：{bad}")
    effective: dict[str, Any] | None = None
    if "effective" in props:
        try:
            parsed_eff = json.loads(props["effective"])
        except ValueError:
            parsed_eff = None
        if isinstance(parsed_eff, dict):
            effective = parsed_eff
    if effective is None:
        errs.append("pytest JUnit effective provenance 缺漏或格式不符（須為 JSON 物件）")
    else:
        # 以 pytest 實際生效設定判定（涵蓋 addopts、PYTEST_ADDOPTS、合併短旗標）。
        narrowed = [key for key in ("keyword", "markexpr") if effective.get(key)]
        narrowed += [key for key in ("deselect", "ignore", "ignore_glob") if effective.get(key)]
        narrowed += [key for key in ("collectonly", "lf", "ff") if effective.get(key)]
        if effective.get("maxfail"):
            narrowed.append("maxfail")
        if narrowed:
            errs.append(f"pytest JUnit 生效設定含縮小範圍或提早終止者：{narrowed}")
    return errs


def _junit_totals(path: Path | None, count: Callable[[Path], dict[str, int]] | None) -> dict[str, int] | None:
    """JUnit 的 testcase 層合計（tests／failures／errors／skipped），計數實作為 P2 `check_evidence_index._junit_counts`。

    不讀 testsuite 的 tests 屬性：pytest 9 的 subtests 會計入屬性（tests=N+k）但不產生 testcase 元素，
    與報告列數永遠不符（P2-T13 審查修正；與 l1_judge 的逐列比對採同一計數慣例）。
    缺檔、不可解析，或計數函式未載入（count 為 None，即 P2 模組缺席）一律回 None（fail-closed）。
    """
    if path is None or not path.is_file() or count is None:
        return None
    try:
        return count(path)
    except (ET.ParseError, OSError):
        return None


def _pytest_junit_cases(path: Path | None, errors: list[str],
                        expected_head: str | None = None) -> dict[str, list[str]]:
    cases: dict[str, list[str]] = {}
    if path is None:
        return cases
    if not path.is_file():
        errors.append(f"缺少 pytest JUnit 報告: {path}")
        return cases
    try:
        root = ET.parse(path).getroot()
    except Exception as e:
        errors.append(f"讀取 pytest JUnit 報告失敗: {e}")
        return cases
    provenance = _junit_provenance_errors(root, expected_head)
    if provenance:
        # provenance 不符即整份拒收（不採信其中任何案例結果），避免非 HEAD、非完整 tests/ 的 JUnit 放行安全不變量。
        errors.extend(f"{msg}（{path.name}）" for msg in provenance)
        return cases

    report_failed = False
    for suite in root.iter("testsuite"):
        try:
            if int(suite.attrib.get("failures", 0)) or int(suite.attrib.get("errors", 0)):
                errors.append("pytest JUnit 報告含失敗或錯誤")
                report_failed = True
                break
        except (TypeError, ValueError):
            errors.append("pytest JUnit 報告的失敗計數無效")
            report_failed = True
            break

    testcases = list(root.iter("testcase"))
    if any(testcase.find("failure") is not None or testcase.find("error") is not None for testcase in testcases):
        report_failed = True

    for testcase in testcases:
        nodeid = testcase.get("nodeid")
        if nodeid:
            case_id = _normalize_case_id(nodeid)
            if case_id.startswith("tests/"):
                case_id = case_id[len("tests/"):]
        else:
            case_id = _normalize_case_id(f"{testcase.get('classname', '')}::{testcase.get('name', '')}")
        if not case_id:
            continue
        if report_failed or testcase.find("failure") is not None or testcase.find("error") is not None:
            outcome = "failed"
        elif testcase.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        cases.setdefault(case_id, []).append(outcome)
    return cases


def _pytest_junit_key(nodeid: str) -> str:
    """與 `_pytest_junit_cases` 相同的 case_id 正規化（去除 `tests/` 前綴），供凍結 nodeid 與 JUnit 對應。"""
    case_id = _normalize_case_id(nodeid)
    if case_id.startswith("tests/"):
        case_id = case_id[len("tests/"):]
    return case_id


def _pytest_nodes_junit_problems(nodes: list[str], pytest_cases: dict[str, list[str]]) -> list[str]:
    """凍結 pytest 節點與 provenance 驗證後 JUnit 的逐列對帳；每個節點須恰出現一次且結果為 passed。"""
    if not nodes:
        return ["s1_pytest_full 凍結 nodeids 為空，無法與 pytest JUnit 逐列對帳"]
    problems: list[str] = []
    for nodeid in nodes:
        outcomes = pytest_cases.get(_pytest_junit_key(nodeid), [])
        if outcomes != ["passed"]:
            problems.append(f"pytest JUnit 逐列對帳失敗：{nodeid} 出現 {len(outcomes)} 次，結果 {outcomes}（須恰一次且 passed）")
    return problems


WINDOWS_COM_FORMAT = "windows-com-evidence.v1"
WINDOWS_COM_SEQUENCE_FORMAT = "windows-com-sequence.v1"
WINDOWS_COM_PAIR = "COM20:COM21"
WINDOWS_COM_P4_NODE = "tests/contract_blackbox/test_cli_mgmt_daemon.py::test_autostart_l15_tx_once"
WINDOWS_COM_SEQUENCE = (
    "python-daemon-start", "session-list-ready", "cmd-submit", "cmd-status-poll",
    "device-release", "session-list-released", "device-attach", "session-list-ready",
)
# OD-17：完整 pytest 全套（linux）的 L1 列；報告為 `<l1>/linux/pytest-full/resource-report.json`，junit 為 --pytest-junit-report。
PYTEST_FULL_SET = "s1_pytest_full"


def _dedupe_consecutive(names: list[str]) -> list[str]:
    out: list[str] = []
    for name in names:
        if not out or out[-1] != name:
            out.append(name)
    return out


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _windows_com_verdict(evidence_dir: Path) -> tuple[str, list[str]]:
    """只接受 `<evidence_dir>/windows-com.json`（與 win-blackbox.ps1 的 OutDir 同層）並逐項驗證。

    不採信任何 `verdict`／`windows_com` 頂層欄位以外的後門（`report.json`、`com-selftest.json` 不再參與判定）。
    回傳 (狀態, 原因清單)；狀態為 PASS、FAIL（doc 明示失敗）或 evidence-insufficient（證據缺漏或不符；OD-15 起不再是 BLOCKED-OWNER）。
    """
    doc = _read_json_dict(evidence_dir / "windows-com.json")
    if doc is None:
        return "evidence-insufficient", ["缺少或無法解析 windows-com.json"]
    if doc.get("windows_com") == "FAIL":
        return "FAIL", ["windows-com.json 明示 FAIL"]
    reasons: list[str] = []
    if doc.get("format") != WINDOWS_COM_FORMAT:
        reasons.append(f"format 不符：{doc.get('format')!r}")
    if doc.get("com_pair") != WINDOWS_COM_PAIR:
        reasons.append(f"com_pair 不符：{doc.get('com_pair')!r}")
    if doc.get("windows_com") != "PASS":
        reasons.append(f"windows_com 非 PASS：{doc.get('windows_com')!r}")
    p4 = doc.get("p4_case")
    if not (isinstance(p4, dict) and p4.get("nodeid") == WINDOWS_COM_P4_NODE and p4.get("outcome") == "passed"):
        reasons.append(f"p4_case 非凍結案例 passed：{p4!r}")
    bridge_stop = doc.get("bridge_stop")
    if not (isinstance(bridge_stop, dict) and bridge_stop.get("status") == "PASS"):
        reasons.append("bridge_stop 非 PASS")

    com_dir = evidence_dir / "windows-com"
    # S0 自測：以 OutDir 內相對路徑讀取，不採信 JSON 內的 Windows 絕對路徑。
    selftest = _read_json_dict(com_dir / "bridge-selftest" / "selftest-report.json")
    if selftest is None or selftest.get("verdict") != "PASS":
        reasons.append("bridge-selftest/selftest-report.json 缺漏或 verdict 非 PASS")

    # 序列：步驟依序為凍結清單，允許結尾的 daemon-stop（finally 清理）；所有含 returncode 的步驟必須為 0。
    # 真機 win-blackbox.ps1 寫入 windows-com/sequence.json（format=windows-com-sequence.v1，頂層 result／steps）；
    # sequence-result.json 只是 wrapper 外層（status／report），此處僅核對其 status 為 PASS。
    sequence = _read_json_dict(com_dir / "sequence.json")
    wrapper = _read_json_dict(com_dir / "sequence-result.json")
    if sequence is None or sequence.get("format") != WINDOWS_COM_SEQUENCE_FORMAT or sequence.get("result") != "PASS":
        reasons.append("sequence.json 缺漏、format 不符或 result 非 PASS")
    else:
        if (sequence.get("serialwrap_com"), sequence.get("bridge_com")) != ("COM20", "COM21"):
            reasons.append(
                f"serialwrap_com／bridge_com 不符：{sequence.get('serialwrap_com')!r}／{sequence.get('bridge_com')!r}"
            )
        if wrapper is None or wrapper.get("status") != "PASS":
            reasons.append("sequence-result.json 缺漏或 status 非 PASS")
        if isinstance(sequence.get("steps"), list):
            steps = [s for s in sequence["steps"] if isinstance(s, dict)]
            deduped = _dedupe_consecutive([str(s.get("name")) for s in steps])
            expected = list(WINDOWS_COM_SEQUENCE)
            tail = deduped[len(expected):]
            if deduped[:len(expected)] != expected or tail not in ([], ["daemon-stop"]):
                reasons.append(f"sequence 步驟順序不符：{deduped}")
            bad = [s.get("name") for s in steps if "returncode" in s and s.get("returncode") != 0]
            if bad:
                reasons.append(f"sequence 步驟 returncode 非 0：{bad}")
        else:
            reasons.append("sequence.json 缺少 steps")

    # wire：DUT 端必須有 dut_stdin_write 的 TX，內容含 `echo hi`。
    wire = com_dir / "bridge" / "wire.ndjson"
    found_tx = False
    if wire.is_file():
        for line in wire.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("dir") != "TX" or row.get("observation") != "dut_stdin_write":
                continue
            try:
                payload = base64.b64decode(str(row.get("payload_b64", "")), validate=True)
            except (ValueError, TypeError):
                continue
            if b"echo hi" in payload:
                found_tx = True
                break
    if not found_tx:
        reasons.append("wire.ndjson 缺少 dut_stdin_write TX（echo hi）或檔案不存在")

    return ("PASS" if not reasons else "evidence-insufficient"), reasons


def _spike3_observation_reasons(cid: str, observations: Any, samples: list[Any]) -> list[str]:
    """驗證各 Spike 3 案例的必要觀察，避免 verdict 或通用摘要欄位單獨放行。"""
    if not isinstance(observations, dict):
        return ["spike3 observations 非物件"]

    reasons: list[str] = []

    def int_at_least(key: str, minimum: int) -> int | None:
        value = observations.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            reasons.append(f"observations.{key} 須為 ≥{minimum} 的整數")
            return None
        return value

    def int_equals(key: str, expected: int) -> None:
        value = observations.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value != expected:
            reasons.append(f"observations.{key} 須為整數 {expected}")

    def verify_record_sequence(data_append_count: int | None) -> None:
        records = int_at_least("records", 1)
        meta_count = observations.get("continuity_meta_count")
        if not isinstance(meta_count, int) or isinstance(meta_count, bool) or meta_count < 0:
            reasons.append("observations.continuity_meta_count 須為非負整數")
        elif data_append_count is not None and records is not None and records != data_append_count + meta_count:
            reasons.append("observations.records 須等於資料 append 數加 continuity META 數")
        if observations.get("seq_continuous") is not True:
            reasons.append("observations.seq_continuous 非 true")
        first_seq = observations.get("first_seq")
        if not isinstance(first_seq, int) or isinstance(first_seq, bool) or first_seq != 1:
            reasons.append("observations.first_seq 必須為 1")
        last_seq = observations.get("last_seq")
        if records is not None and (
            not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq != records
        ):
            reasons.append("observations.last_seq 必須等於 records")

    def drain_must_match_daemon() -> None:
        drain = observations.get("drain")
        if not isinstance(drain, dict):
            reasons.append("observations.drain 缺少 daemon 節奏 tick 記錄")
            return
        interval = drain.get("tick_interval_s")
        finite_interval = (
            isinstance(interval, (int, float))
            and not isinstance(interval, bool)
            and (not isinstance(interval, float) or math.isfinite(interval))
        )
        if not finite_interval or interval != 1.0:
            reasons.append("observations.drain.tick_interval_s 必須等於 daemon 的 1.0 秒週期")
        ticks = drain.get("ticks")
        if not isinstance(ticks, int) or isinstance(ticks, bool) or ticks < 1:
            reasons.append("observations.drain.ticks 須為正整數")
        if drain.get("drained") is not True:
            reasons.append("observations.drain.drained 非 true")

    if cid == "win-wal-av-rotate":
        expected = int_at_least("expected_records", 1)
        archives = int_at_least("archives", 1)
        retries = int_at_least("rotate_retry", 1)
        append_count = observations.get("append_count")
        if not isinstance(append_count, int) or isinstance(append_count, bool) or append_count < 1:
            reasons.append("observations.append_count 須為正整數")
            append_count = None
        if expected is not None and append_count is not None and append_count != expected:
            reasons.append("observations.append_count 與 expected_records 不相等")
        if expected is not None and len(samples) != expected:
            reasons.append("append_latency_ms 樣本數與 expected_records 不相等")
        verify_record_sequence(append_count)
        int_equals("deferred_delete_pending", 0)
        if observations.get("interference_observed") is not True:
            reasons.append("observations.interference_observed 非 true")
        if archives is not None and retries is not None:
            drain_must_match_daemon()
    elif cid == "win-wal-av-unlink":
        attempts = int_at_least("unlink_attempts", 2)
        sharing_violations = int_at_least("sharing_violation_attempts", 1)
        append_attempts = int_at_least("append_attempts_until_meta", 1)
        metas = int_at_least("meta_count", 1)
        if attempts is not None and sharing_violations is not None and attempts <= sharing_violations:
            reasons.append("observations.unlink_attempts 須包含共享違規後的成功重試")
        if observations.get("interference_observed") is not True:
            reasons.append("observations.interference_observed 非 true")
        if observations.get("unlinked") is not True:
            reasons.append("observations.unlinked 非 true")
        if observations.get("loss_flag") is not True:
            reasons.append("observations.loss_flag 非 true")
        range_known = observations.get("range_known")
        lost_range = observations.get("potentially_lost_seq_range")
        if not isinstance(range_known, bool):
            reasons.append("observations.range_known 必須為 boolean")
        elif range_known:
            if (
                not isinstance(lost_range, list)
                or len(lost_range) != 2
                or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in lost_range)
                or lost_range[0] > lost_range[1]
            ):
                reasons.append("observations.potentially_lost_seq_range 須是遞增的正整數二元組")
        elif lost_range is not None:
            reasons.append("range_known=false 時 potentially_lost_seq_range 必須為 null")
        if append_attempts is not None and len(samples) != 10 + append_attempts:
            reasons.append("append_latency_ms 樣本數與 unlink 前置及 META 重建 append 次數不符")
        if metas is not None and metas < 1:
            reasons.append("observations.meta_count 須至少一筆")
    elif cid == "win-wal-av-reader-handle":
        appended = int_at_least("appended", 2)
        append_count = observations.get("append_count")
        if not isinstance(append_count, int) or isinstance(append_count, bool) or append_count < 1:
            reasons.append("observations.append_count 須為正整數")
            append_count = None
        if appended is not None and append_count is not None and appended != append_count:
            reasons.append("observations.appended 與 append_count 不相等")
        if appended is not None and len(samples) != appended:
            reasons.append("append_latency_ms 樣本數與 appended 不相等")
        verify_record_sequence(append_count)
        if observations.get("rotation_failed_seen") is not True:
            reasons.append("observations.rotation_failed_seen 非 true")
        int_equals("write_failures", 0)
        int_equals("deferred_delete_pending", 0)
        if observations.get("interference_observed") is not True:
            reasons.append("observations.interference_observed 非 true")
        drain_must_match_daemon()
    elif cid == "win-wal-f12-progress":
        if len(samples) < 3:
            reasons.append("append_latency_ms 樣本數不足以涵蓋 F12 前後 marker 與寫入")
        append_count = observations.get("append_count")
        if not isinstance(append_count, int) or isinstance(append_count, bool) or append_count < 1:
            reasons.append("observations.append_count 須為正整數")
            append_count = None
        if append_count is not None and len(samples) != append_count:
            reasons.append("append_latency_ms 樣本數與 append_count 不相等")
        archives = int_at_least("archives", 1)
        retries = int_at_least("rotate_retry", 1)
        verify_record_sequence(append_count)
        int_equals("deferred_delete_pending", 0)
        for key in ("pre_rotate", "post_rotate"):
            result = observations.get(key)
            if not isinstance(result, dict) or result.get("ok") is not True:
                reasons.append(f"observations.{key}.ok 非 true")
        if observations.get("crc_errors") != []:
            reasons.append("observations.crc_errors 必須是空陣列")
        if observations.get("interference_observed") is not True:
            reasons.append("observations.interference_observed 非 true")
        if archives is not None and retries is not None:
            drain_must_match_daemon()
    else:
        reasons.append(f"未知的 spike3 case_id：{cid!r}")
    return reasons


def _spike3_status(cid: str, spike_dir: Path | None, schema_path: Path,
                   head: str | None) -> tuple[str, list[str]]:
    """spike3 證據 → (狀態, 原因)。

    PASS 還要求有有效 append 延遲樣本，且 owner 已裁決的上限未超出；缺樣本為 evidence-insufficient，
    上限未裁決時為 BLOCKED-OWNER。EDR 名稱與狀態只是資訊欄位，不參與判定。
    """
    if spike_dir is None:
        return "evidence-insufficient", ["未提供 --spike3-evidence"]
    path = spike_dir / f"{cid}.json"
    if not path.is_file():
        return "evidence-insufficient", [f"缺少 spike3 證據 {path.name}"]
    doc = _read_json_dict(path)
    if doc is None:
        return "evidence-insufficient", ["spike3 證據無法解析"]
    try:
        import jsonschema

        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        jsonschema.validate(instance=doc, schema=schema)
    except ImportError:
        return "evidence-insufficient", ["缺 jsonschema，無法驗證 spike3 證據"]
    except Exception as exc:  # noqa: BLE001 — 任何驗證失敗都只能是證據不足
        return "evidence-insufficient", [f"spike3 schema 驗證失敗：{getattr(exc, 'message', exc)}"]
    if doc.get("case_id") != cid:
        return "evidence-insufficient", [f"spike3 case_id 不符：{doc.get('case_id')!r}"]
    git_head = (doc.get("artifact") or {}).get("git_head")
    if head is None or git_head != head:
        return "evidence-insufficient", [f"spike3 git_head 與受測 HEAD 不符：{git_head!r}"]
    if (doc.get("artifact") or {}).get("git_clean") is not True:
        return "evidence-insufficient", ["spike3 證據非乾淨工作樹產出（artifact.git_clean 非 true）"]
    verdict = doc.get("verdict")
    if verdict == "FAIL":
        return "FAIL", ["spike3 verdict 為 FAIL"]
    if verdict != "PASS":
        return "evidence-insufficient", [f"spike3 verdict={verdict!r}，非 PASS"]
    integrity = doc.get("binary_integrity") or {}
    subjects = integrity.get("subjects")
    if not isinstance(subjects, list) or not subjects:
        return "evidence-insufficient", ["spike3 binary_integrity.subjects 為空：無執行檔前後比對證據"]
    bad_subjects: list[str] = []
    for item in subjects:
        if not isinstance(item, dict):
            bad_subjects.append("非物件")
            continue
        sha_b = item.get("sha256_before")
        sha_a = item.get("sha256_after")
        hex_ok = (isinstance(sha_b, str) and len(sha_b) == 64 and all(c in "0123456789abcdef" for c in sha_b)
                  and isinstance(sha_a, str) and len(sha_a) == 64 and all(c in "0123456789abcdef" for c in sha_a))
        if item.get("exists_before") is not True or item.get("exists_after") is not True:
            bad_subjects.append(f"{item.get('path')!r} 前後存在性非 true")
        elif not hex_ok or sha_b != sha_a or item.get("unchanged") is not True:
            bad_subjects.append(f"{item.get('path')!r} sha256 前後不一致或格式不合")
    if bad_subjects:
        return "evidence-insufficient", ["spike3 binary_integrity.subjects 不合格：" + "；".join(bad_subjects)]
    if integrity.get("unchanged") is not True:
        return "evidence-insufficient", ["spike3 binary_integrity.unchanged 非 true（執行檔前後不一致）"]
    if integrity.get("launch_refused") is not False:
        return "evidence-insufficient", ["spike3 binary_integrity.launch_refused 非 false（啟動被拒）"]
    av = doc.get("av_sim") or {}
    if av.get("launch_error") != "none":
        return "evidence-insufficient", [f"spike3 av_sim.launch_error={av.get('launch_error')!r}，非 none（模擬防毒 helper 未成功啟動）"]
    helper_exit = av.get("helper_exit")
    if not isinstance(helper_exit, int) or isinstance(helper_exit, bool) or helper_exit != 0:
        return "evidence-insufficient", [f"spike3 av_sim.helper_exit={helper_exit!r}，helper 須以 exit 0 完成"]
    grabs = av.get("grabs")
    if not isinstance(grabs, int) or isinstance(grabs, bool) or grabs < 1:
        return "evidence-insufficient", [f"spike3 av_sim.grabs={grabs!r}，須為 ≥1 的整數（模擬防毒未實際佔用任何檔案）"]
    # OD-19 (2) 明文要求含 oplock 變體；未實作（not_implemented）即不得讀成驗收全數滿足。
    oplock = av.get("oplock")
    if oplock not in ("granted", "denied", "unavailable"):
        return "evidence-insufficient", [
            f"oplock_not_covered：av_sim.oplock={oplock!r}，OD-19 (2) 之 oplock 變體未涵蓋（PENDING-OD-S1X-AVSIM-OPLOCK）"
        ]
    samples = (doc.get("observations") or {}).get("append_latency_ms")
    if not isinstance(samples, list) or not samples:
        return "evidence-insufficient", ["spike3 缺少 append 延遲樣本（observations.append_latency_ms）"]
    if any(
        isinstance(sample, bool)
        or not isinstance(sample, (int, float))
        or (isinstance(sample, float) and not math.isfinite(sample))
        or sample < 0
        for sample in samples
    ):
        return "evidence-insufficient", ["spike3 append 延遲樣本須為有限且非負的毫秒數"]
    for sample in samples:
        if isinstance(sample, int):
            try:
                finite_sample = math.isfinite(float(sample))
            except OverflowError:
                finite_sample = False
            if not finite_sample:
                return "evidence-insufficient", ["spike3 append 延遲樣本超出有限數值範圍"]
    observations = doc.get("observations")
    append_count = observations.get("append_count") if isinstance(observations, dict) else None
    if not isinstance(append_count, int) or isinstance(append_count, bool) or append_count < 1:
        return "evidence-insufficient", ["spike3 observations.append_count 須為正整數"]
    if append_count != len(samples):
        return "evidence-insufficient", [
            f"spike3 append_count={append_count} 與 append_latency_ms 樣本數 {len(samples)} 不符"
        ]
    observation_reasons = _spike3_observation_reasons(cid, observations, samples)
    if observation_reasons:
        return "evidence-insufficient", observation_reasons
    max_latency_ms = max(samples)
    if OD19_AVSIM_WRITE_LATENCY_BOUND_MS is None:
        return "BLOCKED-OWNER", [
            "PENDING-OD-S1X-AVSIM-LATENCY：owner 尚未裁決最大 append 延遲上限 "
            f"（本次最大值 {max_latency_ms:.6f} ms）"
        ]
    if max_latency_ms > OD19_AVSIM_WRITE_LATENCY_BOUND_MS:
        return "FAIL", [
            f"最大 append 延遲 {max_latency_ms:.6f} ms 超過 owner 上限 "
            f"{OD19_AVSIM_WRITE_LATENCY_BOUND_MS:.6f} ms"
        ]
    return "PASS", []


def judge_stage1a(
    ab_report_path: Path | None,
    l1_results_dir: Path,
    out_path: Path,
    win_report_path: Path | None = None,
    l15_report_inputs: list[tuple[str, Path]] | None = None,
    pytest_junit_path: Path | None = None,
    windows_com_dir: Path | None = None,
    repo_root: Path | None = None,
    spike3_dir: Path | None = None,
) -> int:
    if repo_root is None:
        repo_root = Path(__file__).resolve().parents[2]
    head = _git_head(repo_root)

    acc_dir = repo_root / "tests" / "acceptance"
    cases_yaml_path = acc_dir / "cases.yaml"
    cases_lock_path = acc_dir / "cases.lock.json"
    l1_lock_path = acc_dir / "l1_required.lock.json"
    coverage_path = repo_root / "tests" / "contract_blackbox" / "data" / "coverage_1a.json"

    errors: list[str] = []

    # 1. 分母（cases.lock.json 與 coverage_1a.json）
    if not cases_lock_path.is_file():
        errors.append(f"cases.lock.json 不存在: {cases_lock_path}")
        cli_1a_cases: list[str] = []
    else:
        try:
            lock_data = json.loads(cases_lock_path.read_text(encoding="utf-8"))
            cli_1a_cases = lock_data.get("sets", {}).get("cli_1A", [])
            if not isinstance(cli_1a_cases, list) or not cli_1a_cases:
                errors.append("cases.lock.json 缺少 sets.cli_1A 或清單為空")
        except Exception as e:
            errors.append(f"讀取 cases.lock.json 失敗: {e}")
            cli_1a_cases = []

    if not coverage_path.is_file():
        errors.append(f"coverage_1a.json 不存在: {coverage_path}")
        cov_data: dict[str, Any] | None = None
        p3_extra: list[str] = []
    else:
        try:
            cov_data = json.loads(coverage_path.read_text(encoding="utf-8"))
            p3_extra = cov_data.get("p3_extra", []) if isinstance(cov_data, dict) else []
        except Exception as e:
            errors.append(f"讀取 coverage_1a.json 失敗: {e}")
            cov_data = None
            p3_extra = []

    required_cases = sorted(set(cli_1a_cases) | set(p3_extra))
    if not required_cases:
        errors.append("Stage 1A 必要案例集合為空，證據不足")

    # 2. cases.yaml 元資料
    case_meta: dict[str, dict[str, Any]] = {}
    if cases_yaml_path.is_file():
        try:
            cases_doc = yaml.safe_load(cases_yaml_path.read_text(encoding="utf-8")) or {}
            c_list = cases_doc.get("cases", []) if isinstance(cases_doc, dict) else []
            for c in c_list:
                if isinstance(c, dict) and "case_id" in c:
                    case_meta[c["case_id"]] = c
        except Exception as e:
            errors.append(f"讀取 cases.yaml 失敗: {e}")

    # 3. ab-report
    ab_cases: dict[str, dict[str, Any]] = {}
    ab_cases_by_norm: dict[str, list[dict[str, Any]]] = {}
    ab_result = "fail"
    if ab_report_path and ab_report_path.is_file():
        try:
            ab_data = json.loads(ab_report_path.read_text(encoding="utf-8"))
            ab_result = ab_data.get("result", "fail")
            for c in ab_data.get("cases", []):
                if isinstance(c, dict) and "id" in c:
                    ab_cases[c["id"]] = c
                    norm = c.get("case") or _normalize_case_id(c["id"])
                    ab_cases_by_norm.setdefault(norm, []).append(c)
        except Exception as e:
            errors.append(f"讀取 ab-report 失敗: {e}")
    else:
        errors.append("缺少 ab-report")

    # 4. L1.5 報告依平台標記；缺檔或格式錯誤不提供可通過的案例證據。
    l15_reports: list[tuple[str, dict[str, Any]]] = []
    for platform, report_path in l15_report_inputs or []:
        if not report_path.is_file():
            errors.append(f"缺少 {platform} l15-report: {report_path}")
            continue
        try:
            report_data = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(report_data, dict):
                errors.append(f"{platform} l15-report 格式錯誤: {report_path}")
                continue
            # F7：部分報告（逾時或崩潰留下、每案中途寫出）不得與完整報告等同；缺 complete=true 即證據不足。
            if report_data.get("complete") is not True:
                errors.append(f"{platform} l15-report 不完整（complete 非 true，視為證據不足）: {report_path}")
                continue
            l15_reports.append((platform, report_data))
        except Exception as e:
            errors.append(f"讀取 {platform} l15-report 失敗: {e}")

    pytest_cases = _pytest_junit_cases(pytest_junit_path, errors, head)

    # 5. Windows COM：--windows-com 指向 OutDir；逐項驗證 windows-com.json 與其引用的子證據。
    # OD-15：Windows COM 為可執行 gate，缺席即證據不足（FAIL），不是 BLOCKED-OWNER。
    windows_com_reasons: list[str] = []
    if windows_com_dir and windows_com_dir.is_dir():
        windows_com, windows_com_reasons = _windows_com_verdict(windows_com_dir)
    else:
        windows_com = "evidence-insufficient"
        windows_com_reasons = ["未提供 Windows COM 證據目錄（--windows-com）"]

    # 6. Windows L1
    if win_report_path and win_report_path.is_file():
        try:
            win_data = json.loads(win_report_path.read_text(encoding="utf-8"))
            if not isinstance(win_data, dict):
                windows_l1 = "evidence-insufficient"
            elif "result" in win_data:
                # win-blackbox.sh 格式
                imports_ok = win_data.get("imports", {}).get("ok") if isinstance(win_data.get("imports"), dict) else True
                startup_ok = win_data.get("startup_100", {}).get("ok") if isinstance(win_data.get("startup_100"), dict) else True
                if win_data.get("result") == "pass" and imports_ok is not False and startup_ok is not False:
                    windows_l1 = "PASS"
                elif win_data.get("result") == "fail":
                    windows_l1 = "FAIL"
                else:
                    windows_l1 = "evidence-insufficient"
            elif "overall" in win_data:
                # win-test.sh 格式
                if win_data.get("overall") == "pass" and win_data.get("l1_verdict") == "PASS":
                    windows_l1 = "PASS"
                elif win_data.get("overall") == "fail" or win_data.get("l1_verdict") == "FAIL":
                    windows_l1 = "FAIL"
                else:
                    windows_l1 = "evidence-insufficient"
            elif "exit" in win_data and "failed" in win_data:
                # Oracle fixture 格式 (test_rust_stage1a_gate.py)
                if win_data.get("exit") == 0 and win_data.get("failed") == 0:
                    windows_l1 = "PASS"
                else:
                    windows_l1 = "FAIL"
            else:
                windows_l1 = "evidence-insufficient"
        except Exception as e:
            windows_l1 = "evidence-insufficient"
            errors.append(f"讀取 win-report 失敗: {e}")
    else:
        windows_l1 = "evidence-insufficient"

    # 7. 逐 case 對帳
    case_rows: list[dict[str, Any]] = []
    for cid in required_cases:
        meta = case_meta.get(cid, {})
        layer = meta.get("layer", "L1")
        evidence = None

        if layer == "L1":
            source_ref = meta.get("source_ref")
            prefix = "tests/contract_blackbox/"
            if isinstance(source_ref, str) and source_ref.startswith(prefix):
                source_case = source_ref[len(prefix):]
                matching_rows = ab_cases_by_norm.get(source_case, [])
                if not matching_rows:
                    status = "evidence-insufficient"
                elif all(r.get("class") in ("ok", "rust-only-ok") for r in matching_rows):
                    status = "ok"
                    evidence = matching_rows
                else:
                    failing = [r for r in matching_rows if r.get("class") not in ("ok", "rust-only-ok")]
                    status = failing[0].get("class", "regression") if failing else "evidence-insufficient"
                    evidence = matching_rows
            elif cid in ab_cases:
                abc = ab_cases[cid]
                status = abc.get("class", "evidence-insufficient")
                evidence = abc
            elif cid in ab_cases_by_norm:
                matching_rows = ab_cases_by_norm[cid]
                if all(r.get("class") in ("ok", "rust-only-ok") for r in matching_rows):
                    status = "ok"
                    evidence = matching_rows
                else:
                    failing = [r for r in matching_rows if r.get("class") not in ("ok", "rust-only-ok")]
                    status = failing[0].get("class", "regression") if failing else "evidence-insufficient"
                    evidence = matching_rows
            else:
                status = "evidence-insufficient"
        elif layer == "L1.5" and cid in SPIKE3_CASES:
            # spike3 產物不是 func-test 報告；只接受 spike3_evidence.schema.json 合格、git_head＝HEAD 的證據。
            status, spike_reasons = _spike3_status(cid, spike3_dir, repo_root / SPIKE3_SCHEMA_REL, head)
            evidence = {"source": "spike3", "reasons": spike_reasons}
        elif layer == "L1.5":
            case_l15 = _l15_case_rows(cid, meta, l15_reports)
            status = _l15_case_status(case_l15, _l15_platforms(meta))
            evidence = case_l15 or None
        elif layer in ("L2", "L3"):
            has_l15_docker_row = any(
                report.get("evidence_layer") == "L1.5-docker"
                and _l15_case_rows(cid, meta, [(platform, report)])
                for platform, report in l15_reports
            )
            if has_l15_docker_row:
                status = "evidence-insufficient"
            else:
                status = "BLOCKED-OWNER"
        else:
            status = "evidence-insufficient"

        case_rows.append(
            {
                "id": cid,
                "layer": layer,
                "status": status,
                "evidence": evidence,
            }
        )

    # 8. coverage_1a.json 檢查
    coverage_rows: list[dict[str, Any]] = []
    coverage_ok = True
    if not cov_data or not isinstance(cov_data, dict) or not cov_data.get("rows") or not cov_data.get("invariants"):
        errors.append("coverage_1a.json 為空或缺少必要 rows/invariants")
        coverage_ok = False
    else:
        c_rows = cov_data.get("rows", [])
        if len(c_rows) < 55:
            errors.append(f"coverage_1a.json rows 數量不足: {len(c_rows)} < 55")
            coverage_ok = False

        for row in c_rows:
            cmd = row.get("command", "")
            is_data_plane = row.get("data_plane", False)
            stubs = row.get("stub", [])
            e2e = row.get("e2e", [])
            l15 = row.get("l15", [])

            row_status = "ok"
            if not stubs:
                row_status = "evidence-insufficient"
                coverage_ok = False
            else:
                for sc in stubs:
                    sc_ok = (sc in ab_cases and ab_cases[sc].get("class") in ("ok", "rust-only-ok")) or \
                            (sc in ab_cases_by_norm and all(r.get("class") in ("ok", "rust-only-ok") for r in ab_cases_by_norm[sc]))
                    if not sc_ok:
                        row_status = "evidence-insufficient"
                        coverage_ok = False
                        break

            if is_data_plane:
                has_e2e_or_l15 = False
                for ec in e2e:
                    if (ec in ab_cases and ab_cases[ec].get("class") in ("ok", "rust-only-ok")) or \
                       (ec in ab_cases_by_norm and all(r.get("class") in ("ok", "rust-only-ok") for r in ab_cases_by_norm[ec])):
                        has_e2e_or_l15 = True
                        break
                if not has_e2e_or_l15:
                    for lc in l15:
                        l15_meta = case_meta.get(lc, {})
                        if _l15_case_status(_l15_case_rows(lc, l15_meta, l15_reports), _l15_platforms(l15_meta)) == "PASS":
                            has_e2e_or_l15 = True
                            break

                if not has_e2e_or_l15:
                    row_status = "evidence-insufficient"
                    coverage_ok = False

            coverage_rows.append(
                {
                    "id": row.get("id"),
                    "command": cmd,
                    "status": row_status,
                    "stub": stubs,
                    "e2e": e2e,
                    "l15": l15,
                }
            )

    # 9. 安全不變量檢查
    cargo_pass_nodes = set()
    linux_cargo_rep = l1_results_dir / "linux" / "cargo" / "resource-report.json"
    if linux_cargo_rep.is_file():
        try:
            c_doc = json.loads(linux_cargo_rep.read_text(encoding="utf-8"))
            for r in c_doc.get("results", []):
                if isinstance(r, dict) and r.get("outcome") == "passed":
                    cargo_pass_nodes.add(r.get("nodeid", ""))
        except Exception:
            pass

    invariant_rows: list[dict[str, Any]] = []
    invariants_ok = True
    if cov_data:
        inv_list = cov_data.get("invariants", [])
        if len(inv_list) < 5:
            errors.append(f"安全不變量數量不足: {len(inv_list)} < 5")
            invariants_ok = False

        has_rust_pytest_cases = any(
            _is_rust_pytest_case(case_id)
            for inv in inv_list
            for case_id in inv.get("cases", [])
        )
        if has_rust_pytest_cases and pytest_junit_path is None:
            errors.append("安全不變量含 test_rust_ pytest 案例，但缺少 pytest JUnit 報告")

        for inv in inv_list:
            inv_id = inv.get("id")
            title = inv.get("title")
            inv_cases = inv.get("cases", [])
            missing_or_failed = []

            if not inv_cases:
                missing_or_failed.append("<empty-cases>")

            for c in inv_cases:
                passed = False
                if _is_rust_pytest_case(c):
                    outcomes = pytest_cases.get(_normalize_case_id(c), [])
                    passed = bool(outcomes) and all(outcome == "passed" for outcome in outcomes)
                elif (c in ab_cases and ab_cases[c].get("class") in ("ok", "rust-only-ok")) or \
                   (c in ab_cases_by_norm and all(r.get("class") in ("ok", "rust-only-ok") for r in ab_cases_by_norm[c])):
                    passed = True
                elif any(node.endswith(c) or ("::" in c and node.endswith(c.split("::")[-1])) for node in cargo_pass_nodes):
                    passed = True
                elif _l15_case_status(
                    _l15_case_rows(c, case_meta.get(c, {}), l15_reports), _l15_platforms(case_meta.get(c, {}))
                ) == "PASS":
                    passed = True
                if not passed:
                    missing_or_failed.append(c)

            if missing_or_failed:
                inv_status = "FAIL"
                invariants_ok = False
            else:
                inv_status = "PASS"

            invariant_rows.append(
                {
                    "id": inv_id,
                    "title": title,
                    "status": inv_status,
                    "cases": inv_cases,
                    "missing_or_failed": missing_or_failed,
                }
            )

    # 10. L1 完整性檢查（R5-F3）
    l1_rows: list[dict[str, Any]] = []
    l1_ok = True
    l1_blocked = False
    owner_rows: list[dict[str, Any]] = []

    if not l1_lock_path.is_file():
        errors.append(f"l1_required.lock.json 不存在: {l1_lock_path}")
        l1_ok = False
    else:
        try:
            l1_lock = json.loads(l1_lock_path.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"讀取 l1_required.lock.json 失敗: {e}")
            l1_lock = {}
            l1_ok = False

        if l1_ok:
            judge_mod = None
            judge_script = acc_dir / "l1_judge.py"
            if judge_script.is_file():
                try:
                    spec = importlib.util.spec_from_file_location("l1_judge", judge_script)
                    if spec and spec.loader:
                        judge_mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(judge_mod)
                except Exception as e:
                    errors.append(f"載入 l1_judge.py 失敗: {e}")
                    judge_mod = None

            # junit 計數唯一實作：P2 check_evidence_index._junit_counts（testcase 層）；依路徑載入、不另寫一份。
            # 載入失敗記入 errors（整體 FAIL）；junit_counter 為 None 時 pytest-full 與黑箱計數一律 fail-closed。
            junit_counter: Callable[[Path], dict[str, int]] | None = None
            index_script = acc_dir / "check_evidence_index.py"
            try:
                spec = importlib.util.spec_from_file_location("check_evidence_index", index_script)
                if spec is None or spec.loader is None:
                    raise ImportError(f"無法建立模組規格: {index_script}")
                index_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(index_mod)
                junit_counter = index_mod._junit_counts
            except Exception as e:
                errors.append(f"載入 check_evidence_index.py（junit 計數）失敗: {e}")

            check_targets = [
                ("s1a_blackbox", "linux", "blackbox"),
                ("s1a_blackbox", "windows", "blackbox"),
                ("s1a_delegation", "linux", "blackbox"),
                ("s1a_delegation", "windows", "blackbox"),
                ("s1a_cargo", "linux", "cargo"),
                ("s1a_cargo", "windows", "cargo"),
                (PYTEST_FULL_SET, "linux", "pytest-full"),
            ]

            for set_id, platform, subfolder in check_targets:
                set_info = (
                    l1_lock.get("sets", {}).get(set_id, {}).get(platform)
                )
                if not set_info:
                    l1_rows.append(
                        {
                            "set_id": set_id,
                            "platform": platform,
                            "verdict": "evidence-insufficient",
                            "reasons": [f"set {set_id} / {platform} not in lock"],
                            "missing": [],
                            "skipped_required": [],
                            "duplicates": [],
                        }
                    )
                    l1_ok = False
                    continue

                rep_file = l1_results_dir / platform / subfolder / "resource-report.json"
                if not rep_file.is_file():
                    l1_rows.append(
                        {
                            "set_id": set_id,
                            "platform": platform,
                            "verdict": "evidence-insufficient",
                            "reasons": [f"report file missing: {rep_file}"],
                            "missing": [],
                            "skipped_required": [],
                            "duplicates": [],
                        }
                    )
                    l1_ok = False
                    continue

                try:
                    rep = json.loads(rep_file.read_text(encoding="utf-8"))
                except Exception as e:
                    l1_rows.append(
                        {
                            "set_id": set_id,
                            "platform": platform,
                            "verdict": "evidence-insufficient",
                            "reasons": [f"report file unparseable: {e}"],
                            "missing": [],
                            "skipped_required": [],
                            "duplicates": [],
                        }
                    )
                    l1_ok = False
                    continue

                required_nodes = list(set_info.get("nodeids", []))
                lock_sel = set_info.get("selector", {})

                # S1X：cargo 報告的擁有者機台相依列（cap_*／登記列）→ BLOCKED-OWNER 列舉；不因不在鎖內而消失。
                owner_info: dict[str, Any] | None = None
                if subfolder == "cargo" and rep.get("runner") == "cargo":
                    owner_info = owner_gated.scan_cargo_report(rep, platform)
                    for nid in owner_info["owner_gated_skipped"]:
                        owner_rows.append({
                            "set_id": set_id, "platform": platform, "nodeid": nid,
                            "in_lock": nid in set(required_nodes),
                        })

                judged: dict[str, Any] = {}
                if judge_mod and hasattr(judge_mod, "judge"):
                    junit_dict = None
                    junit_error: str | None = None
                    if subfolder == "blackbox":
                        # 與 pytest-full 同一計數慣例（testcase 層）；缺檔或不可解析為 None，維持原本「不做 junit 交叉核對」的語意。
                        try:
                            junit_dict = _junit_totals(l1_results_dir / platform / subfolder / "junit.xml", junit_counter)
                        except ValueError as exc:
                            junit_error = str(exc)
                    elif subfolder == "pytest-full":
                        # OD-17：與 --pytest-junit-report（同一次全套執行）對帳，見下方 strict 規則。
                        try:
                            junit_dict = _junit_totals(pytest_junit_path, junit_counter)
                        except ValueError as exc:
                            junit_error = str(exc)
                    judged = judge_mod.judge(rep, required_nodes, platform=platform, junit=junit_dict, selector=lock_sel)
                    v_raw = judged.get("verdict", "INVALID")
                    # OD-17：s1_pytest_full 的 INSUFFICIENT／INVALID 與缺 JUnit 一律為證據不足（不得 PASS）；
                    # 本列不新增放行規則，FAIL 仍為 FAIL。其他列維持原映射。
                    strict = set_id == PYTEST_FULL_SET
                    # 逐列對帳：totals 相同的過期報告不得與新 JUnit 湊成 PASS；每個必要 nodeid 須在通過 provenance
                    # 驗證的 JUnit（pytest_cases，已排除 provenance 不符者）中恰出現一次且為 passed。
                    junit_row_problems: list[str] = []
                    if strict and v_raw == "PASS" and junit_dict is not None:
                        junit_row_problems = _pytest_nodes_junit_problems(required_nodes, pytest_cases)
                    if v_raw == "PASS" and junit_error is None and (junit_dict is not None or not strict) and not junit_row_problems:
                        verdict = "PASS"
                    elif v_raw == "FAIL":
                        verdict = "FAIL"
                    elif v_raw == "INSUFFICIENT" and not strict:
                        verdict = "INSUFFICIENT"
                    else:
                        verdict = "evidence-insufficient"
                    reasons = list(judged.get("reasons", []))
                    if junit_error is not None:
                        reasons.append(f"JUnit suite 宣告與 testcase 計數不一致：{junit_error}")
                    if strict and v_raw == "PASS" and junit_dict is None:
                        reasons.append("pytest JUnit 缺漏或不可解析（s1_pytest_full 須與 --pytest-junit-report 對帳）")
                    reasons.extend(junit_row_problems)
                    missing = list(judged.get("missing", []))
                    skipped_required = list(judged.get("skipped_required", []))
                    duplicates = list(judged.get("duplicates", []))
                else:
                    # 計畫 R5-F3：L1 判定一律依路徑載入 P2 Task 13 的 l1_judge.py（唯一判定實作），
                    # 不得自寫判定代替；缺檔或無 judge() 時該列記證據不足，不得轉 PASS。
                    verdict = "evidence-insufficient"
                    reasons = [f"l1_judge.py 缺少或無 judge()（P2 Task 13 前置缺口）: {judge_script}"]
                    missing = []
                    skipped_required = []
                    duplicates = []

                if owner_info is not None:
                    owner_skipped = owner_info["owner_gated_skipped"]
                    if verdict == "INSUFFICIENT" and owner_gated.insufficiency_owner_only(judged, owner_skipped):
                        # judge 的 INSUFFICIENT 僅因擁有者列未執行（鎖內 cap 列）→ BLOCKED-OWNER（非 PASS）。
                        verdict = "BLOCKED-OWNER"
                        reasons = reasons + ["僅因擁有者機台相依列未執行（BLOCKED-OWNER）"]
                    elif verdict == "PASS" and owner_skipped:
                        verdict = "BLOCKED-OWNER"
                        reasons = reasons + [f"擁有者機台相依列未執行：{owner_skipped}"]
                    if owner_info["absent_expected"]:
                        # 登記列在 selector 涵蓋範圍內卻整列缺席 → 收集失效，不得以 BLOCKED-OWNER 帶過。
                        if verdict in ("PASS", "BLOCKED-OWNER"):
                            verdict = "evidence-insufficient"
                        reasons = reasons + [f"owner-gated 登記列缺席於報告：{owner_info['absent_expected']}"]

                if verdict == "BLOCKED-OWNER":
                    l1_blocked = True
                elif verdict != "PASS":
                    l1_ok = False

                l1_rows.append(
                    {
                        "set_id": set_id,
                        "platform": platform,
                        "verdict": verdict,
                        "reasons": reasons,
                        "missing": missing,
                        "skipped_required": skipped_required,
                        "duplicates": duplicates,
                    }
                )

    # 11. 整體判定
    has_case_fail = any(
        r["status"] not in ("ok", "rust-only-ok", "PASS", "BLOCKED-OWNER", "not-applicable") for r in case_rows
    )
    if (
        errors
        or has_case_fail
        or not coverage_ok
        or not invariants_ok
        or not l1_ok
        or windows_l1 != "PASS"
        or ab_result != "pass"
        or windows_com in ("FAIL", "evidence-insufficient")
    ):
        overall = "FAIL"
    else:
        if l1_blocked or any(
            r["status"] == "BLOCKED-OWNER" for r in case_rows
        ):
            overall = "IMPLEMENTED"
        else:
            overall = "VERIFIED"

    envelope = {
        "required": required_cases,
        "cases": case_rows,
        "invariants": invariant_rows,
        "coverage": coverage_rows,
        "l1": l1_rows,
        "owner_gated": owner_rows,
        "windows_com": windows_com,
        "windows_com_reasons": windows_com_reasons,
        "windows_l1": windows_l1,
        "overall": overall,
        "errors": errors,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return 0 if overall in ("IMPLEMENTED", "VERIFIED") else 1


def main(argv: list[str] | None = None) -> int:
    def parse_l15_report(value: str) -> tuple[str, Path]:
        for platform in ("linux", "windows"):
            prefix = f"{platform}="
            if value.startswith(prefix):
                path = value[len(prefix):]
                if not path:
                    raise argparse.ArgumentTypeError("L1.5 報告路徑不可為空")
                return platform, Path(path)
        if not value:
            raise argparse.ArgumentTypeError("L1.5 報告路徑不可為空")
        return "linux", Path(value)

    parser = argparse.ArgumentParser(description="Stage 1A gate 判定器")
    parser.add_argument("--ab-report", type=Path, default=None, help="A/B 比較報告 JSON")
    parser.add_argument(
        "--l1-results", required=True, type=Path, help="L1 結果目錄 (含各平台 blackbox 與 cargo)"
    )
    parser.add_argument("--out", required=True, type=Path, help="輸出 gate 報告 JSON 路徑")
    parser.add_argument(
        "--win-report", type=Path, default=None, help="Windows 測試報告 JSON"
    )
    parser.add_argument(
        "--l15-report", action="append", type=parse_l15_report, default=[],
        help="L1.5 功能測試報告；未帶平台前綴時視為 linux，也可指定 linux=<path> 或 windows=<path>；可重複指定",
    )
    parser.add_argument(
        "--pytest-junit-report", type=Path, default=None,
        help="SW_REQUIRE_CARGO=1 完整 pytest 執行的 JUnit XML",
    )
    parser.add_argument(
        "--windows-com", type=Path, default=None,
        help="Windows COM 證據 OutDir（須含 windows-com.json；win-blackbox.ps1 的輸出根目錄）",
    )
    parser.add_argument(
        "--spike3-evidence", type=Path, default=None,
        help="spike3 證據目錄（含 <case_id>.json；scripts/stage0/spike3_win_wal.py 的 --evidence-dir）",
    )
    parser.add_argument(
        "--repo", type=Path, default=None, help="Repo 根目錄（預設自動推算）"
    )

    args = parser.parse_args(argv)
    return judge_stage1a(
        ab_report_path=args.ab_report,
        l1_results_dir=args.l1_results,
        out_path=args.out,
        win_report_path=args.win_report,
        l15_report_inputs=args.l15_report,
        pytest_junit_path=args.pytest_junit_report,
        windows_com_dir=args.windows_com,
        repo_root=args.repo,
        spike3_dir=args.spike3_evidence,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
