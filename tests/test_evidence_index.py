"""Stage 0 證據索引 schema 與驗證器測試。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import subprocess
import sys
from xml.sax.saxutils import quoteattr

import pytest
import yaml

# 本檔含子程序調用（process_rpc），依 T14 規則模組層只放平台 marker
pytestmark = [pytest.mark.core_portable]

REPO = pathlib.Path(__file__).resolve().parents[1]


def _load_checker_module():
    """依路徑載入 check_evidence_index.py（與 checker 子程序使用同一份 l1_artifact_digest）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("checker_under_test", REPO / "tests/acceptance/check_evidence_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_l1_pass_artifacts(base: pathlib.Path, set_id: str, platforms: list[str]) -> str:
    """P2-T13：為 l1_set 項目寫出全部必要 nodeid 皆 passed 的 l1-results.v1 報告與 junit（由凍結鎖產生）。

    回傳目錄的 sha256（與 checker 的 l1_artifact_digest 同式）。T12 原本以 dummy 檔案代表所有項目；
    l1_set 項目改以 l1_judge 判定，dummy 無法通過判定，故此處改寫為真實 PASS 形狀的報告。
    """
    lock = json.loads((REPO / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    for plat in platforms:
        spec = lock["sets"][set_id][plat]
        nodeids = list(spec["nodeids"])
        out = base / plat
        out.mkdir(parents=True, exist_ok=True)
        report = {
            "schema_version": 2,
            "format": "l1-results.v1",
            "runner": "pytest",
            "platform": plat,
            "argv": ["/x/pytest/__main__.py", *spec["selector"]["argv"]],
            "markexpr": spec["selector"]["markexpr"],
            "counts": {},
            "resources": {},
            "uart_kind": "native",
            "not_collected_platform": [],
            "collected": nodeids,
            "deselected": [],
            "results": [
                {"nodeid": nid, "outcome": "passed", "skip_kind": None,
                 "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}}
                for nid in nodeids
            ],
            "core_portable_platform_skips": [],
            "exit_code": 0,
            "finished": True,
            "deployed_content": None,
        }
        (out / "resource-report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        # junit 以 testcase 元素為準（l1 判定的 junit 計數層；P2-T13 審查修正）。
        cases = "".join(f'<testcase classname="pytest" name={quoteattr(nid)}/>' for nid in nodeids)
        (out / "junit.xml").write_text(
            f'<?xml version="1.0"?><testsuite name="pytest" tests="{len(nodeids)}" failures="0" errors="0" skipped="0">{cases}</testsuite>\n',
            encoding="utf-8",
        )
    return _load_checker_module().l1_artifact_digest(base, platforms)


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
        encoding="utf-8",
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
        if item.get("l1_set"):
            item["sha256"] = _write_l1_pass_artifacts(
                mock_root / rel_path, item["l1_set"]["set_id"], item["l1_set"]["platforms"]
            )
            continue
        file_path = mock_root / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        required_layer = item.get("required_layer")
        strict_layers = {"L1.5-docker", "host-throwaway", "native-windows", "L2-bench", "L3-bench", "hw-bench"}
        if required_layer in strict_layers and file_path.suffix.lower() == ".json":
            uart_kind = "pty-fake" if required_layer == "L1.5-docker" else "native"
            report = {
                "producer": "test-evidence-index-fixture",
                "evidence_layer": required_layer,
                "uart_kind": uart_kind,
                "results": [{"case_id": item["id"], "verdict": "PASS"}],
            }
            if required_layer == "L1.5-docker":
                report.update({
                    "topology": "docker-2c",
                    "image_digests": {name: "sha256:" + "a" * 64
                                      for name in ("serialwrap", "dut-sim", "systemd")},
                    "implementation": "python",
                    "package_tree_sha256": "a" * 64,
                })
            dummy_content = (json.dumps(report, sort_keys=True) + "\n").encode("utf-8")
        else:
            dummy_content = f"dummy artifact content for {item['id']}\n".encode("utf-8")
        file_path.write_bytes(dummy_content)
        item["sha256"] = hashlib.sha256(dummy_content).hexdigest()
        if required_layer in {"native-windows", "L2-bench", "L3-bench", "hw-bench"} and file_path.suffix.lower() != ".json":
            sidecar = file_path.with_name(file_path.name + ".evidence.json")
            sidecar.write_text(json.dumps({
                "producer": "test-evidence-index-fixture",
                "evidence_layer": required_layer,
                "uart_kind": "native",
                "results": [{"case_id": item["id"], "verdict": "PASS"}],
            }), encoding="utf-8")

    mock_index = mock_acc / "stage0_evidence_index.yaml"
    mock_index.write_text(yaml.safe_dump(temp_index_data), encoding="utf-8")

    proc_all_present = subprocess.run(
        [sys.executable, str(checker_script), "--stage", "0", "--root", str(mock_root)],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
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
        encoding="utf-8",
    )
    assert proc_bad_sha.returncode == 1, (
        f"sha256 mismatch must exit 1, got {proc_bad_sha.returncode}:\n"
        f"{proc_bad_sha.stderr}\n{proc_bad_sha.stdout}"
    )


INDEX_PATH = REPO / "tests/acceptance/stage0_evidence_index.yaml"


def _load_index() -> dict:
    return yaml.safe_load(INDEX_PATH.read_text(encoding="utf-8"))


def _checker_root(tmp_path: pathlib.Path, index_data: dict) -> pathlib.Path:
    """建立僅含索引與 cases.yaml 副本的暫存根目錄（checker 的 --root 語意＝含 tests/acceptance 的目錄）。"""
    root = tmp_path / "root"
    acc = root / "tests" / "acceptance"
    acc.mkdir(parents=True)
    (acc / "cases.yaml").write_text((REPO / "tests/acceptance/cases.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (acc / "stage0_evidence_index.yaml").write_text(
        yaml.safe_dump(index_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return root


def _run_checker(root: pathlib.Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(REPO / "tests/acceptance/check_evidence_index.py"), "--stage", "0", "--root", str(root)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", env=env,
    )


@pytest.mark.process_rpc
def test_evidence_index_rejects_uncovered_platform_row(tmp_path: pathlib.Path):
    """Windows 適用案例缺少某一檔的 windows 列 → FAIL（不得以 Linux 單份報告冒充雙平台證據）。"""
    data = _load_index()
    victim = next(
        i for i in data["items"]
        if i.get("platform") == "windows" and i.get("case_ids") == ["co-01-line-buffering"] and i.get("evidence") == "manifest.json"
    )
    data["items"].remove(victim)
    proc = _run_checker(_checker_root(tmp_path, data))
    assert proc.returncode == 1, f"expected FAIL, got {proc.returncode}:\n{proc.stderr}"
    assert "uncovered_evidence co-01-line-buffering windows manifest.json" in proc.stderr


@pytest.mark.process_rpc
def test_evidence_index_rejects_missing_required_item(tmp_path: pathlib.Path):
    """§4 必要項目（此處為 B13）從索引刪除 → FAIL，不得以刪列縮小出口清單。"""
    data = _load_index()
    data["items"] = [i for i in data["items"] if i["id"] != "S0-B13-WIN-COM-CI-RUNNER"]
    proc = _run_checker(_checker_root(tmp_path, data))
    assert proc.returncode == 1, f"expected FAIL, got {proc.returncode}:\n{proc.stderr}"
    assert "missing_required_item S0-B13-WIN-COM-CI-RUNNER" in proc.stderr


@pytest.mark.process_rpc
@pytest.mark.parametrize("item_id", [
    "S0-L15-DOCKER-48H-LINUX",
    "S0-L15-DOCKER-SUPERVISION-72H-USER",
    "S0-L15-DOCKER-SUPERVISION-72H-SYSTEM",
    "S0-L15-DOCKER-SYSTEMD-USER",
    "S0-L15-DOCKER-SYSTEMD-SYSTEM",
])
def test_evidence_index_rejects_missing_docker_required_item(tmp_path: pathlib.Path, item_id: str):
    data = _load_index()
    data["items"] = [item for item in data["items"] if item["id"] != item_id]
    proc = _run_checker(_checker_root(tmp_path, data))
    assert proc.returncode == 1, f"expected FAIL, got {proc.returncode}:\n{proc.stderr}"
    assert f"missing_required_item {item_id}" in proc.stderr


@pytest.mark.process_rpc
def test_evidence_index_rejects_evidence_outside_case(tmp_path: pathlib.Path):
    """單案例列的 evidence 不屬該案例清單，或 artifact_path 檔名不對應 → FAIL。"""
    data = _load_index()
    row = next(i for i in data["items"] if i["id"] == "S0-f1-limits-queryable-linux-report-json")
    row["evidence"] = "stdout.log"
    proc = _run_checker(_checker_root(tmp_path, data))
    assert proc.returncode == 1
    assert "evidence_not_in_case S0-f1-limits-queryable-linux-report-json" in proc.stderr

    data = _load_index()
    row = next(i for i in data["items"] if i["id"] == "S0-f1-limits-queryable-linux-report-json")
    row["artifact_path"] = "evidence/stage0/f1-limits-queryable/linux/report.md"
    proc = _run_checker(_checker_root(tmp_path / "b", data))
    assert proc.returncode == 1
    assert "artifact_name_mismatch S0-f1-limits-queryable-linux-report-json" in proc.stderr


@pytest.mark.process_rpc
def test_evidence_index_checker_survives_legacy_stdout_encoding():
    """Windows 非 UTF-8 locale 下 checker 仍輸出完整訊息並回 BLOCKED（5），不擲 UnicodeEncodeError。"""
    proc = _run_checker(REPO, env_extra={"PYTHONIOENCODING": "cp1252"})
    assert proc.returncode == 5, f"expected BLOCKED (5), got {proc.returncode}:\n{proc.stderr}"
    assert "UnicodeEncodeError" not in proc.stderr


@pytest.mark.process_rpc
@pytest.mark.parametrize("missing_evidence", ["wire.ndjson", "comparison.json", "wal/"])
def test_evidence_index_rejects_missing_func_test_attachment(tmp_path: pathlib.Path, missing_evidence: str):
    """func-test 案例的必要附件（wire、comparison、WAL 目錄）任一缺列 → FAIL，不得以 manifest 單檔代表全部證據。"""
    data = _load_index()
    victim = next(
        i for i in data["items"]
        if i.get("platform") == "linux" and i.get("case_ids") == ["brcm-01-login-flow"] and i.get("evidence") == missing_evidence
    )
    data["items"].remove(victim)
    proc = _run_checker(_checker_root(tmp_path, data))
    assert proc.returncode == 1, f"expected FAIL, got {proc.returncode}:\n{proc.stderr}"
    assert f"uncovered_evidence brcm-01-login-flow linux {missing_evidence}" in proc.stderr


@pytest.mark.process_rpc
def test_evidence_index_explicit_index_and_cases_paths(tmp_path: pathlib.Path):
    """--index／--cases 可獨立於 --root 指定：索引與 artifact 根目錄分離；指定路徑不存在時回 2。"""
    cases_path = REPO / "tests/acceptance/cases.yaml"
    proc = subprocess.run(
        [sys.executable, str(REPO / "tests/acceptance/check_evidence_index.py"), "--stage", "0",
         "--root", str(tmp_path / "evidence-only"), "--index", str(INDEX_PATH), "--cases", str(cases_path)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 5, f"expected BLOCKED (5), got {proc.returncode}:\n{proc.stderr}"
    proc = subprocess.run(
        [sys.executable, str(REPO / "tests/acceptance/check_evidence_index.py"), "--stage", "0",
         "--root", str(tmp_path / "evidence-only"), "--index", str(tmp_path / "absent.yaml"), "--cases", str(cases_path)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 2, f"expected usage error (2), got {proc.returncode}:\n{proc.stderr}"
