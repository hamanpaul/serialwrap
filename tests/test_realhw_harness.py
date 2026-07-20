"""#122 realhw harness 純邏輯單測（不碰 live）。"""
from __future__ import annotations

import json

import pytest

from realhw import harness


def _mk(id, tier="p0", destructive=False):
    return harness.Case(id=id, tier=tier, title=id, run=lambda ctx: harness.CaseResult("PASS"),
                        destructive=destructive)


def test_select_by_tier_excludes_longrun():
    reg = [_mk("a", "p0"), _mk("b", "p1"), _mk("c", "longrun")]
    got = harness.select_cases(reg, tiers=["p0", "p1"], only=None, skip=[])
    assert [c.id for c in got] == ["a", "b"]  # longrun 絕不被 p0/p1 隱含


def test_select_only_overrides_tier():
    reg = [_mk("a", "p0"), _mk("b", "p1")]
    got = harness.select_cases(reg, tiers=["p0"], only="b", skip=[])
    assert [c.id for c in got] == ["b"]


def test_select_skip():
    reg = [_mk("a", "p1"), _mk("b", "p1")]
    got = harness.select_cases(reg, tiers=["p1"], only=None, skip=["a"])
    assert [c.id for c in got] == ["b"]


def test_select_unknown_only_raises():
    with pytest.raises(harness.UnknownCaseError):
        harness.select_cases([_mk("a")], tiers=["p0"], only="nope", skip=[])


@pytest.mark.parametrize("s,secs", [("32h", 115200), ("45m", 2700), ("3600s", 3600), ("2h", 7200)])
def test_parse_duration(s, secs):
    assert harness.parse_duration(s) == secs


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        harness.parse_duration("soon")


def test_report_md_lists_all_and_details_failures(tmp_path):
    results = [
        ("p0-doctor", harness.CaseResult("PASS", duration_s=1.2)),
        ("p1-con-fanout", harness.CaseResult("FAIL", reason="marker 未出現",
                                             evidence={"pane": "p1-con-fanout/pane.txt"})),
        ("p1-hp-cycle", harness.CaseResult("SKIP", reason="前置不滿足：COM1 非 READY")),
    ]
    hints = {"p1-con-fanout": ("先確認 console 沒掉回 line-buffer",)}
    meta = {"version": "0.2.2", "git": "abc123", "tiers": "p0,p1", "started_at": "2026-07-02T10:00:00"}
    md = harness.render_report_md(meta, results, hints)
    assert "PASS: 1" in md and "FAIL: 1" in md and "SKIP: 1" in md
    assert "p1-con-fanout" in md and "marker 未出現" in md
    assert "先確認 console 沒掉回 line-buffer" in md          # 診斷提示進報告
    assert "p1-con-fanout/pane.txt" in md                      # evidence 連結

    harness.write_reports(tmp_path, meta, results, hints)
    data = json.loads((tmp_path / "report.json").read_text())
    assert data["meta"]["git"] == "abc123"
    assert data["results"][1]["verdict"] == "FAIL"
    assert (tmp_path / "report.md").exists()


def test_recovery_command_is_state_aware():
    # case 間恢復依狀態選語意正確動詞，不再一律 device attach（會強搶已交接的裝置，Copilot #2）。
    assert harness.recovery_command("RELEASED") == ("device", "attach")   # 交接出去→收回
    assert harness.recovery_command("DETACHED") == ("session", "attach")  # 無 bridge→建立
    assert harness.recovery_command("ATTACHED") == ("session", "recover")  # 不健康→重建 bridge
    assert harness.recovery_command("RECOVERING") == ("session", "recover")
    assert harness.recovery_command(None) == ("session", "recover")
    # 只有 RELEASED 才允許 device attach
    assert harness.recovery_command("ATTACHED") != ("device", "attach")


def test_case_result_classification_fields_default_empty():
    r = harness.CaseResult("PASS")
    assert r.category == ""
    assert r.reason_code == ""
    r2 = harness.CaseResult("FAIL", reason="x", category="test", reason_code="cross_talk")
    assert (r2.category, r2.reason_code) == ("test", "cross_talk")


def test_report_shows_category_column_and_json_fields(tmp_path):
    results = [
        ("a", harness.CaseResult("PASS", duration_s=0.1)),
        ("b", harness.CaseResult("FAIL", reason="斷言不過", category="test",
                                 reason_code="cross_talk")),
        ("c", harness.CaseResult("SKIP", reason="缺 base64", category="environment",
                                 reason_code="base64_missing")),
    ]
    meta = {"version": "0.2.3", "git": "abc", "tiers": "p0", "started_at": "t"}
    md = harness.render_report_md(meta, results, {})
    assert "| 分類 |" in md
    assert "test/cross_talk" in md
    assert "environment/base64_missing" in md
    harness.write_reports(tmp_path, meta, results, {})
    data = json.loads((tmp_path / "report.json").read_text())
    assert data["results"][1]["category"] == "test"
    assert data["results"][1]["reason_code"] == "cross_talk"
    assert data["results"][0]["category"] == ""


def test_run_cases_uncaught_exception_is_inconclusive(tmp_path):
    def boom(ctx):
        raise RuntimeError("kaboom")

    cases = [harness.Case(id="x", tier="p0", title="x", run=boom)]
    ctx = harness.Ctx(cfg={}, report_dir=tmp_path, case_dir=tmp_path,
                      sw=None, tmux=None, usbipd=None, systemd=None)
    results = harness.run_cases(cases, ctx, boards=[])
    (cid, r), = results
    assert cid == "x"
    assert r.verdict == "FAIL"
    assert r.category == ""
    assert r.reason_code == "uncaught_exception"


def test_load_cfg_reads_config_json_with_defaults(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"boards": [], "usbipd_exe": "/x", "tmux_prefix": "t"}', encoding="utf-8")
    cfg = harness.load_cfg(p)
    assert cfg["usbipd_exe"] == "/x"
    assert cfg["win_serialwrap_exe"] == ""


def test_load_cfg_injected_dict_equivalent(tmp_path):
    facts = {"boards": [{"com": "COM0", "serial": "S1", "busid": "1-1"}],
             "usbipd_exe": "/x", "tmux_prefix": "t"}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(facts), encoding="utf-8")
    assert harness.load_cfg(p) == harness.load_cfg(injected=facts)
    assert harness.load_cfg(injected=facts)["win_serialwrap_exe"] == ""
    assert "win_serialwrap_exe" not in facts


def test_run_cases_family_gate_runtime_skip(tmp_path):
    ran = []

    def ok_run(ctx):
        ran.append(1)
        return harness.CaseResult("PASS")

    cases = [
        harness.Case(id="rm-x", tier="remote", title="x", run=ok_run,
                     requires=("docker", "remote_capability")),
        harness.Case(id="p0-y", tier="p0", title="y", run=ok_run, requires=("two_boards",)),
    ]
    ctx = harness.Ctx(cfg={}, report_dir=tmp_path, case_dir=tmp_path,
                      sw=None, tmux=None, usbipd=None, systemd=None)
    results = harness.run_cases(cases, ctx, boards=[],
                                missing_caps={"docker": "docker_unavailable"})
    assert results[0][1].verdict == "SKIP"
    assert results[0][1].category == "environment"
    assert results[0][1].reason_code == "docker_unavailable"
    assert results[1][1].verdict == "PASS"
    assert ran == [1]
