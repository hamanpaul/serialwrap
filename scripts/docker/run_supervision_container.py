"""systemd-user／system 容器內的長時監管 observer runner。"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any

from scripts.docker import runtime, systemd_container

REPO = pathlib.Path("/opt/serialwrap")
OUT = pathlib.Path("/evidence")
ROOT = pathlib.Path("/tmp/sw-t17")


def _verdict(failures: list[str]) -> str:
    if not failures:
        return "PASS"
    incomplete = ("observer_", "runner_interrupted", "runner_duration_insufficient")
    if any(reason.startswith(incomplete) for reason in failures):
        return "INSUFFICIENT_EVIDENCE"
    return "FAIL"


def _write_report(mode: str, hours: int, *, exit_code: int, results: list[dict[str, Any]],
                  metrics: dict[str, Any], failures: list[str], old_pid: int | None,
                  service_pid: int | None, runner_elapsed_s: float, header: dict[str, Any] | None) -> bool:
    from sw_core.release_manifest import package_tree_sha256

    return systemd_container.write_report(
        OUT / "l15-docker-report.json", suite="supervision", mode=mode, results=results,
        extra={
            "hours": hours,
            "observer_exit_code": exit_code,
            "observer_header": header,
            "sample_count": metrics.get("sample_count", 0),
            "observation": metrics,
            "runner_elapsed_s": runner_elapsed_s,
            "old_daemon_pid": old_pid,
            "supervised_daemon_pid": service_pid,
            "validation_failures": failures,
            "product_exercised": ["serialwrap_setup_reconcile", "systemd_daemon_lifecycle", "readonly_observer"],
            "harness_exercised": ["Restart=no hold drop-in", "RefuseManualStart=yes hold drop-in"],
        },
        tree_sha256=package_tree_sha256(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("systemd-user", "systemd-system"), required=True)
    parser.add_argument("--hours", type=int, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    mode = args.mode
    hours = args.hours
    cli = pathlib.Path("/opt/serialwrap/.venv/bin/serialwrap")
    daemon = pathlib.Path("/opt/serialwrap/.venv/bin/serialwrapd")
    env = systemd_container.make_env(ROOT / f"supervision-{mode}", cli=cli, daemon=daemon)
    bridge_procs: list[subprocess.Popen] = []
    observer: subprocess.Popen | None = None
    observer_log = None
    hold_path: pathlib.Path | None = None
    old_pid: int | None = None
    service_pid: int | None = None
    interrupted = False
    observer_rc = 125
    metrics: dict[str, Any] = {"observed_s": 0.0, "max_gap_s": 0.0, "sample_count": 0}
    failures: list[str] = []
    header = None
    started = time.monotonic()

    def on_signal(_signum, _frame):
        nonlocal interrupted
        interrupted = True
        if observer is not None and observer.poll() is None:
            observer.terminate()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        bridge_procs, old_pid, service_pid = systemd_container.serve(
            mode, env, args.run_id,
        )
        hold_path = systemd_container.apply_hold(mode, name="od15-supervision-hold.conf")
        attestation = systemd_container.attestation(mode)
        attest_path = OUT / "supervision-attest.json"
        attest_path.write_text(json.dumps(attestation, sort_keys=True) + "\n", encoding="utf-8")

        observer_path = OUT / "observer.ndjson"
        observer_log = (OUT / "observer.log").open("w", encoding="utf-8")
        observer = subprocess.Popen(
            [sys.executable, "-m", "realhw.acceptance.observer",
             "--serialwrap-exe", str(cli), "--boards", "COM0,COM1", "--interval-s", "10",
             "--duration", f"{hours}h", "--supervision-attest", str(attest_path),
             "--out", str(observer_path)],
            cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=observer_log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        observer_started = time.monotonic()
        while observer.poll() is None:
            if interrupted:
                failures.append("runner_interrupted")
                break
            time.sleep(0.5)
        if observer.poll() is None:
            observer.terminate()
        try:
            observer_rc = int(observer.wait(timeout=15))
        except subprocess.TimeoutExpired:
            observer.kill()
            observer_rc = int(observer.wait(timeout=5))
            failures.append("observer_did_not_exit")
        runner_elapsed = max(0.0, time.monotonic() - observer_started)
        if runner_elapsed < max(0.0, hours * 3600.0 - 20.0):
            failures.append("runner_duration_insufficient")
        metrics, observation_failures, header = runtime.read_observer(
            observer_path, hours=hours, interval_s=10.0,
        )
        failures.extend(observation_failures)
        if observer_rc != 0:
            failures.append(f"observer_exit:{observer_rc}")
        if header is None or header.get("supervision") != attestation:
            failures.append("observer_supervision_attestation_invalid")
        verdict = _verdict(failures)
        passed = _write_report(
            mode, hours, exit_code=observer_rc,
            results=[{"case_id": f"supervision-{mode}", "verdict": verdict,
                      "reason": ";".join(failures)}],
            metrics=metrics, failures=failures, old_pid=old_pid, service_pid=service_pid,
            runner_elapsed_s=runner_elapsed, header=header,
        )
        if not passed:
            print("supervision 證據未通過：" + ", ".join(failures[:10]), file=sys.stderr)
            return 1
        return 0
    except BaseException as exc:
        failures.append(f"runner_exception:{type(exc).__name__}")
        try:
            _write_report(
                mode, hours, exit_code=observer_rc,
                results=[{"case_id": f"supervision-{mode}", "verdict": "INSUFFICIENT_EVIDENCE",
                          "reason": f"{type(exc).__name__}: {exc}"}],
                metrics=metrics, failures=failures, old_pid=old_pid, service_pid=service_pid,
                runner_elapsed_s=max(0.0, time.monotonic() - started), header=header,
            )
        except BaseException:
            pass
        if isinstance(exc, KeyboardInterrupt):
            return 130
        print(f"supervision runner 證據不足：{type(exc).__name__}: {exc}", file=sys.stderr)
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
        if hold_path is not None:
            systemd_container.release_hold(mode, hold_path)
        systemd_container.cleanup(mode, env, bridge_procs, args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
