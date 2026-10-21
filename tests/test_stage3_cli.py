"""P9-T17：`python3 -m tools.stage3` 入口的失敗測試（TDD red）。

所有 `tools.stage3` 匯入皆為函式內的延遲匯入，使套件在實作存在前仍可收集。
"""

from __future__ import annotations

import json

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]


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


def test_harness_error_exit_2(capsys):
    rc, out = _run(capsys, ["retirement", "verify", "--mode", "release"])
    assert rc == 2
    assert _json_line(out)["reason_code"] == "HARNESS_RETIREMENT_NOT_APPROVED"


def test_stage3_bundle_plan_candidate_exit_0(capsys):
    rc, out = _run(capsys, ["retirement", "bundle-plan", "--stage", "3"])
    assert rc == 0
    assert _json_line(out)["candidate"] is True


def test_evidence_status_requires_root(capsys, tmp_path):
    rc, _ = _run(capsys, ["evidence", "status", "--index", str(tmp_path / "idx.json"), "--stage", "3"])
    assert rc == 2


def test_canary_policy_freeze_cli(capsys, tmp_path):
    policy = tmp_path / "policy.json"
    addenda = tmp_path / "addenda.json"
    policy.write_text(json.dumps({"status": "draft"}), encoding="utf-8")
    addenda.write_text(json.dumps([]), encoding="utf-8")
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
    assert len(json.loads(addenda.read_text(encoding="utf-8"))) == 1


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
    # PASS 資料 → 0；INCOMPLETE → 1；draft policy → 1 且 POLICY_NOT_FROZEN；policy 被改 → 1 且 POLICY_CHANGED。
    base = [
        "canary", "report",
        "--addenda", str(tmp_path / "addenda.json"),
        "--platform", "linux",
        "--artifact-id", "ART-TEST",
        "--bundle-tree-sha256", "0" * 64,
        "--runtime-tree-sha256", "1" * 64,
    ]
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps({"status": "draft"}), encoding="utf-8")
    ledger = tmp_path / "ledger.ndjson"
    ledger.write_text("", encoding="utf-8")
    rc, out = _run(capsys, base + ["--policy", str(draft), "--ledger", str(ledger)])
    assert rc == 1
    assert _json_line(out)["result"] == "POLICY_NOT_FROZEN"


def test_canary_report_policy_changed_exit_1(capsys, tmp_path):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"status": "frozen", "tampered": True}), encoding="utf-8")
    ledger = tmp_path / "ledger.ndjson"
    ledger.write_text("", encoding="utf-8")
    rc, out = _run(
        capsys,
        [
            "canary", "report",
            "--policy", str(policy),
            "--addenda", str(tmp_path / "addenda.json"),
            "--ledger", str(ledger),
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

    monkeypatch.setattr(entry, "run_ab", _fake_run_ab, raising=False)
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


def test_output_compact_json(capsys):
    rc, out = _run(capsys, ["retirement", "bundle-plan", "--stage", "3"])
    line = out.strip().splitlines()[-1]
    assert ": " not in line and ", " not in line
    json.loads(line)


def test_output_keeps_chinese_detail_unescaped(capsys):
    rc, out = _run(capsys, ["retirement", "verify", "--mode", "release"])
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


def test_retirement_github_output(capsys):
    rc, out = _run(capsys, ["retirement", "github-output"])
    assert rc == 0
    assert out == "retired=false\n"
