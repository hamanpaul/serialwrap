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
import re
import shutil
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = [pytest.mark.core_portable]


def _complete_row(row: dict) -> dict:
    out = dict(row)
    out.setdefault("skip_kind", None)
    phases = dict(out.get("phases") or {})
    default = "passed" if out.get("outcome") == "passed" else None
    for key in ("setup", "call", "teardown"):
        phases.setdefault(key, default)
    out["phases"] = phases
    return out


def _complete(report: dict) -> dict:
    """補齊 l1-results.v1 的必填欄位（測試夾具用）。結構缺欄的判定由 test_l1_judge_unknown_outcome_and_missing_fields 直接驗證。"""
    out = dict(report)
    out.setdefault("schema_version", 2)
    out.setdefault("runner", "pytest")
    out.setdefault("argv", ["/x/pytest/__main__.py", "-q"])
    out.setdefault("markexpr", "")
    out.setdefault("counts", {})
    out.setdefault("resources", {})
    out.setdefault("uart_kind", "native")
    out.setdefault("not_collected_platform", [])
    out.setdefault("core_portable_platform_skips", [])
    out.setdefault("deployed_content", None)
    out["results"] = [_complete_row(row) for row in report.get("results", [])]
    return out


def _judge(report: dict, required, **kwargs):
    import tests.acceptance.l1_judge as l1_judge

    return l1_judge.judge(_complete(report), required, **kwargs)


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
    verdict = _judge(report, required, platform="linux")
    assert verdict["verdict"] == "INSUFFICIENT"
    assert verdict["missing"] == ["tests/test_1.py::test_b", "tests/test_1.py::test_c"]

    junit = {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}
    verdict_junit = _judge(report, required, platform="linux", junit=junit)
    assert verdict_junit["verdict"] == "INSUFFICIENT"


