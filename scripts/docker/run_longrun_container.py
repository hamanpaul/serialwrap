"""Docker 48 h 長跑本體：假 DUT bridge、隔離 daemon、realhw 與 observer。"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any

from scripts.docker import runtime

REPO = pathlib.Path("/opt/serialwrap")
ROOT = pathlib.Path("/tmp/sw-t17")
OUT = pathlib.Path("/evidence")


def _config(env: dict[str, str], report_dir: pathlib.Path) -> pathlib.Path:
    path = ROOT / "realhw-config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "boards": [
            {"com": f"COM{index}", "alias": f"docker-fake-{index}",
             "serial": f"SW-FAKE-{index}", "platform": "prpl"}
            for index in range(2)
        ],
        "usbipd_exe": "",
        "win_serialwrap_exe": "",
        "serialwrap_exe": env["SERIALWRAP_CLI_EXE"],
        "uart_kind": "pty-fake",
        "bench_lock_path": str(pathlib.Path(env["SERIALWRAP_STATE_DIR"]) / "bench.lock"),
        "human_frontend": "tmux",
        "cross_os_probe_argv": [],
        "tmux_prefix": "swlongrun",
        "timeouts": {"ready_wait_s": 120, "reboot_wait_s": 120, "human_active_window_s": 20},
        "longrun": {"snapshot_interval_s": 10, "agent_workers": 2},
        "report_dir": str(report_dir),
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def _image_ids() -> dict[str, str]:
    return {
        "serialwrap": subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", os.environ["SW_SERIALWRAP_IMAGE"]], text=True
        ).strip(),
        "dut-sim": subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", os.environ["SW_DUT_IMAGE"]], text=True
        ).strip(),
        "systemd": subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", os.environ["SW_SYSTEMD_IMAGE"]], text=True
        ).strip(),
    }


def _run_engine(env: dict[str, str], hours: int, config: pathlib.Path) -> tuple[int, float]:
    report_dir = OUT / "realhw"
    report_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-m", "realhw", "--tier", "longrun", "--duration", f"{hours}h",
         "--config", str(config), "--report-dir", str(report_dir)],
        cwd=REPO, env=env, stdin=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        rc = process.wait()
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        raise
    return int(rc), max(0.0, time.monotonic() - started)


def _engine_row(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return next((row for row in report.get("results", []) if row.get("id") == "lr-mixed"), None)


def main() -> int:
    hours = int(os.environ["SW_HOURS"])
    run_id = os.environ["SW_DOCKER_RUN_ID"]
    cli = pathlib.Path(os.environ.get("SW_BUNDLE", "/opt/serialwrap/.venv")).joinpath(
        "bin/serialwrap" if os.environ.get("SW_BUNDLE") else "bin/serialwrap"
    )
    daemon = pathlib.Path(os.environ.get("SW_BUNDLE", "/opt/serialwrap/.venv")).joinpath("bin/serialwrapd")
    if not os.environ.get("SW_BUNDLE"):
        cli = pathlib.Path("/opt/serialwrap/.venv/bin/serialwrap")
        daemon = pathlib.Path("/opt/serialwrap/.venv/bin/serialwrapd")
    env = runtime.isolated_env(ROOT / "isolation", cli=cli, daemon=daemon)
    bridge_procs: list[subprocess.Popen] = []
    observer: subprocess.Popen | None = None
    observer_log = None
    interrupted = False

    def on_signal(_signum, _frame):
        nonlocal interrupted
        interrupted = True
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    failure_reasons: list[str] = []
    engine_rc = 125
    engine_elapsed = 0.0
    observer_rc = 125
    metrics: dict[str, Any] = {"observed_s": 0.0, "max_gap_s": 0.0, "sample_count": 0}
    try:
        runtime.write_profiles(env)
        bridge_procs = runtime.start_bridges(
            env, dut_image=os.environ["SW_DUT_IMAGE"], run_id=run_id,
            evidence_root=OUT / "bridge",
        )
        runtime.start_daemon_and_wait(env, timeout_s=120)
        observer_path = OUT / "observer.ndjson"
        observer_log = (OUT / "observer.log").open("w", encoding="utf-8")
        observer = subprocess.Popen(
            [sys.executable, "-m", "realhw.acceptance.observer", "--serialwrap-exe", str(cli),
             "--boards", "COM0,COM1", "--interval-s", "10", "--duration", f"{hours}h",
             "--out", str(observer_path)],
            cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=observer_log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        config = _config(env, OUT / "realhw")
        engine_rc, engine_elapsed = _run_engine(env, hours, config)
        required_span_s = max(0.0, hours * 3600.0 - 20.0)
        if engine_elapsed < required_span_s:
            failure_reasons.append("realhw_duration_insufficient")
            observer.terminate()
        try:
            observer_rc = int(observer.wait(timeout=60))
        except subprocess.TimeoutExpired:
            observer.terminate()
            try:
                observer.wait(timeout=10)
            except subprocess.TimeoutExpired:
                observer.kill()
                observer.wait(timeout=5)
            failure_reasons.append("observer_did_not_finish")
        metrics, observer_failures, _header = runtime.read_observer(
            observer_path, hours=hours, interval_s=10.0,
        )
        failure_reasons.extend(observer_failures)
        if observer_rc != 0:
            failure_reasons.append(f"observer_exit:{observer_rc}")
        if engine_rc != 0:
            failure_reasons.append(f"realhw_exit:{engine_rc}")
        engine = _engine_row(OUT / "realhw/report.json")
        if engine is None:
            verdict = "INSUFFICIENT_EVIDENCE"
            failure_reasons.append("realhw_report_missing")
        elif engine.get("verdict") == "FAIL":
            verdict = "FAIL"
            failure_reasons.append(str(engine.get("reason") or "realhw_failed"))
        elif engine.get("verdict") != "PASS":
            verdict = "INSUFFICIENT_EVIDENCE"
            failure_reasons.append("realhw_result_not_pass")
        elif failure_reasons:
            verdict = "INSUFFICIENT_EVIDENCE" if any(
                "insufficient" in item or "missing" in item or "observer_" in item for item in failure_reasons
            ) else "FAIL"
        else:
            verdict = "PASS"
        results = [{"case_id": "lr-mixed", "verdict": verdict,
                    "reason": ";".join(failure_reasons) if failure_reasons else ""}]
        from sw_core.release_manifest import package_tree_sha256

        passed = runtime.write_l15_report(
            OUT / "l15-docker-report.json", suite="longrun", producer="run-longrun.sh",
            results=results, exit_code=0 if verdict == "PASS" else (engine_rc or 1),
            images=_image_ids(), tree_sha256=package_tree_sha256(),
            extra={"hours": hours, "engine_elapsed_s": engine_elapsed, "observer_exit_code": observer_rc,
                   "observation": metrics, "engine_result": engine},
        )
        return 0 if passed else 1
    except BaseException as exc:
        failure_reasons.append(f"runner_exception:{type(exc).__name__}")
        return_code = 1
        try:
            from sw_core.release_manifest import package_tree_sha256

            result = {"case_id": "lr-mixed", "verdict": "INSUFFICIENT_EVIDENCE", "reason": str(exc)}
            runtime.write_l15_report(
                OUT / "l15-docker-report.json", suite="longrun", producer="run-longrun.sh",
                results=[result], exit_code=return_code, images=_image_ids(),
                tree_sha256=package_tree_sha256(),
                extra={"hours": hours, "engine_elapsed_s": engine_elapsed, "observer_exit_code": observer_rc,
                       "observation": metrics, "validation_failures": failure_reasons},
            )
        except BaseException:
            pass
        if isinstance(exc, KeyboardInterrupt) or interrupted:
            raise
        print(f"longrun runner 證據不足：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if observer is not None and observer.poll() is None:
            observer.terminate()
            try:
                observer.wait(timeout=10)
            except subprocess.TimeoutExpired:
                observer.kill()
                observer.wait(timeout=5)
        if observer_log is not None:
            observer_log.close()
        runtime.stop_runtime(env, bridge_procs, run_id)


if __name__ == "__main__":
    raise SystemExit(main())
