"""Slice 2.1 出口 gate 判定器（scripts/rust/slice21_gate_judge.py）負向測試（P5-T22，紅燈）。

對應計畫 Task 22 Step 1 的判定表。判定器為純函式，測試以記憶體內假報告驅動，
L1 完整性判定重用 P2 唯一實作 `tests/acceptance/l1_judge.py`（不得另寫）。

本檔約定的判定器介面（實作必須與之相容；計畫只給出 `judge(report, expect)` 名稱）：
- `judge(report, expect) -> dict`，回傳含 `verdict`、`sections`（每列含 `name`、`status`、
  `l1_verdict`）與 `case_uptake`（含 `valid_pass`、`denominator`、`rate`、`pass_case_ids`）。
- `report`：schema 形狀，至少含 `identity`（必填欄位依 schema）與 `sections`；
  section 的 `status`、`l1_verdict` 輸入值由判定器重新計算，不得沿用輸入。
- `expect`：
  - `lock`：`tests/acceptance/l1_required.lock.json` 形狀；必要集合以
    `l1_judge.required_for(lock, required_set, platform)` 取得，缺集合 → EVIDENCE_MISSING。
  - `base_dir`：`section.l1_results` 相對路徑的解析根；檔案缺失 → EVIDENCE_MISSING。
  - `freeze`：Task 0b `tests/acceptance/breaker/slice_2.1.json` 形狀（`denominator`、`required_case_ids`）。
  - `case_verdicts`：case_id → gate 判定（只有 `PASS` 計入 valid_pass）。
  - `daemon_path`：可為 None；有值時與 `identity.daemon_sha256` 比對。

紅燈原因：`scripts/rust/slice21_gate_judge.py` 尚不存在，載入即 FileNotFoundError。
`test_case_uptake_from_valid_pass_only` 另依賴 Task 0b 產物 `slice_2.1.json`，缺檔時亦屬前置缺失。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 測試區段（與 schema sections 列舉對齊）：名稱 → (平台, required_set)
TEST_SECTIONS = {
    "linux_l1": ("linux", "s21_core"),
    "linux_cap_posix_pty": ("linux", "s21_gate:linux_cap_posix_pty"),
    "windows_native_l1": ("windows", "s21_core"),
    "windows_cap_windows_com": ("windows", "s21_gate:windows_cap_windows_com"),
    "blackbox_python": ("linux", "s21_gate:blackbox_python"),
    "blackbox_rust": ("linux", "s21_gate:blackbox_rust"),
    "existing_suites_rust": ("linux", "s21_gate:existing_suites_rust"),
    "pytest_full": ("linux", "s21_gate:pytest_full"),
}
NON_TEST_SECTIONS = ["l15_ab", "event_latency", "wal_s1w_perf", "cfg_boundary", "policy_check", "case_uptake"]
CORE_NODEIDS = ["cargo:sw-core::wal::a", "cargo:sw-core::wal::b", "cargo:sw-core::wal::c"]
GATE_NODEIDS = {  # 每個 gate 集合的凍結 nodeid（測試用，兩個一組）
    name: [f"tests/test_{name}.py::test_{i}" for i in ("alpha", "beta")]
    for name in ("linux_cap_posix_pty", "windows_cap_windows_com", "blackbox_python", "blackbox_rust",
                 "existing_suites_rust", "pytest_full")
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


IDENTITY = {
    "cli_path": "/x/serialwrap",
    "cli_sha256": _sha("cli"),
    "daemon_implementation": "rust",
    "daemon_version": "0.0.0-test",
    "daemon_artifact_id": "artifact-test",
    "daemon_sha256": _sha("daemon"),
    "contracts_revision": "rev-test",
    "cases_lock_sha256": _sha("cases-lock"),
    "freeze_file_sha256": _sha("freeze"),
    "tool_revision": {"uart_stdio_bridge_tree_sha256": _sha("bridge"), "runner_sha256": _sha("runner")},
    "platform": {"os": "linux", "kernel": "6.6.87.2-microsoft-standard-WSL2", "arch": "x86_64"},
    "device": None,
}
FREEZE_SYNTH = {"denominator": 3, "required_case_ids": ["c-1", "c-2", "c-3"],
                "window": {"start": "2026-10-21", "weeks": 3}, "stage2_started_at": "2026-10-21"}
# 斷路器輸入（窗口內、未觸發）：case_uptake 依 slice_breaker.evaluate 判定（審查 b7cd852 #1）。
BREAKER_OK = {"snapshots": [], "today": "2026-10-28", "stage2_estimate_weeks": 0.0}


# event_latency 證據（方法審閱 [High]：judge 須讀證據內容並核對 artifact／commit／run／工具／契約身分）。
EVLAT_REF = {"tool_sha256": _sha("evlat-tool"), "contract_sha256": _sha("contract"), "method_revision": "2026-10-29.2"}


def _evlat_evidence(**identity) -> dict:
    ident = {"artifact_source_commit": "e" * 40, "artifact_build_run_id": "t",       # daemon_sha256 預設跟隨報告 identity
             "run_id": "t", "source_commit": "e" * 40, "source_tree_dirty": False, **EVLAT_REF}
    ident.update(identity)
    return {"schema": "event_latency_external.v1", "verdict": "PASS", "reasons": [], "identity": ident}


def _judge_mod():
    """載入判定器腳本（紅燈階段檔案不存在，於此拋 FileNotFoundError）。"""
    spec = importlib.util.spec_from_file_location("slice21_gate_judge", ROOT / "scripts/rust/slice21_gate_judge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(nodeid: str, outcome: str = "passed", skip_kind: str | None = None) -> dict:
    if outcome == "passed":
        phases = {"setup": "passed", "call": "passed", "teardown": "passed"}
    elif outcome in ("failed", "error"):
        phases = {"setup": "passed", "call": "failed", "teardown": None}
    else:  # skipped／xfailed：整列在 setup 即跳過
        phases = {"setup": "skipped", "call": None, "teardown": None}
    return {"nodeid": nodeid, "outcome": outcome, "skip_kind": skip_kind, "phases": phases}


def _l1(platform: str, runner: str, rows: list[dict], *, collected=None, deselected=(), exit_code=0, finished=True) -> dict:
    """l1-results.v1 報告（與 tests/acceptance/l1_results.schema.json 同形）。"""
    ids = [r["nodeid"] for r in rows]
    return {
        "schema_version": 2, "format": "l1-results.v1", "runner": runner, "platform": platform,
        "argv": ["cargo", "test", "--workspace"] if runner == "cargo" else ["/x/pytest/__main__.py", "-q", "tests/"],
        "markexpr": "", "counts": {}, "resources": {}, "uart_kind": "native",
        "not_collected_platform": [], "core_portable_platform_skips": [],
        "collected": list(collected) if collected is not None else ids + list(deselected),
        "deselected": list(deselected), "results": rows,
        "exit_code": exit_code, "finished": finished, "deployed_content": None,
    }


def _lock() -> dict:
    sets = {"s21_core": {
        "linux": {"nodeids": list(CORE_NODEIDS), "selector": {"argv": ["cargo", "test", "--workspace"]}},
        "windows": {"nodeids": list(CORE_NODEIDS), "selector": {"argv": ["cargo", "test", "--workspace"]}},
    }}
    for name, ids in GATE_NODEIDS.items():
        platform = "windows" if name.startswith("windows_") else "linux"
        sets[f"s21_gate:{name}"] = {platform: {"nodeids": list(ids), "selector": {"argv": ["pytest", "-q"]}}}
    return {"schema_version": 1, "sets": sets}


def _section(name: str, *, exit_code: int = 0, l1_results: str | None = None, required_set: str | None = None,
             allowed_missing_bo: str | None = None, counts: dict | None = None) -> dict:
    """輸入區段：status／l1_verdict 由判定器重算，此處放置占位值。"""
    c = counts or {}
    return {
        "name": name, "status": "PASS", "command": f"run {name}", "exit": exit_code,
        "passed": c.get("passed", 0), "failed": c.get("failed", 0), "skipped": c.get("skipped", 0),
        "deselected": c.get("deselected", 0), "ignored": c.get("ignored", 0),
        "required_set": required_set, "l1_results": l1_results, "l1_verdict": {},
        "allowed_missing_bo": allowed_missing_bo, "log": f"logs/{name}.log",
    }


def _counts(l1: dict) -> dict:
    rows = l1["results"]
    return {
        "passed": sum(1 for r in rows if r["outcome"] == "passed"),
        "failed": sum(1 for r in rows if r["outcome"] in ("failed", "error")),
        "skipped": sum(1 for r in rows if r["outcome"] in ("skipped", "xfailed")),
        "deselected": len(l1["deselected"]),
        "ignored": sum(1 for r in rows if r["skip_kind"] == "capability"),
    }


class _Case:
    """測試夾具：可覆寫各區段的 l1 報告，產生 (report, expect)。"""

    def __init__(self, base_dir: pathlib.Path) -> None:
        self.base = base_dir
        self.l1: dict[str, dict | None] = {}
        self.extra_sections: list[dict] = []
        self.identity = json.loads(json.dumps(IDENTITY))
        self.freeze = FREEZE_SYNTH
        self.case_verdicts: dict[str, str] = {}
        self.daemon_path: pathlib.Path | None = None
        self.breaker: dict | None = dict(BREAKER_OK)
        self.parity: dict | None = None
        self.pending_decisions: list[dict] = []
        self.evlat: dict | None = _evlat_evidence()
        for name, (platform, _) in TEST_SECTIONS.items():
            self.l1[name] = self._default_l1(name, platform)

    def _default_l1(self, name: str, platform: str) -> dict | None:
        if name == "windows_cap_windows_com":
            return None                                   # 無 COM pair：證據缺失（BO-S21-01 豁免）
        if name in ("linux_l1", "windows_native_l1"):
            ids = CORE_NODEIDS
            runner = "cargo"
        else:
            ids = GATE_NODEIDS[name]
            runner = "pytest"
        return _l1(platform, runner, [_row(i) for i in ids])

    def write_l1(self, name: str, report: dict) -> str:
        rel = f"l1/{name}.json"
        path = self.base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report), encoding="utf-8")
        return rel

    def report(self) -> dict:
        sections = []
        for name, (platform, required_set) in TEST_SECTIONS.items():
            l1 = self.l1.get(name)
            rel = self.write_l1(name, l1) if l1 is not None else None
            counts = _counts(l1) if l1 is not None else None
            allowed = "BO-S21-01" if name == "windows_cap_windows_com" else None
            sections.append(_section(name, l1_results=rel, required_set=required_set,
                                     allowed_missing_bo=allowed, counts=counts))
            if name == "linux_l1":
                sections[-1]["run_euid"] = 1000             # F108：linux_l1 以非 root 執行
        for name in NON_TEST_SECTIONS:
            sec = _section(name, exit_code=0)
            sec["section_verdict"] = "PASS"   # 非測試類 section 須明確帶判定（審查 b7cd852 #3）
            if name == "event_latency" and self.evlat is not None:
                rel = "event_latency/rust.json"
                (self.base / rel).parent.mkdir(parents=True, exist_ok=True)
                ev = json.loads(json.dumps(self.evlat))
                ev.setdefault("identity", {}).setdefault("daemon_sha256", self.identity.get("daemon_sha256"))
                (self.base / rel).write_text(json.dumps(ev), encoding="utf-8")
                sec["evidence"] = rel
            sections.append(sec)
        sections.extend(self.extra_sections)
        return {"schema_version": 1, "run_id": "t", "git_revision": "e" * 40, "slice": "2.1", "identity": self.identity,
                "sections": sections, "pending_decisions": list(self.pending_decisions)}

    def expect(self) -> dict:
        out = {"lock": _lock(), "base_dir": self.base, "freeze": self.freeze,
               "case_verdicts": dict(self.case_verdicts), "daemon_path": self.daemon_path,
               "event_latency_ref": dict(EVLAT_REF)}
        if self.breaker is not None:
            out["breaker"] = self.breaker
        if self.parity is not None:
            out["parity"] = self.parity
        return out


def _sec(out: dict, name: str) -> dict:
    return [s for s in out["sections"] if s["name"] == name][0]


@pytest.fixture
def case(tmp_path):
    return _Case(tmp_path)


def test_all_skipped_is_not_pass(case):
    ids = [f"tests/test_x.py::test_{i:02d}" for i in range(36)]
    case.l1["blackbox_python"] = _l1("linux", "pytest", [_row(i, "skipped", "capability") for i in ids])
    out = _judge_mod().judge(case.report(), case.expect())
    assert _sec(out, "blackbox_python")["status"] == "EVIDENCE_MISSING"
    assert out["verdict"] == "INCOMPLETE"


def test_zero_tests_is_not_pass(case):
    # `cargo test -- --ignored cap_posix_pty__` 輸出 0 tests、exit 0
    case.l1["linux_cap_posix_pty"] = _l1("linux", "cargo", [], collected=[], exit_code=0)
    out = _judge_mod().judge(case.report(), case.expect())
    assert _sec(out, "linux_cap_posix_pty")["status"] == "EVIDENCE_MISSING"
    assert out["verdict"] == "INCOMPLETE"


def test_count_threshold_cannot_substitute_required_nodeids(case):
    # R5-F3：以其他 50 個測試湊數，必要 nodeid 皆未執行；舊的計數門檻會誤判 PASS
    others = [f"cargo:sw-core::other::case_{i:02d}" for i in range(50)]
    case.l1["linux_l1"] = _l1("linux", "cargo", [_row(i) for i in others], exit_code=0)
    out = _judge_mod().judge(case.report(), case.expect())
    sec = _sec(out, "linux_l1")
    assert sec["status"] == "EVIDENCE_MISSING"
    assert sorted(sec["l1_verdict"]["missing"]) == sorted(CORE_NODEIDS)
    assert out["verdict"] == "INCOMPLETE"


def test_required_nodeid_ignored_is_missing(case):
    n1, n2 = GATE_NODEIDS["linux_cap_posix_pty"]
    case.l1["linux_cap_posix_pty"] = _l1("linux", "pytest", [_row(n1), _row(n2, "skipped", "capability")])
    out = _judge_mod().judge(case.report(), case.expect())
    sec = _sec(out, "linux_cap_posix_pty")
    assert sec["status"] == "EVIDENCE_MISSING"
    assert sec["l1_verdict"]["skipped_required"][0]["nodeid"] == n2


def test_duplicate_nodeid_is_missing(case):
    n1, n2 = GATE_NODEIDS["linux_cap_posix_pty"]
    # 同一必要 nodeid 兩列皆 passed（兩支 exe 重複執行）
    case.l1["linux_cap_posix_pty"] = _l1("linux", "pytest", [_row(n1), _row(n1), _row(n2)])
    out = _judge_mod().judge(case.report(), case.expect())
    sec = _sec(out, "linux_cap_posix_pty")
    assert sec["status"] == "EVIDENCE_MISSING"
    assert n1 in sec["l1_verdict"]["duplicates"]
    # 一列 failed 一列 passed → FAIL
    case.l1["linux_cap_posix_pty"] = _l1("linux", "pytest", [_row(n1), _row(n1, "failed"), _row(n2)])
    out = _judge_mod().judge(case.report(), case.expect())
    assert _sec(out, "linux_cap_posix_pty")["status"] == "FAIL"


def test_runner_exit_or_unfinished_is_missing(case):
    ids = GATE_NODEIDS["blackbox_python"]
    # 全部必要 nodeid passed，但 runner 以 101 結束（報告 exit_code 與區段 exit 不一致）
    case.l1["blackbox_python"] = _l1("linux", "pytest", [_row(i) for i in ids], exit_code=101)
    out = _judge_mod().judge(case.report(), case.expect())
    assert _sec(out, "blackbox_python")["status"] == "EVIDENCE_MISSING"
    # runner 未完成
    case.l1["blackbox_python"] = _l1("linux", "pytest", [_row(i) for i in ids], finished=False)
    out = _judge_mod().judge(case.report(), case.expect())
    assert _sec(out, "blackbox_python")["status"] == "EVIDENCE_MISSING"


def test_missing_l1_results_is_missing(case):
    # (a) 測試類區段 l1_results 為 null
    case.l1["blackbox_python"] = None
    out = _judge_mod().judge(case.report(), case.expect())
    assert _sec(out, "blackbox_python")["status"] == "EVIDENCE_MISSING"
    # (b) 檔案不存在
    rep = case.report()
    [s for s in rep["sections"] if s["name"] == "blackbox_rust"][0]["l1_results"] = "l1/does-not-exist.json"
    out = _judge_mod().judge(rep, case.expect())
    assert _sec(out, "blackbox_rust")["status"] == "EVIDENCE_MISSING"
    # (c) required_set 不在 lock
    rep = case.report()
    [s for s in rep["sections"] if s["name"] == "pytest_full"][0]["required_set"] = "s21_gate:not_in_lock"
    out = _judge_mod().judge(rep, case.expect())
    assert _sec(out, "pytest_full")["status"] == "EVIDENCE_MISSING"
    # (d) 非測試類 policy_check required_set 為 null，不受影響
    out = _judge_mod().judge(case.report(), case.expect())
    assert _sec(out, "policy_check")["status"] == "PASS"


def test_full_required_set_pass(case):
    out = _judge_mod().judge(case.report(), case.expect())
    for name in TEST_SECTIONS:
        expected = "EVIDENCE_MISSING" if name == "windows_cap_windows_com" else "PASS"
        assert _sec(out, name)["status"] == expected, name
    for name in NON_TEST_SECTIONS:
        assert _sec(out, name)["status"] == "PASS", name
    assert out["verdict"] == "READY_FOR_OWNER_EVIDENCE"


def test_deselected_counts_as_missing(case):
    n1, n2 = GATE_NODEIDS["blackbox_rust"]
    case.l1["blackbox_rust"] = _l1("linux", "pytest", [_row(n1)], collected=[n1, n2], deselected=[n2])
    out = _judge_mod().judge(case.report(), case.expect())
    sec = _sec(out, "blackbox_rust")
    assert sec["status"] == "EVIDENCE_MISSING"
    assert sec["l1_verdict"]["deselected_required"]


def test_only_allowed_bo_section_may_be_missing(case):
    # 只有 windows_cap_windows_com（BO-S21-01 豁免）缺 → READY
    out = _judge_mod().judge(case.report(), case.expect())
    assert out["verdict"] == "READY_FOR_OWNER_EVIDENCE"
    # blackbox_rust 缺（非豁免）→ INCOMPLETE
    case.l1["blackbox_rust"] = None
    out = _judge_mod().judge(case.report(), case.expect())
    assert out["verdict"] == "INCOMPLETE"


def test_failed_section_is_fail(case):
    ids = GATE_NODEIDS["blackbox_python"]
    case.l1["blackbox_python"] = _l1("linux", "pytest", [_row(ids[0], "failed"), _row(ids[1])], exit_code=1)
    out = _judge_mod().judge(case.report(), case.expect())
    assert _sec(out, "blackbox_python")["status"] == "FAIL"
    assert out["verdict"] == "FAIL"


def test_identity_required(case):
    for key in ("daemon_sha256", "cases_lock_sha256"):
        case.identity.pop(key)
        out = _judge_mod().judge(case.report(), case.expect())
        assert out["verdict"] == "INCOMPLETE", key
        case.identity = json.loads(json.dumps(IDENTITY))


def test_identity_hash_matches_artifact(case):
    artifact = case.base / "serialwrapd"
    artifact.write_bytes(b"artifact-A")
    case.daemon_path = artifact
    case.identity["daemon_sha256"] = _sha("artifact-B")      # 不符
    out = _judge_mod().judge(case.report(), case.expect())
    assert out["verdict"] == "FAIL"
    case.identity["daemon_sha256"] = hashlib.sha256(b"artifact-A").hexdigest()   # 相符（正向對照）
    out = _judge_mod().judge(case.report(), case.expect())
    assert out["verdict"] == "READY_FOR_OWNER_EVIDENCE"


def test_case_uptake_from_valid_pass_only(case):
    freeze = json.loads((ROOT / "tests/acceptance/breaker/slice_2.1.json").read_text(encoding="utf-8"))
    ids = freeze["required_case_ids"]
    case.freeze = freeze
    case.case_verdicts = {ids[0]: "PASS", ids[1]: "PASS", ids[2]: "PASS", ids[3]: "SKIP", ids[4]: "EVIDENCE_MISSING"}
    out = _judge_mod().judge(case.report(), case.expect())
    cu = out["case_uptake"]
    assert cu["valid_pass"] == 3
    assert cu["rate"] == cu["valid_pass"] / freeze["denominator"]
    assert cu["denominator"] == freeze["denominator"]
    assert sorted(cu["pass_case_ids"]) == sorted(ids[:3])


# --------------------------------------------------------------------------- 第三輪審查（d2d29a6_7af6567）

def test_case_uptake_tripped_is_fail(case):
    """b7cd852 #1：窗口結束且承接率累計增加 < 10 pp → 斷路器 tripped → case_uptake FAIL（不得固定 PASS）。"""
    case.breaker = {"snapshots": [{"date": "2026-10-21", "rate": 0.0}, {"date": "2026-11-11", "rate": 0.05}],
                    "today": "2026-11-20", "stage2_estimate_weeks": 0.0}
    out = _judge_mod().judge(case.report(), case.expect())
    assert _sec(out, "case_uptake")["status"] == "FAIL"
    assert out["verdict"] == "FAIL"