@pytest.mark.memory_only
def test_l1_judge_pty_fake_cannot_satisfy_physical_uart():
    required = ["tests/test_uart.py::test_device"]
    report = {
        "format": "l1-results.v1",
        "platform": "linux",
        "uart_kind": "pty-fake",
        "finished": True,
        "exit_code": 0,
        "collected": required,
        "deselected": [],
        "resources": {"physical_uart": {"passed": 1, "failed": 0, "skipped": 0}},
        "results": [{"nodeid": required[0], "outcome": "passed", "skip_kind": None,
                     "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}}],
    }
    verdict = _judge(report, required, platform="linux")
    assert verdict["verdict"] == "INSUFFICIENT"
    assert "uart_kind_not_native" in verdict["reasons"]


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
        verdict = _judge(report, required, platform="linux", junit=junit)
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
    verdict_xfail = _judge(report_xfail, required, platform="linux")
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
    v1 = _judge(report1, required, platform="linux")
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
    v2 = _judge(report2, required, platform="linux")
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
    v3 = _judge(report3, required, platform="linux")
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
    v1 = _judge(report1, required, platform="linux")
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
    v2 = _judge(report2, required2, platform="linux")
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
    v1 = _judge(r_exit5, required, platform="linux")
    assert v1["verdict"] == "INSUFFICIENT"
    assert any("runner exit 5" in r for r in v1["reasons"])

    # exit_code == 1 且無失敗列
    r_exit1 = dict(base_report, exit_code=1)
    v2 = _judge(r_exit1, required, platform="linux")
    assert v2["verdict"] == "INSUFFICIENT"

    # finished is False 或缺鍵
    r_notfin = dict(base_report, finished=False)
    v3 = _judge(r_notfin, required, platform="linux")
    assert v3["verdict"] == "INSUFFICIENT"
    assert any("runner not finished" in r for r in v3["reasons"])

    # core_portable_platform_skips 非空
    r_cpskips = dict(base_report, core_portable_platform_skips=["tests/test_1.py::test_x"])
    v4 = _judge(r_cpskips, required, platform="linux")
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
    v1 = _judge(report_fail, required, platform="linux")
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
    v2 = _judge(report_skip_extra, required, platform="linux")
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
    v1 = _judge(report, required, platform="linux", junit=junit)
    assert v1["verdict"] == "INVALID"
    assert any("junit/l1-results mismatch" in r for r in v1["reasons"])

    # 格式版本不符
    r_badfmt = dict(report, format="l1-results.v0")
    v2 = _judge(r_badfmt, required, platform="linux")
    assert v2["verdict"] == "INVALID"

    # 平台不符
    r_win = dict(report, platform="windows")
    v3 = _judge(r_win, required, platform="linux")
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
    v = _judge(report, [], platform="linux")
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
    v1 = _judge(report, required, platform="linux", selector=selector)
    assert v1["verdict"] == "INVALID"
    assert any("selector mismatch" in r for r in v1["reasons"])

    # 兩者僅差 --junitxml 輸出路徑
    selector_ok = {"argv": ["--package", "sw-pal"]}
    report_with_junit = dict(report, argv=["pytest", "--package", "sw-pal", "--junitxml=out.xml"])
    v2 = _judge(report_with_junit, required, platform="linux", selector=selector_ok)
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


def _copy_present_artifacts(index_data: dict, root: Path) -> None:
    """夾具：把基底索引中既有 present 項目的 artifact（及 .evidence.json 標記，若有）複製到暫存根目錄。

    checker 對 present 項目一律檢查檔案存在與 sha256；暫存根若只放本測試自行設定的項目，
    其餘 present 項目（例如 S0-SPIKE1-REMAINING）會判 artifact_missing，使本測試的 exit 斷言失真。
    只複製既有檔案，不改動索引、案例或任何斷言；目錄型 artifact（L1 set）不在此複製。
    """
    for item in index_data.get("items") or []:
        rel = item.get("artifact_path")
        if item.get("status") != "present" or not rel:
            continue
        for name in (rel, f"{rel}.evidence.json"):
            src = REPO_ROOT / name
            if src.is_file() and not (root / name).exists():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, root / name)


@pytest.mark.process_rpc
def test_evidence_index_l1_item_judged(tmp_path: Path) -> None:
    """check_evidence_index.py 對帶 l1_set 的項目進行完整 l1_judge 判定，而非僅驗證 sha256。

    夾具（P2-T13 修正）：原草稿的 items 為字典、artifact_path 為絕對路徑且缺 stage／必要欄位，
    與 T12 索引格式與「絕對路徑不得使用」規則相違，會在判定前 FAIL。此處改用真實索引格式：
    以 stage0_evidence_index.yaml 為底，只把 S0-L1-BC 設為 present（相對路徑 artifacts/s0_bc）。
    linux 僅一列 passed，windows 為空報告，故判定為 INSUFFICIENT（exit 5），且輸出列出 missing。
    """
    import importlib.util

    import yaml as _yaml

    check_script = Path("tests/acceptance/check_evidence_index.py")
    assert check_script.exists(), f"{check_script} does not exist"
    spec = importlib.util.spec_from_file_location("checker_l1_fixture", check_script)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)

    root = tmp_path / "root"
    base = root / "artifacts" / "s0_bc"
    (base / "linux").mkdir(parents=True)
    (base / "windows").mkdir(parents=True)
    partial_report = {
        "schema_version": 2,
        "format": "l1-results.v1",
        "platform": "linux",
        "argv": ["/x/pytest/__main__.py"],
        "finished": True,
        "exit_code": 0,
        "collected": ["tests/test_1.py::test_a"],
        "deselected": [],
        "results": [{"nodeid": "tests/test_1.py::test_a", "outcome": "passed", "skip_kind": None, "phases": {"call": "passed"}}],
        "core_portable_platform_skips": [],
    }
    (base / "linux" / "resource-report.json").write_text(json.dumps(partial_report), encoding="utf-8")
    (base / "linux" / "junit.xml").write_text(
        "<testsuite tests='1' failures='0' errors='0' skipped='0'></testsuite>", encoding="utf-8"
    )
    empty_report = dict(partial_report, platform="windows", collected=[], results=[])
    (base / "windows" / "resource-report.json").write_text(json.dumps(empty_report), encoding="utf-8")
    (base / "windows" / "junit.xml").write_text(
        "<testsuite tests='0' failures='0' errors='0' skipped='0'></testsuite>", encoding="utf-8"
    )
    digest = checker.l1_artifact_digest(base, ["linux", "windows"])

    index_data = _yaml.safe_load((REPO_ROOT / "tests/acceptance/stage0_evidence_index.yaml").read_text(encoding="utf-8"))
    _copy_present_artifacts(index_data, root)
    for item in index_data["items"]:
        if item["id"] == "S0-L1-BC":
            item.update(status="present", artifact_path="artifacts/s0_bc", sha256=digest)
    index_file = tmp_path / "stage0_evidence_index.yaml"
    index_file.write_text(_yaml.safe_dump(index_data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    res1 = subprocess.run(
        [
            sys.executable, str(check_script), "--index-file", str(index_file), "--stage", "0",
            "--root", str(root), "--cases", str(REPO_ROOT / "tests/acceptance/cases.yaml"),
        ],
        capture_output=True,
        text=True,
    )
    assert res1.returncode == 5, f"Expected exit 5 for insufficient evidence, got {res1.returncode}: {res1.stdout}\n{res1.stderr}"
    assert "S0-L1-BC" in res1.stdout or "S0-L1-BC" in res1.stderr
    assert "missing" in res1.stdout or "missing" in res1.stderr
    assert "sha256_mismatch" not in res1.stderr


_GATE_TOKENS = frozenset({"SW_TEST_COM_PAIR", "SERIALWRAP_TEST_SERIAL_PORT", "re2", "_docker_available"})
_SKIP_CALLS = frozenset({"skip", "importorskip", "skipif", "skipUnless"})


def _gate_refs(node) -> set[str]:
    import ast

    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value in _GATE_TOKENS:
            out.add(sub.value)
        if isinstance(sub, ast.Name) and sub.id in _GATE_TOKENS:
            out.add(sub.id)
    return out


def _has_skip_call(node) -> bool:
    import ast

    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _SKIP_CALLS:
                return True
    return False


def _capability_gated(nodeid: str, cache: dict) -> bool:
    """結構判定：項目（或其類別、模組）帶能力 gate（環境變數、re2、docker）的 skip 即 True。

    以 AST 判斷，不執行測試；只看 skip 相關呼叫中是否引用 gate 名稱（P2-T13 審查修正：凍結的 Windows 集合不得含能力 SKIP 案例）。
    """
    import ast
    import re

    # Rust/libtest nodeid 由 cargo_l1_results.py 正規化為 cargo:<crate>::<target>::<test>；
    # 能力測試以 cap_ 前綴標示，不能把 crate 名當成 Python 測試檔路徑解析。
    if nodeid.startswith("cargo:"):
        return bool(re.match(r"^cap_", nodeid.rsplit("::", 1)[-1]))

    path, _, rest = nodeid.partition("::")
    parts = [p.split("[", 1)[0] for p in rest.split("::")]
    if path not in cache:
        cache[path] = ast.parse(Path(path).read_text(encoding="utf-8"), filename=path)
    tree = cache[path]

    def pytestmark_values(body) -> list:
        values = []
        for st in body:
            if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in st.targets):
                values.append(st.value)
        return values

    for value in pytestmark_values(tree.body):
        if _gate_refs(value) and _has_skip_call(value):
            return True
    if len(parts) == 2:
        scope = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == parts[0]), None)
        if scope is None:
            return False
        for deco in scope.decorator_list:
            if _gate_refs(deco) and _has_skip_call(deco):
                return True
        for value in pytestmark_values(scope.body):
            if _gate_refs(value) and _has_skip_call(value):
                return True
        target = next((n for n in scope.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == parts[1]), None)
    else:
        target = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == parts[0]), None)
    if target is None:
        return False
    for deco in target.decorator_list:
        if _gate_refs(deco) and _has_skip_call(deco):
            return True
    gate_refs = _gate_refs(target)
    if gate_refs == {"SW_TEST_COM_PAIR"} and any(
        (isinstance(deco, ast.Attribute) and deco.attr == "windows_com")
        or (
            isinstance(deco, ast.Call)
            and isinstance(deco.func, ast.Attribute)
            and deco.func.attr == "windows_com"
        )
        for deco in target.decorator_list
    ):
        # 必要的原生 COM 案例可在提供凍結 COM 配對的 Windows runner 執行；
        # windows_com marker 使資源需求明確，並由 runner 的 SW_TEST_COM_PAIR 提供。
        return False
    return bool(gate_refs and _has_skip_call(target))


