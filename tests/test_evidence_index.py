"""Stage 0 證據索引 schema 與驗證器測試。"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import subprocess
import sys

import pytest
import yaml

# 本檔含子程序調用（process_rpc），依 T14 規則模組層只放平台 marker
pytestmark = [pytest.mark.core_portable]

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.process_rpc
def test_evidence_index_schema_and_check(tmp_path: pathlib.Path):
    import jsonschema

    index_path = REPO / "tests/acceptance/stage0_evidence_index.yaml"
    schema_path = REPO / "tests/acceptance/evidence_index.schema.json"
    checker_script = REPO / "tests/acceptance/check_evidence_index.py"
    cases_path = REPO / "tests/acceptance/cases.yaml"

    # 1. stage0_evidence_index.yaml 以 schema 驗證通過
    index_data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
    schema_data = json.loads(schema_path.read_text(encoding="utf-8"))
    validator_cls = jsonschema.Draft202012Validator
    validator_cls.check_schema(schema_data)
    validator = validator_cls(schema_data)
    errors = sorted(validator.iter_errors(index_data), key=lambda e: e.path)
    assert errors == [], f"Evidence index schema validation errors: {[e.message for e in errors]}"

    # 2. 每個 stage0_required case 至少被一項引用
    cases_data = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
    stage0_cases = {c["case_id"] for c in cases_data.get("cases", []) if c.get("stage0_required") is True}
    indexed_cases = {cid for item in index_data.get("items", []) for cid in (item.get("case_ids") or [])}
    missing_from_index = stage0_cases - indexed_cases
    assert missing_from_index == set(), (
        f"stage0_required cases missing from evidence index: {missing_from_index}"
    )

    # 3. check_evidence_index.py --stage 0 在目前（含 blocked_owner 項）exit 5
    proc_curr = subprocess.run(
        [sys.executable, str(checker_script), "--stage", "0"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc_curr.returncode == 5, (
        f"check_evidence_index.py --stage 0 must exit 5 (BLOCKED) with blocked_owner items, "
        f"got {proc_curr.returncode}:\n{proc_curr.stderr}\n{proc_curr.stdout}"
    )

    # 4. 暫存副本全部改為 present 且檔案／sha256 正確時 exit 0
    mock_root = tmp_path / "mock_root"
    mock_root.mkdir()
    mock_acc = mock_root / "tests/acceptance"
    mock_acc.mkdir(parents=True)
    mock_cases = mock_acc / "cases.yaml"
    mock_cases.write_text(cases_path.read_text(encoding="utf-8"), encoding="utf-8")

    temp_index_data = copy.deepcopy(index_data)
    for item in temp_index_data.get("items", []):
        item["status"] = "present"
        rel_path = item.get("artifact_path")
        if not rel_path:
            rel_path = f"artifacts/{item['id']}.json"
            item["artifact_path"] = rel_path
        file_path = mock_root / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        dummy_content = f"dummy artifact content for {item['id']}\n".encode("utf-8")
        file_path.write_bytes(dummy_content)
        item["sha256"] = hashlib.sha256(dummy_content).hexdigest()

    mock_index = mock_acc / "stage0_evidence_index.yaml"
    mock_index.write_text(yaml.safe_dump(temp_index_data), encoding="utf-8")

    proc_all_present = subprocess.run(
        [sys.executable, str(checker_script), "--stage", "0", "--root", str(mock_root)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc_all_present.returncode == 0, (
        f"All present with matching sha256 must exit 0, got {proc_all_present.returncode}:\n"
        f"{proc_all_present.stderr}\n{proc_all_present.stdout}"
    )

    # 5. sha256 不符 exit 1
    temp_index_data["items"][0]["sha256"] = "0" * 64
    mock_index.write_text(yaml.safe_dump(temp_index_data), encoding="utf-8")

    proc_bad_sha = subprocess.run(
        [sys.executable, str(checker_script), "--stage", "0", "--root", str(mock_root)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc_bad_sha.returncode == 1, (
        f"sha256 mismatch must exit 1, got {proc_bad_sha.returncode}:\n"
        f"{proc_bad_sha.stderr}\n{proc_bad_sha.stdout}"
    )
