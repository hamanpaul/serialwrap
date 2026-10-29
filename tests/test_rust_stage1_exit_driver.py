"""Stage 1 出口驅動腳本的靜態與記憶體內檢查（S1X G2）。

檢查 scripts/rust/stage1_exit_driver.sh 的順序與參數契約：自測報告以核准 tree 挑選、SW_BUNDLE 有 export、
register 與 check 位於唯一一次完整 pytest 之前、gate 帶 Windows L1.5 與 spike3、報告路徑取工具 run_id 而非 ls -t。
自測報告選擇邏輯以 exec 其內嵌的 python 區段，於 tmp_path 內執行（不啟動子行程、不開 socket／PTY／裝置）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

from scripts.rust import l15_exit_driver

REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER = REPO_ROOT / "scripts" / "rust" / "stage1_exit_driver.sh"
APPROVED_JSON = REPO_ROOT / "tools" / "uart_stdio_bridge" / "APPROVED_REVISION.json"
STALE_TREE = "9eaed6de5abd3d75d18929b6fdb3bdac7e1929fe32f666c3193c9983005ecd1c"


def _driver_text() -> str:
    return DRIVER.read_text(encoding="utf-8")


def _joined() -> str:
    """把 bash 行尾續行（反斜線換行）併為單行，便於以子字串比對完整命令。"""
    return re.sub(r"[ \t]*\\\n[ \t]*", " ", _driver_text())


def _step_index(text: str, needle: str) -> int:
    idx = text.find(needle)
    assert idx >= 0, f"驅動腳本缺少：{needle}"
    return idx


def _approved_tree() -> str:
    return json.loads(APPROVED_JSON.read_text(encoding="utf-8"))["tree_sha256"]


def _selection_source() -> str:
    m = re.search(r"^select_selftest\(\) \{\n  python3 - [^\n]*<<'PY'\n(.*?)\nPY\n\}", _driver_text(),
                  re.S | re.M)
    assert m, "找不到 select_selftest 的內嵌 python 區段"
    return m.group(1)


def _write_report(home: Path, name: str, tree: str, verdict: str = "PASS",
                  created: str = "2026-10-22T00:00:00Z") -> Path:
    d = home / "b-log" / "rust-refactor" / "p2" / name
    d.mkdir(parents=True, exist_ok=True)
    path = d / "selftest-report.json"
    path.write_text(json.dumps({"verdict": verdict, "created_at": created, "tool": {"tree_sha256": tree}}),
                    encoding="utf-8")
    return path


def _run_selection(monkeypatch, capsys, home: Path, kind: str, override: str = "") -> tuple[int, str]:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(sys, "argv", ["select_selftest", str(APPROVED_JSON), kind, override])
    code = compile(_selection_source(), "<select_selftest>", "exec")
    rc = 0
    try:
        exec(code, {"__name__": "__main__"})
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    return rc, capsys.readouterr().out.strip()


# ── 自測報告選擇：核准 tree ───────────────────────────────────────────────

def test_selection_rejects_stale_tree_only(monkeypatch, capsys, tmp_path):
    _write_report(tmp_path, "selftest-linux", STALE_TREE)
    rc, out = _run_selection(monkeypatch, capsys, tmp_path, "linux")
    assert rc == 1
    assert out == "", "找不到核准 tree 時不得輸出任何（舊）報告路徑"


def test_selection_prefers_approved_pass_over_stale(monkeypatch, capsys, tmp_path):
    _write_report(tmp_path, "selftest-linux", STALE_TREE)
    approved = _write_report(tmp_path, "selftest-linux-20261022", _approved_tree())
    rc, out = _run_selection(monkeypatch, capsys, tmp_path, "linux")
    assert rc == 0
    assert out == str(approved)


def test_selection_requires_pass_verdict(monkeypatch, capsys, tmp_path):
    _write_report(tmp_path, "selftest-linux-20261022", _approved_tree(), verdict="FAIL")
    rc, out = _run_selection(monkeypatch, capsys, tmp_path, "linux")
    assert rc == 1
    assert out == ""


def test_selection_override_must_match_approved_tree(monkeypatch, capsys, tmp_path):
    stale = _write_report(tmp_path, "selftest-linux", STALE_TREE)
    rc, out = _run_selection(monkeypatch, capsys, tmp_path, "linux", str(stale))
    assert rc == 1
    assert out == "", "SELFTEST_LINUX 覆寫仍須核對核准 tree"


def test_selection_override_accepts_approved_tree(monkeypatch, capsys, tmp_path):
    approved = _write_report(tmp_path, "selftest-linux-20261022", _approved_tree())
    rc, out = _run_selection(monkeypatch, capsys, tmp_path, "linux", str(approved))
    assert rc == 0
    assert out == str(approved)


def test_windows_selection_uses_windows_glob_only(monkeypatch, capsys, tmp_path):
    approved = _write_report(tmp_path, "selftest-windows-20261022", _approved_tree())
    rc, out = _run_selection(monkeypatch, capsys, tmp_path, "windows")
    assert rc == 0 and out == str(approved)
    rc, out = _run_selection(monkeypatch, capsys, tmp_path, "linux")
    assert rc == 1 and out == "", "Linux 挑選不得取到 Windows 目錄的報告"


# ── 驅動腳本靜態契約 ──────────────────────────────────────────────────────

def test_driver_selftest_paths_follow_approved_tree_not_old_report():
    text = _driver_text()
    assert "tools/uart_stdio_bridge/APPROVED_REVISION.json" in text
    assert 'f"selftest-{kind}*"' in text, "自測報告以 kind 組出 selftest-linux*／selftest-windows* 樣式"
    assert "select_selftest windows" in text and "select_selftest linux" in text
    assert "p2/selftest-linux/selftest-report.json" not in text, "不得寫死舊 tree 9eaed6de 的報告路徑"


def test_sw_bundle_is_exported():
    assert re.search(r"^\s*export SW_BUNDLE=", _driver_text(), re.M), "plan 05 Step 4 要求 export SW_BUNDLE"


def test_full_pytest_runs_exactly_once_after_register_and_check():
    text = _joined()
    full = "env PYTHONPATH=scripts/rust SW_REQUIRE_CARGO=1 " \
           'SW_RESOURCE_REPORT="$EV/l1/linux/pytest-full/resource-report.json" ' \
           "python3 -m pytest -q tests/ -p junit_provenance --junitxml=\"$EV/pytest-full.xml\""
    assert text.count(full) == 1, "完整 pytest 的 argv 必須與 G1 selector 所述命令一致，且只出現一次"
    assert text.count("pytest -q tests/ ") == 1, "完整 pytest 之外不得再有 tests/ 全套執行"
    assert "pytest_final" not in text, "pytest_final 已刪除（同一 HEAD 的完整 pytest 承擔）"
    reg = _step_index(text, "register_p4_evidence.py")
    chk = _step_index(text, "check_p4_cases.py --check")
    pyt = _step_index(text, full)
    assert reg < chk < pyt, "register 與 check_p4_cases 必須先於完整 pytest（測試讀取 target/p4-evidence）"


def test_register_carries_windows_cli_and_junit():
    text = _joined()
    assert '--windows-cli "$WIN_CLI"' in text
    assert '--windows-unit-junit "$WIN_UNIT_JUNIT"' in text
    assert 'WIN_CLI="$WB_DIR/bundle/bin/serialwrap.exe"' in text
    assert 'WIN_UNIT_JUNIT="$WB_DIR/l1/windows/blackbox/junit.xml"' in text


def test_gate_receives_windows_l15_spike3_and_explicit_ab_report():
    text = _joined()
    assert '"windows=$EV/func-test-windows/func-test-report.json"' in text
    assert '--spike3-evidence "$EV/spike3"' in text
    assert "--ab-report" in text
    assert "win_l15_func_test" in text and "spike3_win_wal" in text


def test_gate_receives_basic_windows_l15_report():
    """S1X G3 (d)：gate 5c-iv 要求 cases.lock 宣告的 windows 平台都有列，驅動須以第二個 windows= 報告（基本案例）傳入。"""
    text = _joined()
    assert '"windows=$EV/func-test-windows/basic/func-test-report.json"' in text


def test_env_overridable_variables_are_not_declared_local():
    """S1X G3 (f)：RUN_ID、WIN_L15_WRAPPER、SPIKE3_WRAPPER 可由環境覆寫，不得列於 local 宣告（local 會遮蔽環境值，
    其後的 ${VAR:-預設} 永遠取到預設值）。"""
    text = _driver_text()
    local_lines = [ln for ln in text.splitlines() if re.match(r"^\s*local\b", ln)]
    assert local_lines, "驅動的 main 應仍有 local 宣告"
    for name in ("RUN_ID", "WIN_L15_WRAPPER", "SPIKE3_WRAPPER"):
        assert not any(re.search(rf"\b{name}\b", ln) for ln in local_lines), f"{name} 被 local 遮蔽"
        assert f'{name}="${{{name}:-' in text, f"{name} 缺少環境覆寫的預設值寫法"


def _driver_wrapper_default(var: str) -> Path:
    m = re.search(rf'^\s*{var}="\$\{{{var}:-\$REPO/([^}}]+)\}}"', _driver_text(), re.M)
    assert m, f"驅動缺少 {var} 的預設值"
    return REPO_ROOT / m.group(1)


def _driver_wrapper_options() -> dict[str, list[str]]:
    """驅動呼叫 Windows wrapper 的命令（續行已併為單行）：變數名 → 其傳入的長選項。"""
    out: dict[str, list[str]] = {}
    for line in _joined().splitlines():
        for var in ("WIN_L15_WRAPPER", "SPIKE3_WRAPPER"):
            if f'"${var}"' in line:
                out.setdefault(var, []).extend(re.findall(r"(--[a-z][a-z0-9-]*)", line))
    return out


def test_driver_windows_wrappers_exist_and_accept_every_option_it_passes():
    """F1 回歸：驅動傳給 wrapper 的每個長選項，都必須出現在該 wrapper 的參數解析（case 分支）中。"""
    invocations = _driver_wrapper_options()
    assert set(invocations) == {"WIN_L15_WRAPPER", "SPIKE3_WRAPPER"}, invocations
    for var, opts in invocations.items():
        wrapper = _driver_wrapper_default(var)
        assert wrapper.is_file(), f"驅動呼叫的 wrapper 不存在：{wrapper}"
        src = wrapper.read_text(encoding="utf-8")
        for opt in opts:
            assert re.search(rf"^\s*{re.escape(opt)}\)", src, re.M), f"{wrapper.name} 的參數解析不接受 {opt}"


def test_driver_windows_l15_passes_bundle_and_spike3_runs_once_for_all_cases():
    text = _joined()
    assert '--bundle "$WB_DIR/bundle"' in text, "win-func-test.sh 必須帶 --bundle（G4 匯出的 Windows dev bundle）"
    executable = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("--case" in ln for ln in executable), "win-spike3.sh 不接受 --case（單次執行四案）"
    assert text.count("spike3_win_wal") == 1
    assert '"$SPIKE3_WRAPPER" --evidence-dir "$EV"' in text, "spike3 證據根目錄為 $EV，wrapper 寫入 $EV/spike3/<case>.json"
    assert "win-l15-producer" not in text, "win-l15-producer.sh 不存在；L1.5 由 win-func-test.sh 交付"


def test_p4_evidence_is_quarantined_before_first_step():
    """F3 回歸：既有 target/p4-evidence 與 p4-bench 報告在第一步前隔離（搬不刪），且有 mtime 新鮮度檢查。"""
    text = _driver_text()
    move = _step_index(text, 'mv "$P4" "$EV/p4-prev/p4-evidence"')
    first_step = _step_index(text, "step ci_workflow_test")
    assert move < first_step, "既有 p4-evidence 須於第一個步驟前隔離"
    assert 'mv "$f" "$EV/p4-prev/"' in text, "p4-bench-*.json 亦須隔離"
    assert "RUN_START_EPOCH=" in text and "fresh_or_fail()" in text
    assert 'rm -rf "$P4"' not in text and 'rm -rf "$EV' not in text, "驅動不得刪除既有證據目錄（只搬移）"


def test_edr_gate_removed_spike3_takes_no_edr_arguments():
    """OD-19（oracle 變更，見 commit 的兩位審查者註記）：driver 不再有 SW_EDR_* 或 --edr-* 自填，亦無 EDR 未知警示。"""
    text = _driver_text()
    assert "SW_EDR_" not in text
    assert "--edr-" not in text
    assert "WARN EDR" not in text
    assert "--evidence-dir \"$EV\"" in text


def test_both_bench_steps_run_with_isolated_daemon_and_stub():
    text = _driver_text()
    assert "bench_cli_roundtrip.py" in text
    assert "--platform linux" in text
    assert "--cli-only-stub" in text
    assert "bb_stubd.py" in text, "stub 端點須以 bb_stubd 提供"


def test_report_paths_come_from_tool_run_id_not_ls_t():
    executable = [ln for ln in _driver_text().splitlines() if not ln.lstrip().startswith("#")]
    assert not any(re.search(r"\bls -t\b", ln) for ln in executable), "可執行行不得以 ls -t 猜測報告"
    assert "printed_path" in _driver_text()


def test_driver_checks_clean_tree_at_start_and_records_head():
    text = _driver_text()
    assert "git status --porcelain" in text
    assert 'git rev-parse HEAD' in text


def _gate_with_owner_deferred_case(case_id: str, status: str, windows_verdict: str, message: str) -> dict:
    return {
        "overall": "IMPLEMENTED",
        "errors": [],
        "cases": [{
            "id": case_id, "layer": "L1.5", "status": status,
            "evidence": [
                {"platform": "linux", "verdict": "PASS", "gate_eligible": True},
                {"platform": "windows", "verdict": windows_verdict, "gate_eligible": False,
                 "message": message, "harness_diagnostics": []},
            ],
        }],
    }


def _windows_l15_report(rows: list[dict], *, report_subdir: str = "report") -> dict:
    results = [
        {"case_id": row["case_id"], "mode": "bridge", "run_index": row.get("run_index", 0),
         "verdict": row["verdict"], "message": row["message"], "harness_diagnostics": [],
         "evidence_dir": f"C:/Users/test/AppData/Local/Temp/sw-rust-test/run-1/{report_subdir}/"
                         f"{row['case_id']}/bridge/{row.get('run_index', 0)}"}
        for row in rows
    ]
    verdicts = [row["verdict"] for row in results]
    return {
        "schema_version": "func-test-report.v1", "complete": True,
        "independent_cases": len({row["case_id"] for row in results}),
        "modes": {"bridge": {
            "runs": len(results), "pass": verdicts.count("PASS"), "fail": verdicts.count("FAIL"),
            "insufficient": verdicts.count("INSUFFICIENT_EVIDENCE"), "skip": verdicts.count("SKIP"),
        }},
        "results": results,
    }


def _passing_windows_scenarios_report() -> dict:
    return _windows_l15_report([
        {"case_id": "l15-reboot-min-guard", "verdict": "PASS", "message": "OK"},
    ])


def _windows_l15_meta(scenarios: dict, basic: dict) -> dict:
    def runner_exit(report: dict) -> int:
        return int(any(row.get("verdict") != "PASS" for row in report["results"]))

    def argv(*, include_scenarios: bool, report_dir: str) -> list[str]:
        args = ["-3.12", "func-test/runner.py", "--mode", "bridge"]
        if include_scenarios:
            args.append("--scenarios")
        return args + ["--daemon-com", "COM20", "--bridge-com", "COM21", "--transport-mode",
                       "virtual_com0com", "--report-dir", report_dir]

    scenario_argv = argv(
        include_scenarios=True,
        report_dir="C:/Users/test/AppData/Local/Temp/sw-rust-test/run-1/report",
    )
    basic_argv = argv(
        include_scenarios=False,
        report_dir="C:/Users/test/AppData/Local/Temp/sw-rust-test/run-1/report-basic",
    )
    return {
        "schema_version": "win-func-test-meta.v2", "run_id": "run-1", "git_head": "a" * 40,
        "git_tree": "b" * 40, "git_clean": True, "com_pair": {"daemon": "COM20", "bridge": "COM21"},
        "transport_mode": "virtual_com0com", "runner_timeout_s": 5400,
        "cli_exe_sha256": "c" * 64, "selftest_report_sha256": "d" * 64, "tool_tree_sha256": "e" * 64,
        "runner_argv": scenario_argv, "runner_exit": runner_exit(scenarios),
        "runs": {
            "scenarios": {"report": "func-test-report.json", "runner_argv": scenario_argv,
                          "runner_exit": runner_exit(scenarios)},
            "basic": {"report": "basic/func-test-report.json", "runner_argv": basic_argv,
                      "runner_exit": runner_exit(basic)},
        },
    }


def _windows_l15_evidence(scenarios: dict, basic: dict) -> dict:
    return {"scenarios": scenarios, "basic": basic, "metadata": _windows_l15_meta(scenarios, basic)}


@pytest.mark.parametrize("case_id,windows_verdict,message", [
    ("l15-transport-disconnect", "INSUFFICIENT_EVIDENCE",
     "step 'transport_disconnect' 失敗: HARNESS_CAPABILITY_UNAVAILABLE"),
    ("ev-01-spawn-handler", "FAIL", r"步驟 7: 等待檔案逾時 8s: C:\\temp\\ev-fired"),
    ("sm-04-device-unplug", "SKIP", "cap_missing:by_id_link"),
])
def test_exit_driver_accepts_only_gate_approved_windows_owner_deferred_l15_rc(case_id, windows_verdict, message):
    gate = _gate_with_owner_deferred_case(case_id, "BLOCKED-OWNER", windows_verdict, message)
    if case_id == "l15-transport-disconnect":
        scenarios = _windows_l15_report([{"case_id": case_id, "verdict": windows_verdict, "message": message}])
        basic_rows = [{"case_id": "brcm-01-login-flow", "verdict": "PASS", "message": "OK"}]
    else:
        scenarios = _passing_windows_scenarios_report()
        basic_rows = [
            {"case_id": "brcm-01-login-flow", "verdict": "PASS", "message": "OK"},
            {"case_id": case_id, "verdict": windows_verdict, "message": message},
        ]
    basic = _windows_l15_report(basic_rows, report_subdir="report-basic")
    unresolved, suppressed = l15_exit_driver.unresolved_steps(
        "win_l15_func_test 1\nstage1a_gate 0\n", gate, _windows_l15_evidence(scenarios, basic),
    )
    assert unresolved == []
    assert suppressed == [{"step": "win_l15_func_test", "rc": 1}]


def test_exit_driver_accepts_complete_declared_five_run_case_with_owner_defer():
    deferred_message = r"步驟 7: 等待檔案逾時 8s: C:\\temp\\ev-fired"
    gate = _gate_with_owner_deferred_case("ev-01-spawn-handler", "BLOCKED-OWNER", "FAIL", deferred_message)
    scenarios = _passing_windows_scenarios_report()
    basic = _windows_l15_report([
        {"case_id": "brcm-01-login-flow", "verdict": "PASS", "message": "OK"},
        {"case_id": "ev-01-spawn-handler", "verdict": "FAIL", "message": deferred_message},
        *[
            {"case_id": "rc-01-interactive-mode-switch", "run_index": index,
             "verdict": "PASS", "message": "OK"}
            for index in range(5)
        ],
    ], report_subdir="report-basic")

    unresolved, suppressed = l15_exit_driver.unresolved_steps(
        "win_l15_func_test 1\nstage1a_gate 0\n", gate, _windows_l15_evidence(scenarios, basic),
    )

    assert unresolved == []
    assert suppressed == [{"step": "win_l15_func_test", "rc": 1}]


@pytest.mark.parametrize("repeat_rows", [
    [
        {"case_id": "rc-01-interactive-mode-switch", "run_index": index,
         "verdict": "PASS", "message": "OK"}
        for index in (0, 1, 2, 4)
    ],
    [
        {"case_id": "rc-01-interactive-mode-switch", "run_index": index,
         "verdict": "PASS", "message": "OK"}
        for index in (0, 1, 2, 3, 3, 4)
    ],
    [
        {"case_id": "rc-01-interactive-mode-switch", "run_index": index,
         "verdict": "FAIL" if index == 3 else "PASS",
         "message": "unauthorized failure" if index == 3 else "OK"}
        for index in range(5)
    ],
], ids=["missing-run", "duplicate-run-index", "unauthorized-repeat-failure"])
def test_exit_driver_rejects_incomplete_or_failing_declared_repeat(repeat_rows):
    deferred_message = r"步驟 7: 等待檔案逾時 8s: C:\\temp\\ev-fired"
    gate = _gate_with_owner_deferred_case("ev-01-spawn-handler", "BLOCKED-OWNER", "FAIL", deferred_message)
    scenarios = _passing_windows_scenarios_report()
    basic = _windows_l15_report([
        {"case_id": "brcm-01-login-flow", "verdict": "PASS", "message": "OK"},
        {"case_id": "ev-01-spawn-handler", "verdict": "FAIL", "message": deferred_message},
        *repeat_rows,
    ], report_subdir="report-basic")

    unresolved, suppressed = l15_exit_driver.unresolved_steps(
        "win_l15_func_test 1\nstage1a_gate 0\n", gate, _windows_l15_evidence(scenarios, basic),
    )

    assert unresolved == [{"step": "win_l15_func_test", "rc": 1}]
    assert suppressed == []


@pytest.mark.parametrize("rc_text,gate", [
    ("win_l15_func_test 1\nstage1a_gate 1\n", _gate_with_owner_deferred_case(
        "l15-transport-disconnect", "BLOCKED-OWNER", "INSUFFICIENT_EVIDENCE",
        "step 'transport_disconnect' 失敗: HARNESS_CAPABILITY_UNAVAILABLE")),
    ("win_l15_func_test 2\nstage1a_gate 0\n", _gate_with_owner_deferred_case(
        "l15-transport-disconnect", "BLOCKED-OWNER", "INSUFFICIENT_EVIDENCE",
        "step 'transport_disconnect' 失敗: HARNESS_CAPABILITY_UNAVAILABLE")),
    ("win_l15_func_test 1\nstage1a_gate 0\npolicy_check 1\n", _gate_with_owner_deferred_case(
        "l15-transport-disconnect", "BLOCKED-OWNER", "INSUFFICIENT_EVIDENCE",
        "step 'transport_disconnect' 失敗: HARNESS_CAPABILITY_UNAVAILABLE")),
    ("win_l15_func_test 1\nstage1a_gate 0\n", _gate_with_owner_deferred_case(
        "l15-transport-disconnect", "FAIL", "INSUFFICIENT_EVIDENCE",
        "step 'transport_disconnect' 失敗: HARNESS_CAPABILITY_UNAVAILABLE")),
    ("win_l15_func_test 1\nstage1a_gate 0\n", {
        "overall": "FAIL", "errors": ["有其他 gate 失敗"], "cases": [
            {"id": "l15-transport-disconnect", "layer": "L1.5", "status": "BLOCKED-OWNER"},
        ],
    }),
])
def test_exit_driver_does_not_hide_non_deferred_or_gate_failures(rc_text, gate):
    unresolved, suppressed = l15_exit_driver.unresolved_steps(rc_text, gate)
    assert unresolved
    assert suppressed == []


def test_exit_driver_requires_an_explicit_od30_to_od32_blocked_owner_row():
    unresolved, suppressed = l15_exit_driver.unresolved_steps(
        "win_l15_func_test 1\nstage1a_gate 0\n",
        {"overall": "IMPLEMENTED", "errors": [], "cases": []},
    )
    assert unresolved == [{"step": "win_l15_func_test", "rc": 1}]
    assert suppressed == []


def test_exit_driver_does_not_suppress_owner_blocker_with_another_l15_failure():
    gate = _gate_with_owner_deferred_case(
        "ev-01-spawn-handler", "BLOCKED-OWNER", "FAIL",
        r"步驟 7: 等待檔案逾時 8s: C:\\temp\\ev-fired",
    )
    gate["cases"].append({
        "id": "co-01-line-buffering", "layer": "L1.5", "status": "FAIL",
        "evidence": [{"platform": "windows", "verdict": "FAIL", "gate_eligible": True,
                      "message": "TX payload mismatch"}],
    })

    unresolved, suppressed = l15_exit_driver.unresolved_steps(
        "win_l15_func_test 1\nstage1a_gate 0\n", gate,
    )

    assert unresolved == [{"step": "win_l15_func_test", "rc": 1}]
    assert suppressed == []


def test_exit_driver_checks_raw_reports_for_failures_missing_from_gate():
    deferred_message = r"步驟 7: 等待檔案逾時 8s: C:\\temp\\ev-fired"
    gate = _gate_with_owner_deferred_case("ev-01-spawn-handler", "BLOCKED-OWNER", "FAIL", deferred_message)
    scenarios = _passing_windows_scenarios_report()
    basic = _windows_l15_report([
        {"case_id": "ev-01-spawn-handler", "verdict": "FAIL", "message": deferred_message},
        {"case_id": "co-01-line-buffering", "verdict": "FAIL", "message": "TX payload mismatch"},
        {"case_id": "brcm-01-login-flow", "verdict": "PASS", "message": "OK"},
    ], report_subdir="report-basic")
    gate["_windows_l15_evidence"] = _windows_l15_evidence(scenarios, basic)

    unresolved, suppressed = l15_exit_driver.unresolved_steps(
        "win_l15_func_test 1\nstage1a_gate 0\n", gate,
    )

    assert unresolved == [{"step": "win_l15_func_test", "rc": 1}]
    assert suppressed == []


def test_exit_driver_cli_reads_both_raw_reports_and_execution_metadata(capsys, tmp_path):
    deferred_message = r"步驟 7: 等待檔案逾時 8s: C:\\temp\\ev-fired"
    gate = _gate_with_owner_deferred_case("ev-01-spawn-handler", "BLOCKED-OWNER", "FAIL", deferred_message)
    scenarios = _passing_windows_scenarios_report()
    basic = _windows_l15_report([
        {"case_id": "ev-01-spawn-handler", "verdict": "FAIL", "message": deferred_message},
        {"case_id": "co-01-line-buffering", "verdict": "FAIL", "message": "TX payload mismatch"},
        {"case_id": "brcm-01-login-flow", "verdict": "PASS", "message": "OK"},
    ], report_subdir="report-basic")
    evidence = _windows_l15_evidence(scenarios, basic)
    paths = [tmp_path / name for name in ("rc.txt", "gate.json", "scenarios.json", "basic.json", "meta.json")]
    paths[0].write_text("win_l15_func_test 1\nstage1a_gate 0\n", encoding="utf-8")
    for path, document in zip(paths[1:], (gate, scenarios, basic, evidence["metadata"])):
        path.write_text(json.dumps(document), encoding="utf-8")

    assert l15_exit_driver.main([str(path) for path in paths]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["unresolved"] == [{"step": "win_l15_func_test", "rc": 1}]
    assert summary["suppressed"] == []


def test_exit_driver_rejects_raw_report_from_another_windows_run():
    deferred_message = r"步驟 7: 等待檔案逾時 8s: C:\\temp\\ev-fired"
    gate = _gate_with_owner_deferred_case("ev-01-spawn-handler", "BLOCKED-OWNER", "FAIL", deferred_message)
    scenarios = _passing_windows_scenarios_report()
    basic = _windows_l15_report([
        {"case_id": "ev-01-spawn-handler", "verdict": "FAIL", "message": deferred_message},
        {"case_id": "brcm-01-login-flow", "verdict": "PASS", "message": "OK"},
    ], report_subdir="report-basic")
    basic["results"][0]["evidence_dir"] = (
        "C:/Users/test/AppData/Local/Temp/sw-rust-test/other-run/report-basic/"
        "ev-01-spawn-handler/bridge/0"
    )

    unresolved, suppressed = l15_exit_driver.unresolved_steps(
        "win_l15_func_test 1\nstage1a_gate 0\n", gate, _windows_l15_evidence(scenarios, basic),
    )

    assert unresolved == [{"step": "win_l15_func_test", "rc": 1}]
    assert suppressed == []


def test_driver_filters_windows_l15_rc_only_through_owner_deferred_gate_summary():
    text = _joined()
    assert ('scripts/rust/l15_exit_driver.py "$EV/rc.txt" "$EV/stage1a-gate.json" '
            '"$EV/func-test-windows/func-test-report.json" '
            '"$EV/func-test-windows/basic/func-test-report.json" '
            '"$EV/func-test-windows/win-func-test-meta.json"') in text
    assert 'awk \'$2 != 0\' "$EV/rc.txt"' not in text


# ── no_onefile_extract：報告新鮮度與 verdict（雲端審查 bf8583b#2）──────────────

def _onefile_verdict_source() -> str:
    m = re.search(r"^onefile_verdict\(\) \{\n  python3 - [^\n]*<<'PY'\n(.*?)\nPY\n\}", _driver_text(), re.S | re.M)
    assert m, "找不到 onefile_verdict 的內嵌 python 區段"
    return m.group(1)


def _run_onefile_verdict(monkeypatch, path: Path) -> int:
    monkeypatch.setattr(sys, "argv", ["onefile_verdict", str(path)])
    rc = 0
    try:
        exec(compile(_onefile_verdict_source(), "<onefile_verdict>", "exec"), {"__name__": "__main__"})
    except SystemExit as exc:
        rc = exc.code if isinstance(exc.code, int) else 1
    return rc


@pytest.mark.parametrize("report,expected", [
    ({"verdict": "PASS", "tmp_isolation": "mount-ns-private-tmp"}, 0),
    ({"verdict": "FAIL", "tmp_isolation": "mount-ns-private-tmp"}, 1),
    ({"verdict": "INSUFFICIENT", "tmp_isolation": None}, 1),
    ({"verdict": "PASS", "tmp_isolation": None}, 1),
    ({"verdict": "PASS"}, 1),
])
def test_onefile_verdict_requires_pass_and_private_tmp(monkeypatch, tmp_path, report, expected):
    path = tmp_path / "no-onefile-extract.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert _run_onefile_verdict(monkeypatch, path) == expected


def test_onefile_verdict_unreadable_report_is_failure(monkeypatch, tmp_path):
    path = tmp_path / "no-onefile-extract.json"
    path.write_text("{not json", encoding="utf-8")
    assert _run_onefile_verdict(monkeypatch, path) != 0
    assert _run_onefile_verdict(monkeypatch, tmp_path / "missing.json") != 0


def test_no_onefile_extract_step_checks_freshness_and_verdict():
    text = _joined()
    step = _step_index(text, "step no_onefile_extract ")
    fresh = _step_index(text, 'fresh_or_fail no_onefile_extract "$EV/no-onefile-extract.json"')
    verdict = _step_index(text, 'echo "verdict_no_onefile_extract 1" >> "$EV/rc.txt"')
    assert step < fresh < verdict
    assert 'onefile_verdict "$EV/no-onefile-extract.json"' in text
