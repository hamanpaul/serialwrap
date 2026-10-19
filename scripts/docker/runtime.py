"""OD-15 runner 共用的假 UART、daemon 啟停與長度證據工具。"""
from __future__ import annotations

import json
import math
import os
import pathlib
import signal
import subprocess
import time
from typing import Any

REPO = pathlib.Path("/opt/serialwrap")
ISOLATED_DIRS = {
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


def isolated_env(root: pathlib.Path, *, cli: pathlib.Path, daemon: pathlib.Path) -> dict[str, str]:
    """清除外層產品環境變數，僅建立本 run 專屬的九個 SERIALWRAP 目錄。"""
    env = {key: value for key, value in os.environ.items() if not key.startswith("SERIALWRAP_")}
    home = pathlib.Path("/tmp/sw-home")
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "PYTHONPATH": f"{REPO}:{REPO / 'tools/uart_stdio_bridge/src'}:{REPO / 'regression'}",
        "SERIALWRAP_CLI_EXE": str(cli),
        "SERIALWRAP_DAEMON_EXE": str(daemon),
        "SW_UART_KIND": "pty-fake",
    })
    home.mkdir(parents=True, exist_ok=True)
    for key, subdir in ISOLATED_DIRS.items():
        target = root / subdir
        target.mkdir(parents=True, exist_ok=True)
        env[key] = str(target)
    return env


def write_profiles(env: dict[str, str]) -> None:
    import yaml

    config_dir = pathlib.Path(env["SERIALWRAP_CONFIG_DIR"])
    profiles = config_dir / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    profile = {
        "platform": "prpl",
        "prompt_regex": r"(?m)^root@prplOS:.*# ",
        "ready_probe": "echo __READY__${nonce}",
        "timeout_s": 15,
    }
    targets = [
        {"act_no": index + 1, "com": f"COM{index}", "alias": f"docker-fake-{index}",
         "profile": "docker-fake", "device_by_id": str(pathlib.Path(env["SERIALWRAP_BY_ID_DIR"]) / f"usb-fake-{index}")}
        for index in range(2)
    ]
    document = {"defaults": {}, "profiles": {"docker-fake": profile}, "targets": targets}
    (config_dir / "config.yaml").write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                                             encoding="utf-8")
    (profiles / "docker-fake.yaml").write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                                                encoding="utf-8")


def start_bridges(env: dict[str, str], *, dut_image: str, run_id: str,
                  evidence_root: pathlib.Path) -> list[subprocess.Popen]:
    """於 serialwrap 容器內啟動兩個 bridge，各自以 Docker 啟動一個 DUT。"""
    procs: list[subprocess.Popen] = []
    link_dir = pathlib.Path(env["SERIALWRAP_BY_ID_DIR"])
    for index in range(2):
        case_id = f"{run_id}-{index}"
        evidence = evidence_root / f"bridge-{index}"
        evidence.mkdir(parents=True, exist_ok=True)
        log_path = evidence_root / f"bridge-{index}.log"
        log = log_path.open("w", encoding="utf-8")
        command = [
            "uart-stdio-bridge", "run", "--run-id", f"{run_id}-{index}", "--case-id", case_id,
            "--port-id", f"COM{index}", "--evidence-dir", str(evidence),
            "--endpoint", "posix-pty", "--by-id-link", str(link_dir / f"usb-fake-{index}"),
            "--dut-mode", "docker", "--dut-image", dut_image,
        ]
        proc = subprocess.Popen(command, cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        # 保留 log handle 供 cleanup 明確關閉。
        setattr(proc, "_od15_log", log)
        procs.append(proc)
    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if all((link_dir / f"usb-fake-{index}").is_symlink()
                   and (link_dir / f"usb-fake-{index}").exists() for index in range(2)):
                return procs
            if any(proc.poll() is not None for proc in procs):
                raise RuntimeError("bridge 啟動失敗；檢查 bridge-*.log")
            time.sleep(0.1)
        raise TimeoutError("bridge PTY pair 未在 30 秒內就緒")
    except BaseException:
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            log = getattr(proc, "_od15_log", None)
            if log is not None:
                log.close()
        raise


def wait_ready(env: dict[str, str], *, timeout_s: float = 120.0) -> list[dict[str, Any]]:
    """等待 daemon 與 COM0／COM1 皆報 READY；逾時視為證據不足。"""
    cli = env["SERIALWRAP_CLI_EXE"]
    deadline = time.monotonic() + timeout_s
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        result = subprocess.run([cli, "session", "list"], env=env, cwd=REPO,
                                capture_output=True, text=True, check=False)
        try:
            payload = json.loads(result.stdout)
            last = [row for row in payload.get("sessions", []) if isinstance(row, dict)]
        except json.JSONDecodeError:
            last = []
        ready = {row.get("com") for row in last if row.get("state") == "READY"}
        if result.returncode == 0 and {"COM0", "COM1"} <= ready:
            return last
        time.sleep(0.5)
    raise TimeoutError(f"COM0／COM1 未於 {timeout_s:g} 秒內 READY；最後 session list={last!r}")


def start_daemon_and_wait(env: dict[str, str], *, timeout_s: float = 120.0) -> list[dict[str, Any]]:
    result = subprocess.run([env["SERIALWRAP_CLI_EXE"], "daemon", "start"], cwd=REPO, env=env,
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"serialwrap daemon start 失敗：{result.stdout[-1000:]} {result.stderr[-1000:]}")
    return wait_ready(env, timeout_s=timeout_s)


def stop_runtime(env: dict[str, str], procs: list[subprocess.Popen], run_id: str) -> None:
    """收掉本 run daemon、bridge 及唯一命名的 DUT 容器。"""
    try:
        subprocess.run([env["SERIALWRAP_CLI_EXE"], "daemon", "stop"], cwd=REPO, env=env,
                       capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    for proc in procs:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
    for proc in procs:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log = getattr(proc, "_od15_log", None)
        if log is not None:
            log.close()
    # bridge 子 DUT 名稱以 run-id 為唯一前綴，僅清理此 run 建立的容器。
    try:
        ids = subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", f"name=sw-dut-{run_id}"], text=True
        ).split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], check=False, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        pass


