"""P7 Task 19（08-slice23-login-events.md）：Slice 2.3 出口 gate 報告產生器與驗證器（合成 fixture，不讀「最新報告」）。

斷言依計畫 Task 19 Step 1；l1-results fixture 為合法的 P2 `l1-results.v1`（計畫範例的 fixture 缺 `counts`／`resources`／
`not_collected_platform`／`deployed_content` 等必填欄位、phases 為空，交給 P2 `l1_judge` 一律 INVALID，無法表達
「完整必要集合 → PASS」，故補齊形狀、斷言不變）。
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/rust"))
import p7_gate_report as g  # noqa: E402

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]
RESOURCE_AUDIT_WAIVERS = {"process_rpc": "test_required_rows_complete 僅啟動 p7_gate.sh --dry-run，不啟動 daemon 或進行 RPC"}


def _results(tmp, rows, caps_missing=(), name="run", owner_evidence=None):
    d = tmp / name
    d.mkdir()
    (d / "results.json").write_text(json.dumps({"run_id": "r1", "git_head": "abc", "artifacts": {},
                                                "caps_missing": list(caps_missing), "rows": rows,
                                                "owner_evidence": owner_evidence or {}}), encoding="utf-8")
    return d


def _row(name, rc=0, passed=3, failed=0, ev=()):
    return {"name": name, "argv": [name], "rc": rc, "test_counts": {"passed": passed, "failed": failed, "ignored": 0},
            "evidence": list(ev), "log_path": "x.log", "log_sha256": None}


def test_zero_tests_is_not_pass(tmp_path):
    md = g.build_report(_results(tmp_path, [_row("win-test.sh (non-COM)", passed=0)]))
    assert "| win-test.sh (non-COM) | INCOMPLETE |" in md          # R5-F3：缺 l1_results／0 測試＝證據不足，永不 PASS


def test_missing_com_pair_makes_com_row_blocked_not_pass(tmp_path):
    md = g.build_report(_results(tmp_path, [_row("win-test.sh --caps windows_com", passed=0)], caps_missing=["windows_com"]))
    assert "| win-test.sh --caps windows_com | BLOCKED-OWNER | B-06 |" in md


def test_rejected_shared_windows_import_is_incomplete_not_blocked_owner(tmp_path):
    """匯入被拒時缺少 l1_results 必須維持 INCOMPLETE；實際能力缺失仍由 caps_missing 單獨判 BLOCKED-OWNER。"""
    names = ("win-test.sh (non-COM)", "win-test.sh --caps windows_com")
    rows = []
    for name in names:
        row = _row(name, rc=None, passed=0)
        row["required_set"] = g.expected_set(name)
        row["platform"] = "windows"
        rows.append(row)
    md = g.build_report(_results(tmp_path, rows, caps_missing=[]))
    for name in names:
        assert f"| {name} | INCOMPLETE | 缺 l1_results |" in md


def test_com_row_with_pair_judged_by_run_not_blocked(tmp_path):
    # OD-15：擁有者 host 提供 COM20:COM21 時，COM 列依實跑結果判定，不得為 BLOCKED-OWNER
    md = g.build_report(_results(tmp_path, [_row("win-test.sh --caps windows_com", rc=1, passed=2, failed=1)]))
    assert "| win-test.sh --caps windows_com | FAIL |" in md


def test_owner_items_blocked_without_evidence_and_pass_with_verified_evidence(tmp_path):
    md = g.build_report(_results(tmp_path, []))
    assert "| B-02 | BLOCKED-OWNER |" in md
    ev = tmp_path / "b02.json"
    ev.write_text("{}", encoding="utf-8")
    import hashlib
    digest = hashlib.sha256(ev.read_bytes()).hexdigest()
    md = g.build_report(_results(tmp_path, [], name="run2", owner_evidence={"B-02": [{"path": str(ev), "sha256": digest}]}))
    assert "| B-02 | PASS |" in md
    md = g.build_report(_results(tmp_path, [], name="run3", owner_evidence={"B-02": [{"path": str(ev), "sha256": "0" * 64}]}))
    assert "| B-02 | BLOCKED-OWNER |" in md


def _artifacts(tmp):
    """必要 artifact（serialwrap／serialwrapd）的合成檔與正確 sha256。"""
    import hashlib
    out = {}
    for name in g.REQUIRED_ARTIFACTS:
        f = tmp / name
        f.write_bytes(name.encode())
        out[str(f)] = hashlib.sha256(name.encode()).hexdigest()
    return out


def test_verify_detects_tampering(tmp_path):
    d = _results(tmp_path, [_row("cargo test --workspace")])
    data = json.loads((d / "results.json").read_text(encoding="utf-8"))
    data["git_head"] = g.current_head()          # verify 核對目前 HEAD（P7-c5f0fc9-3）
    data["artifacts"] = _artifacts(tmp_path)     # verify 要求必要 artifact（P7-c5f0fc9-3 第二輪）
    prior = d / "prior-slice-artifacts" / "release" / "serialwrapd"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"release daemon")
    import hashlib
    data["prior_slice_artifacts"] = {"serialwrapd_release": hashlib.sha256(prior.read_bytes()).hexdigest()}
    data["prior_slice_artifact_paths"] = {"serialwrapd_release": "prior-slice-artifacts/release/serialwrapd"}
    (d / "results.json").write_text(json.dumps(data), encoding="utf-8")
    md = tmp_path / "gate.md"
    md.write_text(g.build_report(d), encoding="utf-8")
    assert g.verify_report(md, d) == []
    prior.write_bytes(b"tampered release daemon")
    assert any("先前切片 artifact sha256" in problem for problem in g.verify_report(md, d))
    md.write_text(md.read_text(encoding="utf-8").replace("| INCOMPLETE |", "| PASS |").replace(data["git_head"], "zzz"), encoding="utf-8")
    assert g.verify_report(md, d), "竄改後必須驗證失敗"


def test_required_rows_complete(tmp_path):
    assert set(g.REQUIRED_ROWS) >= {"check_cfg_boundary", "cargo test --workspace", "win-test.sh (non-COM)", "win-test.sh --caps windows_com",
        "contract_blackbox p7 A", "contract_blackbox p7 B", *g.OD59_ROWS, "contract_blackbox slice21 B", "contract_blackbox slice22 B",
        "l15 slice21 B", "l15 slice23 B", "func-test brcm-01-login-flow", "func-test ev-01-spawn-handler",
        g.WINDOWS_EV01_ROW,
        "regen_lock --check", "cases.lock slice_2.3 coverage",
        "python3 -m pytest -q tests/ --deselect tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native --deselect tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence",
        "policy_check"}
    script = (ROOT / "scripts/rust/p7_gate.sh").read_text(encoding="utf-8")
    assert 'run_row "policy_check" policy-check --evidence "$RUN/logs/policy-check.log" -- python3 -m policy_check --repo . --pr-labels "$POLICY_LABEL"' in script
    row = _row("policy_check")
    row["argv"] = ["python3", "-m", "policy_check", "--repo", ".", "--pr-labels", "release:0.5.0"]
    policy_md = g.build_report(_results(tmp_path, [row], name="policy-label-report"))
    assert "- policy_check_pr_label: `release:0.5.0`" in policy_md
    assert 'win_l1() {  # win_l1 <dir> <該次 win-test log>' in script
    assert 'ls -t "$ROOT"/target/win-test-report' not in script
    assert "--windows-ev01-report" in script and "--windows-ev01-bundle" in script
    assert "P7_WIN_EV01_REPORT" in script and "P7_WIN_EV01_BUNDLE" in script

    run_id = "default-report-path"
    dry_run = subprocess.run(
        ["bash", str(ROOT / "scripts/rust/p7_gate.sh"), "--dry-run", "--run-id", run_id],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=30,
    )
    assert dry_run.returncode == 0, dry_run.stdout + dry_run.stderr
    default_report = ROOT / "target/p7-gate" / run_id / "p7-gate.md"
    assert str(default_report) in dry_run.stdout, dry_run.stdout
    assert "docs/rust-refactor/evidence/p7-gate-" not in dry_run.stdout


def test_missing_required_row_is_incomplete(tmp_path):
    md = g.build_report(_results(tmp_path, []))
    assert "| policy_check | INCOMPLETE | 未執行 |" in md
    assert "一般 p7 A／B 列只作小樣本快速檢查，不代表達成 500×3 契約" in md
    contract_section = md.index("## OD-59 契約量測")
    assert md.index("| OD-59 control latency 500x3 A | INCOMPLETE | 未執行 |", contract_section) > contract_section
    assert md.index("| OD-59 control latency 500x3 B | INCOMPLETE | 未執行 |", contract_section) > contract_section
    assert md.index("## 列狀態") < contract_section < md.index("## 擁有者項目")
    assert "結論：NOT-PASS" in md


# ---- R5-F3：測試列以凍結 nodeid 清單逐案判定（failing-first） ----
REQ = ["cargo:sw-core::login_fsm::a", "cargo:sw-core::login_fsm::b", "cargo:sw-core::quiet_gate::c"]
PHASES_OK = {"setup": "passed", "call": "passed", "teardown": "passed"}


def _lock(tmp, sid="p7_gate:cargo-test-workspace", plat="linux", ids=REQ):
    p = tmp / "l1.lock.json"
    p.write_text(json.dumps({"schema_version": 1, "sets": {sid: {plat: {"selector": {"paths": [], "markexpr": ""}, "nodeids": sorted(ids)}}}}), encoding="utf-8")
    return p


def _l1_row(tmp, rows, *, exit_code=0, finished=True, plat="linux", name="cargo test --workspace", sid="p7_gate:cargo-test-workspace", tag="a"):
    def phases(o):
        if o == "passed":
            return PHASES_OK
        if o == "failed":
            return {"setup": "passed", "call": "failed", "teardown": "passed"}
        return {"setup": "skipped", "call": None, "teardown": None}
    results = [{"nodeid": n, "outcome": o, "skip_kind": "capability" if o == "skipped" else None, "phases": phases(o)} for n, o in rows]
    tally = {"passed": sum(o == "passed" for _, o in rows), "failed": sum(o == "failed" for _, o in rows),
             "skipped": sum(o == "skipped" for _, o in rows)}
    rep = {"schema_version": 2, "format": "l1-results.v1", "runner": "cargo", "platform": plat, "argv": [], "markexpr": "",
           "counts": {"memory_only": tally}, "resources": {}, "not_collected_platform": [], "core_portable_platform_skips": [],
           "collected": [n for n, _ in rows], "deselected": [], "results": results, "exit_code": exit_code,
           "finished": finished, "deployed_content": None}
    f = tmp / f"l1-{tag}.json"
    f.write_text(json.dumps(rep), encoding="utf-8")
    r = _row(name, rc=exit_code if exit_code != 101 else 101, passed=tally["passed"], failed=tally["failed"])
    r.update({"required_set": sid, "l1_results": str(f)})
    return r


def test_positive_count_without_required_nodeids_is_not_pass(tmp_path):
    lock = _lock(tmp_path)
    row = _l1_row(tmp_path, [("cargo:sw-core::other::x", "passed")])          # passed>0、failed==0：舊規則會給 PASS
    md = g.build_report(_results(tmp_path, [row]), l1_lock=lock)
    assert "| cargo test --workspace | INCOMPLETE |" in md and "missing" in md


def test_required_ignored_is_not_pass(tmp_path):
    lock = _lock(tmp_path)
    row = _l1_row(tmp_path, [(REQ[0], "passed"), (REQ[1], "passed"), (REQ[2], "skipped")])
    assert "| cargo test --workspace | INCOMPLETE |" in g.build_report(_results(tmp_path, [row]), l1_lock=lock)


def test_duplicate_or_unfinished_is_not_pass(tmp_path):
    lock = _lock(tmp_path)
    dup = _l1_row(tmp_path, [(REQ[0], "passed"), (REQ[0], "passed"), (REQ[1], "passed"), (REQ[2], "passed")], tag="dup")
    assert "| cargo test --workspace | INCOMPLETE |" in g.build_report(_results(tmp_path, [dup]), l1_lock=lock)
    unf = _l1_row(tmp_path, [(n, "passed") for n in REQ], exit_code=101, finished=False, tag="unf")
    assert "| cargo test --workspace | INCOMPLETE |" in g.build_report(_results(tmp_path, [unf], name="run_unf"), l1_lock=lock)


def test_test_row_without_l1_results_is_incomplete(tmp_path):
    md = g.build_report(_results(tmp_path, [_row("cargo test --workspace", passed=500)]), l1_lock=_lock(tmp_path))
    assert "| cargo test --workspace | INCOMPLETE |" in md

    row = _l1_row(tmp_path, [(REQ[0], "passed")], plat="windows", name="win-test.sh (non-COM)",
                  sid="p7_gate:win-test-sh-non-com", tag="windows-old")
    row["platform"] = "windows"
    report_path = pathlib.Path(row["l1_results"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["provenance"] = {"git": {"head": "f" * 40, "clean": True,
                                    "end": {"head": "f" * 40, "clean": True}}}
    report_path.write_text(json.dumps(report), encoding="utf-8")
    run = _results(tmp_path, [row], name="run_windows_old_head")
    data = json.loads((run / "results.json").read_text(encoding="utf-8"))
    data["git_head"] = g.current_head()
    (run / "results.json").write_text(json.dumps(data), encoding="utf-8")
    win_lock = _lock(tmp_path, sid="p7_gate:win-test-sh-non-com", plat="windows", ids=[REQ[0]])
    md = g.build_report(run, l1_lock=win_lock)
    assert "| win-test.sh (non-COM) | INCOMPLETE |" in md
    assert "provenance.git.head" in md


def test_full_required_set_passes(tmp_path):
    lock = _lock(tmp_path)
    row = _l1_row(tmp_path, [(n, "passed") for n in REQ] + [("cargo:sw-core::extra::y", "passed")], tag="ok")
    assert "| cargo test --workspace | PASS |" in g.build_report(_results(tmp_path, [row]), l1_lock=lock)
    bad = _l1_row(tmp_path, [(REQ[0], "failed"), (REQ[1], "passed"), (REQ[2], "passed")], tag="bad")
    assert "| cargo test --workspace | FAIL |" in g.build_report(_results(tmp_path, [bad], name="run_bad"), l1_lock=lock)


def test_non_test_row_evidence_mismatch_is_incomplete(tmp_path):
    ev = tmp_path / "ev.txt"
    ev.write_text("x", encoding="utf-8")
    row = _row("regen_lock --check", ev=[{"path": str(ev), "sha256": "0" * 64}])  # hash 不符
    assert "| regen_lock --check | INCOMPLETE |" in g.build_report(_results(tmp_path, [row]))
    row = _row("policy_check", rc=1)
    assert "| policy_check | FAIL |" in g.build_report(_results(tmp_path, [row], name="run_pc"))


# ---- 審查 9065f14_c5f0fc9（Codex sol）回歸 ----
def test_substitute_smaller_required_set_is_rejected(tmp_path):
    """P7-c5f0fc9-1：列名對應的必要集合固定，不得以較小集合替代。"""
    lock = tmp_path / "l1.lock.json"
    lock.write_text(json.dumps({"schema_version": 1, "sets": {
        "p7_gate:cargo-test-workspace": {"linux": {"selector": {"paths": [], "markexpr": ""}, "nodeids": sorted(REQ)}},
        "p7_gate:py_parity_attach": {"linux": {"selector": {"paths": [], "markexpr": ""}, "nodeids": [REQ[0]]}}}}), encoding="utf-8")
    row = _l1_row(tmp_path, [(REQ[0], "passed")], sid="p7_gate:py_parity_attach", tag="sub")
    md = g.build_report(_results(tmp_path, [row]), l1_lock=lock)
    assert "| cargo test --workspace | INCOMPLETE |" in md, md


def test_non_test_row_without_evidence_or_hash_is_not_pass(tmp_path):
    """P7-c5f0fc9-2：非測試列缺證據、或證據缺有效 sha256 → 不得 PASS；擁有者證據缺 hash → BLOCKED-OWNER。"""
    md = g.build_report(_results(tmp_path, [_row("func-test ev-01-spawn-handler")]))
    assert "| func-test ev-01-spawn-handler | INCOMPLETE |" in md
    ev = tmp_path / "e.txt"
    ev.write_text("x", encoding="utf-8")
    md = g.build_report(_results(tmp_path, [_row("policy_check", ev=[{"path": str(ev)}])], name="r2"))
    assert "| policy_check | INCOMPLETE |" in md
    md = g.build_report(_results(tmp_path, [], name="r3", owner_evidence={"B-02": [{"path": str(ev)}]}))
    assert "| B-02 | BLOCKED-OWNER |" in md


def test_verify_checks_head_and_artifacts(tmp_path):
    """P7-c5f0fc9-3：verify 核對目前 HEAD 與 artifact 實際 sha256。"""
    d = _results(tmp_path, [_row("cargo test --workspace")])
    data = json.loads((d / "results.json").read_text(encoding="utf-8"))
    data["git_head"] = g.current_head()
    arts = _artifacts(tmp_path)
    art = tmp_path / "serialwrapd"
    art.write_bytes(b"artifact")
    data["artifacts"] = {**arts, str(art): "0" * 64}
    (d / "results.json").write_text(json.dumps(data), encoding="utf-8")
    md = tmp_path / "gate.md"
    md.write_text(g.build_report(d), encoding="utf-8")
    assert any("artifact" in p for p in g.verify_report(md, d))
    import hashlib
    data["artifacts"] = {**arts, str(art): hashlib.sha256(b"artifact").hexdigest()}
    (d / "results.json").write_text(json.dumps(data), encoding="utf-8")
    md.write_text(g.build_report(d), encoding="utf-8")
    assert g.verify_report(md, d) == []
    data["git_head"] = "0" * 40
    (d / "results.json").write_text(json.dumps(data), encoding="utf-8")
    md.write_text(g.build_report(d), encoding="utf-8")
    assert any("HEAD" in p for p in g.verify_report(md, d))


def test_init_and_record_only_record_identity_and_counts(tmp_path):
    """`init`／`record` 只記錄不判定：artifact 為 repo 相對路徑＋sha256；測試列附列名對應的 required_set 與 l1 計數，
    log／evidence 落 sha256；同名列覆寫（審查 P7-c5f0fc9-1：required_set 不由呼叫端決定）。"""
    run = tmp_path / "run"
    art = ROOT / "scripts/rust/p7_gate_report.py"
    prior = run / "prior-slice-artifacts" / "release" / "serialwrapd"
    prior.parent.mkdir(parents=True)
    prior.write_bytes(b"release daemon")
    data = g.init_results(run, "r9", caps_missing=["windows_com", "windows_com"], artifacts=[art],
                          prior_slice_artifacts=[("serialwrapd_release", prior)])
    assert data["caps_missing"] == ["windows_com"] and data["git_head"] == g.current_head()
    assert data["artifacts"] == {"scripts/rust/p7_gate_report.py": g._sha256(art)}
    assert data["prior_slice_artifacts"] == {"serialwrapd_release": g._sha256(prior)}
    assert data["prior_slice_artifact_paths"] == {"serialwrapd_release": "prior-slice-artifacts/release/serialwrapd"}
    log = run / "x.log"
    log.write_text("hello", encoding="utf-8")
    l1 = run / "l1.json"
    l1.write_text(json.dumps({"results": [{"outcome": "passed"}, {"outcome": "failed"}, {"outcome": "skipped"}]}),
                  encoding="utf-8")
    row = g.record_row(run, name="cargo test --workspace", rc=1, log=log, argv=["cargo"], started_at="t0",
                       ended_at="t1", l1_results=l1, evidence=[log])
    assert row["required_set"] == "p7_gate:cargo-test-workspace" and row["l1_results"] == "l1.json"
    assert row["test_counts"] == {"passed": 1, "failed": 1, "ignored": 1}
    assert row["log_path"] == "x.log" and row["evidence"] == [{"path": "x.log", "sha256": g._sha256(log)}]
    g.record_row(run, name="cargo test --workspace", rc=0, log=log, argv=["cargo"], started_at="t0", ended_at="t1")
    rows = json.loads((run / "results.json").read_text(encoding="utf-8"))["rows"]
    assert [r["rc"] for r in rows] == [0]
    nt = g.record_row(run, name="policy_check", rc=0, log=log, argv=["p"], started_at="t0", ended_at="t1")
    assert "required_set" not in nt and "test_counts" not in nt


def test_verify_requires_cli_and_daemon_artifacts(tmp_path):
    """審查 P7-c5f0fc9-3（第二輪）：`artifacts:{}` 或缺 CLI／daemon 任一者，verify 必須回報不一致。"""
    d = _results(tmp_path, [_row("cargo test --workspace")])
    data = json.loads((d / "results.json").read_text(encoding="utf-8"))
    data["git_head"] = g.current_head()
    md = tmp_path / "gate.md"
    for arts, missing in (({}, {"serialwrap", "serialwrapd"}),
                          ({k: v for k, v in _artifacts(tmp_path).items() if k.endswith("serialwrapd")}, {"serialwrap"})):
        data["artifacts"] = arts
        (d / "results.json").write_text(json.dumps(data), encoding="utf-8")
        md.write_text(g.build_report(d), encoding="utf-8")
        problems = g.verify_report(md, d)
        assert {p.split("：", 1)[1] for p in problems if p.startswith("缺必要 artifact")} == missing, problems



def test_full_pytest_row_needs_whole_frozen_set(tmp_path):
    """N3：兩個 Stage 1 證據 nodeid 移出必要集合後，gate 必須使用裁決核准的新 selector。"""
    lock = json.loads((ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    sid = "p7_gate:python3-m-pytest-q-tests"
    spec = lock["sets"][sid]["linux"]
    frozen = spec["nodeids"]
    deselected = [
        "tests/test_rust_bench_tools.py::test_cargo_workspace_tests_windows_native",
        "tests/test_rust_stage1b_registration.py::test_p4_required_cases_have_passing_evidence",
    ]
    assert not set(deselected) & set(frozen)
    assert spec["selector"]["argv"] == [
        "-q", "tests/", "--deselect", deselected[0], "--deselect", deselected[1],
    ]
    assert len(frozen) > 5000, len(frozen)
    assert "tests/test_p7_gate_win_argv.py::test_gate_snapshots_artifacts_before_cargo_rebuilds" in frozen
    name = (
        "python3 -m pytest -q tests/ --deselect " + deselected[0] + " --deselect " + deselected[1]
    )
    judge = g._load_judge()
    for subset, want in ((frozen[:49], "INCOMPLETE"), (frozen, "PASS")):
        row = _l1_row(tmp_path, [(n, "passed") for n in subset], name=name, sid=sid, tag=f"n{len(subset)}")
        rep_path = tmp_path / row["l1_results"]
        rep = json.loads(rep_path.read_text(encoding="utf-8"))
        rep.update(runner="pytest", argv=["pytest/__main__.py", *spec["selector"]["argv"]])
        rep_path.write_text(json.dumps(rep), encoding="utf-8")
        status, detail = g._test_row_status(tmp_path, row, lock, judge)
        assert status == want, (len(subset), status, detail)

    legacy_name = "python3 -m pytest -q tests/"
    row = _l1_row(tmp_path, [(n, "passed") for n in frozen], name=legacy_name, sid=sid, tag="n3-old-selector")
    rep_path = tmp_path / row["l1_results"]
    rep = json.loads(rep_path.read_text(encoding="utf-8"))
    rep.update(runner="pytest", argv=["pytest/__main__.py", "-q", "tests/"])
    rep_path.write_text(json.dumps(rep), encoding="utf-8")
    status, detail = g._test_row_status(tmp_path, row, lock, judge)
    assert status == "INCOMPLETE" and "selector mismatch" in detail, (status, detail)


def test_report_records_tool_versions(tmp_path):
    """審查 P7-b85eec5-1：results.json 與報告須記受測 pytest／Python 版本（凍結 nodeid 的參數 id 依 pytest 版本而異）。"""
    import pytest as _pytest
    data = g.init_results(tmp_path / "run", "rv", caps_missing=[], artifacts=[])
    assert data["tool_versions"]["pytest"] == _pytest.__version__ and data["tool_versions"]["python"]
    md = g.build_report(tmp_path / "run")
    assert f'"pytest": "{_pytest.__version__}"' in md


def test_report_records_od87_exemption_and_actual_breaker_verdict(tmp_path):
    run = _results(tmp_path, [])
    breaker = {
        "slice": "2.3",
        "actual_verdict": "FAIL",
        "gate_verdict": "OD-87_EXEMPT",
        "exemption": {"decision": "OD-87", "date": "2026-11-14"},
        "assessment": {"tripped": True, "insufficient_evidence": False, "window_delta_pp": 0.0},
    }
    evidence = tmp_path / "breaker.json"
    evidence.write_text(json.dumps(breaker), encoding="utf-8")
    g.record_breaker(run, evidence, exit_code=0)
    md = g.build_report(run)
    assert "OD-87 豁免" in md and "2026-11-14" in md, md
    assert "實際狀態：FAIL" in md, md
    stored = json.loads((run / "results.json").read_text(encoding="utf-8"))
    assert stored["breaker"]["actual_verdict"] == "FAIL"
    assert stored["breaker"]["gate_verdict"] == "OD-87_EXEMPT"


def test_owner_items_cover_every_coverage_b_number(tmp_path):
    """rulings/p7.md SPEC-GAP #4 (i)：coverage 引用的每個 B 編號（含先前切片 B-09～B-16）都須是報告的擁有者項目，
    無證據時列為 BLOCKED-OWNER（不得 PASS）。"""
    import p7_case_coverage as cov
    used = set(cov.OWNER_BY_SUITE.values()) | set(cov.OWNER_BY_REGRESSION_FAMILY.values())
    assert {f"B-{i:02d}" for i in range(9, 17)} <= used <= set(g.OWNER_ITEMS), sorted(used - set(g.OWNER_ITEMS))
    md = g.build_report(_results(tmp_path, []))
    for bid in sorted(used):
        assert f"| {bid} | BLOCKED-OWNER |" in md, bid


def _windows_ev01_fixture(tmp_path, *, bundle_head=None, meta_head=None, verdict="PASS", gate_eligible=True):
    import hashlib

    head = bundle_head or g.current_head()
    bundle = tmp_path / "windows-bundle"
    version = "0.5.0"
    version_dir = bundle / "versions" / version
    version_bin = version_dir / "bin"
    root_bin = bundle / "bin"
    version_bin.mkdir(parents=True)
    root_bin.mkdir(parents=True)
    cli = b"stage2-cli-exe"
    daemon = b"stage2-daemon-exe"
    (version_bin / "serialwrap.exe").write_bytes(cli)
    (version_bin / "serialwrapd.exe").write_bytes(daemon)
    (root_bin / "serialwrap.exe").write_bytes(cli)
    (root_bin / "serialwrapd.exe").write_bytes(daemon)
    (root_bin / "current-release.json").write_text(
        json.dumps({"bundle_version": version, "dir": f"versions/{version}"}), encoding="utf-8"
    )
    manifest = {
        "schema_version": 1,
        "bundle_version": version,
        "stage": "rust-2",
        "git_revision": head,
        "target": "x86_64-pc-windows-gnu",
        "components": {
            "cli": {"implementation": "rust", "version": version, "git_revision": head,
                    "path": "bin/serialwrap.exe", "sha256": hashlib.sha256(cli).hexdigest()},
            "daemon": {"implementation": "rust", "version": version, "git_revision": head,
                       "launcher": {"path": "bin/serialwrapd.exe", "sha256": hashlib.sha256(daemon).hexdigest()}},
        },
    }
    (version_dir / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (version_dir / "SHA256SUMS").write_text(
        f"{hashlib.sha256(cli).hexdigest()}  bin/serialwrap.exe\n"
        f"{hashlib.sha256(daemon).hexdigest()}  bin/serialwrapd.exe\n", encoding="utf-8"
    )
    report_root = tmp_path / "win-func-test"
    basic = report_root / "basic" / "func-test-report.json"
    basic.parent.mkdir(parents=True)
    report = {
        "schema_version": "func-test-report.v1",
        "complete": True,
        "independent_cases": 1,
        "modes": {"bridge": {"runs": 1, "pass": int(verdict == "PASS"), "fail": int(verdict == "FAIL"),
                              "insufficient": int(verdict == "INSUFFICIENT_EVIDENCE"), "skip": int(verdict == "SKIP")}},
        "results": [{"case_id": "ev-01-spawn-handler", "mode": "bridge", "run_index": 0,
                     "verdict": verdict, "gate_eligible": gate_eligible}],
    }
    report_bytes = json.dumps(report, sort_keys=True).encode()
    basic.write_bytes(report_bytes)
    identity = g.windows_bundle_identity(bundle)
    meta = {
        "schema_version": "win-func-test-meta.v2",
        "git_head": meta_head or g.current_head(),
        "git_tree": "a" * 40,
        "git_clean": True,
        "com_pair": {"daemon": "COM20", "bridge": "COM21"},
        "transport_mode": "virtual_com0com",
        "basic_report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "bundle_sha256": identity["bundle_sha256"],
        "bundle_stage": identity["stage"],
        "bundle_version": identity["version"],
        "bundle_git_revision": identity["git_revision"],
        "bundle_manifest_sha256": identity["manifest_sha256"],
        "cli_exe_sha256": identity["cli_sha256"],
        "daemon_exe_sha256": identity["daemon_sha256"],
        "daemon_exe_used": True,
        "case_filter": "ev-01-spawn-handler",
        "scenario_skipped": True,
        "runs": {"scenarios": {"runner_argv": [], "skipped": True, "runner_exit": 0},
                 "basic": {"report": "basic/func-test-report.json", "runner_exit": 0,
                            "runner_argv": ["-3.12", "func-test/runner.py", "--mode", "bridge", "--case",
                                            "ev-01-spawn-handler", "--daemon-com", "COM20", "--bridge-com", "COM21",
                                            "--transport-mode", "virtual_com0com"]}},
    }
    (report_root / "win-func-test-meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return basic, bundle


def test_windows_ev01_is_a_required_row_and_missing_report_is_incomplete(tmp_path):
    assert g.WINDOWS_EV01_ROW in g.REQUIRED_ROWS
    status, _detail = g.windows_ev01_status(None, None, g.current_head())
    assert status == "INCOMPLETE"
    run = _results(tmp_path, [])
    data = json.loads((run / "results.json").read_text(encoding="utf-8"))
    data["git_head"] = g.current_head()
    (run / "results.json").write_text(json.dumps(data), encoding="utf-8")
    g.record_windows_ev01(run, report_path=None, bundle_dir=None, log_path=run / "logs/windows-ev01.log")
    md = g.build_report(run)
    row = next(line for line in md.splitlines() if g.WINDOWS_EV01_ROW in line)
    assert f"| {g.WINDOWS_EV01_ROW} | INCOMPLETE |" in row
    assert "BLOCKED-OWNER" not in row
    assert "結論：NOT-PASS" in md


def test_windows_ev01_rejects_report_bound_to_another_head(tmp_path):
    basic, bundle = _windows_ev01_fixture(tmp_path, meta_head="f" * 40)
    status, detail = g.windows_ev01_status(basic, bundle, g.current_head())
    assert status == "INCOMPLETE", detail
    assert "HEAD" in detail


@pytest.mark.parametrize("verdict,gate_eligible", [("FAIL", True), ("INSUFFICIENT_EVIDENCE", False),
                                                      ("SKIP", False), ("PASS", False)])
def test_windows_ev01_nonpassing_or_ineligible_case_never_passes(tmp_path, verdict, gate_eligible):
    basic, bundle = _windows_ev01_fixture(tmp_path, verdict=verdict, gate_eligible=gate_eligible)
    status, _detail = g.windows_ev01_status(basic, bundle, g.current_head())
    assert status != "PASS"
    assert status == ("FAIL" if verdict == "FAIL" else "INCOMPLETE")


def test_windows_ev01_passes_with_verified_head_bundle_and_eligible_result(tmp_path):
    basic, bundle = _windows_ev01_fixture(tmp_path)
    status, detail = g.windows_ev01_status(basic, bundle, g.current_head())
    assert status == "PASS", detail
    assert "gate_eligible" in detail


def test_windows_ev01_snapshot_row_is_reported_and_recomputed(tmp_path):
    basic, bundle = _windows_ev01_fixture(tmp_path)
    run = _results(tmp_path, [])
    data = json.loads((run / "results.json").read_text(encoding="utf-8"))
    data["git_head"] = g.current_head()
    (run / "results.json").write_text(json.dumps(data), encoding="utf-8")
    log = run / "logs/windows-ev01.log"
    g.record_windows_ev01(run, report_path=basic, bundle_dir=bundle, log_path=log)
    md = g.build_report(run)
    assert f"| {g.WINDOWS_EV01_ROW} | PASS |" in md
    assert g._row_status(run, json.loads((run / "results.json").read_text())['rows'][0],
                         set(), None, None, g.current_head())[0] == "PASS"


def test_windows_bundle_identity_rejects_release_test_hooks(tmp_path):
    import hashlib

    _basic, bundle = _windows_ev01_fixture(tmp_path)
    version_dir = bundle / "versions/0.5.0"
    manifest_path = version_dir / "release-manifest.json"
    sums_path = version_dir / "SHA256SUMS"

    def replace_binaries(cli: bytes, daemon: bytes) -> None:
        for root in (version_dir / "bin", bundle / "bin"):
            (root / "serialwrap.exe").write_bytes(cli)
            (root / "serialwrapd.exe").write_bytes(daemon)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["components"]["cli"]["sha256"] = hashlib.sha256(cli).hexdigest()
        manifest["components"]["daemon"]["launcher"]["sha256"] = hashlib.sha256(daemon).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        sums_path.write_text(
            f"{hashlib.sha256(cli).hexdigest()}  bin/serialwrap.exe\n"
            f"{hashlib.sha256(daemon).hexdigest()}  bin/serialwrapd.exe\n",
            encoding="utf-8",
        )

    allowed = (
        b"SERIALWRAP_TEST_DEVICE_ALLOWLIST",
        b"SERIALWRAP_TEST_SYSTEM_CONFIG_PATH",
        b"SERIALWRAP_TEST_KILL_AFTER_SAVE",
        b"SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE",
    )
    daemon = b"stage2-daemon-exe\0" + b"\0".join(allowed)
    cli = b"stage2-cli-exe\0SERIALWRAP_TEST_SYSTEM_CONFIG_PATH"
    replace_binaries(cli, daemon)
    identity = g.windows_bundle_identity(bundle)
    assert identity["daemon_sha256"] == hashlib.sha256(daemon).hexdigest()

    for forbidden in (b"SERIALWRAP_TEST_UNAPPROVED_HOOK", b"SW_TEST_UPDATE_FUTURE_HOOK"):
        replace_binaries(cli, b"stage2-daemon-exe\0" + forbidden)
        with pytest.raises(ValueError, match="禁用測試鉤子"):
            g.windows_bundle_identity(bundle)

    for daemon_only in (
        b"SERIALWRAP_TEST_DEVICE_ALLOWLIST",
        b"SERIALWRAP_TEST_KILL_AFTER_SAVE",
        b"SERIALWRAP_TEST_PAUSE_BEFORE_RELEASE_SAVE",
    ):
        replace_binaries(cli + b"\0" + daemon_only, daemon)
        with pytest.raises(ValueError, match="禁用測試鉤子"):
            g.windows_bundle_identity(bundle)


def test_windows_ev01_requires_stage2_bundle_sha256_identity(tmp_path):
    basic, bundle = _windows_ev01_fixture(tmp_path)
    metadata_path = basic.parent.parent / "win-func-test-meta.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["bundle_sha256"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    status, detail = g.windows_ev01_status(basic, bundle, g.current_head())
    assert status == "INCOMPLETE", detail
    assert "SHA-256" in detail or "SHA256" in detail
