"""P9-T7B L1.5 A/B 差分工具的失敗測試（TDD red）。"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = REPO_ROOT / "contracts" / "ab_normalization.yaml"
SC15_CASE_ID = "sc15-console-ring-drop-oldest"


def _load_ab_diff():
    """延遲匯入待實作的差分工具，讓整套測試仍可完成 collection。"""
    return importlib.import_module("tools.stage3.ab_diff")


def _prepare_reports(tmp_path: Path):
    """用共用 suite fake 產生兩側報告樹。"""
    ab_diff = _load_ab_diff()
    fakes_suites = importlib.import_module("fakes_suites")
    env = fakes_suites.ab_env(tmp_path, layer="l1.5")
    report_dirs: dict[str, Path] = {}

    for side in env.m.sides:
        report_dir = tmp_path / "reports" / side.label
        report_dirs[side.label] = report_dir
        env.runner(
            [
                sys.executable,
                str(env.repo / "func-test" / "runner.py"),
                "--mode",
                "bridge",
                "--dut-mode",
                "local-process",
                "--report-dir",
                str(report_dir),
                "--selftest-report",
                str(env.m.suite.selftest_report.path),
            ],
            env={
                "SW_STAGE3_SIDE": side.label,
                "SERIALWRAP_CLI_EXE": str(side.cli_exe),
                "SERIALWRAP_DAEMON_EXE": str(side.daemon_exe),
                "SERIALWRAP_RUN_DIR": str(tmp_path / "instances" / side.label),
            },
        )

    return ab_diff, report_dirs["A"], report_dirs["B"]


def _case_ids(report_dir: Path) -> list[str]:
    return sorted({path.parent.parent.parent.name for path in report_dir.glob("*/bridge/*/manifest.json")})


def _case_runs(report_dir: Path, case_id: str) -> list[Path]:
    return sorted(path for path in (report_dir / case_id / "bridge").iterdir() if path.is_dir())


def _equalize_reports(a_dir: Path, b_dir: Path) -> None:
    """以 A 側證據統一兩側的同名報告項目，建立差分測試的共同基準。"""
    for case_id in _case_ids(a_dir):
        for a_run in _case_runs(a_dir, case_id):
            b_run = b_dir / a_run.relative_to(a_dir)
            for name in ("manifest.json", "bridge-manifest.json", "comparison.json", "cli.ndjson"):
                shutil.copyfile(a_run / name, b_run / name)


def _first_run(report_dir: Path, case_id: str) -> Path:
    return _case_runs(report_dir, case_id)[0]


def _set_comparison(
    report_dir: Path,
    case_id: str,
    *,
    verdict: str | None = None,
    invariants: list[dict[str, str]] | None = None,
) -> None:
    path = _first_run(report_dir, case_id) / "comparison.json"
    comparison = json.loads(path.read_text(encoding="utf-8"))
    if verdict is not None:
        comparison["verdict"] = verdict
    if invariants is not None:
        comparison["invariants"] = invariants
    path.write_text(json.dumps(comparison, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _set_cli_calls(report_dir: Path, case_id: str, calls: list[dict[str, Any]]) -> None:
    path = _first_run(report_dir, case_id) / "cli.ndjson"
    with path.open("w", encoding="utf-8") as stream:
        for index, call in enumerate(calls, start=1):
            response = call["response"]
            record = {
                "argv": call.get("argv", ["session", "list"]),
                "mono_start_ns": index,
                "rc": call.get("rc", 0),
                "response": response,
                "stderr": "",
                "stdout": json.dumps(response, ensure_ascii=False, separators=(",", ":")),
            }
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _ensure_case(report_dir: Path, case_id: str, template_case_id: str) -> None:
    if (report_dir / case_id).exists():
        return
    shutil.copytree(report_dir / template_case_id, report_dir / case_id)
    for run_dir in _case_runs(report_dir, case_id):
        for name in ("manifest.json", "comparison.json"):
            path = run_dir / name
            document = json.loads(path.read_text(encoding="utf-8"))
            document["case_id"] = case_id
            path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _diff(
    ab_diff,
    a_dir: Path,
    b_dir: Path,
    *,
    required_case_ids: set[str],
    case_changes: dict[str, str] | None = None,
) -> dict[str, Any]:
    return ab_diff.diff_l15(
        a_dir,
        b_dir,
        registered={"SC-15": "absolute_only"},
        case_changes=case_changes or {},
        required_case_ids=required_case_ids,
        rules_path=RULES_PATH,
    )


def test_identical_reports_ok(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["ok"] is True
    assert result["differences"] == []


def test_missing_required_case_breaks_integrity(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    missing_case_id = _case_ids(a_dir)[0]
    shutil.rmtree(b_dir / missing_case_id)

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["integrity"]["b"] is False
    assert any(missing_case_id in str(problem) for problem in result["integrity_problems"]["b"])


def test_insufficient_comparison_breaks_integrity(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    _set_comparison(a_dir, case_id, verdict="INSUFFICIENT_EVIDENCE")

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["integrity"]["a"] is False


def test_invariant_fail_both_sides_reported(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    failed = [{"id": "zero_tx_on_reject", "verdict": "FAIL"}]
    _set_comparison(a_dir, case_id, verdict="FAIL", invariants=failed)
    _set_comparison(b_dir, case_id, verdict="FAIL", invariants=failed)

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert f"A:{case_id}:zero_tx_on_reject" in result["safety_violations"]
    assert f"B:{case_id}:zero_tx_on_reject" in result["safety_violations"]


def test_exception_message_ignored_g1(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    _set_cli_calls(
        a_dir,
        case_id,
        [
            {
                "argv": ["session", "list"],
                "response": {
                    "id": 1,
                    "ok": False,
                    "error_code": "EXCEPTION",
                    "message": "Python detail",
                },
            }
        ],
    )
    _set_cli_calls(
        b_dir,
        case_id,
        [
            {
                "argv": ["session", "list"],
                "response": {
                    "id": 1,
                    "ok": False,
                    "error_code": "EXCEPTION",
                    "message": "Rust detail",
                },
            }
        ],
    )

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"] == []


def test_set_path_order_ignored_g2(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    _set_cli_calls(
        a_dir,
        case_id,
        [
            {"argv": ["event", "reload"], "response": {"ok": True, "added": ["alpha", "beta"]}},
            {"argv": ["event", "tail"], "response": {"ok": True, "entries": ["alpha", "beta"]}},
        ],
    )
    _set_cli_calls(
        b_dir,
        case_id,
        [
            {"argv": ["event", "reload"], "response": {"ok": True, "added": ["beta", "alpha"]}},
            {"argv": ["event", "tail"], "response": {"ok": True, "entries": ["beta", "alpha"]}},
        ],
    )

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert len(result["differences"]) == 1
    assert result["differences"][0]["kind"] == "value"


def test_volatile_and_root_normalized(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    _set_cli_calls(
        a_dir,
        case_id,
        [{"response": {"pid": 4101, "state_path": f"{a_dir}/instance/state.json"}}],
    )
    _set_cli_calls(
        b_dir,
        case_id,
        [{"response": {"pid": 4202, "state_path": f"{b_dir}/instance/state.json"}}],
    )

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"] == []


def test_poll_failure_is_not_collapsed_with_following_success(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    success = {"argv": ["daemon", "status"], "rc": 0, "response": {"ok": True, "running": True}}
    _set_cli_calls(a_dir, case_id, [success])
    _set_cli_calls(
        b_dir,
        case_id,
        [
            {"argv": ["daemon", "status"], "rc": 1,
             "response": {"ok": False, "error_code": "INTERNAL_ERROR"}},
            success,
        ],
    )

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"]


def test_poll_status_transition_is_not_collapsed(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    running = {"argv": ["cmd", "status", "--cmd-id", "a" * 32], "rc": 0,
               "response": {"ok": True, "command": {"status": "running"}}}
    done = {"argv": ["cmd", "status", "--cmd-id", "b" * 32], "rc": 0,
            "response": {"ok": True, "command": {"status": "done"}}}
    _set_cli_calls(a_dir, case_id, [running, done])
    _set_cli_calls(b_dir, case_id, [done])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"]


@pytest.mark.parametrize(
    "a_holders,b_holders",
    [
        (
            {},
            {"/dev/pts/20": {"device": "/dev/pts/20", "holder_unknown": True}},
        ),
        (
            {"/dev/pts/20": {"device": "/dev/pts/20", "holder_unknown": True}},
            {"/dev/pts/24": {"device": "/dev/pts/24", "holder_unknown": True, "pids": [4102]}},
        ),
        (
            {"/dev/pts/20": {"device": "/dev/pts/20", "holder_unknown": True, "pids": [4102]}},
            {"/dev/pts/24": {"device": "/dev/pts/24", "holder_unknown": True, "pids": [5203, 5204]}},
        ),
    ],
    ids=["empty-to-holder", "no-pids-to-pids", "pid-scan-changes"],
)
def test_foreign_holders_scan_snapshot_flapping_is_ignored(tmp_path, a_holders, b_holders):
    """A/A 的 /proc 快照可在空值、無 pids 與含 pids 間變動，不得形成產品差異。

    欄位形狀取自 261023 的 l15-reject-zero-tx 與 l15-release-inflight 報告。
    """
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    _set_cli_calls(a_dir, case_id, [{"argv": ["daemon", "status"], "rc": 0, "response": {
        "ok": True,
        "pid": 4100,
        "foreign_holders": a_holders,
    }}])
    _set_cli_calls(b_dir, case_id, [{"argv": ["daemon", "status"], "rc": 0, "response": {
        "ok": True,
        "pid": 4100,
        "foreign_holders": b_holders,
    }}])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"] == []


def _device_still_held(pids: list[int], *, holder_unknown: bool = True) -> dict[str, Any]:
    return {"ok": False, "error_code": "DEVICE_STILL_HELD", "pids": pids, "holder_unknown": holder_unknown}


@pytest.mark.parametrize(
    "a_pids,b_pids",
    [([100, 200], []), ([100, 200], [100]), ([100], [100, 200])],
    ids=["pids-vs-empty", "pids-count-shrunk", "pids-count-grown"],
)
def test_device_still_held_pid_list_changes_are_differences(tmp_path, a_pids, b_pids):
    """回歸（24f42d0 Medium）：DEVICE_STILL_HELD 的 pids 數量不得被正規化吞掉。"""
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    call = {"argv": ["device", "attach", "--selector", "COM0"], "rc": 1}
    _set_cli_calls(a_dir, case_id, [{**call, "response": _device_still_held(a_pids)}])
    _set_cli_calls(b_dir, case_id, [{**call, "response": _device_still_held(b_pids)}])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"]


def test_device_still_held_pid_identity_relation_is_difference(tmp_path):
    """回歸（24f42d0 Medium）：pid 與 pids 的關聯改變（A 的 pid 屬於 pids，B 不屬於）須判為差異。"""
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    _set_cli_calls(a_dir, case_id, [{"argv": ["daemon", "status"], "rc": 0,
                                     "response": {"ok": True, "pid": 100, "pids": [100, 200]}}])
    _set_cli_calls(b_dir, case_id, [{"argv": ["daemon", "status"], "rc": 0,
                                     "response": {"ok": True, "pid": 300, "pids": [100, 200]}}])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"]


def test_device_still_held_missing_pids_differs_from_empty_list(tmp_path):
    """DEVICE_STILL_HELD 的 pids 欄位結構保留；缺欄不得與空清單相等。"""
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    call = {"argv": ["device", "attach", "--selector", "COM0"], "rc": 1}
    a_response = _device_still_held([])
    b_response = _device_still_held([])
    b_response.pop("pids")
    _set_cli_calls(a_dir, case_id, [{**call, "response": a_response}])
    _set_cli_calls(b_dir, case_id, [{**call, "response": b_response}])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"]


def test_device_still_held_distinct_pids_differ_from_duplicate_pid(tmp_path):
    """DEVICE_STILL_HELD 的兩個不同 holder PID 不得與重複同一 PID 正規化成相同結果。"""
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    call = {"argv": ["device", "attach", "--selector", "COM0"], "rc": 1}
    _set_cli_calls(a_dir, case_id, [{**call, "response": _device_still_held([101, 202])}])
    _set_cli_calls(b_dir, case_id, [{**call, "response": _device_still_held([101, 101])}])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"]


@pytest.mark.parametrize(
    "a_pids,b_pids",
    [([101, 202], [303, 404]), ([101, 202], [202, 101])],
    ids=["different-runtime-pids", "pids-list-permutation"],
)
def test_device_still_held_same_pid_relation_is_equal(tmp_path, a_pids, b_pids):
    """A/A 的 PID 數量與重複關係相同時，實際 PID 值及清單順序不構成差異。"""
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    call = {"argv": ["device", "attach", "--selector", "COM0"], "rc": 1}
    _set_cli_calls(a_dir, case_id, [{**call, "response": _device_still_held(a_pids)}])
    _set_cli_calls(b_dir, case_id, [{**call, "response": _device_still_held(b_pids)}])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"] == []


def test_device_still_held_holder_unknown_flip_is_difference(tmp_path):
    """回歸（24f42d0 Medium）：holder_unknown 由 true 變 false 須判為差異。"""
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    call = {"argv": ["device", "attach", "--selector", "COM0"], "rc": 1}
    _set_cli_calls(a_dir, case_id, [{**call, "response": _device_still_held([100], holder_unknown=True)}])
    _set_cli_calls(b_dir, case_id, [{**call, "response": _device_still_held([100], holder_unknown=False)}])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"]


def test_health_status_daemon_pid_value_is_aliased(tmp_path):
    """health.status 的 daemon PID 是每次執行都會變動的數值，不應單獨形成差異。"""
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    _set_cli_calls(a_dir, case_id, [{"argv": ["health", "status"], "rc": 0, "response": {
        "ok": True, "pid": 10,
    }}])
    _set_cli_calls(b_dir, case_id, [{"argv": ["health", "status"], "rc": 0, "response": {
        "ok": True, "pid": 20,
    }}])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"] == []


def test_foreign_holder_pid_relation_in_scan_snapshot_is_volatile(tmp_path):
    """掃描快照中 holder PID 與 daemon PID 的關係隨 /proc 掃描變動，不是差分契約。"""
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    _set_cli_calls(a_dir, case_id, [{"argv": ["daemon", "status"], "rc": 0, "response": {
        "ok": True, "pid": 10, "foreign_holders": {"/dev/pts/1": 10},
    }}])
    _set_cli_calls(b_dir, case_id, [{"argv": ["daemon", "status"], "rc": 0, "response": {
        "ok": True, "pid": 10, "foreign_holders": {"/dev/pts/1": 30},
    }}])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"] == []


def test_device_still_held_foreign_holders_are_compared_strictly(tmp_path):
    """foreign_holders 只在成功的 daemon status 快照中易變；DEVICE_STILL_HELD 回應中持有者與空值不得相等。"""
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    call = {"argv": ["device", "attach", "--selector", "COM0"], "rc": 1}
    _set_cli_calls(a_dir, case_id, [{**call, "response": {
        **_device_still_held([101]), "foreign_holders": {"/dev/pts/20": 101},
    }}])
    _set_cli_calls(b_dir, case_id, [{**call, "response": {
        **_device_still_held([101]), "foreign_holders": {},
    }}])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"]


def test_daemon_status_error_response_compares_foreign_holders_strictly(tmp_path):
    """daemon status 的錯誤回應不屬於掃描快照情境，其 foreign_holders 仍須嚴格比較。"""
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    _set_cli_calls(a_dir, case_id, [{"argv": ["daemon", "status"], "rc": 1, "response": {
        "ok": False, "error_code": "EXCEPTION", "message": "x", "foreign_holders": {},
    }}])
    _set_cli_calls(b_dir, case_id, [{"argv": ["daemon", "status"], "rc": 1, "response": {
        "ok": False, "error_code": "EXCEPTION", "message": "x",
        "foreign_holders": {"/dev/pts/24": 4102},
    }}])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"]


def test_daemon_status_nested_foreign_holders_remain_strict(tmp_path):
    """成功 daemon status 僅忽略回應最上層的掃描欄位，巢狀同名欄位仍須比較。"""
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    call = {"argv": ["daemon", "status"], "rc": 0}
    _set_cli_calls(a_dir, case_id, [{**call, "response": {
        "ok": True,
        "foreign_holders": {"/dev/pts/20": 4102},
        "metadata": {"foreign_holders": {"source": "A"}},
    }}])
    _set_cli_calls(b_dir, case_id, [{**call, "response": {
        "ok": True,
        "foreign_holders": {"/dev/pts/24": 5203},
        "metadata": {"foreign_holders": {"source": "B"}},
    }}])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"]


def test_daemon_status_polls_with_flapping_snapshot_collapse(tmp_path):
    """連續成功的 daemon status 輪詢若只有 foreign_holders 不同，應收斂為單筆，與另一側單次輪詢的呼叫數一致。"""
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    _equalize_reports(a_dir, b_dir)
    case_id = _case_ids(a_dir)[0]
    status = {"argv": ["daemon", "status"], "rc": 0}
    _set_cli_calls(a_dir, case_id, [
        {**status, "response": {"ok": True, "pid": 4100, "foreign_holders": {}}},
        {**status, "response": {"ok": True, "pid": 4100, "foreign_holders": {
            "/dev/pts/24": {"device": "/dev/pts/24", "holder_unknown": True, "pids": [4102]},
        }}},
    ])
    _set_cli_calls(b_dir, case_id, [
        {**status, "response": {"ok": True, "pid": 4100, "foreign_holders": {
            "/dev/pts/9": {"device": "/dev/pts/9", "holder_unknown": True},
        }}},
    ])

    result = _diff(ab_diff, a_dir, b_dir, required_case_ids=set(_case_ids(a_dir)))

    assert result["differences"] == []


def test_difference_attributed_to_case_change(tmp_path):
    ab_diff, a_dir, b_dir = _prepare_reports(tmp_path)
    template_case_id = _case_ids(a_dir)[0]
    _ensure_case(a_dir, SC15_CASE_ID, template_case_id)
    _ensure_case(b_dir, SC15_CASE_ID, template_case_id)
    _equalize_reports(a_dir, b_dir)
    _set_cli_calls(a_dir, SC15_CASE_ID, [{"response": {"value": "python"}}])
    _set_cli_calls(b_dir, SC15_CASE_ID, [{"response": {"value": "rust"}}])

    result = _diff(
        ab_diff,
        a_dir,
        b_dir,
        required_case_ids={SC15_CASE_ID},
        case_changes={SC15_CASE_ID: "SC-15"},
    )

    assert len(result["differences"]) == 1
    assert result["differences"][0]["registered_change"] == "SC-15"
