"""OD-15 systemd 容器的隔離服務、產品 reconcile 與報告工具。"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

import yaml

from scripts.docker import runtime

REPO = pathlib.Path("/opt/serialwrap")
USER = "swtest"
USER_HOME = pathlib.Path("/home/swtest")


def _user_env(env: dict[str, str]) -> dict[str, str]:
    result = dict(env)
    result.update({
        "HOME": str(USER_HOME),
        "USER": USER,
        "LOGNAME": USER,
        "XDG_CONFIG_HOME": str(USER_HOME / ".config"),
        "XDG_STATE_HOME": str(USER_HOME / ".local" / "state"),
        "XDG_DATA_HOME": str(USER_HOME / ".local" / "share"),
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "PATH": f"{USER_HOME}/.local/bin:/opt/serialwrap/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "SERIALWRAP_SOCKET_GROUP": USER,
    })
    return result


def _run_as_user(env: dict[str, str], argv: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    user_env = _user_env(env)
    command = ["runuser", "-u", USER, "--preserve-environment", "--", "env"]
    command.extend(f"{key}={value}" for key, value in user_env.items())
    command.extend(argv)
    return subprocess.run(command, cwd=REPO, env=user_env, capture_output=True, text=True,
                          check=False, timeout=timeout)


def _user_systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["runuser", "-u", USER, "--preserve-environment", "--", "env",
         "XDG_RUNTIME_DIR=/run/user/1000",
         "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
         "systemctl", "--user", *args],
        capture_output=True, text=True, check=False, timeout=30,
    )


def systemctl(mode: str, *args: str) -> subprocess.CompletedProcess[str]:
    if mode == "systemd-user":
        return _user_systemctl(*args)
    return subprocess.run(["systemctl", *args], capture_output=True, text=True,
                          check=False, timeout=30)


def _require_ok(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        raise RuntimeError(f"{label} 失敗 rc={result.returncode}: {result.stdout[-1000:]} {result.stderr[-1000:]}")


def make_env(root: pathlib.Path, *, cli: pathlib.Path, daemon: pathlib.Path) -> dict[str, str]:
    """建立 systemd 服務和 observer 共用的隔離 HOME 與九個 SERIALWRAP 目錄。"""
    env = runtime.isolated_env(root, cli=cli, daemon=daemon)
    env.update(_user_env(env))
    for key in runtime.ISOLATED_DIRS:
        pathlib.Path(env[key]).mkdir(parents=True, exist_ok=True)
    USER_HOME.mkdir(parents=True, exist_ok=True)
    (USER_HOME / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    return env


def prepare_user_manager() -> None:
    _require_ok(subprocess.run(["loginctl", "enable-linger", USER], capture_output=True,
                               text=True, check=False, timeout=30), "啟用 user lingering")
    _require_ok(subprocess.run(["systemctl", "start", "user@1000.service"], capture_output=True,
                               text=True, check=False, timeout=30), "啟動 user manager")
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if pathlib.Path("/run/user/1000/bus").exists():
            return
        time.sleep(0.2)
    raise TimeoutError("systemd-user D-Bus 未於 20 秒內就緒")


def write_daemon_wrapper(env: dict[str, str]) -> pathlib.Path:
    path = USER_HOME / ".local" / "bin" / "serialwrapd"
    exports = [
        "HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
        *runtime.ISOLATED_DIRS.keys(), "SERIALWRAP_SOCKET_GROUP", "SW_UART_KIND",
    ]
    body = ["#!/bin/sh", "set -eu"]
    for key in exports:
        value = env.get(key, "")
        body.append(f"export {key}={json.dumps(value)}")
    body.extend([
        'if [ "$#" -eq 0 ]; then',
        '  set -- --socket "$SERIALWRAP_RUN_DIR/serialwrapd.sock" --profile-dir "$SERIALWRAP_CONFIG_DIR/profiles"',
        "fi",
        'exec /opt/serialwrap/.venv/bin/serialwrapd "$@"',
    ])
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def initialize_supervised(mode: str, env: dict[str, str], *, run_id: str,
                           bridge_procs: list[subprocess.Popen]) -> tuple[int, int]:
    """經產品 setup 將 on-demand daemon reconcile 成 systemd 服務，再回報 PID。"""
    config_dir = pathlib.Path(env["SERIALWRAP_CONFIG_DIR"])
    profiles = config_dir / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text("defaults: {}\nprofiles: {}\ntargets: []\n", encoding="utf-8")
    write_daemon_wrapper(env)
    for key in runtime.ISOLATED_DIRS:
        subprocess.run(["chown", "-R", f"{USER}:{USER}", env[key]], check=True)
    subprocess.run(["chown", "-R", f"{USER}:{USER}", str(USER_HOME)], check=True)

    start = _run_as_user(env, [env["SERIALWRAP_CLI_EXE"], "daemon", "start"])
    _require_ok(start, "啟動舊 on-demand daemon")
    old_pid = 0
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            old_pid = daemon_pid(env)
            break
        except RuntimeError:
            time.sleep(0.2)
    if old_pid <= 0:
        raise TimeoutError("舊 on-demand daemon 未在 30 秒內回報有效 PID")

    prepare_user_manager()
    if mode == "systemd-system":
        sudoers = pathlib.Path("/etc/sudoers.d/swtest")
        sudoers.write_text(f"{USER} ALL=(ALL) NOPASSWD: ALL\n", encoding="utf-8")
        sudoers.chmod(0o440)
        setup = _run_as_user(env, [env["SERIALWRAP_CLI_EXE"], "setup", "--system", "--with-sudo"])
    else:
        setup = _run_as_user(env, [env["SERIALWRAP_CLI_EXE"], "setup", "--user"])
    _require_ok(setup, f"serialwrap setup --{mode}")

    # Python 基線的 systemd-system unit 固定使用 /etc/serialwrap/profiles；user unit 則依隔離 env 取 profile dir。
    runtime.write_profiles(env)
    config_path = config_dir / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config["supervision_mode"] = mode
    if mode == "systemd-system":
        config["socket_path"] = "/run/serialwrap/serialwrapd.sock"
        system_profiles = pathlib.Path("/etc/serialwrap/profiles")
        system_profiles.mkdir(parents=True, exist_ok=True)
        shutil.copy2(profiles / "docker-fake.yaml", system_profiles / "docker-fake.yaml")
    else:
        config["socket_path"] = str(pathlib.Path(env["SERIALWRAP_RUN_DIR"]) / "serialwrapd.sock")
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    subprocess.run(["chown", "-R", f"{USER}:{USER}", str(config_dir)], check=True)

    restart = _service_action(mode, env, "restart")
    _require_ok(restart, "serialwrap service restart")
    _wait_daemon_ready(env, timeout_s=120.0)
    new_pid = _main_pid(mode)
    if new_pid <= 0 or new_pid == old_pid:
        raise RuntimeError(f"reconcile 未啟動新 daemon pid：old={old_pid} new={new_pid}")
    if _pid_alive(old_pid):
        raise RuntimeError(f"舊 daemon 未退出：pid={old_pid}")
    return old_pid, new_pid


def _service_action(mode: str, env: dict[str, str], action: str) -> subprocess.CompletedProcess[str]:
    argv = [env["SERIALWRAP_CLI_EXE"], "service", action]
    if mode == "systemd-system":
        argv.append("--with-sudo")
    return _run_as_user(env, argv)


def _wait_daemon_ready(env: dict[str, str], *, timeout_s: float) -> None:
    runtime.wait_ready(env, timeout_s=timeout_s)


def daemon_pid(env: dict[str, str]) -> int:
    result = subprocess.run([env["SERIALWRAP_CLI_EXE"], "daemon", "status"], env=env,
                            cwd=REPO, capture_output=True, text=True, check=False, timeout=15)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"daemon status 非 JSON：{result.stdout[-500:]}") from exc
    pid = payload.get("pid")
    if result.returncode != 0 or not isinstance(pid, int) or pid <= 0:
        raise RuntimeError(f"daemon status 無有效 pid：{payload}")
    return pid


def _main_pid(mode: str) -> int:
    result = systemctl(mode, "show", "serialwrap", "-p", "MainPID", "--value")
    _require_ok(result, "讀取 systemd MainPID")
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"systemd MainPID 無效：{result.stdout!r}") from exc


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def apply_hold(mode: str, *, name: str = "hold.conf") -> pathlib.Path:
    if mode == "systemd-user":
        unit_dir = USER_HOME / ".config" / "systemd" / "user" / "serialwrap.service.d"
    else:
        unit_dir = pathlib.Path("/etc/systemd/system/serialwrap.service.d")
    unit_dir.mkdir(parents=True, exist_ok=True)
    path = unit_dir / name
    path.write_text("[Unit]\nRefuseManualStart=yes\n\n[Service]\nRestart=no\n", encoding="utf-8")
    if mode == "systemd-user":
        subprocess.run(["chown", f"{USER}:{USER}", str(path)], check=True)
    _require_ok(systemctl(mode, "daemon-reload"), "systemd daemon-reload")
    shown = systemctl(mode, "show", "serialwrap", "-p", "Restart", "-p", "RefuseManualStart")
    _require_ok(shown, "讀取 hold drop-in 屬性")
    if "Restart=no" not in shown.stdout or "RefuseManualStart=yes" not in shown.stdout:
        raise RuntimeError(f"systemd 未套用 hold drop-in：{shown.stdout}")
    return path


def release_hold(mode: str, path: pathlib.Path) -> None:
    path.unlink(missing_ok=True)
    _require_ok(systemctl(mode, "daemon-reload"), "移除 hold 後 daemon-reload")


def _journal(mode: str) -> str:
    if mode == "systemd-user":
        command = ["runuser", "-u", USER, "--preserve-environment", "--", "env",
                   "XDG_RUNTIME_DIR=/run/user/1000",
                   "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                   "journalctl", "--user", "-u", "serialwrap", "-o", "cat", "--no-pager"]
    else:
        command = ["journalctl", "-u", "serialwrap", "-o", "cat", "--no-pager"]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    _require_ok(result, "讀取 journald")
    return result.stdout


def image_ids() -> dict[str, str]:
    return {
        name: subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", os.environ[image]],
                                      text=True).strip()
        for name, image in (("serialwrap", "SW_SERIALWRAP_IMAGE"),
                            ("dut-sim", "SW_DUT_IMAGE"), ("systemd", "SW_SYSTEMD_IMAGE"))
    }


def write_report(path: pathlib.Path, *, suite: str, mode: str, results: list[dict[str, Any]],
                 extra: dict[str, Any], tree_sha256: str) -> bool:
    from tests.acceptance.evidence_labels import label_report

    report = label_report({"schema_version": 1, "suite": suite, "mode": mode, "results": results,
                           **extra},
                          evidence_layer="L1.5-docker", producer="run-systemd.sh",
                          topology="docker-2c", uart_kind="pty-fake", image_digests=image_ids(),
                          tree_sha256=tree_sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    host_uid = os.environ.get("SW_HOST_UID")
    host_gid = os.environ.get("SW_HOST_GID")
    if host_uid and host_gid:
        for output in path.parent.rglob("*"):
            if output.is_file():
                os.chown(output, int(host_uid), int(host_gid))
        os.chown(path.parent, int(host_uid), int(host_gid))
    return bool(results) and all(row.get("verdict") == "PASS" for row in results)


def serve(mode: str, env: dict[str, str], run_id: str) -> tuple[list[subprocess.Popen], int, int]:
    write_daemon_wrapper(env)
    bridge_procs = runtime.start_bridges(env, dut_image=os.environ["SW_DUT_IMAGE"],
                                         run_id=run_id, evidence_root=pathlib.Path("/evidence/bridge"))
    for index in range(2):
        link = pathlib.Path(env["SERIALWRAP_BY_ID_DIR"]) / f"usb-fake-{index}"
        os.chown(link.resolve(), 1000, -1)
    old_pid, new_pid = initialize_supervised(mode, env, run_id=run_id, bridge_procs=bridge_procs)
    return bridge_procs, old_pid, new_pid


def cleanup(mode: str, env: dict[str, str], bridge_procs: list[subprocess.Popen], run_id: str) -> None:
    try:
        _service_action(mode, env, "stop")
    except (OSError, subprocess.SubprocessError):
        pass
    runtime.stop_runtime(env, bridge_procs, run_id)
    if mode == "systemd-user":
        systemctl(mode, "disable", "serialwrap")
    else:
        systemctl(mode, "disable", "serialwrap")


def attestation(mode: str) -> dict[str, Any]:
    return {"mode": mode, "auto_restart_disabled": True, "evidence": "Restart=no"}

