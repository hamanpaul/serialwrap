"""systemd 容器內執行產品 reconcile、journald 與 hold drop-in 驗證。"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import time
from typing import Any

from scripts.docker import systemd_container

OUT = pathlib.Path("/evidence")
ROOT = pathlib.Path("/tmp/sw-t17")


def _stderr_probe(mode: str) -> tuple[bool, str]:
    token = "OD15_INVALID_DAEMON_ARGUMENT"
    if mode == "systemd-user":
        unit_dir = pathlib.Path("/home/swtest/.config/systemd/user")
    else:
        unit_dir = pathlib.Path("/etc/systemd/system")
    unit_path = unit_dir / "serialwrap-stderr-probe.service"
    unit_path.write_text(
        "[Unit]\nDescription=OD-15 serialwrapd stderr transport probe\n\n"
        "[Service]\nType=oneshot\nExecStart=/opt/serialwrap/.venv/bin/serialwrapd --od15-invalid-argument\n",
        encoding="utf-8",
    )
    if mode == "systemd-user":
        subprocess.run(["chown", "swtest:swtest", str(unit_path)], check=True)
    systemd_container._require_ok(systemd_container.systemctl(mode, "daemon-reload"), "載入 stderr probe unit")
    # 此 unit 預期以 argparse exit 2；驗收對象是 serialwrapd 的 stderr 是否進 journald。
    systemd_container.systemctl(mode, "start", "serialwrap-stderr-probe")
    journal_command = (
        ["runuser", "-u", "swtest", "--preserve-environment", "--", "env",
         "XDG_RUNTIME_DIR=/run/user/1000", "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
         "journalctl", "--user", "-u", "serialwrap-stderr-probe", "-o", "cat", "--no-pager"]
        if mode == "systemd-user" else
        ["journalctl", "-u", "serialwrap-stderr-probe", "-o", "cat", "--no-pager"]
    )
    journal = ""
    for _ in range(50):
        result = subprocess.run(journal_command, capture_output=True, text=True, check=False, timeout=15)
        if result.returncode == 0:
            journal = result.stdout
            if "unrecognized arguments" in journal or "error:" in journal:
                break
        time.sleep(0.2)
    path = OUT / "stderr-probe-journal.log"
    path.write_text(journal, encoding="utf-8")
    return ("unrecognized arguments" in journal or "error:" in journal), journal


def _main_pid(mode: str) -> int:
    result = systemd_container.systemctl(mode, "show", "serialwrap", "-p", "MainPID", "--value")
    systemd_container._require_ok(result, "查詢 serialwrap MainPID")
    return int(result.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("systemd-user", "systemd-system"), required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    mode = args.mode
    cli = pathlib.Path("/opt/serialwrap/.venv/bin/serialwrap")
    daemon = pathlib.Path("/opt/serialwrap/.venv/bin/serialwrapd")
    env = systemd_container.make_env(ROOT / f"systemd-{mode}", cli=cli, daemon=daemon)
    bridge_procs: list[subprocess.Popen] = []
    hold_path: pathlib.Path | None = None
    failures: list[str] = []
    results: list[dict[str, Any]] = []
    old_pid: int | None = None
    service_pid: int | None = None
    blocked_start_rc: int | None = None
    stderr_ok = False
    try:
        bridge_procs, old_pid, service_pid = systemd_container.serve(
            mode, env, args.run_id,
        )
        stderr_ok, _journal = _stderr_probe(mode)
        if not stderr_ok:
            failures.append("daemon_stderr_not_in_journald")

        hold_path = systemd_container.apply_hold(mode, name="hold.conf")
        show = systemd_container.systemctl(mode, "show", "serialwrap", "-p", "Restart", "-p", "RefuseManualStart")
        (OUT / "systemctl-show.txt").write_text(show.stdout, encoding="utf-8")
        stopped = systemd_container._service_action(mode, env, "stop")
        if stopped.returncode != 0 or _main_pid(mode) != 0:
            failures.append("product_service_stop_failed")

        blocked_start = systemd_container._service_action(mode, env, "start")
        blocked_start_rc = blocked_start.returncode
        if blocked_start.returncode == 0 or _main_pid(mode) != 0:
            failures.append("hold_did_not_refuse_product_service_start")

        systemd_container.release_hold(mode, hold_path)
        hold_path = None
        started = systemd_container._service_action(mode, env, "start")
        if started.returncode != 0:
            failures.append("product_service_start_failed_after_hold_release")
        else:
            systemd_container._wait_daemon_ready(env, timeout_s=120.0)
        resumed_pid = _main_pid(mode)
        if resumed_pid <= 0 or resumed_pid == service_pid:
            failures.append("service_restart_pid_not_replaced")

        verdict = "PASS" if not failures else "FAIL"
        results.append({"case_id": mode, "verdict": verdict, "reason": ";".join(failures)})
        from sw_core.release_manifest import package_tree_sha256

        passed = systemd_container.write_report(
            OUT / "l15-docker-report.json", suite=mode, mode=mode, results=results,
            extra={
                "old_daemon_pid": old_pid,
                "setup_daemon_pid": service_pid,
                "resumed_daemon_pid": resumed_pid,
                "hold_blocked_start_exit_code": blocked_start_rc,
                "product_exercised": ["serialwrap_setup_reconcile", "serialwrap_service_stop_start",
                                      "serialwrapd_stderr_to_journald"],
                "harness_exercised": ["Restart=no hold drop-in", "RefuseManualStart=yes hold drop-in"],
                "scope_limitations": ["SC-23 rollback hold 由 Rust-only P4 實作；此 Python 基線只驗 systemd drop-in 語意"],
                "stderr_probe_passed": stderr_ok,
            },
            tree_sha256=package_tree_sha256(),
        )
        if not passed:
            print("systemd 證據未通過：" + ", ".join(failures), file=__import__("sys").stderr)
            return 1
        return 0
    except BaseException as exc:
        failures.append(f"runner_exception:{type(exc).__name__}")
        try:
            from sw_core.release_manifest import package_tree_sha256

            systemd_container.write_report(
                OUT / "l15-docker-report.json", suite=mode, mode=mode,
                results=[{"case_id": mode, "verdict": "INSUFFICIENT_EVIDENCE", "reason": str(exc)}],
                extra={"validation_failures": failures, "product_exercised": [],
                       "harness_exercised": []}, tree_sha256=package_tree_sha256(),
            )
        except BaseException:
            pass
        print(f"systemd runner 證據不足：{type(exc).__name__}: {exc}", file=__import__("sys").stderr)
        return 1
    finally:
        if hold_path is not None:
            systemd_container.release_hold(mode, hold_path)
        systemd_container.cleanup(mode, env, bridge_procs, args.run_id)


if __name__ == "__main__":
    raise SystemExit(main())