@pytest.mark.memory_only
def test_l1_judge_unknown_outcome_and_missing_fields() -> None:
    """結構違規一律 INVALID（P2-T13 審查修正：原判定只檢少數型別，未知 outcome 或缺 deployed_content 仍可 PASS）。"""
    import tests.acceptance.l1_judge as l1_judge

    nid = "tests/test_1.py::test_a"
    full = {
        "format": "l1-results.v1", "platform": "linux", "runner": "pytest", "schema_version": 2,
        "argv": ["/x/pytest/__main__.py", "-q"], "markexpr": "", "counts": {}, "resources": {},
        "not_collected_platform": [], "core_portable_platform_skips": [], "deselected": [],
        "collected": [nid], "exit_code": 0, "finished": True, "deployed_content": None,
        "results": [{"nodeid": nid, "outcome": "passed", "skip_kind": None,
                     "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}}],
    }
    assert l1_judge.judge(full, [nid], platform="linux")["verdict"] == "PASS"

    missing = {k: v for k, v in full.items() if k != "deployed_content"}
    v_missing = l1_judge.judge(missing, [nid], platform="linux")
    assert v_missing["verdict"] == "INVALID"
    assert any("deployed_content" in r for r in v_missing["reasons"])

    for bad_outcome in ("weird", "PASSED"):
        bad = dict(full, results=[dict(full["results"][0], outcome=bad_outcome)])
        v = l1_judge.judge(bad, [nid], platform="linux")
        assert v["verdict"] == "INVALID", bad_outcome
        assert any("未知 outcome" in r for r in v["reasons"])

    no_call = dict(full, results=[dict(full["results"][0], phases={"setup": "passed", "call": None, "teardown": "passed"})])
    v_nc = l1_judge.judge(no_call, [nid], platform="linux")
    assert v_nc["verdict"] == "INVALID"
    assert any("outcome passed 與 phases 不一致" in r for r in v_nc["reasons"])


@pytest.mark.process_rpc
def test_l1_lock_nodeids_subset_of_collect_only() -> None:
    """鎖檔每個 nodeid 都必須出現在其 selector 的 --collect-only 輸出中（P2-T13 審查修正：原測試未檢查）。"""
    lock = json.loads(Path("tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    for set_id, plat in (("s0_bc", "linux"), ("s0_win_pytest", "windows")):
        spec = lock["sets"][set_id][plat]
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", *spec["selector"]["argv"]],  # argv 已含 -q
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        collected = {line.strip() for line in res.stdout.splitlines() if "::" in line}
        missing = sorted(set(spec["nodeids"]) - collected)
        assert not missing, f"{set_id}.{plat} 鎖檔 nodeid 不在 collect-only 輸出中：{missing[:5]}"


@pytest.mark.memory_only
@pytest.mark.parametrize("set_id", ["s1a_cargo", "s1b_cargo"])
def test_boot_done_profile_parity_cases_are_frozen_and_required(set_id: str) -> None:
    """#234 新增的四支 profile parity 測試須進 Linux cargo 分母；judge 漏收任一支都不能 PASS。"""
    import tests.acceptance.l1_judge as l1_judge

    required = [
        "cargo:sw-cli::profile_checks::boot_done_regex_is_reported_like_python",
        "cargo:sw-cli::profile_checks::boot_done_regex_unset_reports_empty_pattern",
        "cargo:sw-cli::profile_checks::boot_done_value_error_wins_over_later_bootloader_overflow",
        "cargo:sw-cli::profile_checks::password_value_error_wins_over_later_boot_done_overflow",
    ]
    lock = json.loads(Path("tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    platform = "linux"
    frozen = set(lock["sets"][set_id][platform]["nodeids"])
    assert set(required) <= frozen, f"{set_id}.{platform} 缺 frozen parity nodeid：{sorted(set(required) - frozen)}"

    def report_for(nodeids: list[str], report_platform: str) -> dict:
        return {
            "format": "l1-results.v1", "platform": report_platform, "runner": "cargo", "schema_version": 2,
            "argv": lock["sets"][set_id][platform]["selector"]["argv"], "markexpr": "", "counts": {},
            "resources": {}, "uart_kind": "native", "not_collected_platform": [],
            "core_portable_platform_skips": [], "deselected": [], "collected": nodeids,
            "exit_code": 0, "finished": True, "deployed_content": None,
            "results": [{"nodeid": nodeid, "outcome": "passed", "skip_kind": None,
                         "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}}
                        for nodeid in nodeids],
        }

    for judge_platform in ("linux", "windows"):
        assert l1_judge.judge(report_for(required, judge_platform), required, platform=judge_platform)["verdict"] == "PASS"
        missing = required[:-1]
        verdict = l1_judge.judge(report_for(missing, judge_platform), required, platform=judge_platform)
        assert verdict["verdict"] == "INSUFFICIENT", verdict
        assert required[-1] in verdict["missing"]


@pytest.mark.memory_only
def test_l1_lock_frozen_set_excludes_capability_gated() -> None:
    """凍結的 Windows 必要集合不得含能力 SKIP 案例（plan Task 13：不適用的案例不得列入該平台集合）。

    結構判定以 _capability_gated 完成；reference 環境（hosted Windows 無 re2／無 SW_TEST_COM_PAIR／無真機序列埠）
    下這些案例必定 SKIP，判定 rule 6 會因此得 INSUFFICIENT，故鎖檔不得含之。
    """
    lock = json.loads(Path("tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    cache: dict = {}
    for set_id in lock["sets"]:
        for plat, spec in lock["sets"][set_id].items():
            if plat != "windows":
                continue
            flagged = [nid for nid in spec["nodeids"] if _capability_gated(nid, cache)]
            assert not flagged, f"{set_id}.windows 含能力 SKIP 案例（須自凍結集合移除）：{flagged[:5]}"


def _rust_ignored_tests() -> set[tuple[str, str | None, str]]:
    """掃描 crates/*/ 原始碼中帶 `#[ignore…]`（含 `cfg_attr(…, ignore…)`，保守視為 ignored）的測試函式。

    回傳 (package, target, fn 名稱)；`tests/<stem>.rs` 的 target 為 stem，`tests/<dir>/…` 為 dir，
    src／benches／examples 內的單元測試 target 記為 None（以 package＋函式名比對）。
    """
    found: set[tuple[str, str | None, str]] = set()
    attr_re = re.compile(r"#\[(?:ignore\b|cfg_attr\(.*\bignore\b)")
    fn_re = re.compile(r"(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)")
    stop_re = re.compile(r"(?:pub\b|mod\b|struct\b|enum\b|impl\b|use\b|const\b|static\b|let\b|\})")
    for cargo_toml in sorted((REPO_ROOT / "crates").glob("*/Cargo.toml")):
        matched = re.search(r'^\[package\][^\[]*?^name\s*=\s*"([^"]+)"', cargo_toml.read_text(encoding="utf-8"), re.M | re.S)
        assert matched, f"{cargo_toml} 缺 [package] name"
        pkg = matched.group(1)
        crate_dir = cargo_toml.parent
        for rs in sorted(crate_dir.rglob("*.rs")):
            parts = rs.relative_to(crate_dir).parts
            if parts[0] == "tests":
                target: str | None = Path(parts[1]).stem if len(parts) == 2 else parts[1]
            elif parts[0] in ("src", "benches", "examples"):
                target = None
            else:
                continue
            pending = False
            for line in rs.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if attr_re.match(text):
                    pending = True
                    continue
                if not pending:
                    continue
                fn = fn_re.match(text)
                if fn:
                    found.add((pkg, target, fn.group(1)))
                    pending = False
                elif stop_re.match(text):
                    pending = False
    return found


_CAP_FILTER_RE = re.compile(r"^cap_([a-z0-9_]+?)__$")


def _selector_explicit_caps(selector: dict) -> list[str]:
    """OD-53：selector 在 test-binary 參數（`--` 之後）以 `--ignored cap_<cap>__` 明確只執行 ignored 能力測試時，
    回傳其 caps（重現命令為 `cargo_l1_results.py --caps <cap> -- <selector argv>`）；否則回空串列。
    `--include-ignored`、`--skip cap_` 或混有非 cap 篩選者不算能力集合。"""
    argv = [str(x) for x in (selector.get("argv") or [])]
    if "--" not in argv:
        return []
    tail = argv[argv.index("--") + 1:]
    if "--ignored" not in tail:
        return []
    filters = [t for t in tail if not t.startswith("-")]
    caps = [m.group(1) for t in filters if (m := _CAP_FILTER_RE.match(t))]
    return caps if caps and len(caps) == len(filters) else []


def _ignored_lock_rows(lock: dict, ignored: set[tuple[str, str | None, str]]) -> list[str]:
    """L1 鎖中 OD-24 (2) 禁止的 `#[ignore]` 列（`<set>:<platform>:<nodeid>`）；OD-53 能力集合內符合其 cap 前綴者豁免。"""
    flagged: list[str] = []
    for set_id, platforms in lock["sets"].items():
        for plat, spec in platforms.items():
            caps = _selector_explicit_caps(spec["selector"])
            for nid in spec["nodeids"]:
                if not nid.startswith("cargo:"):
                    continue
                parts = nid[len("cargo:"):].split("::")
                if len(parts) < 3:
                    continue
                pkg, target, name = parts[0], parts[1], parts[-1]
                if (pkg, target, name) not in ignored and (pkg, None, name) not in ignored:
                    continue
                if any(name.startswith(f"cap_{cap}__") for cap in caps):
                    continue  # OD-53：selector 明確以 --ignored 執行的能力測試
                flagged.append(f"{set_id}:{plat}:{nid}")
    return flagged


@pytest.mark.memory_only
def test_l1_lock_cargo_sets_exclude_ignored_tests() -> None:
    """OD-24（2026-10-27）：所有 cargo 凍結集合（linux 與 windows）不得含 `#[ignore]` 列。

    官方 cargo L1 selector 不執行 ignored 測試，鎖入後只能得到 skipped；cap_* 與委派 entry
    由其他已鎖案例或 capability gate 承擔。補足只擋 Windows cap_* 的
    test_l1_lock_frozen_set_excludes_capability_gated。以原始碼 `#[ignore]` 屬性判定。
    OD-53（2026-10-29）：selector 明確以 `--ignored cap_<cap>__` 執行 ignored 能力測試的集合，其 cap 列豁免
    （`_selector_explicit_caps`）；其他集合仍禁止 `#[ignore]` 列。
    """
    ignored = _rust_ignored_tests()
    # 非空洞檢查：掃描器必須認得已知的 ignored 測試，否則斷言形同虛設
    assert ("sw-cli", "mgmt_layout", "mkbundle_respects_cargo_target_dir_override") in ignored, sorted(ignored)
    lock = json.loads(Path("tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    flagged = _ignored_lock_rows(lock, ignored)
    assert not flagged, f"cargo 凍結集合含 #[ignore] 列（須經擁有者裁決以 --allow-removal 移除）：{flagged}"


# OD-55（2026-10-29）：s21_core.linux 的能力列移入 s21_gate:cap_<cap>（selector `--ignored cap_<cap>__`）。
_S21_LINUX_CAPS = ("posix_pty", "sys_admin", "windows_native", "interactive_desktop", "supervisor_runtime")


@pytest.mark.memory_only
def test_s21_core_linux_capability_rows_moved_to_cap_sets_od55() -> None:
    """OD-55：s21_core.linux 不得再有 `cap_*` 或其他 `#[ignore]` 列；每個能力的列改登記於 `s21_gate:cap_<cap>.linux`，
    其 selector 為 OD-53 能力集合（`_selector_explicit_caps` 恰為該 cap）、nodeid 全為 `cap_<cap>__*` 且為 ignored 測試。
    非能力列三筆（helper `plain_holder_child`、`real_mkbundle_dev_output_verifies`、`windows_switch_running_exe_rename`）
    直接自 s21_core.linux 移除。s21_core.windows 須待 Windows 原生收集登記能力集合後才移除（LOCAL-WINDOWS-TODO）。"""
    ignored = _rust_ignored_tests()
    lock = json.loads(Path("tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    core = lock["sets"]["s21_core"]["linux"]["nodeids"]
    assert not [n for n in core if n.rsplit("::", 1)[-1].startswith("cap_")], "s21_core.linux 仍含 cap_* 列"
    for name in ("plain_holder_child", "real_mkbundle_dev_output_verifies", "windows_switch_running_exe_rename"):
        assert not [n for n in core if n.endswith(f"::{name}")], name
    assert not [r for r in _ignored_lock_rows(lock, ignored) if r.startswith("s21_core:linux:")]
    moved = 0
    for cap in _S21_LINUX_CAPS:
        spec = lock["sets"][f"s21_gate:cap_{cap}"]["linux"]
        assert _selector_explicit_caps(spec["selector"]) == [cap], spec["selector"]
        assert spec["nodeids"], cap
        for nid in spec["nodeids"]:
            pkg, *_mid, name = nid[len("cargo:"):].split("::")
            assert name.startswith(f"cap_{cap}__"), nid
            assert any(p == pkg and n == name for p, _t, n in ignored), f"{nid} 不是 ignored 測試"
        moved += len(spec["nodeids"])
    assert moved == 24, moved          # 23（OD-55 列舉）＋ cap_interactive_desktop__exec_or_wait_ctrl_c（linux 亦收集）
    assert not [r for r in _ignored_lock_rows(lock, ignored) if r.startswith("s21_gate:cap_")]


@pytest.mark.process_rpc
def test_l1_add_only_creates_new_set_inline(tmp_path: Path) -> None:
    """新 set／新平台可由行內 selector 建立（P2-T13 審查修正：計畫的 --l1-add-only 呼叫未附 selector，原實作拒絕）。"""
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps({"schema_version": 1, "sets": {}}), encoding="utf-8")
    collect = tmp_path / "collect.txt"
    collect.write_text("tests/x.py::test_b\ntests/x.py::test_a\n", encoding="utf-8")
    regen = [sys.executable, "tests/acceptance/regen_lock.py", "--lock-file", str(lock_path)]

    no_sel = subprocess.run(
        [*regen, "--l1-add-only", "--set", "s9_new", "--platform", "linux", "--collect", str(collect)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert no_sel.returncode == 2, no_sel.stderr
    assert json.loads(lock_path.read_text(encoding="utf-8"))["sets"] == {}

    add = subprocess.run(
        [*regen, "--l1-add-only", "--set", "s9_new", "--platform", "linux", "--collect", str(collect),
         "--argv-json", '["-q","tests/x.py"]', "--paths-json", '["tests/x.py"]', "--markexpr", ""],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert add.returncode == 0, add.stdout + add.stderr
    created = json.loads(lock_path.read_text(encoding="utf-8"))["sets"]["s9_new"]["linux"]
    assert created["nodeids"] == ["tests/x.py::test_a", "tests/x.py::test_b"]
    assert created["selector"] == {"argv": ["-q", "tests/x.py"], "paths": ["tests/x.py"], "markexpr": ""}

    chk = subprocess.run(
        [sys.executable, "tests/acceptance/regen_lock.py", "--lock-file", str(lock_path), "--check"],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert chk.returncode == 0, chk.stdout + chk.stderr


@pytest.mark.process_rpc
def test_regen_lock_check_requires_l1_lock(tmp_path: Path) -> None:
    """--check 缺 L1 鎖即失敗；--cases-only 只驗案例鎖（P2-T13 審查修正：刪除鎖檔不得讓 --check 通過）。"""
    missing = tmp_path / "absent.json"
    full = subprocess.run(
        [sys.executable, "tests/acceptance/regen_lock.py", "--check", "--lock-file", str(missing)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert full.returncode == 1, full.stdout + full.stderr
    cases_only = subprocess.run(
        [sys.executable, "tests/acceptance/regen_lock.py", "--check", "--cases-only", "--lock-file", str(missing)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert cases_only.returncode == 0, cases_only.stdout + cases_only.stderr


def _load_checker():
    import importlib.util

    spec = importlib.util.spec_from_file_location("checker_l1_phase", REPO_ROOT / "tests/acceptance/check_evidence_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_platform_evidence(base: Path, plat: str, argv: list[str], nodeids: list[str], fail_nid: str | None = None) -> None:
    from xml.sax.saxutils import quoteattr

    folder = base / plat
    folder.mkdir(parents=True, exist_ok=True)
    results = []
    for nid in nodeids:
        failed = nid == fail_nid
        results.append({
            "nodeid": nid,
            "outcome": "failed" if failed else "passed",
            "skip_kind": None,
            "phases": {"setup": "passed", "call": "failed" if failed else "passed", "teardown": "passed"},
        })
    report = {
        "schema_version": 2, "format": "l1-results.v1", "runner": "pytest", "platform": plat,
        "argv": ["/x/pytest/__main__.py", *argv], "markexpr": "", "counts": {}, "resources": {},
        "uart_kind": "native",
        "not_collected_platform": [], "core_portable_platform_skips": [],
        "collected": list(nodeids), "deselected": [], "results": results,
        "exit_code": 1 if fail_nid else 0, "finished": True, "deployed_content": None,
    }
    (folder / "resource-report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    cases = "".join(
        f"<testcase classname={quoteattr('x')} name={quoteattr(nid)}>"
        + ("<failure message=\"x\"/>" if nid == fail_nid else "")
        + "</testcase>"
        for nid in nodeids
    )
    (folder / "junit.xml").write_text(f"<testsuites><testsuite tests=\"{len(nodeids)}\">{cases}</testsuite></testsuites>", encoding="utf-8")


@pytest.mark.process_rpc
def test_evidence_index_l1_item_phases_2_and_3(tmp_path: Path) -> None:
    """計畫 phase 2／3：全部必要 nodeid passed 且兩平台齊備 → 項目不因 L1 判定而阻擋；任一列 failed → exit 1（P2-T13 審查修正）。"""
    checker = _load_checker()
    lock = json.loads((REPO_ROOT / "tests/acceptance/l1_required.lock.json").read_text(encoding="utf-8"))
    index_data = yaml.safe_load((REPO_ROOT / "tests/acceptance/stage0_evidence_index.yaml").read_text(encoding="utf-8"))

    def build(fail: bool) -> Path:
        root = tmp_path / ("fail" if fail else "pass")
        _copy_present_artifacts(index_data, root)
        base = root / "artifacts" / "s0_bc"
        plats = {}
        for plat in ("linux", "windows"):
            spec = lock["sets"]["s0_bc"][plat]
            fail_nid = spec["nodeids"][0] if fail and plat == "linux" else None
            _write_platform_evidence(base, plat, spec["selector"]["argv"], spec["nodeids"], fail_nid)
            plats[plat] = fail_nid
        digest = checker.l1_artifact_digest(base, ["linux", "windows"])
        data = yaml.safe_load(yaml.safe_dump(index_data, allow_unicode=True, sort_keys=False))
        for item in data["items"]:
            if item["id"] == "S0-L1-BC":
                item.update(status="present", artifact_path="artifacts/s0_bc", sha256=digest)
        index_file = root / "index.yaml"
        index_file.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return root, index_file

    script = str(REPO_ROOT / "tests/acceptance/check_evidence_index.py")

    root_ok, index_ok = build(fail=False)
    res_ok = subprocess.run(
        [sys.executable, script, "--index-file", str(index_ok), "--stage", "0", "--root", str(root_ok),
         "--cases", str(REPO_ROOT / "tests/acceptance/cases.yaml")],
        capture_output=True, text=True,
    )
    out_ok = res_ok.stdout + res_ok.stderr
    assert "l1_" not in " ".join(line for line in out_ok.splitlines() if "S0-L1-BC" in line), out_ok
    assert "missing S0-L1-BC" not in out_ok, out_ok
    assert res_ok.returncode in (0, 5), out_ok  # 其他未收集項目仍 BLOCKED，本測試只判 S0-L1-BC

    root_bad, index_bad = build(fail=True)
    res_bad = subprocess.run(
        [sys.executable, script, "--index-file", str(index_bad), "--stage", "0", "--root", str(root_bad),
         "--cases", str(REPO_ROOT / "tests/acceptance/cases.yaml")],
        capture_output=True, text=True,
    )
    assert res_bad.returncode == 1, res_bad.stdout + res_bad.stderr
    assert "l1_fail S0-L1-BC" in res_bad.stderr, res_bad.stderr


@pytest.mark.process_rpc
def test_evidence_index_pins_l1_set(tmp_path: Path) -> None:
    """S0-L1-BC 移除 l1_set 或少報平台 → FAIL（P2-T13 審查修正：不得退回 sha256-only，不得漏平台）。"""
    index_data = yaml.safe_load((REPO_ROOT / "tests/acceptance/stage0_evidence_index.yaml").read_text(encoding="utf-8"))
    script = str(REPO_ROOT / "tests/acceptance/check_evidence_index.py")
    cases = str(REPO_ROOT / "tests/acceptance/cases.yaml")

    no_set = yaml.safe_load(yaml.safe_dump(index_data, allow_unicode=True, sort_keys=False))
    for item in no_set["items"]:
        if item["id"] == "S0-L1-BC":
            item.pop("l1_set", None)
    f1 = tmp_path / "no_set.yaml"
    f1.write_text(yaml.safe_dump(no_set, allow_unicode=True, sort_keys=False), encoding="utf-8")
    r1 = subprocess.run([sys.executable, script, "--index-file", str(f1), "--stage", "0", "--cases", cases],
                        capture_output=True, text=True)
    assert r1.returncode == 1 and "l1_set_missing S0-L1-BC" in r1.stderr, r1.stderr

    one_plat = yaml.safe_load(yaml.safe_dump(index_data, allow_unicode=True, sort_keys=False))
    for item in one_plat["items"]:
        if item["id"] == "S0-L1-BC":
            item["l1_set"]["platforms"] = ["linux"]
    f2 = tmp_path / "one_plat.yaml"
    f2.write_text(yaml.safe_dump(one_plat, allow_unicode=True, sort_keys=False), encoding="utf-8")
    r2 = subprocess.run([sys.executable, script, "--index-file", str(f2), "--stage", "0", "--cases", cases],
                        capture_output=True, text=True)
    assert r2.returncode == 1 and "l1_platforms_mismatch S0-L1-BC" in r2.stderr, r2.stderr


@pytest.mark.memory_only
def test_junit_counts_testcase_layer(tmp_path: Path) -> None:
    """junit 計數以 testcase 元素為準；subtests 只計入 testsuite tests 屬性（pytest 9），不得使計數失配（P2-T13 審查修正）。"""
    checker = _load_checker()
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="6" failures="2" errors="0" skipped="0">'
        '<testcase name="a"><failure/></testcase><testcase name="b"/>'
        '<testcase name="c"><failure/></testcase></testsuite></testsuites>',
        encoding="utf-8",
    )
    assert checker._junit_counts(junit) == {"tests": 3, "failures": 2, "errors": 0, "skipped": 0}


@pytest.mark.process_rpc
def test_l1_add_only_removes_only_explicitly_approved_nodeids(tmp_path: Path) -> None:
    """已替換的凍結占位節點須逐名核准移除，未列名或多列名都不得改鎖。"""
    lock_path = tmp_path / "lock.json"
    original = {
        "schema_version": 1,
        "sets": {
            "s1b_cargo": {
                "linux": {
                    "selector": {"argv": ["--workspace"], "paths": [], "markexpr": ""},
                    "nodeids": ["cargo:sw-cli::target::old_placeholder", "cargo:sw-cli::target::kept"],
                }
            }
        },
    }
    lock_path.write_text(json.dumps(original), encoding="utf-8")
    original_bytes = lock_path.read_bytes()
    collect = tmp_path / "collect.json"
    collect.write_text(json.dumps({"collected": ["cargo:sw-cli::target::kept", "cargo:sw-cli::target::replacement"]}), encoding="utf-8")
    regen = [sys.executable, "tests/acceptance/regen_lock.py", "--lock-file", str(lock_path),
             "--l1-add-only", "--set", "s1b_cargo", "--platform", "linux", "--collect", str(collect)]

    refused = subprocess.run(regen, capture_output=True, text=True, cwd=REPO_ROOT)
    assert refused.returncode == 1, refused.stdout + refused.stderr
    assert lock_path.read_bytes() == original_bytes

    allowed = subprocess.run([*regen, "--allow-removal", "s1b_cargo:linux:cargo:sw-cli::target::old_placeholder"],
                             capture_output=True, text=True, cwd=REPO_ROOT)
    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    revised = json.loads(lock_path.read_text(encoding="utf-8"))
    assert revised["sets"]["s1b_cargo"]["linux"]["nodeids"] == [
        "cargo:sw-cli::target::kept", "cargo:sw-cli::target::replacement"
    ]

    revised_bytes = lock_path.read_bytes()
    overapproved = subprocess.run([*regen, "--allow-removal", "s1b_cargo:linux:cargo:sw-cli::target::missing"],
                                  capture_output=True, text=True, cwd=REPO_ROOT)
    assert overapproved.returncode == 1, overapproved.stdout + overapproved.stderr
    assert lock_path.read_bytes() == revised_bytes


@pytest.mark.process_rpc
@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_l1_add_only_excludes_ignored_nodeids_od24(tmp_path: Path, platform: str) -> None:
    """OD-24 回歸：以 68d0941 的收集形狀（--list-only 含 #[ignore] 列與 OD-16／OD-17 已移除列）
    餵 --l1-add-only，只能新增未 ignored 的真實測試；ignored 列不得加入凍結集合。"""
    existing = "cargo:sw-cli::mgmt_daemon::start_default_locates_daemon"
    real_new = {
        "linux": [
            "cargo:sw-cli::mgmt_daemon::start_windows_python_launcher_uses_manifest_runtime",
            "cargo:sw-pal::proc_scan::posix_tty_holder_scan_ignores_unrelated_tty_file",
            "cargo:sw-pal::proc_scan::posix_tty_holder_scan_reports_unknown_when_target_unstatable",
        ],
        "windows": ["cargo:sw-cli::mgmt_daemon::start_windows_python_launcher_uses_manifest_runtime"],
    }[platform]
    ignored = {
        "linux": [
            "cargo:sw-cli::mgmt_layout::mkbundle_respects_cargo_target_dir_override",
            "cargo:sw-cli::mgmt_layout::real_mkbundle_dev_output_verifies",
            "cargo:sw-cli::mgmt_layout::windows_switch_running_exe_rename",
            "cargo:sw-daemon::launcher::cap_windows_native__launcher_gui_subsystem",
            "cargo:sw-daemon::launcher::cap_windows_native__launcher_parent_kill_kills_child",
            "cargo:sw-pal::exec_delegate::cap_interactive_desktop__exec_or_wait_ctrl_c",
            "cargo:sw-pal::service_ctl::cap_supervisor_runtime__systemd_hold_blocks_restart_on_failure",
        ],
        "windows": [
            "cargo:sw-cli::mgmt_layout::mkbundle_respects_cargo_target_dir_override",
            "cargo:sw-cli::mgmt_layout::real_mkbundle_dev_output_verifies",
            "cargo:sw-cli::mgmt_layout::windows_switch_running_exe_rename",
            "cargo:sw-pal::exec_delegate::cap_interactive_desktop__exec_or_wait_ctrl_c",
            "cargo:sw-pal::exec_delegate::exec_or_wait_tree_delegate_entry",
            "cargo:sw-pal::exec_delegate::exec_or_wait_tree_grandchild_entry",
            "cargo:sw-pal::exec_delegate::exec_or_wait_tree_launcher_entry",
            "cargo:sw-pal::service_ctl::cap_supervisor_runtime__windows_task_roundtrip",
        ],
    }[platform]
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps({
        "schema_version": 1,
        "sets": {"s1b_cargo": {platform: {"selector": {"argv": ["--workspace"], "paths": [], "markexpr": ""},
                                          "nodeids": [existing]}}},
    }), encoding="utf-8")
    collect = tmp_path / "collect.json"
    collect.write_text(json.dumps({"collected": [existing, *real_new, *ignored], "ignored": ignored}), encoding="utf-8")
    res = subprocess.run(
        [sys.executable, "tests/acceptance/regen_lock.py", "--lock-file", str(lock_path),
         "--l1-add-only", "--set", "s1b_cargo", "--platform", platform, "--collect", str(collect)],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    got = json.loads(lock_path.read_text(encoding="utf-8"))["sets"]["s1b_cargo"][platform]["nodeids"]
    assert got == sorted([existing, *real_new])
    assert not set(got) & set(ignored)


@pytest.mark.process_rpc
def test_l1_add_only_ignored_existing_rows_kept_unless_approved_od24(tmp_path: Path) -> None:
    """OD-24：鎖中既有的 ignored 列仍在 collected 內，不被視為移除；只有逐名 --allow-removal 才移除。"""
    kept = "cargo:sw-pal::exec_delegate::exec_or_wait_terminate_kills_default_grandchild"
    entry = "cargo:sw-pal::exec_delegate::exec_or_wait_tree_delegate_entry"
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps({
        "schema_version": 1,
        "sets": {"s1b_cargo": {"windows": {"selector": {"argv": ["--workspace"], "paths": [], "markexpr": ""},
                                           "nodeids": [kept, entry]}}},
    }), encoding="utf-8")
    collect = tmp_path / "collect.json"
    collect.write_text(json.dumps({"collected": [kept, entry], "ignored": [entry]}), encoding="utf-8")
    regen = [sys.executable, "tests/acceptance/regen_lock.py", "--lock-file", str(lock_path),
             "--l1-add-only", "--set", "s1b_cargo", "--platform", "windows", "--collect", str(collect)]

    unchanged = subprocess.run(regen, capture_output=True, text=True, cwd=REPO_ROOT)
    assert unchanged.returncode == 0, unchanged.stdout + unchanged.stderr
    assert json.loads(lock_path.read_text(encoding="utf-8"))["sets"]["s1b_cargo"]["windows"]["nodeids"] == [kept, entry]

    before = lock_path.read_bytes()
    not_ignored = subprocess.run([*regen, "--allow-removal", f"s1b_cargo:windows:{kept}"],
                                 capture_output=True, text=True, cwd=REPO_ROOT)
    assert not_ignored.returncode == 1, not_ignored.stdout + not_ignored.stderr  # 仍收集且未 ignored：核准無對象
    assert lock_path.read_bytes() == before

    removed = subprocess.run([*regen, "--allow-removal", f"s1b_cargo:windows:{entry}"],
                             capture_output=True, text=True, cwd=REPO_ROOT)
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert json.loads(lock_path.read_text(encoding="utf-8"))["sets"]["s1b_cargo"]["windows"]["nodeids"] == [kept]
