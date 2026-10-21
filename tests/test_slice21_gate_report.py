"""Slice 2.1 出口 gate 報告結構與 BLOCKED-OWNER 清單契約（P5-T22，紅燈）。

對應計畫 `docs/rust-refactor/plans/06-slice21-daemon-core.md` Task 22 Step 1：
- `tests/acceptance/slice21_gate_report.schema.json`：報告 schema（BLOCKED-OWNER 不可為 PASS、
  移除計數門檻 `expected_min_passed`、identity 必填欄位）。
- `scripts/rust/slice21_gate.sh --dry-run`：列出 BO-S21-01～16、verdict 永不為 READY、
  `windows_cap_windows_com` 在未提供 `SW_TEST_COM_PAIR` 時為 EVIDENCE_MISSING。
- `docs/rust-refactor/slice21-owner-runbook.md`：每個 BO 項目有 `### BO-S21-NN` 錨點。
- `tests/acceptance/slice21_bo.yaml`：機器可讀 BO 清單，含 Windows 凍結案例 `case_ids`。

紅燈原因：上述檔案與 gate 腳本尚未存在（FileNotFoundError／bash 非零結束），屬缺少行為，非語法錯誤。
本檔只跑 POSIX（gate 為 bash 腳本，Windows 側證據由 win-test 另行產生）。
"""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

pytestmark = [pytest.mark.posix_only, pytest.mark.process_rpc]

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _schema():
    return json.loads((ROOT / "tests/acceptance/slice21_gate_report.schema.json").read_text(encoding="utf-8"))


def test_blocked_owner_items_can_never_be_pass():
    s = _schema()
    assert s["properties"]["blocked_owner"]["items"]["properties"]["status"]["const"] == "BLOCKED-OWNER"
    assert set(s["properties"]["sections"]["items"]["properties"]["status"]["enum"]) == {"PASS", "FAIL", "EVIDENCE_MISSING"}
    assert set(s["properties"]["verdict"]["enum"]) == {"READY_FOR_OWNER_EVIDENCE", "INCOMPLETE", "FAIL"}
    sec = s["properties"]["sections"]["items"]["required"]
    assert {"skipped", "deselected", "required_set", "l1_results", "l1_verdict", "ignored"} <= set(sec)
    assert "expected_min_passed" not in s["properties"]["sections"]["items"]["properties"]   # R5-F3：計數門檻已移除
    assert {"cli_sha256", "daemon_sha256", "daemon_implementation", "contracts_revision", "cases_lock_sha256", "tool_revision", "platform"} <= set(s["properties"]["identity"]["required"])


def test_gate_dry_run_lists_all_blocked_owner_items(tmp_path):
    r = subprocess.run(["bash", "scripts/rust/slice21_gate.sh", "--dry-run", "--run-id", "t", "--out", str(tmp_path)],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    rep = json.loads((tmp_path / "t" / "report.json").read_text(encoding="utf-8"))
    ids = [b["id"] for b in rep["blocked_owner"]]
    assert ids == [f"BO-S21-{i:02d}" for i in range(1, 17)]
    assert all(b["status"] == "BLOCKED-OWNER" for b in rep["blocked_owner"])
    assert rep["verdict"] == "INCOMPLETE"                            # dry-run 永不 READY
    com = [s for s in rep["sections"] if s["name"] == "windows_cap_windows_com"][0]
    assert com["status"] == "EVIDENCE_MISSING"                      # 未提供 SW_TEST_COM_PAIR


def test_runbook_has_anchor_per_item():
    text = (ROOT / "docs/rust-refactor/slice21-owner-runbook.md").read_text(encoding="utf-8")
    for i in range(1, 17):
        assert f"### BO-S21-{i:02d}" in text


def test_bo_case_ids_cover_frozen_windows_cases():
    import yaml

    items = yaml.safe_load((ROOT / "tests/acceptance/slice21_bo.yaml").read_text(encoding="utf-8"))["items"]
    rep_ids = {c for b in items for c in b["case_ids"]}
    assert {"win-com-open-exclusive", "win-com-txrx-bridge", "win-com-cancel-close", "win-wal-av-rotate", "win-wal-av-unlink",
            "win-wal-av-reader-handle", "win-wal-f12-progress"} <= rep_ids