def read_observer(path: pathlib.Path, *, hours: int, interval_s: float) -> tuple[dict[str, Any], list[str], dict[str, Any] | None]:
    """拒絕截短、樣本不足或觀測缺口過大的長跑報告。"""
    failures: list[str] = []
    records: list[dict[str, Any]] = []
    try:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"JSON root at line {line_no} is not an object")
                records.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        failures.append(f"observer_report_invalid:{type(exc).__name__}")
    header = next((row for row in records if row.get("kind") == "header"), None)
    samples = [row for row in records if row.get("kind") == "sample"]
    times = [float(row["t_mono"]) for row in samples
             if isinstance(row.get("t_mono"), (int, float)) and not isinstance(row.get("t_mono"), bool)]
    times_are_valid = len(times) == len(samples) and all(math.isfinite(value) for value in times)
    times_are_ordered = times_are_valid and all(right > left for left, right in zip(times, times[1:]))
    duration_s = float(hours) * 3600.0
    observed_s = max(0.0, times[-1] - times[0]) if times_are_ordered and len(times) >= 2 else 0.0
    max_gap_s = max((right - left for left, right in zip(times, times[1:])), default=0.0)
    min_span_s = max(0.0, duration_s - 2.0 * interval_s)
    min_samples = max(2, int(min_span_s // interval_s))
    metrics = {"observed_s": observed_s, "max_gap_s": max_gap_s, "sample_count": len(samples),
               "timestamps_ordered": times_are_ordered,
               "minimum_sample_count": min_samples, "required_span_s": min_span_s}
    if not times_are_valid:
        failures.append("observer_sample_time_missing")
    elif not times_are_ordered:
        failures.append("observer_timestamps_not_monotonic")
    if len(samples) < min_samples:
        failures.append("observer_sample_count_insufficient")
    if observed_s < min_span_s:
        failures.append("observer_duration_insufficient")
    if max_gap_s > 3.0 * interval_s:
        failures.append("observer_gap_exceeded")
    if not samples:
        failures.append("observer_samples_missing")
    for index, row in enumerate(samples):
        daemon = row.get("daemon") or {}
        if daemon.get("ok") is not True or daemon.get("pid") in (None, 0):
            failures.append(f"daemon_unhealthy_sample:{index}")
        if daemon.get("multi_open") is not False:
            failures.append(f"multi_open_unknown_or_true:{index}")
        if row.get("procscan", {}).get("other_serialwrapd") != []:
            failures.append(f"foreign_daemon_present_or_unknown:{index}")
        if any((row.get("sessions") or {}).get(board) != "READY" for board in ("COM0", "COM1")):
            failures.append(f"session_not_ready:{index}")
    return metrics, failures, header


def write_l15_report(path: pathlib.Path, *, suite: str, producer: str, results: list[dict[str, Any]],
                     exit_code: int, images: dict[str, str], tree_sha256: str,
                     extra: dict[str, Any] | None = None) -> bool:
    """寫入帶有固定 L1.5-docker 標記的報告，僅全 PASS 才回傳 True。"""
    from tests.acceptance.evidence_labels import label_report

    report = {"schema_version": 1, "suite": suite, "exit_code": exit_code, "results": results}
    report.update(extra or {})
    report = label_report(report, evidence_layer="L1.5-docker", producer=producer,
                          topology="docker-2c", uart_kind="pty-fake",
                          image_digests=images, tree_sha256=tree_sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return exit_code == 0 and bool(results) and all(row.get("verdict") == "PASS" for row in results)
