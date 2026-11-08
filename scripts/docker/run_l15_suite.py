"""在隔離的 Docker 工作目錄執行一項 L1.5 驗證並寫入來源標記報告。"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from typing import Any

from scripts.docker import runtime

ROOT = pathlib.Path("/opt/serialwrap")
OUT = pathlib.Path(os.environ["SW_OUT"])
SUITE = os.environ["SW_SUITE"]
CASE_ID = os.environ.get("SW_CASE", "").strip()
# serialwrap.Dockerfile 以容器系統 Python 安裝 repo（無 venv），console script 位於 /usr/local/bin
CLI = pathlib.Path(os.environ.get("SERIALWRAP_CLI_EXE", "/usr/local/bin/serialwrap"))
DAEMON = pathlib.Path(os.environ.get("SERIALWRAP_DAEMON_EXE", "/usr/local/bin/serialwrapd"))
DUT_IMAGE = os.environ["SW_DUT_IMAGE"]


@contextmanager
def _fake_uart_runtime(env: dict[str, str]):
    """啟動兩個假 UART session，結束時收掉本次 daemon、bridge 與 DUT。"""
    run_id = os.environ.get("SW_DOCKER_RUN_ID", "od15-l15-suite")
    bridge_procs: list[subprocess.Popen] = []
    try:
        runtime.write_profiles(env)
        bridge_procs = runtime.start_bridges(
            env, dut_image=DUT_IMAGE, run_id=run_id, evidence_root=OUT / "bridge",
        )
        runtime.start_daemon_and_wait(env, timeout_s=120.0)
        yield
    finally:
        runtime.stop_runtime(env, bridge_procs, run_id)


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> int:
    print("+ " + " ".join(argv), flush=True)
    result = subprocess.run(argv, cwd=ROOT, env=env, check=False)
    return int(result.returncode)


def _isolation_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("SERIALWRAP_")}
    home = pathlib.Path("/tmp/sw-home")
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "SERIALWRAP_CLI_EXE": str(CLI),
        "SERIALWRAP_DAEMON_EXE": str(DAEMON),
        "SW_UART_KIND": "pty-fake",
        "PYTHONPATH": f"{ROOT}:{ROOT / 'tools/uart_stdio_bridge/src'}:{ROOT / 'regression'}",
    })
    names = {
        "SERIALWRAP_STATE_DIR": "state",
        "SERIALWRAP_RUN_DIR": "run",
        "SERIALWRAP_WAL_DIR": "wal",
        "SERIALWRAP_CONFIG_DIR": "config",
        "SERIALWRAP_LOG_DIR": "blog",
        "SERIALWRAP_EVENTS_DIR": "events.d",
        "SERIALWRAP_EVENTS_RUNTIME_DIR": "events-rt",
        "SERIALWRAP_BY_ID_DIR": "by-id",
        "SERIALWRAP_BY_PATH_DIR": "by-path",
    }
    for key, subdir in names.items():
        path = pathlib.Path("/tmp/sw-l15-run/isolation") / subdir
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        pathlib.Path(env[key]).mkdir(parents=True, exist_ok=True)
    return env


def _write_realhw_config(env: dict[str, str]) -> pathlib.Path:
    path = pathlib.Path("/tmp/sw-l15-run/realhw-config.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    boards = [
        {"com": f"COM{index}", "alias": f"docker-fake-{index}", "serial": f"SW-FAKE-{index}", "platform": "prpl"}
        for index in range(2)
    ]
    path.write_text(json.dumps({
        "boards": boards,
        "usbipd_exe": "",
        "win_serialwrap_exe": "",
        "serialwrap_exe": str(CLI),
        "uart_kind": "pty-fake",
        "bench_lock_path": "/tmp/sw-l15-run/isolation/state/bench.lock",
        "human_frontend": "tmux",
        "cross_os_probe_argv": [],
        "tmux_prefix": "swl15",
        "timeouts": {"ready_wait_s": 120, "reboot_wait_s": 120, "human_active_window_s": 20},
        "longrun": {"snapshot_interval_s": 10, "agent_workers": 2},
    }, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def _pytest_counts(path: pathlib.Path) -> dict[str, int] | None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    cases = root.findall(".//testcase")
    skipped = failures = errors = 0
    for case in cases:
        skipped += int(case.find("skipped") is not None)
        failures += int(case.find("failure") is not None)
        errors += int(case.find("error") is not None)
    return {"tests": len(cases), "passed": len(cases) - skipped - failures - errors,
            "skipped": skipped, "failures": failures, "errors": errors}


def _func_test(env: dict[str, str]) -> tuple[int, list[dict[str, Any]]]:
    selftest_dir = OUT / "func-test/selftest"
    selftest_dir.mkdir(parents=True, exist_ok=True)
    selftest_rc = _run(["uart-stdio-bridge", "selftest", "--endpoint", "posix-pty", "--out",
                        str(selftest_dir)], env=env)
    selftest_report = selftest_dir / "selftest-report.json"
    if selftest_rc != 0:
        return selftest_rc, [{"case_id": CASE_ID or "bridge-selftest", "verdict": "INSUFFICIENT_EVIDENCE",
                              "reason": "bridge_selftest_not_qualified"}]
    base = [sys.executable, "func-test/runner.py", "--mode", "bridge", "--dut-mode", "docker",
            "--dut-image", DUT_IMAGE, "--selftest-report", str(selftest_report)]
    runs: list[tuple[str, bool]]
    if CASE_ID:
        is_scenario = (ROOT / "func-test/scenarios" / f"{CASE_ID}.yaml").is_file()
        runs = [("scenario" if is_scenario else "case", is_scenario)]
    else:
        runs = [("case", False), ("scenario", True)]
    rows: list[dict[str, Any]] = []
    exit_code = 0
    for name, scenarios in runs:
        report_dir = OUT / "func-test" / name
        report_dir.mkdir(parents=True, exist_ok=True)
        argv = list(base)
        if scenarios:
            argv.append("--scenarios")
        if CASE_ID:
            argv.extend(["--case", CASE_ID])
        argv.extend(["--report-dir", str(report_dir)])
        rc = _run(argv, env=env)
        exit_code = exit_code or rc
        report_path = report_dir / "func-test-report.json"
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            source_rows = data.get("results")
        except (OSError, json.JSONDecodeError):
            source_rows = None
        if not isinstance(source_rows, list) or not source_rows:
            rows.append({"case_id": CASE_ID or name, "verdict": "INSUFFICIENT_EVIDENCE",
                         "reason": "func_test_report_missing_or_empty"})
            continue
        for source in source_rows:
            case_id = source.get("case_id") or source.get("case") or CASE_ID or name
            verdict = source.get("verdict")
            if verdict == "PASS" and (source.get("gate_eligible") is not True
                                       or source.get("comparison_verdict") != "PASS"):
                verdict = "INSUFFICIENT_EVIDENCE"
            if rc != 0 and verdict == "PASS":
                verdict = "FAIL"
            rows.append({"case_id": case_id, "verdict": verdict or "INSUFFICIENT_EVIDENCE"})
    return exit_code, rows


def _pytest_suite(env: dict[str, str]) -> tuple[int, list[dict[str, Any]]]:
    junit = OUT / "pytest" / "junit.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    rc = _run([sys.executable, "-m", "pytest", "-q", "-m", "posix_pty", "tests/",
               f"--junitxml={junit}"], env=env)
    counts = _pytest_counts(junit)
    if counts is None or counts["tests"] == 0 or counts["passed"] == 0:
        verdict = "INSUFFICIENT_EVIDENCE"
    elif counts["skipped"] > 0:
        verdict = "INSUFFICIENT_EVIDENCE"
    elif counts["failures"] or counts["errors"] or rc != 0:
        verdict = "FAIL"
    else:
        verdict = "PASS"
    return rc, [{"case_id": "pytest-posix-pty", "verdict": verdict, "counts": counts}]


def _realhw_suite(env: dict[str, str]) -> tuple[int, list[dict[str, Any]]]:
    config = _write_realhw_config(env)
    report_dir = OUT / "realhw"
    report_dir.mkdir(parents=True, exist_ok=True)
    with _fake_uart_runtime(env):
        rc = _run([sys.executable, "-m", "realhw", "--tier", "p0,p1", "--config", str(config),
                   "--report-dir", str(report_dir)], env=env)
    try:
        report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
        source_rows = report.get("results")
    except (OSError, json.JSONDecodeError):
        source_rows = None
    if not isinstance(source_rows, list) or not source_rows:
        return rc, [{"case_id": "realhw-p0p1", "verdict": "INSUFFICIENT_EVIDENCE",
                     "reason": "realhw_report_missing_or_empty"}]
    rows = [{"case_id": row.get("id", "realhw-unknown"),
             "verdict": row.get("verdict") if row.get("verdict") in {"PASS", "FAIL"} else "INSUFFICIENT_EVIDENCE"}
            for row in source_rows]
    if rc != 0:
        rows = [{**row, "verdict": "FAIL" if row["verdict"] == "PASS" else row["verdict"]} for row in rows]
    return rc, rows


def _regression_cases() -> list[str]:
    from serialwrap_regression import core

    return [case.id for case in core.load_registry() if not case.destructive]


def _regression_suite(env: dict[str, str]) -> tuple[int, list[dict[str, Any]]]:
    if shutil.which("testpilot") is None:
        return 3, [{"case_id": "regression-nondestructive", "verdict": "INSUFFICIENT_EVIDENCE",
                    "reason": "dependency_blocked:testpilot_unavailable"}]
    plugin_dir = ROOT / "regression/serialwrap_regression"
    testbed = plugin_dir / "testbed.yaml"
    if not testbed.is_file():
        return 3, [{"case_id": "regression-nondestructive", "verdict": "INSUFFICIENT_EVIDENCE",
                    "reason": "dependency_blocked:isolated_testbed_missing"}]
    config = json.loads(testbed.read_text(encoding="utf-8"))
    if pathlib.Path(str(config.get("serialwrap_exe"))).resolve() != CLI.resolve():
        return 3, [{"case_id": "regression-nondestructive", "verdict": "INSUFFICIENT_EVIDENCE",
                    "reason": "dependency_blocked:testbed_cli_mismatch"}]
    report_root = pathlib.Path(env["HOME"]) / "b-log" / "regression-reports"
    report_root.mkdir(parents=True, exist_ok=True)
    selected = _regression_cases()
    rows: list[dict[str, Any]] = []
    exit_code = 0
    with _fake_uart_runtime(env):
        for case_id in selected:
            before = {path.resolve() for path in report_root.glob("tp-*/report.json")}
            rc = _run(["testpilot", "--root", str(ROOT), "run", "serialwrap_regression", "--case", case_id], env=env)
            exit_code = exit_code or rc
            after = [path for path in report_root.glob("tp-*/report.json") if path.resolve() not in before]
            if len(after) != 1:
                rows.append({"case_id": case_id, "verdict": "INSUFFICIENT_EVIDENCE",
                             "reason": "testpilot_report_missing_or_ambiguous"})
                continue
            try:
                report = json.loads(after[0].read_text(encoding="utf-8"))
                result = next((row for row in report.get("results", []) if row.get("id") == case_id), None)
            except (OSError, json.JSONDecodeError):
                result = None
            verdict = result.get("verdict") if isinstance(result, dict) else None
            if verdict not in {"PASS", "FAIL"}:
                verdict = "INSUFFICIENT_EVIDENCE"
            if rc != 0 and verdict == "PASS":
                verdict = "FAIL"
            rows.append({"case_id": case_id, "verdict": verdict})
            time.sleep(1.05)
    return exit_code, rows


def _image_id(image: str) -> str:
    return subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True).strip()


def _identity() -> tuple[str, str]:
    bundle_value = os.environ.get("SW_BUNDLE")
    if bundle_value:
        from realhw.deployed_content import tree_digests
        from sw_core.release_manifest import file_sha256, load_release_manifest

        bundle = pathlib.Path(bundle_value)
        manifest_path = bundle / "release-manifest.json"
        manifest = load_release_manifest(str(manifest_path))
        if (manifest["components"]["cli"].get("implementation") != "rust"
                or manifest["components"]["daemon"].get("implementation") != "rust"):
            raise RuntimeError("bundle CLI／daemon 未標記為 Rust 實作")
        cli = bundle / "bin/serialwrap"
        daemon = bundle / "bin/serialwrapd"
        if not cli.is_file() or not os.access(cli, os.X_OK) or not daemon.is_file() or not os.access(daemon, os.X_OK):
            raise RuntimeError("bundle CLI／daemon 不存在或不可執行")
        for rel, digest in ((manifest["components"]["cli"]["path"], manifest["components"]["cli"]["sha256"]),
                            (manifest["components"]["daemon"]["launcher"]["path"],
                             manifest["components"]["daemon"]["launcher"]["sha256"])):
            candidate = (bundle / rel).resolve()
            if bundle.resolve() not in candidate.parents or not candidate.is_file() or file_sha256(str(candidate)) != digest:
                raise RuntimeError(f"bundle manifest artifact 摘要不符：{rel}")
        return "rust", tree_digests(bundle)[0]
    from sw_core.release_manifest import package_tree_sha256

    return "python", package_tree_sha256()


def main() -> int:
    global CLI, DAEMON
    OUT.mkdir(parents=True, exist_ok=True)
    env = _isolation_env()
    if env.get("SW_BUNDLE"):
        CLI = pathlib.Path(env["SW_BUNDLE"]) / "bin/serialwrap"
        DAEMON = pathlib.Path(env["SW_BUNDLE"]) / "bin/serialwrapd"
        env["SERIALWRAP_CLI_EXE"] = str(CLI)
        env["SERIALWRAP_DAEMON_EXE"] = str(DAEMON)
    suite_functions = {
        "func-test-bridge": _func_test,
        "pytest-physical-uart": _pytest_suite,
        "realhw-p0p1": _realhw_suite,
        "regression-nondestructive": _regression_suite,
    }
    suite_rc = 125
    try:
        suite_rc, results = suite_functions[SUITE](env)
        implementation, tree_digest = _identity()
        image_digests = {
            "serialwrap": _image_id(os.environ["SW_SERIALWRAP_IMAGE"]),
            "dut-sim": _image_id(DUT_IMAGE),
            "systemd": _image_id(os.environ["SW_SYSTEMD_IMAGE"]),
        }
        report: dict[str, Any] = {
            "schema_version": 1,
            "suite": SUITE,
            "exit_code": suite_rc,
            "implementation": implementation,
            "results": results,
        }
        selftest = OUT / "func-test/selftest/selftest-report.json"
        if selftest.is_file():
            import hashlib

            report["bridge_selftest"] = {
                "path": "func-test/selftest/selftest-report.json",
                "sha256": hashlib.sha256(selftest.read_bytes()).hexdigest(),
                "verdict": json.loads(selftest.read_text(encoding="utf-8")).get("verdict"),
            }
        else:
            report["bridge_selftest"] = {"path": "func-test/selftest/selftest-report.json", "verdict": "NOT_APPLICABLE"}
        from tests.acceptance.evidence_labels import label_report

        labeled = label_report(report, evidence_layer="L1.5-docker", producer="run-l15.sh",
                               topology="docker-2c", uart_kind="pty-fake",
                               image_digests=image_digests, tree_sha256=tree_digest)
        output = pathlib.Path(os.environ["SW_REPORT"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(labeled, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                          encoding="utf-8")
        good = suite_rc == 0 and bool(results) and all(row.get("verdict") == "PASS" for row in results)
        return 0 if good else (suite_rc if suite_rc not in (0, 5) else 1)
    except Exception as exc:
        print(f"L1.5 runner 証據不足：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
if __name__ == "__main__":
    raise SystemExit(main())
