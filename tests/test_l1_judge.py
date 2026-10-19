"""Task P2-T13: L1 必要案例集合與完整性判定（l1_judge）測試。

依據 Stage 0 測試基礎計畫 Task 13 規範（R5-F3，§11.3、§11.4），驗證：
- L1 出口完整性判定：單一 pass 不足、全跑但任一必要 skip 為 INSUFFICIENT、重複 nodeid 為 INSUFFICIENT
- deselected 與 not_run 之必要項目偵測
- 執行未完成（not finished）與非零 exit code 之判定
- 任何項目失敗判定為 FAIL（不因非必要而忽略）
- junit 與 l1-results mismatch / selector mismatch 判定為 INVALID
- 空 required 集合防護
- l1_judge_vectors.json golden 向量回歸
- l1_required.lock.json schema 結構與 --l1-add-only 鎖定行為
- check_evidence_index.py 對 l1_set 之整合判定
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

pytestmark = [pytest.mark.core_portable]


@pytest.mark.memory_only
def test_l1_judge_single_pass_not_enough() -> None:
    """報告只有單一 pass，而必要集合包含更多測試時，判定必為 INSUFFICIENT。"""
    import tests.acceptance.l1_judge as l1_judge

    report = {
        "format": "l1-results.v1",
        "platform": "linux",
        "finished": True,
        "exit_code": 0,
        "collected": ["tests/test_1.py::test_a"],
        "deselected": [],
        "results": [{"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}}],
    }
    required = ["tests/test_1.py::test_a", "tests/test_1.py::test_b", "tests/test_1.py::test_c"]
    verdict = l1_judge.judge(report, required, platform="linux")
    assert verdict["verdict"] == "INSUFFICIENT"
    assert verdict["missing"] == ["tests/test_1.py::test_b", "tests/test_1.py::test_c"]

    junit = {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}
    verdict_junit = l1_judge.judge(report, required, platform="linux", junit=junit)
    assert verdict_junit["verdict"] == "INSUFFICIENT"


@pytest.mark.memory_only
def test_l1_judge_required_skip_any_reason_insufficient() -> None:
    """必要案例不論任何 skip 原因（包含 xfail），一律判定為 INSUFFICIENT。"""
    import tests.acceptance.l1_judge as l1_judge

    required = ["tests/test_1.py::test_a"]
    for kind in ("other", "capability", "platform", None):
        report = {
            "format": "l1-results.v1",
            "platform": "linux",
            "finished": True,
            "exit_code": 0,
            "collected": ["tests/test_1.py::test_a"],
            "deselected": [],
            "results": [{"nodeid": "tests/test_1.py::test_a", "outcome": "skipped", "skip_kind": kind, "phases": {"setup": "skipped"}}],
        }
        junit = {"tests": 1, "failures": 0, "errors": 0, "skipped": 1}
        verdict = l1_judge.judge(report, required, platform="linux", junit=junit)
        assert verdict["verdict"] == "INSUFFICIENT"
        assert len(verdict["skipped_required"]) == 1
        assert verdict["skipped_required"][0]["nodeid"] == "tests/test_1.py::test_a"

    report_xfail = {
        "format": "l1-results.v1",
        "platform": "linux",
        "finished": True,
        "exit_code": 0,
        "collected": ["tests/test_1.py::test_a"],
        "deselected": [],
        "results": [{"nodeid": "tests/test_1.py::test_a", "outcome": "xfailed", "skip_kind": "other", "phases": {"call": "failed"}}],
    }
    verdict_xfail = l1_judge.judge(report_xfail, required, platform="linux")
    assert verdict_xfail["verdict"] == "INSUFFICIENT"


@pytest.mark.memory_only
def test_l1_judge_duplicates_insufficient() -> None:
    """collected 或 results 出現重複 nodeid 時為 INSUFFICIENT，且任一列失敗時判 FAIL（不取最好一列）。"""
    import tests.acceptance.l1_judge as l1_judge

    required = ["tests/test_1.py::test_a"]
    # Case 1: collected 重複
    report1 = {
        "format": "l1-results.v1",
        "platform": "linux",
        "finished": True,
        "exit_code": 0,
        "collected": ["tests/test_1.py::test_a", "tests/test_1.py::test_a"],
        "deselected": [],
        "results": [{"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}}],
    }
    v1 = l1_judge.judge(report1, required, platform="linux")
    assert v1["verdict"] == "INSUFFICIENT"
    assert v1["duplicates"] == ["tests/test_1.py::test_a"]

    # Case 2: results 兩列 passed
    report2 = {
        "format": "l1-results.v1",
        "platform": "linux",
        "finished": True,
        "exit_code": 0,
        "collected": ["tests/test_1.py::test_a"],
        "deselected": [],
        "results": [
            {"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}},
            {"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}},
        ],
    }
    v2 = l1_judge.judge(report2, required, platform="linux")
    assert v2["verdict"] == "INSUFFICIENT"
    assert v2["duplicates"] == ["tests/test_1.py::test_a"]

    # Case 3: results 一列 failed 一列 passed -> 必須 FAIL
    report3 = {
        "format": "l1-results.v1",
        "platform": "linux",
        "finished": True,
        "exit_code": 1,
        "collected": ["tests/test_1.py::test_a"],
        "deselected": [],
        "results": [
            {"nodeid": "tests/test_1.py::test_a", "outcome": "failed", "skip_kind": None, "phases": {"call": "failed"}},
            {"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}},
        ],
    }
    v3 = l1_judge.judge(report3, required, platform="linux")
    assert v3["verdict"] == "FAIL"


@pytest.mark.memory_only
def test_l1_judge_deselected_and_not_run() -> None:
    """必要項目被 deselected 或在 collected 但未執行（not_run）時，判定為 INSUFFICIENT。"""
    import tests.acceptance.l1_judge as l1_judge

    required = ["tests/test_1.py::test_a", "tests/test_1.py::test_b"]
    # Case 1: deselected_required
    report1 = {
        "format": "l1-results.v1",
        "platform": "linux",
        "finished": True,
        "exit_code": 0,
        "collected": ["tests/test_1.py::test_a"],
        "deselected": ["tests/test_1.py::test_b"],
        "results": [{"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}}],
    }
    v1 = l1_judge.judge(report1, required, platform="linux")
    assert v1["verdict"] == "INSUFFICIENT"
    assert v1["deselected_required"] == ["tests/test_1.py::test_b"]

    # Case 2: not_run
    required2 = ["tests/test_1.py::test_a", "tests/test_1.py::test_c"]
    report2 = {
        "format": "l1-results.v1",
        "platform": "linux",
        "finished": True,
        "exit_code": 0,
        "collected": ["tests/test_1.py::test_a", "tests/test_1.py::test_c"],
        "deselected": [],
        "results": [{"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}}],
    }
    v2 = l1_judge.judge(report2, required2, platform="linux")
    assert v2["verdict"] == "INSUFFICIENT"
    assert v2["not_run"] == ["tests/test_1.py::test_c"]
    assert any("results do not match collection" in r for r in v2["reasons"])


@pytest.mark.memory_only
def test_l1_judge_exit_and_finish() -> None:
    """exit_code 非 0、finished 未完或帶 core_portable_platform_skips 時，判定為 INSUFFICIENT。"""
    import tests.acceptance.l1_judge as l1_judge

    required = ["tests/test_1.py::test_a"]
    base_report = {
        "format": "l1-results.v1",
        "platform": "linux",
        "finished": True,
        "exit_code": 0,
        "collected": ["tests/test_1.py::test_a"],
        "deselected": [],
        "results": [{"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}}],
    }

    # exit_code == 5
    r_exit5 = dict(base_report, exit_code=5)
    v1 = l1_judge.judge(r_exit5, required, platform="linux")
    assert v1["verdict"] == "INSUFFICIENT"
    assert any("runner exit 5" in r for r in v1["reasons"])

    # exit_code == 1 且無失敗列
    r_exit1 = dict(base_report, exit_code=1)
    v2 = l1_judge.judge(r_exit1, required, platform="linux")
    assert v2["verdict"] == "INSUFFICIENT"

    # finished is False 或缺鍵
    r_notfin = dict(base_report, finished=False)
    v3 = l1_judge.judge(r_notfin, required, platform="linux")
    assert v3["verdict"] == "INSUFFICIENT"
    assert any("runner not finished" in r for r in v3["reasons"])

    # core_portable_platform_skips 非空
    r_cpskips = dict(base_report, core_portable_platform_skips=["tests/test_1.py::test_x"])
    v4 = l1_judge.judge(r_cpskips, required, platform="linux")
    assert v4["verdict"] == "INSUFFICIENT"
    assert any("core_portable platform skip" in r for r in v4["reasons"])


@pytest.mark.memory_only
def test_l1_judge_failure_anywhere_fails() -> None:
    """任何列（包含非必要項目）失敗即判定為 FAIL；額外項目若僅為 skipped 則仍可 PASS。"""
    import tests.acceptance.l1_judge as l1_judge

    required = ["tests/test_1.py::test_a"]
    # 額外非必要項目 failed -> FAIL
    report_fail = {
        "format": "l1-results.v1",
        "platform": "linux",
        "finished": True,
        "exit_code": 1,
        "collected": ["tests/test_1.py::test_a", "tests/test_1.py::test_z"],
        "deselected": [],
        "results": [
            {"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}},
            {"nodeid": "tests/test_1.py::test_z", "outcome": "failed", "skip_kind": None, "phases": {"call": "failed"}},
        ],
    }
    v1 = l1_judge.judge(report_fail, required, platform="linux")
    assert v1["verdict"] == "FAIL"
    assert v1["failed"] == ["tests/test_1.py::test_z"]

    # 額外非必要項目 skipped -> PASS
    report_skip_extra = {
        "format": "l1-results.v1",
        "platform": "linux",
        "finished": True,
        "exit_code": 0,
        "collected": ["tests/test_1.py::test_a", "tests/test_1.py::test_z"],
        "deselected": [],
        "results": [
            {"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}},
            {"nodeid": "tests/test_1.py::test_z", "outcome": "skipped", "skip_kind": "other", "phases": {"call": "skipped"}},
        ],
    }
    v2 = l1_judge.judge(report_skip_extra, required, platform="linux")
    assert v2["verdict"] == "PASS"
    assert v2["extra"] == ["tests/test_1.py::test_z"]


@pytest.mark.memory_only
def test_l1_judge_junit_mismatch_invalid() -> None:
    """junit 計數不符、格式版本錯誤或平台不相符時，判定為 INVALID。"""
    import tests.acceptance.l1_judge as l1_judge

    required = ["tests/test_1.py::test_a", "tests/test_1.py::test_b"]
    report = {
        "format": "l1-results.v1",
        "platform": "linux",
        "finished": True,
        "exit_code": 0,
        "collected": ["tests/test_1.py::test_a", "tests/test_1.py::test_b"],
        "deselected": [],
        "results": [
            {"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}},
            {"nodeid": "tests/test_1.py::test_b", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}},
        ],
    }

    # junit 測試數與 results 列數不合
    junit = {"tests": 3, "failures": 0, "errors": 0, "skipped": 0}
    v1 = l1_judge.judge(report, required, platform="linux", junit=junit)
    assert v1["verdict"] == "INVALID"
    assert any("junit/l1-results mismatch" in r for r in v1["reasons"])

    # 格式版本不符
    r_badfmt = dict(report, format="l1-results.v0")
    v2 = l1_judge.judge(r_badfmt, required, platform="linux")
    assert v2["verdict"] == "INVALID"

    # 平台不符
    r_win = dict(report, platform="windows")
    v3 = l1_judge.judge(r_win, required, platform="linux")
    assert v3["verdict"] == "INVALID"


@pytest.mark.memory_only
def test_l1_judge_empty_required_insufficient() -> None:
    """required 清單為空時判定為 INSUFFICIENT，且 lock 查詢不存在 set 時拋 KeyError。"""
    import tests.acceptance.l1_judge as l1_judge

    report = {
        "format": "l1-results.v1",
        "platform": "linux",
        "finished": True,
        "exit_code": 0,
        "collected": ["tests/test_1.py::test_a"],
        "deselected": [],
        "results": [{"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}}],
    }
    v = l1_judge.judge(report, [], platform="linux")
    assert v["verdict"] == "INSUFFICIENT"
    assert any("no required set" in r for r in v["reasons"])

    lock = {"schema_version": 1, "sets": {}}
    with pytest.raises(KeyError):
        l1_judge.required_for(lock, "nope", "linux")


@pytest.mark.memory_only
def test_l1_judge_selector_argv_mismatch_invalid() -> None:
    """報告 argv 與 selector.argv 不一致時判 INVALID；只差 --junitxml 等輸出參數時正常判定。"""
    import tests.acceptance.l1_judge as l1_judge

    required = ["tests/test_1.py::test_a"]
    report = {
        "format": "l1-results.v1",
        "platform": "linux",
        "argv": ["pytest", "--package", "sw-pal"],
        "finished": True,
        "exit_code": 0,
        "collected": ["tests/test_1.py::test_a"],
        "deselected": [],
        "results": [{"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}}],
    }
    selector = {"argv": ["--workspace", "--all-features"]}
    v1 = l1_judge.judge(report, required, platform="linux", selector=selector)
    assert v1["verdict"] == "INVALID"
    assert any("selector mismatch" in r for r in v1["reasons"])

    # 兩者僅差 --junitxml 輸出路徑
    selector_ok = {"argv": ["--package", "sw-pal"]}
    report_with_junit = dict(report, argv=["pytest", "--package", "sw-pal", "--junitxml=out.xml"])
    v2 = l1_judge.judge(report_with_junit, required, platform="linux", selector=selector_ok)
    assert v2["verdict"] == "PASS"


@pytest.mark.memory_only
def test_l1_judge_vectors_roundtrip() -> None:
    """對 l1_judge_vectors.json 進行完整 roundtrip 驗證，至少涵蓋 10 種關鍵場景。"""
    import tests.acceptance.l1_judge as l1_judge

    vector_path = Path("tests/acceptance/l1_judge_vectors.json")
    assert vector_path.exists(), f"{vector_path} does not exist"
    vectors = json.loads(vector_path.read_text(encoding="utf-8"))
    assert isinstance(vectors, list)

    names = {vec["name"] for vec in vectors}
    required_names = {
        "single-pass",
        "required-skip",
        "duplicate",
        "deselected",
        "not-run",
        "exit-nonzero",
        "not-finished",
        "junit-mismatch",
        "failure-extra",
        "all-pass",
    }
    missing_names = required_names - names
    assert not missing_names, f"Missing vector names: {missing_names}"

    for vec in vectors:
        res = l1_judge.judge(
            vec["report"],
            vec["required"],
            platform=vec["platform"],
            junit=vec.get("junit"),
            selector=vec.get("selector"),
        )
        assert res["verdict"] == vec["verdict"], f"Vector {vec['name']} expected {vec['verdict']} got {res['verdict']}"
        for sub in vec.get("reasons_contains", []):
            assert any(sub in r for r in res.get("reasons", [])), f"Vector {vec['name']} reasons missing substring '{sub}': {res.get('reasons')}"


@pytest.mark.process_rpc
def test_l1_lock_shape_and_add_only(tmp_path: Path) -> None:
    """驗證 l1_required.lock.json 格式正確，且 --l1-add-only 具備只增不減保護。"""
    schema_path = Path("tests/acceptance/l1_required.schema.json")
    lock_path = Path("tests/acceptance/l1_required.lock.json")
    assert schema_path.exists() and lock_path.exists()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    jsonschema.validate(instance=lock, schema=schema)

    sets = lock.get("sets", {})
    assert "s0_bc" in sets and "linux" in sets["s0_bc"] and "windows" in sets["s0_bc"]
    assert "s0_win_pytest" in sets and "windows" in sets["s0_win_pytest"]

    for set_id, plat_dict in sets.items():
        for plat, spec in plat_dict.items():
            nids = spec.get("nodeids", [])
            assert len(nids) > 0, f"{set_id}.{plat} nodeids is empty"
            assert nids == sorted(nids), f"{set_id}.{plat} nodeids not sorted"
            assert len(nids) == len(set(nids)), f"{set_id}.{plat} nodeids has duplicates"

    temp_lock = tmp_path / "test.lock.json"
    temp_lock.write_text(json.dumps(lock, indent=2), encoding="utf-8")
    orig_bytes = temp_lock.read_bytes()

    s0_linux_nids = lock["sets"]["s0_bc"]["linux"]["nodeids"]
    missing_one = s0_linux_nids[:-1]
    collect_file = tmp_path / "collect.txt"
    collect_file.write_text("\n".join(missing_one) + "\n", encoding="utf-8")

    regen_script = Path("tests/acceptance/regen_lock.py")
    res1 = subprocess.run(
        [
            sys.executable,
            str(regen_script),
            "--lock-file",
            str(temp_lock),
            "--l1-add-only",
            "--set",
            "s0_bc",
            "--platform",
            "linux",
            "--collect",
            str(collect_file),
        ],
        capture_output=True,
        text=True,
    )
    assert res1.returncode == 1, f"Expected exit 1 when nodeid missing: {res1.stdout}\n{res1.stderr}"
    assert s0_linux_nids[-1] in res1.stderr
    assert temp_lock.read_bytes() == orig_bytes, "Lock file must remain unchanged on failure"

    extra_nid = "tests/test_wal.py::test_brand_new_item"
    added = s0_linux_nids + [extra_nid]
    collect_file.write_text("\n".join(added) + "\n", encoding="utf-8")
    res2 = subprocess.run(
        [
            sys.executable,
            str(regen_script),
            "--lock-file",
            str(temp_lock),
            "--l1-add-only",
            "--set",
            "s0_bc",
            "--platform",
            "linux",
            "--collect",
            str(collect_file),
        ],
        capture_output=True,
        text=True,
    )
    assert res2.returncode == 0, f"Expected exit 0 when adding nodeid: {res2.stdout}\n{res2.stderr}"
    updated_lock = json.loads(temp_lock.read_text(encoding="utf-8"))
    assert extra_nid in updated_lock["sets"]["s0_bc"]["linux"]["nodeids"]


@pytest.mark.process_rpc
def test_evidence_index_l1_item_judged(tmp_path: Path) -> None:
    """check_evidence_index.py 對帶 l1_set 的項目進行完整 l1_judge 判定，而非僅驗證 sha256。"""
    check_script = Path("tests/acceptance/check_evidence_index.py")
    assert check_script.exists(), f"{check_script} does not exist"

    index_file = tmp_path / "stage0_evidence_index.yaml"
    artifact_dir = tmp_path / "artifacts"
    linux_dir = artifact_dir / "linux"
    win_dir = artifact_dir / "windows"
    linux_dir.mkdir(parents=True)
    win_dir.mkdir(parents=True)

    partial_report = {
        "format": "l1-results.v1",
        "platform": "linux",
        "finished": True,
        "exit_code": 0,
        "collected": ["tests/test_1.py::test_a"],
        "deselected": [],
        "results": [{"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}}],
    }
    (linux_dir / "resource-report.json").write_text(json.dumps(partial_report), encoding="utf-8")
    (linux_dir / "junit.xml").write_text("<testsuite tests='1' failures='0' errors='0' skipped='0'></testsuite>", encoding="utf-8")

    index_data = {
        "schema_version": 1,
        "items": {
            "S0-L1-BC": {
                "status": "present",
                "artifact_path": str(artifact_dir),
                "l1_set": {"set_id": "s0_bc", "platforms": ["linux", "windows"]},
            }
        },
    }
    index_file.write_text(yaml.dump(index_data), encoding="utf-8")

    res1 = subprocess.run(
        [sys.executable, str(check_script), "--index-file", str(index_file), "--stage", "0"],
        capture_output=True,
        text=True,
    )
    assert res1.returncode == 5, f"Expected exit 5 for insufficient evidence, got {res1.returncode}: {res1.stdout}\n{res1.stderr}"
    assert "S0-L1-BC" in res1.stdout or "S0-L1-BC" in res1.stderr
    assert "missing" in res1.stdout or "missing" in res1.stderr
