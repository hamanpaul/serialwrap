"""P7 Task 19（08-slice23-login-events.md）：Slice 2.3 出口 gate 報告產生器與驗證器（合成 fixture，不讀「最新報告」）。

斷言依計畫 Task 19 Step 1；l1-results fixture 為合法的 P2 `l1-results.v1`（計畫範例的 fixture 缺 `counts`／`resources`／
`not_collected_platform`／`deployed_content` 等必填欄位、phases 為空，交給 P2 `l1_judge` 一律 INVALID，無法表達
「完整必要集合 → PASS」，故補齊形狀、斷言不變）。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/rust"))
import p7_gate_report as g  # noqa: E402

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


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


def test_verify_detects_tampering(tmp_path):
    d = _results(tmp_path, [_row("cargo test --workspace")])
    data = json.loads((d / "results.json").read_text(encoding="utf-8"))
    data["git_head"] = g.current_head()          # verify 核對目前 HEAD（P7-c5f0fc9-3）
    (d / "results.json").write_text(json.dumps(data), encoding="utf-8")
    md = tmp_path / "gate.md"
    md.write_text(g.build_report(d), encoding="utf-8")
    assert g.verify_report(md, d) == []
    md.write_text(md.read_text(encoding="utf-8").replace("| INCOMPLETE |", "| PASS |").replace(data["git_head"], "zzz"), encoding="utf-8")
    assert g.verify_report(md, d), "竄改後必須驗證失敗"


def test_required_rows_complete():
    assert set(g.REQUIRED_ROWS) >= {"check_cfg_boundary", "cargo test --workspace", "win-test.sh (non-COM)", "win-test.sh --caps windows_com",
        "contract_blackbox p7 A", "contract_blackbox p7 B", "contract_blackbox slice21 B", "contract_blackbox slice22 B",
        "l15 slice21 B", "l15 slice23 B", "func-test brcm-01-login-flow", "func-test ev-01-spawn-handler",
        "regen_lock --check", "cases.lock slice_2.3 coverage", "python3 -m pytest -q tests/", "policy_check"}


def test_missing_required_row_is_incomplete(tmp_path):
    md = g.build_report(_results(tmp_path, []))
    assert "| policy_check | INCOMPLETE | 未執行 |" in md
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
    art = tmp_path / "bin"
    art.write_bytes(b"artifact")
    data["artifacts"] = {str(art): "0" * 64}
    (d / "results.json").write_text(json.dumps(data), encoding="utf-8")
    md = tmp_path / "gate.md"
    md.write_text(g.build_report(d), encoding="utf-8")
    assert any("artifact" in p for p in g.verify_report(md, d))
    import hashlib
    data["artifacts"] = {str(art): hashlib.sha256(b"artifact").hexdigest()}
    (d / "results.json").write_text(json.dumps(data), encoding="utf-8")
    md.write_text(g.build_report(d), encoding="utf-8")
    assert g.verify_report(md, d) == []
    data["git_head"] = "0" * 40
    (d / "results.json").write_text(json.dumps(data), encoding="utf-8")
    md.write_text(g.build_report(d), encoding="utf-8")
    assert any("HEAD" in p for p in g.verify_report(md, d))
