"""systemd 容器內執行產品 reconcile、journald 與 hold drop-in 驗證。"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import threading
import time
from typing import Any

from scripts.docker import systemd_container
from scripts.docker import runtime
from uart_stdio_bridge.bridge.control import ControlClient

OUT = pathlib.Path("/evidence")
ROOT = pathlib.Path("/tmp/sw-t17")


def _bundle_manifest_path(bundle: pathlib.Path, expected_version: str | None = None) -> pathlib.Path:
    """依 current-release.json 或 dev bundle 的 versions 目錄定位 manifest。"""
    current_path = bundle / "bin" / "current-release.json"
    version = expected_version
    if current_path.is_file():
        current = json.loads(current_path.read_text(encoding="utf-8"))
        current_version = current.get("bundle_version")
        if not isinstance(current_version, str) or not current_version:
            raise ValueError(f"current-release.json 缺少 bundle_version：{current_path}")
        if expected_version is not None and current_version != expected_version:
            raise ValueError(
                f"bundle 版本應為 {expected_version}，實際為 {current_version}：{current_path}"
            )
        version = current_version
    if version is not None:
        manifest = bundle / "versions" / version / "release-manifest.json"
        if manifest.is_file():
            return manifest
        if not current_path.is_file() and (bundle / "release-manifest.json").is_file():
            return bundle / "release-manifest.json"
        raise ValueError(f"bundle 缺少版本 manifest：{manifest}")
    legacy = bundle / "release-manifest.json"
    if legacy.is_file():
        return legacy
    manifests = sorted((bundle / "versions").glob("*/release-manifest.json"))
    if len(manifests) == 1:
        return manifests[0]
    raise ValueError(f"無法唯一定位 bundle manifest：{bundle}")


def _stderr_probe(mode: str) -> tuple[bool, str]:
    token = "OD15_INVALID_DAEMON_ARGUMENT"
    if mode == "systemd-user":
        unit_dir = systemd_container.USER_HOME / ".config" / "systemd" / "user"
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


def _holder_pids(device_path: pathlib.Path) -> list[int]:
    target = os.path.realpath(device_path)
    holders: list[int] = []
    for process in pathlib.Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        fd_dir = process / "fd"
        try:
            if any(os.path.realpath(os.readlink(fd)) == target for fd in fd_dir.iterdir()):
                holders.append(int(process.name))
        except OSError:
            continue
    return sorted(holders)


def _bridge_mark(control: ControlClient, label: str, pid: int) -> None:
    result = control.call("mark", {"label": label, "op_id": str(pid)})
    if not isinstance(result.get("wire_seq"), int):
        raise RuntimeError(f"P2 bridge mark 缺少 wire_seq：{result}")


def _completed_marks(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        row for row in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
        if row.get("action") == "mark" and row.get("phase") == "completed"
    ]


def _read_daemon_status(env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    result = subprocess.run([env["SERIALWRAP_CLI_EXE"], "daemon", "status"], cwd=REPO,
                            env=env, capture_output=True, text=True, check=False, timeout=20)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"daemon status 非 JSON：{result.stdout[-500:]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"daemon status 不是 JSON 物件：{payload!r}")
    return result, payload


def _import_manager_environment(env: dict[str, str]) -> None:
    names = [
        "HOME", "USER", "LOGNAME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "SERIALWRAP_SOCKET_GROUP", "SW_UART_KIND",
        *runtime.ISOLATED_DIRS.keys(),
    ]
    user_env = systemd_container._user_env(env)
    result = subprocess.run(
        ["runuser", "-u", systemd_container.USER, "--preserve-environment", "--",
         "systemctl", "--user", "import-environment", *names],
        env=user_env, capture_output=True, text=True, check=False, timeout=30,
    )
    systemd_container._require_ok(result, "匯入隔離 runtime 環境到 systemd-user manager")


def _show_value(mode: str, property_name: str) -> str:
    result = systemd_container.systemctl(mode, "show", "serialwrap", "-p", property_name, "--value")
    systemd_container._require_ok(result, f"查詢 systemd {property_name}")
    return result.stdout.strip()


def _wait_path(path: pathlib.Path, thread: threading.Thread, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        if not thread.is_alive():
            return False
        time.sleep(0.05)
    return path.is_file()


def _run_update_exit1(args: argparse.Namespace) -> int:
    mode = args.mode
    run_id = args.run_id
    old_bundle = pathlib.Path(args.bundle_old).resolve()
    new_bundle = pathlib.Path(args.bundle_new).resolve()
    old_cli = old_bundle / "bin" / "serialwrap"
    new_cli = new_bundle / "bin" / "serialwrap"
    old_daemon = (old_bundle / "bin" / "serialwrapd").resolve()
    old_manifest = _bundle_manifest_path(old_bundle, "0.4.0")
    new_manifest = _bundle_manifest_path(new_bundle, "0.4.1")
    evidence_root = runtime.host_evidence_root()
    scenario_root = ROOT / f"update-exit1-{run_id}"
    env: dict[str, str] | None = None
    bridge_procs: list[subprocess.Popen] = []
    control: ControlClient | None = None
    update_thread: threading.Thread | None = None
    update_rows: list[subprocess.CompletedProcess[str]] = []
    pause_dir: pathlib.Path | None = None
    failures: list[str] = []
    evidence: dict[str, Any] = {
        "scenario": "update-exit1",
        "old_bundle_version": "0.4.0",
        "new_bundle_version": "0.4.1",
        "product_exercised": ["serialwrap setup --user 初次安裝", "serialwrap setup --user 更新"],
        "harness_exercised": [
            "P2 bridge 假 UART holder PID 證據",
            "隔離 wrapper 將舊 daemon 的正常 handoff exit 轉為 systemd 可觀察的 exit 1",
        ],
    }
    try:
        if mode != "systemd-user":
            raise ValueError("update-exit1 僅支援 systemd-user")
        for bundle, expected in ((old_bundle, "0.4.0"), (new_bundle, "0.4.1")):
            manifest = json.loads(_bundle_manifest_path(bundle, expected).read_text(encoding="utf-8"))
            if manifest.get("bundle_version") != expected:
                raise ValueError(f"bundle version 應為 {expected}：{bundle}")
        for path in (old_cli, new_cli, old_daemon):
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError(f"dev bundle 缺少可執行檔：{path}")

        systemd_container.prepare_user_manager()
        env = systemd_container.make_env(scenario_root, cli=old_cli, daemon=old_daemon)
        env.update({
            "SERIALWRAP_RELEASE_MANIFEST": str(old_manifest),
            "SW_DOCKER_RUN_ID": run_id,
        })
        runtime.write_profiles(env)
        for key in runtime.ISOLATED_DIRS:
            subprocess.run(["chown", "-R", f"{systemd_container.USER}:{systemd_container.USER}", env[key]], check=True)
        subprocess.run(["chown", "-R", f"{systemd_container.USER}:{systemd_container.USER}", str(systemd_container.USER_HOME)], check=True)
        _import_manager_environment(env)
        bridge_procs = runtime.start_bridges(
            env, dut_image=os.environ["SW_DUT_IMAGE"], run_id=run_id,
            evidence_root=evidence_root / "bridge",
        )

        initial = systemd_container._run_as_user(env, [str(old_cli), "setup", "--user"])
        evidence["initial_setup_exit"] = initial.returncode
        if initial.returncode != 0:
            failures.append(f"old bundle setup 失敗：{initial.stdout[-500:]} {initial.stderr[-500:]}")
        else:
            systemd_container._wait_daemon_ready(env, timeout_s=120)
            old_pid = systemd_container.daemon_pid(env)
            evidence["old_daemon_pid"] = old_pid
            evidence["old_unit_main_pid_initial"] = _main_pid(mode)
            old_holders = _holder_pids(pathlib.Path(env["SERIALWRAP_BY_ID_DIR"]) / "usb-fake-0")
            if old_pid not in old_holders:
                failures.append(f"舊 daemon PID {old_pid} 未持有 bridge PTY：{old_holders}")

            bridge_control_path = evidence_root / "bridge" / "bridge-0" / "control.json"
            control = ControlClient(bridge_control_path, timeout_s=30)
            _bridge_mark(control, "opened", old_pid)

            stopped = systemd_container._service_action(mode, env, "stop")
            if stopped.returncode != 0:
                failures.append(f"切換隔離 holder wrapper 前停止舊服務失敗：{stopped.stderr[-500:]}")
            else:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and old_pid in _holder_pids(pathlib.Path(env["SERIALWRAP_BY_ID_DIR"]) / "usb-fake-0"):
                    time.sleep(0.05)
                if old_pid in _holder_pids(pathlib.Path(env["SERIALWRAP_BY_ID_DIR"]) / "usb-fake-0"):
                    failures.append("初次 setup 後停止服務仍有舊 daemon 持有 PTY")
                else:
                    _bridge_mark(control, "closed", old_pid)

            stable_daemon = systemd_container.USER_HOME / ".local" / "bin" / "serialwrapd"
            if stable_daemon.exists() or stable_daemon.is_symlink():
                stable_daemon.unlink()
            stable_daemon.parent.mkdir(parents=True, exist_ok=True)
            stable_daemon.write_text(
                "#!/bin/sh\n"
                "set +e\n"
                f"{shlex.quote(str(old_daemon))} \"$@\"\n"
                "status=$?\n"
                "if [ \"$status\" -eq 0 ]; then exit 1; fi\n"
                "exit \"$status\"\n",
                encoding="utf-8",
            )
            stable_daemon.chmod(0o755)
            subprocess.run(["chown", f"{systemd_container.USER}:{systemd_container.USER}", str(stable_daemon)], check=True)
            restarted = systemd_container._service_action(mode, env, "start")
            if restarted.returncode != 0:
                failures.append(f"啟動 exit-1 wrapper 服務失敗：{restarted.stderr[-500:]}")
            else:
                systemd_container._wait_daemon_ready(env, timeout_s=120)
                old_pid = systemd_container.daemon_pid(env)
                evidence["old_daemon_pid"] = old_pid
                evidence["old_wrapper_main_pid"] = _main_pid(mode)
                evidence["restart_policy_before_update"] = _show_value(mode, "Restart")
                if evidence["restart_policy_before_update"] != "on-failure":
                    failures.append(f"更新前 Restart 應為 on-failure：{evidence['restart_policy_before_update']}")
                _bridge_mark(control, "opened", old_pid)

                env["SERIALWRAP_CLI_EXE"] = str(new_cli)
                env["SERIALWRAP_DAEMON_EXE"] = str((new_bundle / "bin" / "serialwrapd").resolve())
                env["SERIALWRAP_RELEASE_MANIFEST"] = str(new_manifest)
                pause_dir = pathlib.Path(env["SERIALWRAP_RUN_DIR"]) / "update-exit1-pause"
                env["SW_TEST_UPDATE_PAUSE_AFTER"] = "handoff_stop:replied"
                env["SW_TEST_UPDATE_PAUSE_DIR"] = str(pause_dir)

                def run_update() -> None:
                    update_rows.append(systemd_container._run_as_user(env, [str(new_cli), "setup", "--user"], timeout=900))

                update_thread = threading.Thread(target=run_update, name="systemd-update-exit1", daemon=True)
                update_thread.start()
                reached = pause_dir / "handoff_stop_replied.reached"
                if not _wait_path(reached, update_thread, timeout_s=180):
                    failures.append("更新沒有到達 handoff_stop:replied 測試屏障")
                else:
                    tty_path = pathlib.Path(env["SERIALWRAP_BY_ID_DIR"]) / "usb-fake-0"
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        holders = _holder_pids(tty_path)
                        if old_pid not in holders:
                            break
                        time.sleep(0.05)
                    else:
                        failures.append(f"handoff 回覆後舊 holder 仍持有 PTY：{_holder_pids(tty_path)}")
                    if old_pid not in _holder_pids(tty_path):
                        _bridge_mark(control, "closed", old_pid)

                    deadline = time.monotonic() + 15
                    exit_status = ""
                    restarts_at_exit = ""
                    while time.monotonic() < deadline:
                        main_pid = _main_pid(mode)
                        exit_status = _show_value(mode, "ExecMainStatus")
                        restarts_at_exit = _show_value(mode, "NRestarts")
                        if main_pid == 0 and exit_status == "1":
                            break
                        time.sleep(0.1)
                    evidence["old_exit_status"] = exit_status
                    evidence["n_restarts_at_exit1"] = restarts_at_exit
                    evidence["main_pid_at_exit1"] = _main_pid(mode)
                    evidence["restart_policy_during_update"] = _show_value(mode, "Restart")
                    if evidence["old_exit_status"] != "1" or evidence["main_pid_at_exit1"] != 0:
                        failures.append("systemd 未觀察到舊 daemon exit 1 後進入無 MainPID 狀態")
                    if restarts_at_exit != "0":
                        failures.append(f"Restart=no hold 期間 NRestarts 應為 0：{restarts_at_exit}")
                    if evidence["restart_policy_during_update"] != "no":
                        failures.append(f"handoff 期間 systemd Restart 應為 no：{evidence['restart_policy_during_update']}")

                if pause_dir is not None:
                    pause_dir.joinpath("handoff_stop_replied.resume").write_text("resume\n", encoding="utf-8")
                update_thread.join(240)
                if update_thread.is_alive():
                    failures.append("Rust setup update 未在 240 秒內結束")
                elif not update_rows:
                    failures.append("Rust setup update 沒有子行程結果")
                else:
                    update = update_rows[0]
                    evidence["update_exit"] = update.returncode
                    if update.returncode != 0:
                        failures.append(f"新版 setup update 失敗：{update.stdout[-800:]} {update.stderr[-800:]}")
                    else:
                        systemd_container._wait_daemon_ready(env, timeout_s=120)
                        status_result, status = _read_daemon_status(env)
                        new_pid = status.get("pid")
                        evidence["new_daemon_pid"] = new_pid
                        evidence["new_daemon_status_version"] = status.get("version")
                        evidence["new_daemon_status_exit"] = status_result.returncode
                        tty_path = pathlib.Path(env["SERIALWRAP_BY_ID_DIR"]) / "usb-fake-0"
                        if not isinstance(new_pid, int) or new_pid <= 0 or new_pid == old_pid:
                            failures.append(f"新版 daemon PID 無效或未更換：old={old_pid} new={new_pid}")
                        elif new_pid not in _holder_pids(tty_path):
                            failures.append(f"新版 daemon PID {new_pid} 未持有 bridge PTY：{_holder_pids(tty_path)}")
                        else:
                            _bridge_mark(control, "opened", new_pid)
                        if status_result.returncode != 0 or status.get("version") != "0.4.1":
                            failures.append(f"新版 daemon health version 未為 0.4.1：{status}")
                        evidence["restart_policy_after_update"] = _show_value(mode, "Restart")
                        evidence["n_restarts_after_update"] = _show_value(mode, "NRestarts")
                        if evidence["restart_policy_after_update"] != "on-failure":
                            failures.append("更新完成後 Restart 未恢復 on-failure")
                        if evidence["n_restarts_after_update"] != "0":
                            failures.append(f"更新期間 systemd 自動重啟次數非零：{evidence['n_restarts_after_update']}")
                        if evidence.get("new_daemon_pid") == _main_pid(mode) and isinstance(new_pid, int):
                            evidence["new_unit_main_pid"] = _main_pid(mode)
                        elif _main_pid(mode) <= 0:
                            failures.append("更新完成後 systemd unit 沒有 MainPID")

                        current_path = pathlib.Path(env["XDG_DATA_HOME"]) / "serialwrap" / "current-release.json"
                        if current_path.is_file():
                            current = json.loads(current_path.read_text(encoding="utf-8"))
                            evidence["current_release_version"] = current.get("bundle_version")
                            if evidence["current_release_version"] != "0.4.1":
                                failures.append(f"current-release 版本不是 0.4.1：{current}")
                        else:
                            failures.append(f"缺少 current-release.json：{current_path}")

                        stopped_new = systemd_container._service_action(mode, env, "stop")
                        if stopped_new.returncode != 0:
                            failures.append("更新後停止新版 service 以配對 holder evidence 失敗")
                        else:
                            deadline = time.monotonic() + 15
                            while time.monotonic() < deadline and new_pid in _holder_pids(tty_path):
                                time.sleep(0.05)
                            if new_pid in _holder_pids(tty_path):
                                failures.append(f"停止新版 service 後 holder 仍在：{_holder_pids(tty_path)}")
                            else:
                                _bridge_mark(control, "closed", new_pid)

                        marks = _completed_marks(evidence_root / "bridge" / "bridge-0" / "control.ndjson")
                        old_open = next((row for row in marks if row.get("params", {}).get("label") == "opened"
                                         and row.get("params", {}).get("op_id") == str(old_pid)), None)
                        old_closed = next((row for row in marks if row.get("params", {}).get("label") == "closed"
                                           and row.get("params", {}).get("op_id") == str(old_pid)), None)
                        new_open = next((row for row in marks if row.get("params", {}).get("label") == "opened"
                                         and row.get("params", {}).get("op_id") == str(new_pid)), None)
                        new_closed = next((row for row in marks if row.get("params", {}).get("label") == "closed"
                                           and row.get("params", {}).get("op_id") == str(new_pid)), None)
                        evidence["holder_marks"] = [
                            {"label": row.get("params", {}).get("label"), "pid": row.get("params", {}).get("op_id"), "seq": row.get("seq")}
                            for row in marks
                        ]
                        if not all((old_open, old_closed, new_open, new_closed)):
                            failures.append("control.ndjson 缺少 old/new holder-paired opened/closed mark")
                        elif not (old_open["seq"] < old_closed["seq"] < new_open["seq"] < new_closed["seq"]):
                            failures.append("control.ndjson holder 順序不是 old opened/closed → new opened/closed")

                        journal = systemd_container._journal(mode)
                        (evidence_root / "update-exit1-journal.log").write_text(journal, encoding="utf-8")
                        # 只接受 status=1/FAILURE；status=137／143 等信號結束的記錄不得匹配 exit 1。
                        evidence["journal_confirms_exit1"] = "status=1/FAILURE" in journal
                        if not evidence["journal_confirms_exit1"]:
                            failures.append("systemd journal 未記錄舊服務 status=1")
        if update_thread is not None and update_thread.is_alive():
            if pause_dir is not None:
                pause_dir.joinpath("handoff_stop_replied.resume").write_text("resume\n", encoding="utf-8")
            update_thread.join(15)
    except Exception as exc:
        evidence["runner_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if env is not None:
            env.pop("SW_TEST_UPDATE_PAUSE_AFTER", None)
            env.pop("SW_TEST_UPDATE_PAUSE_DIR", None)
            try:
                systemd_container.cleanup(mode, env, bridge_procs, run_id)
            except BaseException as exc:
                evidence["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        if control is not None:
            control.close()
        if scenario_root.exists():
            import shutil
            shutil.rmtree(scenario_root)

    if "cleanup_error" in evidence:
        failures.append(f"隔離 systemd／bridge 清理失敗：{evidence['cleanup_error']}")
    if "runner_error" in evidence:
        verdict = "INSUFFICIENT_EVIDENCE"
        reason = evidence["runner_error"]
    else:
        verdict = "FAIL" if failures else "PASS"
        reason = ";".join(failures)
    evidence.update({"verdict": verdict, "reason": reason})
    from sw_core.release_manifest import package_tree_sha256

    passed = systemd_container.write_report(
        evidence_root / "l15-docker-report.json", suite="update-exit1", mode=mode,
        results=[{"case_id": "update-exit1", "verdict": verdict, "reason": reason}],
        extra=evidence, tree_sha256=package_tree_sha256(),
    )
    if not passed:
        print(f"systemd update-exit1 證據未通過：{reason}", file=__import__("sys").stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("systemd-user", "systemd-system"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", choices=("hold", "update-exit1"), default="hold")
    parser.add_argument("--bundle-old")
    parser.add_argument("--bundle-new")
    args = parser.parse_args()
    if args.scenario == "update-exit1":
        if not args.bundle_old or not args.bundle_new:
            parser.error("update-exit1 需要 --bundle-old 與 --bundle-new")
        return _run_update_exit1(args)
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