def test_case_uptake_od02_resubmit_is_fail(case):
    case.breaker = dict(BREAKER_OK, stage2_estimate_weeks=30.0)
    out = _judge_mod().judge(case.report(), case.expect())
    assert _sec(out, "case_uptake")["status"] == "FAIL"


def test_case_uptake_insufficient_evidence_is_missing(case):
    case.breaker = {"snapshots": [], "today": "2026-11-20", "stage2_estimate_weeks": 0.0}
    out = _judge_mod().judge(case.report(), case.expect())
    assert _sec(out, "case_uptake")["status"] == "EVIDENCE_MISSING"
    assert out["verdict"] == "INCOMPLETE"


def test_case_uptake_without_breaker_inputs_is_missing(case):
    case.breaker = None
    out = _judge_mod().judge(case.report(), case.expect())
    assert _sec(out, "case_uptake")["status"] == "EVIDENCE_MISSING"


def test_case_uptake_rate_zero_reported_in_reasons(case):
    """正向對照：窗口內未觸發 → PASS，且 rate 照實回報（不因 PASS 改寫）。"""
    out = _judge_mod().judge(case.report(), case.expect())
    sec = _sec(out, "case_uptake")
    assert sec["status"] == "PASS"
    assert out["case_uptake"]["rate"] == 0.0
    assert out["case_uptake"]["breaker"]["tripped"] is False


