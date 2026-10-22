"""P9-T17：`python3 -m tools.stage3` 入口的失敗測試（TDD red）。

所有 `tools.stage3` 匯入皆為函式內的延遲匯入，使套件在實作存在前仍可收集。
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

# 夾具一律由 `stage3_fakes` 於 tmp 目錄產生，不讀取 repo 內 `tests/acceptance/canary_policy.json` 或
# `packaging/python_daemon_retirement.json`：擁有者凍結或退役核准後，那些檔案會改變，測試不得隨之變紅（R-A2 審查）。
# 合法的 draft 政策必須通過 plan Task 12 的 `load_policy`，故以覆寫欄位產生，而非 `{"status": "draft"}`。
_BTREE = "b" * 64
_RTREE = "d" * 64


def _fakes():
    return importlib.import_module("stage3_fakes")


def _draft_policy(tmp_path: Path, name: str = "draft-policy.json") -> Path:
    """未凍結的 draft 政策；凍結欄位以覆寫清空，與 repo 內政策檔的凍結狀態無關。"""
    return _fakes().write_policy(
        tmp_path / name,
        status="draft",
        frozen_by=None,
        frozen_at=None,
        decision_ref=None,
        **{"platforms.linux.evidence_owner": None, "platforms.windows.evidence_owner": None},
    )


def _frozen_policy_doc() -> dict:
    """已凍結政策的 JSON 物件（decision_ref 為 OD-TEST-POLICY，與 addenda 列對應）。"""
    return _fakes().policy_dict(
        decision_ref="OD-TEST-POLICY",
        **{"platforms.linux.evidence_owner": "linux-owner", "platforms.windows.evidence_owner": "windows-owner"},
    )


def _frozen_policy_file(tmp_path: Path) -> tuple[Path, Path]:
    """已凍結政策檔與含 decision_ref 列的 addenda（`canary report` 的 load_policy 需要）。"""
    policy = _fakes().write_policy(tmp_path / "policy.json")
    addenda = tmp_path / "addenda.md"
    addenda.write_text(
        "| 決策 | 主題 | 日期 | 說明 |\n|---|---|---|---|\n"
        "| OD-10-C1 | Canary policy 凍結 | 2026-11-01 | frozen |\n",
        encoding="utf-8",
    )
    return policy, addenda


def _report_argv(policy: Path, addenda: Path, ledgers: list[Path], *, artifact: str = "art-test") -> list[str]:
    return [
        "canary", "report",
        "--policy", str(policy),
        "--addenda", str(addenda),
        "--ledger", *(str(path) for path in ledgers),
        "--platform", "linux",
        "--artifact-id", artifact,
        "--bundle-tree-sha256", _BTREE,
        "--runtime-tree-sha256", _RTREE,
    ]


def _retirement_root(tmp_path: Path) -> Path:
    """未核准的退役裁決檔（retired:false）於 tmp repo 根目錄；`--repo-root` 指向此處。"""
    root = tmp_path / "repo"
    (root / "packaging").mkdir(parents=True)
    doc = {
        "approved_at": None,
        "approved_by": None,
        "decision_ref": None,
        "effective_bundle_version": None,
        "evidence_index": "docs/rust-refactor/evidence/index.json",
        "evidence_index_sha256": None,
        "retired": False,
        "schema_version": 2,
        "stage3_target_artifact_ids": {"linux": None, "windows": None},
    }
    (root / "packaging" / "python_daemon_retirement.json").write_text(json.dumps(doc), encoding="utf-8")
    return root


def _run(capsys, argv):
    from tools.stage3.__main__ import main

    rc = main(argv)
    out = capsys.readouterr().out
    return rc, out


def _json_line(out: str) -> dict:
    return json.loads(out.strip().splitlines()[-1])


def test_help_lists_groups():
    from tools.stage3.__main__ import build_parser

    text = build_parser().format_help()
    for group in ("ab", "canary", "rollback-drill", "evidence", "od09", "retirement"):
        assert group in text


def test_harness_error_exit_2(capsys, tmp_path):
    root = _retirement_root(tmp_path)
    rc, out = _run(capsys, ["retirement", "verify", "--mode", "release", "--repo-root", str(root)])
    assert rc == 2
    assert _json_line(out)["reason_code"] == "HARNESS_RETIREMENT_NOT_APPROVED"


def test_stage3_bundle_plan_candidate_exit_0(capsys, tmp_path):
    root = _retirement_root(tmp_path)
    rc, out = _run(capsys, ["retirement", "bundle-plan", "--stage", "3", "--repo-root", str(root)])
    assert rc == 0
    assert _json_line(out)["candidate"] is True


def test_evidence_status_requires_root(capsys, tmp_path):
    rc, _ = _run(capsys, ["evidence", "status", "--index", str(tmp_path / "idx.json"), "--stage", "3"])
    assert rc == 2


def test_canary_policy_freeze_cli(capsys, tmp_path):
    policy = _draft_policy(tmp_path)
    # P9-T17 修正：addenda 為附加式 Markdown 表（plan Task 12／owner-decisions-addenda.md），非 JSON 陣列。
    addenda = tmp_path / "addenda.md"
    addenda.write_text("| 決策 | 主題 | 日期 | 說明 |\n|---|---|---|---|\n", encoding="utf-8")
    rc, _ = _run(
        capsys,
        [
            "canary", "policy", "freeze",
            "--policy", str(policy),
            "--decision-id", "OD-TEST-1",
            "--frozen-by", "owner",
            "--evidence-owner", "linux=alice",
            "--evidence-owner", "windows=bob",
            "--addenda", str(addenda),
        ],
    )
    assert rc == 0
    assert json.loads(policy.read_text(encoding="utf-8"))["status"] == "frozen"
    rows = [ln for ln in addenda.read_text(encoding="utf-8").splitlines() if ln.startswith("| OD-TEST-1 |")]
    assert len(rows) == 1


def test_drill_requires_ack_flag(capsys, tmp_path):
    rc, _ = _run(
        capsys,
        [
            "rollback-drill", "run",
            "--cli", str(tmp_path / "serialwrap"),
            "--platform", "linux",
            "--boards", "COM0,COM1",
            "--expect-current", "1.0.0",
            "--expect-previous", "0.9.0",
            "--out", str(tmp_path / "out"),
        ],
    )
    assert rc == 2


def test_canary_report_exit_codes(capsys, tmp_path):
    # 結束碼矩陣（plan Task 17）：draft 政策 → 1／POLICY_NOT_FROZEN；已凍結但帳本為空 → 1／INCOMPLETE。
    # PASS → 0 與 --out 契約見 test_canary_report_pass_out_contract；POLICY_CHANGED 見 test_canary_report_policy_changed_exit_1。
    draft = _draft_policy(tmp_path)
    addenda = tmp_path / "addenda.md"
    addenda.write_text("| 決策 | 主題 | 日期 | 說明 |\n|---|---|---|---|\n", encoding="utf-8")
    ledger = tmp_path / "ledger.ndjson"
    ledger.write_text("", encoding="utf-8")
    rc, out = _run(capsys, _report_argv(draft, addenda, [ledger]))
    assert rc == 1
    assert _json_line(out)["result"] == "POLICY_NOT_FROZEN"

    policy, frozen_addenda = _frozen_policy_file(tmp_path / "frozen")
    empty = tmp_path / "frozen" / "empty.ndjson"
    empty.write_text("", encoding="utf-8")
    rc, out = _run(capsys, _report_argv(policy, frozen_addenda, [empty]))
    assert rc == 1
    assert _json_line(out)["result"] == "INCOMPLETE"


def test_canary_report_pass_out_contract(capsys, tmp_path):
    # 14 活躍日、5,040 次提交與五類循環（共 50 次）→ PASS、exit 0；`--out` 含 ingest 所需的四個鍵（R3-F1、R4-F1）。
    from tools.stage3 import canary_policy  # noqa: PLC0415

    policy, addenda = _frozen_policy_file(tmp_path)
    pol = canary_policy.load_policy(policy, addenda_path=addenda)
    records = importlib.import_module("fakes_canary").synthetic_canary(
        days=14,
        submits_per_day=360,
        cycles={
            "handoff_release_attach": 10,
            "hotplug_reattach": 5,
            "dut_reboot": 10,
            "recovery": 10,
            "human_agent_coexist": 15,
        },
        platform="linux",
        artifact="art-test",
        policy_sha256=pol.sha256,
    )
    ledger = tmp_path / "ledger.ndjson"
    ledger.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in records),
                      encoding="utf-8")
    out_path = tmp_path / "canary-linux.json"
    rc, out = _run(capsys, _report_argv(policy, addenda, [ledger]) + ["--out", str(out_path)])
    assert rc == 0
    assert _json_line(out)["result"] == "PASS"
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    assert doc["result"] == "PASS"
    assert doc["bundle_tree_sha256"] == _BTREE and doc["runtime_tree_sha256"] == _RTREE
    assert isinstance(doc["ledger_records"], int) and doc["ledger_records"] > 0
    assert isinstance(doc["ledger_through"], str) and doc["ledger_through"]
    rc, md = _run(capsys, _report_argv(policy, addenda, [ledger]) + ["--md"])
    assert rc == 0
    assert md.startswith("# Canary Verification Report")


def test_canary_report_missing_ledger_fails_closed(capsys, tmp_path):
    # 拼錯的第二個帳本路徑不得被略過（否則可能由部分帳本得出 PASS）：exit 2、HARNESS_LEDGER_CORRUPT，且不寫 --out。
    policy, addenda = _frozen_policy_file(tmp_path)
    real = tmp_path / "real.ndjson"
    real.write_text("", encoding="utf-8")
    out_path = tmp_path / "canary.json"
    rc, out = _run(capsys, _report_argv(policy, addenda, [real, tmp_path / "typo.ndjson"]) + ["--out", str(out_path)])
    assert rc == 2
    assert _json_line(out)["reason_code"] == "HARNESS_LEDGER_CORRUPT"
    assert not out_path.exists()


def test_canary_incident_list_missing_ledger_fails_closed(capsys, tmp_path):
    # 事故清單不得把不存在的帳本解讀為「沒有事故」（R-A1 fail-open）。
    policy = _draft_policy(tmp_path)
    rc, out = _run(
        capsys,
        ["canary", "incident", "list", "--ledger", str(tmp_path / "missing.ndjson"), "--policy", str(policy)],
    )
    assert rc == 2
    assert _json_line(out)["reason_code"] == "HARNESS_LEDGER_CORRUPT"


def test_canary_report_policy_changed_exit_1(capsys, tmp_path):
    # P9-T17 修正：原夾具（空帳本＋缺 addenda＋只有 status 的凍結政策）與計畫矛盾——
    # 不可解析的政策會先在 load_policy 失敗，且 POLICY_CHANGED 只來自帳本中 policy_ref 與目前政策 sha 不符
    # （plan Task 12 gate 第 2 步 policy_mismatch）。故改為：凍結後的合法政策＋舊 sha 的 policy_ref＋其後一筆紀錄。
    from tools.stage3 import canary_collect, canary_incidents, canary_policy

    policy = tmp_path / "policy.json"
    addenda = tmp_path / "addenda.md"
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    frozen = _frozen_policy_doc()
    policy.write_text(json.dumps(frozen), encoding="utf-8")
    addenda.write_text("| OD-TEST-POLICY | Canary policy 凍結 | 2026-10-01 | frozen |\n", encoding="utf-8")
    old = canary_policy.load_policy(policy, addenda_path=addenda)
    canary_collect.write_policy_ref(ledger_dir, old, now=lambda: "2026-10-01T00:00:00+00:00", platform="linux")
    frozen["active_day"]["min_distinct_submits"] = 5  # 凍結後竄改門檻
    policy.write_text(json.dumps(frozen), encoding="utf-8")
    canary_incidents.write_note(
        ledger_dir,
        kind="planned_downtime",
        start="2026-10-02T00:00:00+00:00",
        end="2026-10-02T01:00:00+00:00",
        reason="policy edit test",
        author="tester",
        host_id=canary_collect.host_id(),
        platform="linux",
    )
    rc, out = _run(
        capsys,
        [
            "canary", "report",
            "--policy", str(policy),
            "--addenda", str(addenda),
            "--ledger", str(ledger_dir / "ledger.ndjson"),
            "--platform", "linux",
            "--artifact-id", "ART-TEST",
            "--bundle-tree-sha256", "0" * 64,
            "--runtime-tree-sha256", "1" * 64,
        ],
    )
    assert rc == 1
    assert _json_line(out)["result"] == "POLICY_CHANGED"


def test_ab_run_infra_exit_3(capsys, tmp_path, monkeypatch):
    # 替身：A 階段釋放不明 → INFRA_ERROR → exit 3
    import tools.stage3.__main__ as entry  # noqa: PLC0415

    def _fake_run_ab(*_a, **_k):
        return {"verdict": "INFRA_ERROR", "reason_code": "A_RELEASE_UNKNOWN"}

    monkeypatch.setattr(entry, "run_ab", _fake_run_ab)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("schema_version: 1\n", encoding="utf-8")
    rc, _ = _run(
        capsys,
        [
            "ab", "run",
            "--manifest", str(manifest),
            "--out", str(tmp_path / "out"),
            "--repo-root", str(tmp_path),
        ],
    )
    assert rc == 3


def test_output_compact_json(capsys, tmp_path):
    root = _retirement_root(tmp_path)
    rc, out = _run(capsys, ["retirement", "bundle-plan", "--stage", "3", "--repo-root", str(root)])
    line = out.strip().splitlines()[-1]
    assert ": " not in line and ", " not in line
    json.loads(line)


def test_output_keeps_chinese_detail_unescaped(capsys, tmp_path):
    root = _retirement_root(tmp_path)
    rc, out = _run(capsys, ["retirement", "verify", "--mode", "release", "--repo-root", str(root)])
    assert "\\u" not in out
    assert _json_line(out)["detail"]


def test_od09_check_exit(capsys, tmp_path):
    acceptance = tmp_path / "longrun_acceptance.yaml"
    acceptance.write_text("od09: {frozen: false}\n", encoding="utf-8")
    rc, _ = _run(
        capsys,
        ["od09", "check", "--acceptance", str(acceptance), "--run-started-at", "2026-10-21T00:00:00+00:00"],
    )
    assert rc == 1


def test_retirement_github_output(capsys, tmp_path):
    root = _retirement_root(tmp_path)
    rc, out = _run(capsys, ["retirement", "github-output", "--repo-root", str(root)])
    assert rc == 0
    assert out == "retired=false\n"


# ---- P9-T17 審查後補強（R-A1～A7）：入口守門、退出碼、輸出編碼與 stdlib-only 路徑 ----


def _index_doc() -> dict:
    return {"schema_version": 2, "entries": [], "stage_targets": {}, "target_history": []}


def _entry_doc(**overrides) -> dict:
    """完整 entry JSON（與 `EvidenceEntry` 欄位一致）；add 不讀取證據檔，雜湊只需格式正確。"""
    doc = {
        "entry_id": "E-TEST-1",
        "item_id": "S3-ROLLBACK-LINUX",
        "platform": "linux",
        "result": "PASS",
        "artifact_id": "ART-TEST",
        "runtime_digest": "d" * 64,
        "bundle_tree_sha256": "b" * 64,
        "runtime_tree_sha256": "r" * 64,
        "contract_revision": "contract-test",
        "tool_revision": "tool-test",
        "case_list_sha256": "c" * 64,
        "l1_set_sha256": None,
        "workload_sha256": None,
        "environment_id": "e" * 64,
        "produced_at": "2026-10-20T00:00:00+00:00",
        "started_at": "2026-10-20T00:00:00+00:00",
        "producer": "ingest:drill-report",
        "files": [{"path": "drill-report.json", "sha256": "a" * 64}],
        "xfail_cases": [],
        "reuse_of": None,
        "note": "",
        "ledger_through": None,
        "ledger_records": None,
    }
    doc.update(overrides)
    return doc


def _write_index_and_entry(tmp_path: Path, entry: dict) -> tuple[Path, Path]:
    index = tmp_path / "index.json"
    index.write_text(json.dumps(_index_doc()), encoding="utf-8")
    entry_path = tmp_path / "entry.json"
    entry_path.write_text(json.dumps(entry), encoding="utf-8")
    return index, entry_path


@pytest.mark.parametrize("result", ["PASS", "XFAIL"])
def test_evidence_add_refuses_tool_producer_pass_or_xfail(capsys, tmp_path, result):
    # 防回歸（R1-F5）：add 不得成為 PASS／XFAIL 的後門；工具產出的結果只能經 ingest 推導。
    entry = _entry_doc(result=result, xfail_cases=["case-x"] if result == "XFAIL" else [])
    index, entry_path = _write_index_and_entry(tmp_path, entry)
    before = index.read_bytes()
    rc, out = _run(capsys, ["evidence", "add", "--index", str(index), "--entry", str(entry_path)])
    assert rc == 2
    assert _json_line(out)["reason_code"] == "HARNESS_EVIDENCE_INGEST_INVALID"
    assert index.read_bytes() == before


def test_evidence_add_refuses_manual_producer_on_tool_item(capsys, tmp_path):
    # manual 只適用 manual-procedure 項目；工具項目即使以 manual 登錄也必須拒絕。
    entry = _entry_doc(producer="manual", item_id="S3-ROLLBACK-LINUX")
    index, entry_path = _write_index_and_entry(tmp_path, entry)
    before = index.read_bytes()
    rc, out = _run(capsys, ["evidence", "add", "--index", str(index), "--entry", str(entry_path)])
    assert rc == 2
    assert index.read_bytes() == before


def test_evidence_add_accepts_manual_pass_on_manual_procedure_item(capsys, tmp_path):
    entry = _entry_doc(
        entry_id="E-MANUAL-1",
        item_id="S24-P2-1-MCU-FLASH",
        platform="linux",
        producer="manual",
    )
    index, entry_path = _write_index_and_entry(tmp_path, entry)
    rc, out = _run(capsys, ["evidence", "add", "--index", str(index), "--entry", str(entry_path)])
    assert rc == 0
    assert _json_line(out)["entry_id"] == "E-MANUAL-1"


@pytest.mark.parametrize("omit", ["--bundle-tree-sha256", "--runtime-tree-sha256"])
def test_canary_report_requires_tree_flags(capsys, tmp_path, omit):
    # 兩個 tree 旗標必填（R4-F1）；缺任一即 argparse 錯誤，輸出同形 JSON 且 exit 2。
    argv = [
        "canary", "report",
        "--policy", str(tmp_path / "p.json"),
        "--addenda", str(tmp_path / "a.md"),
        "--ledger", str(tmp_path / "l.ndjson"),
        "--platform", "linux",
        "--artifact-id", "ART-TEST",
        "--bundle-tree-sha256", "0" * 64,
        "--runtime-tree-sha256", "1" * 64,
    ]
    i = argv.index(omit)
    del argv[i:i + 2]
    rc, out = _run(capsys, argv)
    assert rc == 2
    assert _json_line(out)["reason_code"] == "HARNESS_CLI_ERROR"


def test_ab_run_keyboard_interrupt_maps_to_aborted_exit_3(capsys, tmp_path, monkeypatch):
    # 執行器中斷時（P9-T6 已寫出 ABORTED 報告並重新拋出）→ 結束碼對齊 ABORTED→3。
    import tools.stage3.__main__ as entry  # noqa: PLC0415

    def _interrupted(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(entry, "run_ab", _interrupted)
    rc, out = _run(
        capsys,
        ["ab", "run", "--manifest", str(tmp_path / "m.yaml"), "--out", str(tmp_path / "out"), "--repo-root", str(tmp_path)],
    )
    assert rc == 3
    assert _json_line(out)["reason_code"] == "HARNESS_ABORTED"


def test_ab_run_interrupt_keeps_recorded_product_fail(capsys, tmp_path, monkeypatch):
    # 中斷前已記錄產品 FAIL（P9-T6 步驟 8～9）：結束碼依報告為 1，不得被 ABORTED／INFRA 的 3 蓋過。
    import tools.stage3.__main__ as entry  # noqa: PLC0415

    def _interrupted(*_a, out_dir, **_k):
        out_dir.mkdir(parents=True)
        (out_dir / "ab-report.json").write_text(
            json.dumps({"verdict": "FAIL", "reason_code": "HARNESS_SUITE_INFRA_ERROR"}), encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(entry, "run_ab", _interrupted)
    rc, out = _run(
        capsys,
        ["ab", "run", "--manifest", str(tmp_path / "m.yaml"), "--out", str(tmp_path / "out"), "--repo-root", str(tmp_path)],
    )
    assert rc == 1
    doc = _json_line(out)
    assert doc["verdict"] == "FAIL" and doc["interrupted"] is True


def test_ab_run_interrupt_never_reports_pass(capsys, tmp_path, monkeypatch):
    # 中斷的執行不得成為 PASS：報告若誤記為 PASS，一律回退為 ABORTED、exit 3。
    import tools.stage3.__main__ as entry  # noqa: PLC0415

    def _interrupted(*_a, out_dir, **_k):
        out_dir.mkdir(parents=True)
        (out_dir / "ab-report.json").write_text(json.dumps({"verdict": "PASS"}), encoding="utf-8")
        raise KeyboardInterrupt

    monkeypatch.setattr(entry, "run_ab", _interrupted)
    rc, out = _run(
        capsys,
        ["ab", "run", "--manifest", str(tmp_path / "m.yaml"), "--out", str(tmp_path / "out"), "--repo-root", str(tmp_path)],
    )
    assert rc == 3
    assert _json_line(out)["reason_code"] == "HARNESS_ABORTED"


def test_unexpected_exception_is_cli_error_not_verdict(capsys, monkeypatch):
    # 未預期例外不得以 exit 1（FAIL／INCOMPLETE 判定）離開；統一為 HARNESS_CLI_ERROR 且 exit 2。
    import tools.stage3.__main__ as entry  # noqa: PLC0415

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(entry, "_dispatch", _boom)
    rc, out = _run(
        capsys,
        ["od09", "check", "--acceptance", "a.yaml", "--run-started-at", "2026-10-21T00:00:00+00:00"],
    )
    assert rc == 2
    assert _json_line(out)["reason_code"] == "HARNESS_CLI_ERROR"


def test_value_error_reports_cli_error_code(capsys, tmp_path):
    # ValueError 與 OSError 一律帶 HARNESS_CLI_ERROR（而非 null）；--boards 為空在任何步驟前即拒絕。
    rc, out = _run(
        capsys,
        [
            "rollback-drill", "run",
            "--cli", str(tmp_path / "serialwrap"),
            "--platform", "linux",
            "--boards", ",",
            "--expect-current", "1.0.0",
            "--expect-previous", "0.9.0",
            "--out", str(tmp_path / "out"),
            "--i-understand-this-restarts-the-daemon",
        ],
    )
    assert rc == 2
    assert _json_line(out)["reason_code"] == "HARNESS_CLI_ERROR"
    assert not (tmp_path / "out").exists()


def test_abbreviated_destructive_flag_is_refused(capsys, tmp_path):
    # 防回歸：`--i-under` 不得代替 `--i-understand-this-restarts-the-daemon`（關閉前綴縮寫）。
    out_dir = tmp_path / "out"
    rc, out = _run(
        capsys,
        [
            "rollback-drill", "run",
            "--cli", str(tmp_path / "serialwrap"),
            "--platform", "linux",
            "--boards", "COM0,COM1",
            "--expect-current", "1.0.0",
            "--expect-previous", "0.9.0",
            "--out", str(out_dir),
            "--i-under",
        ],
    )
    assert rc == 2
    assert not out_dir.exists()
    assert _json_line(out)["reason_code"] == "HARNESS_CLI_ERROR"


def test_argparse_error_emits_cli_error_json(capsys):
    rc, out = _run(capsys, ["evidence", "status"])
    assert rc == 2
    assert _json_line(out)["reason_code"] == "HARNESS_CLI_ERROR"


def test_od09_check_frozen_before_run_exit_0(capsys, tmp_path):
    # 已於 run 起點前凍結 → 0（R-A7）。夾具以 P2 凍結流程建立，與 test_stage3_od09.py 相同做法。
    import fakes_od09  # noqa: PLC0415

    from realhw.acceptance import od09_freeze  # noqa: PLC0415

    env = fakes_od09.od09_env(tmp_path)
    proposal = od09_freeze.propose(env.linux_stats, env.windows_stats, baseline_gates=env.baseline_gates)
    od09_freeze.freeze(
        env.acceptance,
        proposal,
        decision_id="OD-09-F1",
        approved_by="owner",
        overrides={},
        stage0_index=env.stage0_index,
        evidence_root=env.evidence_root,
        ledger_path=env.ledger,
        addenda_path=env.addenda,
        now=fakes_od09.ts(20, 9),
    )
    rc, out = _run(
        capsys,
        ["od09", "check", "--acceptance", str(env.acceptance), "--run-started-at", fakes_od09.ts(21, 0)],
    )
    assert rc == 0
    assert _json_line(out)["result"] == "PASS"


def test_od09_check_arguments_match_consumer(capsys):
    # build_parser 的本地 od09 引數須與 P9-T15 消費端定義一致（R-A4 的漂移守門）。
    import argparse  # noqa: PLC0415

    from tools.stage3 import od09_consume  # noqa: PLC0415
    from tools.stage3.__main__ import build_parser  # noqa: PLC0415

    local = argparse.ArgumentParser()
    od09_consume.add_check_arguments(local)
    argv = ["--acceptance", "a.yaml", "--run-started-at", "2026-10-21T00:00:00+00:00"]
    expected = vars(local.parse_args(argv))
    actual = vars(build_parser().parse_args(["od09", "check", *argv]))
    assert {key: actual[key] for key in expected} == expected
    for missing in ([], ["--acceptance", "a.yaml"]):
        with pytest.raises(SystemExit):
            local.parse_args(missing)
        with pytest.raises(SystemExit):
            build_parser().parse_args(["od09", "check", *missing])


# ---- P9-T17 審查二次補強：退出碼對照與各子命令接線（不依賴 P9-T6／T12 的部分）----


@dataclasses.dataclass(frozen=True)
class _FakeDrillResult:
    verdict: str


@dataclasses.dataclass(frozen=True)
class _FakeIngestEntry:
    entry_id: str
    result: str


_ENV_DOC = {
    "os_build": "Windows 11 26100",
    "cpu": "x86_64",
    "ram_mb": 16384,
    "filesystem": "NTFS",
    "av_product": "third-party EDR",
    "drivers": "com0com-3.0",
    "board_firmware": "fw-1.0",
    "toolchains": "rust-1.96",
}


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("PASS", 0),
        ("FAIL", 1),
        ("INCOMPLETE", 1),
        ("INFRA_ERROR", 3),
        ("BASELINE_INVALID", 3),
        ("ABORTED", 3),
        ("UNKNOWN_VERDICT", 3),
    ],
)
def test_ab_run_verdict_exit_mapping(capsys, tmp_path, monkeypatch, verdict, expected):
    # `_AB_EXIT` 對照：PASS→0；FAIL、INCOMPLETE→1；INFRA_ERROR、BASELINE_INVALID、ABORTED 與未知判定→3。
    import tools.stage3.__main__ as entry  # noqa: PLC0415

    monkeypatch.setattr(entry, "run_ab", lambda *_a, **_k: {"verdict": verdict})
    rc, out = _run(
        capsys,
        ["ab", "run", "--manifest", str(tmp_path / "m.yaml"), "--out", str(tmp_path / "out"), "--repo-root", str(tmp_path)],
    )
    assert rc == expected
    assert _json_line(out)["verdict"] == verdict


@pytest.mark.parametrize(("violations", "expected"), [([], 0), (["tool tree mismatch: tools/stage3"], 1)])
def test_ab_verify_manifest_exit_mapping(capsys, tmp_path, monkeypatch, violations, expected):
    from tools.stage3 import ab_manifest  # noqa: PLC0415

    monkeypatch.setattr(ab_manifest, "load_manifest", lambda _path: object())
    monkeypatch.setattr(ab_manifest, "verify_frozen", lambda _m, *, repo_root: list(violations))
    rc, out = _run(
        capsys,
        ["ab", "verify-manifest", "--manifest", str(tmp_path / "m.json"), "--repo-root", str(tmp_path)],
    )
    assert rc == expected
    assert _json_line(out)["violations"] == violations


def test_canary_collect_draft_policy_exit_2(capsys, tmp_path):
    # draft policy 不得收集（只接受已凍結 policy）：HarnessError → exit 2，且在任何寫入前拒絕（不建立帳本目錄）。
    policy = _draft_policy(tmp_path)
    ledger_dir = tmp_path / "ledger"
    rc, out = _run(
        capsys,
        [
            "canary", "collect",
            "--cli", str(tmp_path / "serialwrap"),
            "--ledger-dir", str(ledger_dir),
            "--policy", str(policy),
            "--platform", "linux",
            "--once",
        ],
    )
    assert rc == 2
    assert _json_line(out)["reason_code"] == "HARNESS_CANARY_POLICY_NOT_FROZEN"
    assert not ledger_dir.exists()


def test_canary_note_appends_note_record(capsys, tmp_path):
    from tools.stage3 import canary_incidents  # noqa: PLC0415

    ledger_dir = tmp_path / "ledger"
    rc, out = _run(
        capsys,
        [
            "canary", "note",
            "--ledger-dir", str(ledger_dir),
            "--kind", "planned_restart",
            "--from", "2026-10-01T00:00:00+00:00",
            "--to", "2026-10-01T01:00:00+00:00",
            "--reason", "計畫性重啟",
            "--author", "owner",
            "--platform", "linux",
        ],
    )
    assert rc == 0
    assert _json_line(out)["record"]["type"] == "note"
    assert (ledger_dir / canary_incidents.LEDGER_FILE).is_file()


def test_canary_incident_open_then_close(capsys, tmp_path):
    ledger_dir = tmp_path / "ledger"
    policy = _draft_policy(tmp_path)
    rc, out = _run(
        capsys,
        [
            "canary", "incident", "open",
            "--ledger-dir", str(ledger_dir),
            "--kind", "other",
            "--platform", "linux",
            "--artifact-id", "ART-TEST",
            "--summary", "人工開案測試",
        ],
    )
    assert rc == 0
    incident_id = _json_line(out)["incident"]["incident_id"]
    rc, out = _run(
        capsys,
        [
            "canary", "incident", "close",
            "--ledger-dir", str(ledger_dir),
            "--policy", str(policy),
            "--id", incident_id,
            "--resolution", "duplicate",
            "--note", "重複事故",
            "--closed-by", "owner",
        ],
    )
    assert rc == 0
    assert _json_line(out)["record"]["type"] == "incident_close"


def test_canary_pack_collector_writes_zipapp(capsys, tmp_path):
    out_path = tmp_path / "dist" / "sw-canary.pyz"
    rc, out = _run(capsys, ["canary", "pack-collector", "--out", str(out_path)])
    assert rc == 0
    assert out_path.is_file()
    assert Path(_json_line(out)["path"]).resolve() == out_path.resolve()


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [("PASS", 0), ("FAIL", 1), ("INCOMPLETE", 1), ("PRECONDITION_FAILED", 1)],
)
def test_drill_run_verdict_exit_mapping(capsys, tmp_path, monkeypatch, verdict, expected):
    # 只有 PASS 為 0；其餘 verdict（含 INCOMPLETE 與 PRECONDITION_FAILED）皆為 1。
    from tools.stage3 import rollback_drill  # noqa: PLC0415

    monkeypatch.setattr(rollback_drill, "run_drill", lambda _config: _FakeDrillResult(verdict=verdict))
    rc, out = _run(
        capsys,
        [
            "rollback-drill", "run",
            "--cli", str(tmp_path / "serialwrap"),
            "--platform", "linux",
            "--boards", "COM0,COM1",
            "--expect-current", "1.0.0",
            "--expect-previous", "0.9.0",
            "--out", str(tmp_path / "out"),
            "--i-understand-this-restarts-the-daemon",
        ],
    )
    assert rc == expected
    assert _json_line(out)["verdict"] == verdict


@pytest.mark.parametrize("sub", ["make-corrupted", "make-start-fail"])
def test_drill_make_bundle_missing_source_exit_2(capsys, tmp_path, sub):
    # 來源 bundle 不存在 → HarnessError；統一為 JSON 與 exit 2，且不建立目的地。
    dst = tmp_path / "dst"
    rc, out = _run(capsys, ["rollback-drill", sub, "--src", str(tmp_path / "missing"), "--dst", str(dst)])
    assert rc == 2
    assert _json_line(out)["ok"] is False
    assert not dst.exists()


def test_evidence_stage3_target_requires_bundle_dir(capsys, tmp_path):
    index = tmp_path / "index.json"
    index.write_text(json.dumps(_index_doc()), encoding="utf-8")
    rc, _ = _run(
        capsys,
        [
            "evidence", "target",
            "--index", str(index),
            "--stage", "3",
            "--platform", "linux",
            "--release-manifest", str(tmp_path / "release-manifest.json"),
            "--contract-revision", "c" * 40,
            "--tool-revision", "t" * 40,
            "--case-list-sha256", "0" * 64,
            "--set-by", "owner",
        ],
    )
    assert rc == 2
    assert json.loads(index.read_text(encoding="utf-8"))["stage_targets"] == {}


def test_evidence_stage0_target_is_recorded(capsys, tmp_path):
    index = tmp_path / "index.json"
    index.write_text(json.dumps(_index_doc()), encoding="utf-8")
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "components": {}}), encoding="utf-8")
    rc, out = _run(
        capsys,
        [
            "evidence", "target",
            "--index", str(index),
            "--stage", "0",
            "--platform", "linux",
            "--release-manifest", str(manifest),
            "--contract-revision", "c" * 40,
            "--tool-revision", "t" * 40,
            "--case-list-sha256", "0" * 64,
            "--set-by", "owner",
        ],
    )
    assert rc == 0
    assert _json_line(out) == {"ok": True, "stage": "0", "platform": "linux"}
    stored = json.loads(index.read_text(encoding="utf-8"))["stage_targets"]["0"]["linux"]
    assert stored["contract_revision"] == "c" * 40


def test_evidence_env_id_is_stable_and_strict(capsys, tmp_path):
    env = tmp_path / "env.json"
    env.write_text(json.dumps(_ENV_DOC), encoding="utf-8")
    rc1, out1 = _run(capsys, ["evidence", "env-id", "--env", str(env)])
    rc2, out2 = _run(capsys, ["evidence", "env-id", "--env", str(env)])
    assert rc1 == 0 and rc2 == 0
    environment_id = _json_line(out1)["environment_id"]
    assert re.fullmatch(r"[0-9a-f]{64}", environment_id)
    assert _json_line(out2)["environment_id"] == environment_id

    extra = tmp_path / "env-extra.json"
    extra.write_text(json.dumps({**_ENV_DOC, "unexpected": "x"}), encoding="utf-8")
    rc, _ = _run(capsys, ["evidence", "env-id", "--env", str(extra)])
    assert rc == 2


def test_evidence_status_blocked_without_target_exit_1(capsys, tmp_path):
    index = tmp_path / "index.json"
    index.write_text(json.dumps(_index_doc()), encoding="utf-8")
    rc, out = _run(
        capsys,
        ["evidence", "status", "--index", str(index), "--stage", "0", "--evidence-root", str(tmp_path)],
    )
    assert rc == 1
    assert _json_line(out)["result"] == "BLOCKED"


def test_evidence_report_renders_stage_tables(capsys, tmp_path):
    index = tmp_path / "index.json"
    index.write_text(json.dumps(_index_doc()), encoding="utf-8")
    rc, out = _run(capsys, ["evidence", "report", "--index", str(index), "--evidence-root", str(tmp_path)])
    assert rc == 0
    assert out.startswith("# Stage 0～3 證據出口")
    assert "S3-OD09-ALL-FROZEN" in out


def test_evidence_ingest_non_pass_exit_1(capsys, tmp_path, monkeypatch):
    # 接線測試：ingest 結果非 PASS（此處為 FAIL）→ exit 1，且 entry 原樣輸出。
    from tools.stage3 import evidence_ingest  # noqa: PLC0415

    monkeypatch.setattr(evidence_ingest, "ingest", lambda *_a, **_k: _FakeIngestEntry("E-FAIL-1", "FAIL"))
    env = tmp_path / "env.json"
    env.write_text(json.dumps(_ENV_DOC), encoding="utf-8")
    rc, out = _run(
        capsys,
        [
            "evidence", "ingest",
            "--index", str(tmp_path / "index.json"),
            "--kind", "junit",
            "--item", "S0-L1-BC",
            "--platform", "linux",
            "--report", str(tmp_path / "report.xml"),
            "--evidence-root", str(tmp_path),
            "--environment", str(env),
        ],
    )
    assert rc == 1
    assert _json_line(out)["entry"] == {"entry_id": "E-FAIL-1", "result": "FAIL"}


def test_retirement_pack_harness_error_is_json_exit_2(capsys, tmp_path):
    # 統一錯誤契約（P9-T17 §2）：pack 的 HarnessError（輸出位於 bundle 內）→ 單行 JSON 與 exit 2，不以純文字 exit 1。
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "payload.txt").write_text("x", encoding="utf-8")
    rc, out = _run(capsys, ["retirement", "pack", "--bundle-dir", str(bundle), "--out", str(bundle / "inside.zip")])
    assert rc == 2
    doc = _json_line(out)
    assert doc["ok"] is False
    assert doc["reason_code"] == "HARNESS_ARTIFACT_MISMATCH"


def test_retirement_pack_missing_bundle_dir_is_cli_error(capsys, tmp_path):
    rc, out = _run(capsys, ["retirement", "pack", "--out", str(tmp_path / "x.zip")])
    assert rc == 2
    assert _json_line(out)["reason_code"] == "HARNESS_CLI_ERROR"