def test_non_test_section_requires_explicit_verdict(case):
    """b7cd852 #3：非測試類 section 只有 exit 0、缺 section_verdict → EVIDENCE_MISSING（不得預設放行）。"""
    rep = case.report()
    for sec in rep["sections"]:
        if sec["name"] == "policy_check":
            sec.pop("section_verdict")
    out = _judge_mod().judge(rep, case.expect())
    assert _sec(out, "policy_check")["status"] == "EVIDENCE_MISSING"
    assert out["verdict"] == "INCOMPLETE"


def _parity_cfg(rows: list[dict]) -> dict:
    return {"status": {"rows": rows, "source_sha256": None}, "status_file": "x.yaml",
            "source_rows": [{"id": r["id"], "take": "P5"} for r in rows], "source_sha256_actual": None}


def test_owner_decision_pending_requires_registered_decision(case):
    """4920d90 #1：OWNER-DECISION-PENDING 須帶存在於 pending_decisions 的 decision id，否則降為 INCOMPLETE。"""
    case.parity = _parity_cfg([{"id": "#32", "status": "OWNER-DECISION-PENDING"}])
    out = _judge_mod().judge(case.report(), case.expect())
    row = out["parity_amendments"]["rows"][0]
    assert row["status"] == "INCOMPLETE"
    assert out["verdict"] == "INCOMPLETE"
    case.parity = _parity_cfg([{"id": "#32", "status": "OWNER-DECISION-PENDING", "decision": "PA-OD-F67"}])
    out = _judge_mod().judge(case.report(), case.expect())
    assert out["parity_amendments"]["rows"][0]["status"] == "INCOMPLETE"      # 未登記於 pending_decisions
    case.pending_decisions = [{"id": "PA-OD-F67", "scenario": "x", "evidence": "y"}]
    out = _judge_mod().judge(case.report(), case.expect())
    assert out["parity_amendments"]["rows"][0]["status"] == "OWNER-DECISION-PENDING"
    assert out["verdict"] == "READY_FOR_OWNER_EVIDENCE"


def test_preflight_not_frozen_never_ready(case):
    """§5.3：event_latency 未凍結（preflight.event_latency_frozen false）時，即使 `--evidence-run` 執行了全部 section
    且皆 PASS，verdict 仍為 INCOMPLETE（不得 READY）。"""
    rep = case.report()
    rep["preflight"] = {"event_latency_frozen": False, "reasons": ["event_latency.freeze_status='pending_review_record'"]}
    out = _judge_mod().judge(rep, case.expect())
    assert out["verdict"] == "INCOMPLETE"
    assert any("event_latency 未凍結" in r for r in out["verdict_reasons"])


def test_cmd_judge_reloads_parity_like_assemble(case, capsys):
    """sol-backfill p5 #8：`judge` 子命令必須與 `assemble` 一樣重新載入 #104 parity 狀態與來源；
    來源文件不可得時 parity 為 INCOMPLETE，重新判定不得因漏載 parity 而放寬成 READY_FOR_OWNER_EVIDENCE。"""
    import yaml

    base = case.base
    report_path = base / "report.json"
    report_path.write_text(json.dumps(case.report()), encoding="utf-8")
    (base / "lock.json").write_text(json.dumps(_lock()), encoding="utf-8")
    (base / "freeze.json").write_text(json.dumps(FREEZE_SYNTH), encoding="utf-8")
    (base / "snapshots.ndjson").write_text("", encoding="utf-8")
    (base / "parity.yaml").write_text(yaml.safe_dump({
        "schema_version": 1, "source": "", "source_ref": None, "source_sha256": None,
        "rows": [{"id": "#32", "status": "INCOMPLETE"}],
    }), encoding="utf-8")
    expect = {
        "schema_version": 1, "lock_file": str(base / "lock.json"), "freeze_file": str(base / "freeze.json"),
        "blocked_owner_file": "tests/acceptance/slice21_bo.yaml",
        "breaker_snapshots_file": str(base / "snapshots.ndjson"), "stage2_estimate_weeks": 0.0,
        "parity": {"status_file": str(base / "parity.yaml")}, "sections": [],
    }
    expect_path = base / "expect.yaml"
    expect_path.write_text(yaml.safe_dump(expect, allow_unicode=True), encoding="utf-8")
    assert _judge_mod().main(["judge", "--report", str(report_path), "--expect", str(expect_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "INCOMPLETE", out
    assert any("#104" in r for r in out["verdict_reasons"]), out


def test_l15_ab_excluded_case_requires_covering_pytest_passed(case):
    """OD-40：`l15-wal-interference` 移出 l15_ab 判定後，改由逐名列出的 pytest WAL 測試承擔；
    l15_ab 只有在每個 covered_by 測試於指定 section 恰為 passed 時才得 PASS，否則 EVIDENCE_MISSING（不得默默放行）。"""
    nid = GATE_NODEIDS["blackbox_rust"][0]
    exp = case.expect()
    exp["l15_ab_excluded"] = [{"case_id": "l15-wal-interference", "decision": "OD-40",
                               "covered_by": [{"section": "blackbox_rust", "nodeid": nid},
                                              {"section": "blackbox_python", "nodeid": "tests/x.py::test_absent"}]}]
    out = _judge_mod().judge(case.report(), exp)
    assert _sec(out, "l15_ab")["status"] == "EVIDENCE_MISSING"
    assert any("OD-40" in r and "test_absent" in r for r in _sec(out, "l15_ab")["reasons"])
    assert out["verdict"] == "INCOMPLETE"
    exp["l15_ab_excluded"][0]["covered_by"][1]["nodeid"] = GATE_NODEIDS["blackbox_python"][0]
    out = _judge_mod().judge(case.report(), exp)
    assert _sec(out, "l15_ab")["status"] == "PASS"
    # 只在其他 section 通過不算（必須在列出的 section）
    exp["l15_ab_excluded"][0]["covered_by"] = [{"section": "blackbox_python", "nodeid": nid}]
    out = _judge_mod().judge(case.report(), exp)
    assert _sec(out, "l15_ab")["status"] == "EVIDENCE_MISSING"


def test_event_latency_section_requires_readable_evidence(case):
    """方法審閱 [High]：judge 須讀取 event_latency 證據內容；`evidence: missing.json` 不得判 PASS。"""
    assert _sec(_judge_mod().judge(case.report(), case.expect()), "event_latency")["status"] == "PASS"
    rep = case.report()
    for sec in rep["sections"]:
        if sec["name"] == "event_latency":
            sec["evidence"] = "missing.json"
    out = _judge_mod().judge(rep, case.expect())
    assert _sec(out, "event_latency")["status"] == "EVIDENCE_MISSING"
    assert out["verdict"] == "INCOMPLETE"
    case.evlat = None
    assert _sec(_judge_mod().judge(case.report(), case.expect()), "event_latency")["status"] == "EVIDENCE_MISSING"


@pytest.mark.parametrize("field,value", [
    ("daemon_sha256", "0" * 64),          # 量測的不是本 run 的 artifact
    ("artifact_source_commit", "f" * 40),  # 建置來源不是本 run 的 git_revision（如建置失敗留下舊 binary）
    ("artifact_source_commit", None),
    ("artifact_build_run_id", "other-run"),
    ("run_id", "other-run"),
    ("tool_sha256", "1" * 64),
    ("contract_sha256", "2" * 64),
    ("method_revision", "2026-10-29.1"),
])
def test_event_latency_evidence_identity_must_match_run(case, field, value):
    """方法審閱 [High]：量測 JSON 的 artifact、commit、run、工具與契約身分須與 gate 本 run 一致，否則不得 PASS。"""
    case.evlat = _evlat_evidence(**{field: value})
    out = _judge_mod().judge(case.report(), case.expect())
    sec = _sec(out, "event_latency")
    assert sec["status"] == "EVIDENCE_MISSING", (field, sec)
    assert any(field in r for r in sec["reasons"]), sec["reasons"]


def test_event_latency_evidence_verdict_drives_section(case):
    """證據 verdict FAIL → section FAIL（即使 gate 片段帶 PASS）；INSUFFICIENT_EVIDENCE → EVIDENCE_MISSING。"""
    case.evlat = {**_evlat_evidence(), "verdict": "FAIL", "reasons": ["外部量測超過上限：1/idle/stop_request"]}
    out = _judge_mod().judge(case.report(), case.expect())
    assert _sec(out, "event_latency")["status"] == "FAIL" and out["verdict"] == "FAIL"
    case.evlat = {**_evlat_evidence(), "verdict": "INSUFFICIENT_EVIDENCE", "reasons": ["x"]}
    assert _sec(_judge_mod().judge(case.report(), case.expect()), "event_latency")["status"] == "EVIDENCE_MISSING"


def test_release_build_failure_is_fail_and_build_sha_must_match(case):
    """方法審閱 [High]：Linux release 建置失敗（舊 binary 可能留存）→ FAIL；建置當下 sha256 與報告 artifact 不符 → FAIL。"""
    case.identity["build"] = {"linux_exit": 0, "windows_exit": 0, "daemon_sha256_at_build": IDENTITY["daemon_sha256"]}
    assert _judge_mod().judge(case.report(), case.expect())["verdict"] == "READY_FOR_OWNER_EVIDENCE"
    case.identity["build"] = {"linux_exit": 101, "windows_exit": 0, "daemon_sha256_at_build": None}
    out = _judge_mod().judge(case.report(), case.expect())
    assert out["verdict"] == "FAIL" and any("建置失敗" in r for r in out["verdict_reasons"])
    case.identity["build"] = {"linux_exit": 0, "windows_exit": 0, "daemon_sha256_at_build": "0" * 64}
    assert _judge_mod().judge(case.report(), case.expect())["verdict"] == "FAIL"


def test_gate_script_removes_stale_artifact_and_binds_build_sha():
    """方法審閱 [High]：gate 建置前移除舊 artifact；只在建置成功時把來源 commit／run／建置當下 sha256 傳給量測工具。"""
    text = (ROOT / "scripts" / "rust" / "slice21_gate.sh").read_text(encoding="utf-8")
    build_at = text.index("run_logged build_linux")
    assert text.rindex('rm -f "$RUST_D" "$RUST_CLI"', 0, build_at) > 0
    assert 'BUILD_LINUX_RC="$LAST_RC"' in text and 'BUILD_WINDOWS_RC="$LAST_RC"' in text
    assert '--artifact-sha256 "$BUILT_D_SHA"' in text
    evlat = text[text.index("run_logged event_latency "):]
    assert '"${evlat_bind[@]}"' in evlat.splitlines()[2] or '"${evlat_bind[@]}"' in evlat[:400]
    assert "--build-linux-exit" in text and "--built-daemon-sha256" in text


def test_l15_ab_excluded_case_covered_by_section_requires_section_pass(case):
    """OD-58：`s21-event-latency` 移出 l15_ab 後由 event_latency section 承擔（`covered_by_sections`）；
    承擔 section 非 PASS 時 l15_ab 不得 PASS。"""
    exp = case.expect()
    exp["l15_ab_excluded"] = [{"case_id": "s21-event-latency", "decision": "OD-58",
                               "covered_by": [{"section": "linux_l1", "nodeid": CORE_NODEIDS[0]}],
                               "covered_by_sections": ["event_latency"]}]
    assert _sec(_judge_mod().judge(case.report(), exp), "l15_ab")["status"] == "PASS"
    case.evlat = None                                    # event_latency 證據缺 → EVIDENCE_MISSING
    out = _judge_mod().judge(case.report(), exp)
    assert _sec(out, "event_latency")["status"] == "EVIDENCE_MISSING"
    sec = _sec(out, "l15_ab")
    assert sec["status"] == "EVIDENCE_MISSING" and any("OD-58" in r and "event_latency" in r for r in sec["reasons"])


def test_linux_l1_run_as_root_is_evidence_missing(case):
    """F108（P5 第 5 輪 OWNER-BATCH #4，編排者裁決）：linux_l1 須以非 root 執行；euid==0 時不得記 PASS，也不得因
    root 專屬失敗記 FAIL 了事，一律 EVIDENCE_MISSING 並要求非 root 補跑。未記錄 euid 同為證據不足。"""
    def with_euid(euid, outcome="passed"):
        rep = case.report()
        for sec in rep["sections"]:
            if sec["name"] == "linux_l1":
                if euid is None:
                    sec.pop("run_euid", None)
                else:
                    sec["run_euid"] = euid
                if outcome != "passed":
                    l1 = _l1("linux", "cargo", [_row(CORE_NODEIDS[0], outcome)] + [_row(i) for i in CORE_NODEIDS[1:]])
                    sec["l1_results"] = case.write_l1("linux_l1_x", l1)
                    sec.update(_counts(l1))
        return _sec(_judge_mod().judge(rep, case.expect()), "linux_l1")
    assert with_euid(1000)["status"] == "PASS"
    assert with_euid(1000, "failed")["status"] == "FAIL"
    root = with_euid(0)
    assert root["status"] == "EVIDENCE_MISSING" and any("非 root" in r for r in root["reasons"])
    assert with_euid(0, "failed")["status"] == "EVIDENCE_MISSING"
    assert with_euid(None)["status"] == "EVIDENCE_MISSING"


def test_gate_script_linux_l1_non_root_and_daemon_version_from_rpc():
    """F108／F139：gate 的 linux_l1 於 euid==0 且無法降權時不執行並記 EVIDENCE_MISSING；daemon_version 取自 RPC。"""
    text = (ROOT / "scripts" / "rust" / "slice21_gate.sh").read_text(encoding="utf-8")
    l1 = text[text.index("# 2. Linux L1"):text.index("# 3. Linux cap_posix_pty")]
    assert "id -u" in l1 and "SW_GATE_L1_USER" in l1 and "--run-euid" in l1 and "EVIDENCE_MISSING" in l1
    assert "probe_daemon_version.py" in text
    assert '"$RUST_D" --version' not in text      # serialwrapd 依 Python 1:1 不提供 --version


def test_uncarried_gate_set_blocks_ready(case):
    """sol P5-4f8acdf-1（High）：鎖中有 `s21_gate:*` 集合未被任何 section（含 additional_required_sets）承擔時，
    其 nodeid 從未被執行或判定——不得 READY，verdict 須為 INCOMPLETE 並列出該集合。"""
    exp = case.expect()
    assert _judge_mod().judge(case.report(), exp)["verdict"] == "READY_FOR_OWNER_EVIDENCE"
    exp["lock"]["sets"]["s21_gate:cap_sys_admin"] = {
        "linux": {"nodeids": ["cargo:sw-pal::serial_posix_process::cap_sys_admin__x"],
                  "selector": {"argv": ["--workspace", "--", "--ignored", "cap_sys_admin__"]}}}
    out = _judge_mod().judge(case.report(), exp)
    assert out["verdict"] == "INCOMPLETE"
    assert any("s21_gate:cap_sys_admin" in r for r in out["verdict_reasons"]), out["verdict_reasons"]
    # 以 additional_required_sets 指派給 section 即視為承擔（由該 section 判定）
    exp["additional_required_sets"] = {"linux_cap_posix_pty": ["s21_gate:cap_sys_admin"]}
    out = _judge_mod().judge(case.report(), exp)
    assert not any("未被任何 section 承擔" in r for r in out["verdict_reasons"])


def test_uncarried_gate_set_is_judged_per_platform(case):
    """sol P5-86b54d8-1 (a)：承擔以（集合, 平台）為粒度——windows section 承擔同名集合時，該集合的 linux 列
    從未被任何 linux section 執行，仍須判 INCOMPLETE 並列出 linux 平台。"""
    exp = case.expect()
    sel = {"argv": ["--workspace", "--", "--ignored", "cap_example__"]}
    exp["lock"]["sets"]["s21_gate:cap_example"] = {
        "linux": {"nodeids": ["cargo:sw-daemon::launcher::cap_example__linux_only"], "selector": sel},
        "windows": {"nodeids": [CORE_NODEIDS[0]], "selector": sel}}
    exp["additional_required_sets"] = {"windows_native_l1": ["s21_gate:cap_example"]}
    out = _judge_mod().judge(case.report(), exp)
    assert _sec(out, "windows_native_l1")["status"] == "PASS"   # windows 列確實由 windows section 判定
    assert out["verdict"] == "INCOMPLETE", out["verdict_reasons"]
    assert any("s21_gate:cap_example.linux" in r for r in out["verdict_reasons"]), out["verdict_reasons"]
    assert not any("s21_gate:cap_example.windows" in r for r in out["verdict_reasons"])


def test_non_test_section_fragment_cannot_carry_gate_set(case):
    """sol P5-86b54d8-1 (b)：非測試類 section 不判集合，其片段自帶的 additional_required_sets 不得算承擔
    （不接受報告自行宣告承擔）。"""
    exp = case.expect()
    exp["lock"]["sets"]["s21_gate:cap_example"] = {
        "linux": {"nodeids": ["cargo:sw-pal::serial_posix_process::cap_example__x"],
                  "selector": {"argv": ["--workspace", "--", "--ignored", "cap_example__"]}}}
    rep = case.report()
    for sec in rep["sections"]:
        if sec["name"] == "l15_ab":
            sec["additional_required_sets"] = ["s21_gate:cap_example"]
    out = _judge_mod().judge(rep, exp)
    assert out["verdict"] == "INCOMPLETE", out["verdict_reasons"]
    assert any("s21_gate:cap_example.linux" in r for r in out["verdict_reasons"]), out["verdict_reasons"]
    # 測試類 section 片段自帶者照常由該 section 判定，故算承擔
    for sec in rep["sections"]:
        if sec["name"] == "linux_cap_posix_pty":
            sec["additional_required_sets"] = ["s21_gate:cap_example"]
    out = _judge_mod().judge(rep, exp)
    assert not any("未被任何 section 承擔" in r for r in out["verdict_reasons"]), out["verdict_reasons"]
    assert _sec(out, "linux_cap_posix_pty")["status"] == "EVIDENCE_MISSING"   # 該 nodeid 未執行 → 不得 PASS
