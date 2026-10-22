"""L3 realhw suite adapter。"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from . import SuiteRun, _frozen_path, _read_json_object, _repo_root


_GATE_NORMAL_RCS = {0, 1, 4, 5}
_GATE_EXIT_CODES = {
    "PASS": 0,
    "NOT_APPLICABLE_BASELINE": 0,
    "FAIL": 1,
    "INSUFFICIENT_EVIDENCE": 4,
    "BLOCKED": 5,
}
_OBSERVER_STARTUP_TIMEOUT_S = 15.0


def _load_acceptance(m: Any, repo_root: Path | None = None) -> dict[str, Any]:
    if m.acceptance is None:
        raise ValueError("L3 acceptance file is missing")
    value = yaml.safe_load(_frozen_path(m, m.acceptance, repo_root).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("L3 acceptance file must be a YAML object")
    return value


def _endpoint_argv(cli: Path, endpoint: str | None, *args: str) -> list[str]:
    result = [str(cli)]
    if endpoint:
        result.extend(("--endpoint", endpoint))
    result.extend(args)
    return result


def _run(run: Callable[..., Any], argv: list[str], inst: Any, m: Any, *,
         timeout: float | None = None, repo_root: Path | None = None) -> Any:
    return run(argv, env=inst.env, timeout=timeout, cwd=_repo_root(m, repo_root))


def _status_json(result: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(str(getattr(result, "stdout", "")))
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _registered_service(doc: Mapping[str, Any] | None) -> bool | None:
    if doc is None:
        return None
    if doc.get("registered") is True:
        return True
    units = doc.get("units")
    if isinstance(units, list) and units:
        return True
    if doc.get("registered") is False or isinstance(units, list):
        return False
    return None


def _no_systemd_response(result: Any, doc: Mapping[str, Any] | None) -> bool:
    """辨認 Python／Rust CLI 在 on-demand 模式的正式 service status 回應。"""
    return (int(getattr(result, "rc", -1)) == 2 and doc is not None
            and doc.get("ok") is False and doc.get("error_code") == "NO_SYSTEMD"
            and doc.get("mode") == "on-demand")


def _systemd_not_booted(result: Any) -> bool:
    output = (str(getattr(result, "stdout", "")) + "\n"
              + str(getattr(result, "stderr", ""))).casefold()
    return (int(getattr(result, "rc", -1)) != 0
            and "system has not been booted with systemd" in output)


def _linux_supervisor_query(run: Callable[..., Any], inst: Any, m: Any,
                            repo_root: Path) -> tuple[bool, dict[str, Any]]:
    """唯讀查詢 system 與 user unit files；任一查詢不明即 fail closed。"""
    queries = (
        ("system", ["systemctl", "list-unit-files", "--type=service", "--no-legend", "--no-pager"]),
        ("user", ["systemctl", "--user", "list-unit-files", "--type=service", "--no-legend", "--no-pager"]),
    )
    evidence: dict[str, Any] = {"platform": "linux", "queries": []}
    registered: list[dict[str, str]] = []
    for scope, argv in queries:
        try:
            result = _run(run, argv, inst, m, timeout=15.0, repo_root=repo_root)
        except FileNotFoundError:
            evidence["systemctl_unavailable"] = True
            return True, evidence
        except Exception as exc:  # noqa: BLE001 - 無法查證時不得宣稱無監管
            evidence["queries"].append({"scope": scope, "error": type(exc).__name__})
            return False, evidence
        rc = int(getattr(result, "rc", -1))
        timed_out = bool(getattr(result, "timed_out", False))
        output = str(getattr(result, "stdout", ""))
        evidence["queries"].append({"scope": scope, "rc": rc, "timed_out": timed_out,
                                    "stdout_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest()})
        if _systemd_not_booted(result):
            evidence.setdefault("systemd_not_booted_scopes", []).append(scope)
            continue
        if rc != 0 or timed_out:
            return False, evidence
        for line in output.splitlines():
            columns = line.split()
            if not columns:
                continue
            unit = columns[0].casefold()
            if unit.startswith("serialwrap") and unit.endswith((".service", ".timer", ".path", ".target")):
                state = columns[1].casefold() if len(columns) > 1 else "unknown"
                if state in {"enabled", "enabled-runtime", "static"}:
                    registered.append({"scope": scope, "unit": columns[0], "state": state})
                elif state not in {"disabled", "masked", "masked-runtime", "indirect"}:
                    evidence["unknown_unit_state"] = {"unit": columns[0], "state": state}
                    return False, evidence
    evidence["registered_units"] = registered
    return not registered, evidence


def _windows_supervisor_query(run: Callable[..., Any], inst: Any, m: Any,
                              repo_root: Path) -> tuple[bool, dict[str, Any]]:
    """列出所有排程工作並精確過濾 serialwrap 名稱。"""
    argv = ["schtasks", "/Query", "/FO", "CSV", "/NH"]
    try:
        result = _run(run, argv, inst, m, timeout=15.0, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 - 查詢不明時不得宣稱無監管
        return False, {"platform": "windows", "error": type(exc).__name__}
    rc = int(getattr(result, "rc", -1))
    output = str(getattr(result, "stdout", ""))
    evidence: dict[str, Any] = {"platform": "windows", "rc": rc,
                                "timed_out": bool(getattr(result, "timed_out", False)),
                                "stdout_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest()}
    if rc != 0 or evidence["timed_out"]:
        return False, evidence
    try:
        rows = list(csv.reader(output.splitlines()))
    except csv.Error:
        return False, evidence
    tasks = [row[0] for row in rows if row and "serialwrap" in row[0].casefold()]
    evidence["registered_tasks"] = tasks
    return not tasks, evidence


def _supervision_attestation(run: Callable[..., Any], inst: Any, m: Any, side: Any, *,
                             repo_root: Path) -> tuple[dict[str, Any] | None, str | None]:
    """合併 CLI 模式與唯讀平台查詢，只有兩者皆證實 on-demand 才簽章。"""
    supervision = _run(run, [str(side.cli_exe), "supervision-mode"], inst, m,
                       timeout=15.0, repo_root=repo_root)
    mode = str(getattr(supervision, "stdout", "")).strip()
    if int(getattr(supervision, "rc", -1)) != 0 or mode != "on-demand":
        return None, "HARNESS_FAILURE_RETENTION_UNVERIFIED: supervision-mode is not on-demand"

    service = _run(run, [str(side.cli_exe), "service", "status"], inst, m,
                   timeout=15.0, repo_root=repo_root)
    service_doc = _status_json(service)
    registered = _registered_service(service_doc)
    if registered is True:
        return None, "HARNESS_FAILURE_RETENTION_UNVERIFIED: registered supervisor"
    service_absent = (
        int(getattr(service, "rc", -1)) == 0 and service_doc is not None
        and service_doc.get("ok") is True and service_doc.get("mode") == "on-demand"
        and registered is False
    )
    if not service_absent and not _no_systemd_response(service, service_doc):
        return None, "HARNESS_FAILURE_RETENTION_UNVERIFIED: unknown service status"

    if m.platform == "windows":
        absent, platform_evidence = _windows_supervisor_query(run, inst, m, repo_root)
    else:
        absent, platform_evidence = _linux_supervisor_query(run, inst, m, repo_root)
    if not absent:
        return None, "HARNESS_FAILURE_RETENTION_UNVERIFIED: registered supervisor or platform query unknown"
    service_evidence = {
        "rc": int(getattr(service, "rc", -1)),
        "response": service_doc,
    }
    return ({"mode": "on-demand", "auto_restart_disabled": True,
             "evidence": {"service_status": service_evidence,
                          "platform_query": platform_evidence}}, None)


def _duration_flag_present(args: list[str]) -> bool:
    return any(arg == "--duration" or arg.startswith("--duration=") for arg in args)


def _duration_seconds(value: str) -> float | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", value.strip().casefold())
    if match is None:
        return None
    factors = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    try:
        return float(match.group(1)) * factors[match.group(2)]
    except (KeyError, ValueError):
        return None


def _longrun_input_error(m: Any, acceptance: Mapping[str, Any], extra_args: list[str]) -> str | None:
    workload = acceptance.get("workload")
    if not isinstance(workload, Mapping):
        return "longrun workload is missing"
    if not isinstance(workload.get("engine_case"), str) or not workload["engine_case"].strip():
        return "longrun engine_case is missing"
    minimum = workload.get("min_continuous_duration_s")
    interval = workload.get("observer_interval_s")
    if (not isinstance(minimum, (int, float)) or isinstance(minimum, bool) or minimum <= 0
            or not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0):
        return "longrun duration or observer interval is invalid"
    if m.platform == "windows":
        probe_interval = workload.get("windows_excl_probe_interval_s")
        if (not isinstance(probe_interval, (int, float)) or isinstance(probe_interval, bool)
                or probe_interval <= 0):
            return "Windows exclusive probe interval is invalid"

    requested = math.ceil(float(minimum) / 3600.0) * 3600.0
    for index, arg in enumerate(extra_args):
        if arg.startswith("--duration="):
            parsed = _duration_seconds(arg.partition("=")[2])
            if parsed is None:
                return "longrun duration argument is invalid"
            requested = parsed
        elif arg == "--duration":
            if index + 1 >= len(extra_args):
                return "longrun duration argument is missing"
            parsed = _duration_seconds(extra_args[index + 1])
            if parsed is None:
                return "longrun duration argument is invalid"
            requested = parsed
    if requested < minimum:
        return "longrun duration is shorter than the frozen minimum"
    timeout_s = m.suite.timeout_s
    if (not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool)
            or timeout_s <= requested):
        return "suite timeout does not cover the longrun duration"
    return None


def _baseline_gate_path(inst: Any) -> Path | None:
    """取本輪或最近既有 A 側 gate，支援 B→A reverify 順序。"""
    base = Path(inst.root).parent
    round_number = int(inst.round)
    candidates = [base / f"r{number}-A" / "report" / "release-gate.json"
                  for number in range(round_number, 0, -1)]
    return next((path for path in candidates
                 if path.is_file() and _read_json_object(path) is not None), None)


def _wait_observer_header(proc: Any, header_path: Path, boards: list[str], *,
                          require_paths: bool, timeout_s: float = _OBSERVER_STARTUP_TIMEOUT_S) -> bool:
    """等 observer 寫完含目標板綁定的第一行，或在程序退出／逾時時失敗。"""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if proc.poll() is not None:
                return False
            first = Path(header_path).read_text(encoding="utf-8").splitlines()[0]
            header = json.loads(first)
        except (OSError, IndexError, TypeError, ValueError):
            header = None
        if isinstance(header, Mapping) and header.get("kind") == "header":
            binding = header.get("device_binding")
            if isinstance(binding, Mapping) and set(boards).issubset(binding):
                if not require_paths or all(
                    isinstance(binding.get(board), str) and binding[board].strip()
                    for board in boards
                ):
                    return proc.poll() is None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _copy_wal(wal_path: str | Path, report_dir: Path) -> Path | None:
    """只複製 serialwrap WAL 與 mirror 檔，拒絕輸出目錄位於來源樹內。"""
    source = Path(wal_path)
    if source.is_file():
        source = source.parent
    if not source.is_dir():
        return None
    source = source.resolve()
    report_dir = Path(report_dir).resolve()
    try:
        report_dir.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("report directory is inside WAL source")

    files: list[Path] = []
    for pattern in ("raw.wal.ndjson*", "raw.mirror.log*"):
        files.extend(path for path in sorted(source.glob(pattern))
                     if path.is_file() and not path.is_symlink())
    if not files:
        return None
    destination = report_dir / "wal"
    destination.mkdir(parents=True, exist_ok=False)
    for path in files:
        shutil.copy2(path, destination / path.name)
    return destination


def _stop_observer(proc: Any, notes: list[str]) -> None:
    """以 terminate→wait 10 秒→kill 的順序停止 observer。"""
    if proc is None:
        return
    terminate_error: Exception | None = None
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception as exc:  # noqa: BLE001
        terminate_error = exc
    try:
        proc.wait(timeout=10)
    except Exception as wait_error:  # noqa: BLE001 - 逾時或 terminate 失敗時嘗試 kill
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception as kill_exc:  # noqa: BLE001
            notes.append(f"observer stop failed: {type(kill_exc).__name__}: {kill_exc}")
        else:
            source = terminate_error or wait_error
            notes.append(f"observer needed kill after stop failure: {type(source).__name__}")
    if terminate_error is not None and not any("observer stop failed" in item for item in notes):
        notes.append(f"observer terminate failed: {type(terminate_error).__name__}: {terminate_error}")


def _deployed_identity(report_dir: Path, release_gate: Mapping[str, Any] | None,
                       endpoint: str, *, supervisor_absent: bool) -> dict[str, Any]:
    report = _read_json_object(report_dir / "realhw" / "report.json") or {}
    meta = report.get("meta") if isinstance(report.get("meta"), dict) else {}
    gate_run = release_gate.get("run") if isinstance(release_gate, Mapping) else {}
    if not isinstance(gate_run, Mapping):
        gate_run = {}
    report_cli = meta.get("cli_identity") or {}
    gate_cli = gate_run.get("cli_identity") or {}
    cli_identity = report_cli or gate_cli
    daemon_identity = meta.get("daemon_identity") or {}
    report_deployed = meta.get("deployed_content")
    gate_deployed = gate_run.get("deployed_content")
    if report_deployed is not None and gate_deployed is not None and report_deployed != gate_deployed:
        deployed_content: Any = [report_deployed, gate_deployed]
    else:
        deployed_content = report_deployed if report_deployed is not None else gate_deployed
    return {
        "cli": dict(cli_identity) if isinstance(cli_identity, Mapping) else {},
        "report_cli_identity": dict(report_cli) if isinstance(report_cli, Mapping) else {},
        "gate_cli_identity": dict(gate_cli) if isinstance(gate_cli, Mapping) else {},
        "daemon": dict(daemon_identity) if isinstance(daemon_identity, Mapping) else {},
        "daemon_version": gate_run.get("daemon_version") or daemon_identity.get("version"),
        "release_manifest_sha256": meta.get("release_manifest_sha256"),
        "deployed_content": deployed_content,
        "explicit_endpoint": endpoint,
        "supervisor_absent": supervisor_absent,
    }


def _write_config(m: Any, side: Any, inst: Any, repo_root: Path | None = None) -> Path:
    template = m.suite.realhw_config_template
    if template is None:
        raise ValueError("realhw config template is missing")
    source = _frozen_path(m, template, repo_root)
    config = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("realhw config template must be a JSON object")
    config = dict(config)
    config["serialwrap_exe"] = str(side.cli_exe)
    config["endpoint"] = m.endpoint
    bench_lock = m.platform_settings.get("bench_lock_path")
    if bench_lock is not None:
        config["bench_lock_path"] = str(bench_lock)
    elif m.platform == "windows" and not str(config.get("bench_lock_path", "")).strip():
        raise ValueError("Windows L3 requires the shared bench_lock_path")
    destination = Path(inst.root) / "realhw-config.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
    return destination


def run(m: Any, side: Any, inst: Any, *, run: Callable[..., Any], spawn: Callable[..., Any],
        now: Callable[[], str], repo_root: Path | None = None) -> SuiteRun:
    """執行 side-specific realhw；longrun 另包 observer、WAL 與 outer gate。"""
    del now
    repo_root = _repo_root(m, repo_root)

    def execute(argv: list[str], *, timeout: float | None = None) -> Any:
        return _run(run, argv, inst, m, timeout=timeout, repo_root=repo_root)

    report_dir = Path(inst.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    if not isinstance(m.endpoint, str) or not m.endpoint.strip():
        return SuiteRun("INFRA_ERROR", None, report_dir, {},
                        ("HARNESS_SUITE_INFRA_ERROR: L3 requires an explicit endpoint",))
    try:
        config_path = _write_config(m, side, inst, repo_root)
        acceptance = _load_acceptance(m, repo_root)
    except Exception as exc:  # noqa: BLE001 - 無法載入凍結輸入不可執行
        return SuiteRun("INFRA_ERROR", None, report_dir, {},
                        (f"HARNESS_SUITE_INFRA_ERROR: invalid realhw inputs: {type(exc).__name__}: {exc}",))

    tier = str(m.suite.tier or "")
    extra_args = [str(arg) for arg in m.suite.extra_args]
    workload = acceptance.get("workload")
    if tier == "longrun":
        input_error = _longrun_input_error(m, acceptance, extra_args)
        if input_error is not None:
            return SuiteRun("INFRA_ERROR", None, report_dir, {},
                            (f"HARNESS_SUITE_INFRA_ERROR: {input_error}",))

    attestation, attestation_error = _supervision_attestation(
        run, inst, m, side, repo_root=repo_root)
    if attestation is None:
        return SuiteRun("INFRA_ERROR", None, report_dir, {}, (str(attestation_error),))
    attestation_path = report_dir / "supervision-attest.json"
    try:
        attestation_path.write_text(
            json.dumps(attestation, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8", newline="\n")
    except OSError as exc:
        return SuiteRun("INFRA_ERROR", None, report_dir, {},
                        (f"HARNESS_FAILURE_RETENTION_UNVERIFIED: cannot write attestation: {exc}",))

    try:
        status_result = execute(_endpoint_argv(side.cli_exe, m.endpoint, "daemon", "status"),
                                timeout=15.0)
        if int(getattr(status_result, "rc", -1)) != 0 or bool(getattr(status_result, "timed_out", False)):
            raise ValueError("daemon status failed")
        daemon_status = _status_json(status_result)
        if daemon_status is None:
            raise ValueError("daemon status is not a JSON object")
    except Exception as exc:  # noqa: BLE001 - 無法取得 observer/WAL 來源資訊
        return SuiteRun("INFRA_ERROR", None, report_dir, {},
                        (f"HARNESS_SUITE_INFRA_ERROR: daemon preflight failed: {type(exc).__name__}: {exc}",))

    realhw_dir = report_dir / "realhw"
    engine_argv = [sys.executable, "-m", "realhw", "--config", str(config_path),
                   "--tier", tier, "--report-dir", str(realhw_dir)]
    case_ids = tuple(m.case_ids)
    if len(case_ids) == 1:
        # realhw 的正式 selector 名稱是 --only（不是計畫草稿中的 --case）。
        engine_argv.extend(("--only", case_ids[0]))
    elif case_ids:
        notes.append("realhw --only accepts one id; ran the complete configured tier")
    if tier == "longrun":
        if not _duration_flag_present(extra_args):
            min_duration_s = int(workload["min_continuous_duration_s"])
            extra_args.extend(("--duration", f"{math.ceil(min_duration_s / 3600)}h"))
    engine_argv.extend(extra_args)

    if tier != "longrun":
        try:
            result = execute(engine_argv, timeout=m.suite.timeout_s)
        except Exception as exc:  # noqa: BLE001
            return SuiteRun("INFRA_ERROR", None, report_dir, {},
                            (f"HARNESS_SUITE_INFRA_ERROR: realhw launch failed: {type(exc).__name__}: {exc}",))
        rc = int(getattr(result, "rc", -1))
        if bool(getattr(result, "timed_out", False)) or rc not in (0, 1):
            return SuiteRun("INFRA_ERROR", rc, report_dir, {},
                            (f"HARNESS_SUITE_INFRA_ERROR: realhw rc={rc} timed_out={bool(getattr(result, 'timed_out', False))}",))
        report = _read_json_object(realhw_dir / "report.json")
        if report is None or not isinstance(report.get("meta"), Mapping):
            return SuiteRun("INFRA_ERROR", rc, report_dir, {},
                            ("HARNESS_SUITE_INFRA_ERROR: realhw report is missing or invalid",))
        identity = _deployed_identity(report_dir, None, str(m.endpoint or ""),
                                      supervisor_absent=True)
        return SuiteRun("PASS" if rc == 0 else "FAIL", rc, report_dir, identity, tuple(notes))

    baseline_gate: Path | None = None
    infra_notes: list[str] = []
    if side.role != "baseline":
        baseline_gate = _baseline_gate_path(inst)
        if baseline_gate is None:
            return SuiteRun("INFRA_ERROR", None, report_dir, {},
                            ("HARNESS_SUITE_INFRA_ERROR: no existing A-side release gate",))

    observer_path = report_dir / "observer.ndjson"
    observer_argv = [sys.executable, "-m", "realhw.acceptance.observer",
                     "--serialwrap-exe", str(side.cli_exe), "--endpoint", str(m.endpoint),
                     "--boards", ",".join(m.dut.boards),
                     "--interval-s", str(workload["observer_interval_s"]), "--out", str(observer_path),
                     "--supervision-attest", str(attestation_path)]
    excl_dir = report_dir / "excl"
    if m.platform == "windows":
        observer_argv.extend(("--excl-dir", str(excl_dir), "--excl-probe-interval-s",
                              str(workload["windows_excl_probe_interval_s"])))
    observer_env = dict(inst.env)
    existing_pythonpath = observer_env.get("PYTHONPATH", "")
    observer_env["PYTHONPATH"] = str(repo_root) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")
    cleanup_notes: list[str] = []
    pre_violated = False
    abort_before_gate = False
    observer = None
    try:
        try:
            observer = spawn(observer_argv, env=observer_env, cwd=report_dir)
        except Exception as exc:  # noqa: BLE001
            infra_notes.append(
                f"HARNESS_SUITE_INFRA_ERROR: observer launch failed: {type(exc).__name__}: {exc}")
            abort_before_gate = True

        if observer is not None and not abort_before_gate:
            if not _wait_observer_header(
                    observer, observer_path, list(m.dut.boards),
                    require_paths=m.platform == "windows"):
                infra_notes.append("HARNESS_SUITE_INFRA_ERROR: observer exited or header was not ready")
                abort_before_gate = True

        if m.platform == "windows" and observer is not None and not abort_before_gate:
            pre_argv = [sys.executable, "-m", "realhw.acceptance.winexcl", "handoff", "--phase", "pre",
                        "--serialwrap-exe", str(side.cli_exe), "--endpoint", str(m.endpoint),
                        "--boards", ",".join(m.dut.boards),
                        "--observer-header", str(observer_path), "--excl-dir", str(excl_dir)]
            try:
                pre = execute(pre_argv, timeout=m.suite.timeout_s)
                pre_rc = int(getattr(pre, "rc", -1))
            except Exception as exc:  # noqa: BLE001
                infra_notes.append(f"HARNESS_SUITE_INFRA_ERROR: winexcl pre failed: {type(exc).__name__}: {exc}")
                abort_before_gate = True
            else:
                if bool(getattr(pre, "timed_out", False)) or pre_rc == 4:
                    infra_notes.append("HARNESS_SUITE_INFRA_ERROR: Windows exclusive preflight is insufficient")
                    abort_before_gate = True
                elif pre_rc == 1:
                    pre_violated = True
                    notes.append("Windows exclusive preflight found a holder; the gate evaluates retained evidence")
                elif pre_rc != 0:
                    infra_notes.append(f"HARNESS_SUITE_INFRA_ERROR: Windows exclusive preflight rc={pre_rc}")
                    abort_before_gate = True

        if observer is not None and not abort_before_gate and not pre_violated:
            try:
                engine_result = execute(engine_argv, timeout=m.suite.timeout_s)
                engine_rc = int(getattr(engine_result, "rc", -1))
                if bool(getattr(engine_result, "timed_out", False)) or engine_rc not in (0, 1):
                    infra_notes.append(
                        f"HARNESS_SUITE_INFRA_ERROR: realhw rc={engine_rc} timed_out={bool(getattr(engine_result, 'timed_out', False))}")
            except Exception as exc:  # noqa: BLE001 - 仍須保存 post handoff 與 gate 證據
                infra_notes.append(f"HARNESS_SUITE_INFRA_ERROR: realhw launch failed: {type(exc).__name__}: {exc}")

        if observer is not None:
            try:
                if observer.poll() is not None:
                    infra_notes.append("HARNESS_SUITE_INFRA_ERROR: observer exited before suite cleanup")
            except Exception as exc:  # noqa: BLE001
                infra_notes.append(f"HARNESS_SUITE_INFRA_ERROR: observer status unknown: {type(exc).__name__}: {exc}")

        if (m.platform == "windows" and observer is not None
                and not abort_before_gate and not pre_violated):
            post_argv = [sys.executable, "-m", "realhw.acceptance.winexcl", "handoff", "--phase", "post",
                         "--serialwrap-exe", str(side.cli_exe), "--endpoint", str(m.endpoint),
                         "--boards", ",".join(m.dut.boards),
                         "--observer-header", str(observer_path), "--excl-dir", str(excl_dir)]
            try:
                post = execute(post_argv, timeout=m.suite.timeout_s)
                if int(getattr(post, "rc", -1)) not in (0, 1, 4) or bool(getattr(post, "timed_out", False)):
                    infra_notes.append(f"HARNESS_SUITE_INFRA_ERROR: Windows exclusive post rc={getattr(post, 'rc', -1)}")
            except Exception as exc:  # noqa: BLE001
                infra_notes.append(f"HARNESS_SUITE_INFRA_ERROR: winexcl post failed: {type(exc).__name__}: {exc}")
    finally:
        _stop_observer(observer, cleanup_notes)
    infra_notes.extend(cleanup_notes)
    if abort_before_gate:
        return SuiteRun("INFRA_ERROR", None, report_dir, {}, tuple([*notes, *infra_notes]))

    wal_dir: Path | None = None
    wal_path = daemon_status.get("wal_path")
    if isinstance(wal_path, str) and wal_path:
        try:
            wal_dir = _copy_wal(wal_path, report_dir)
            if wal_dir is None:
                infra_notes.append("HARNESS_SUITE_INFRA_ERROR: daemon WAL files are missing")
        except (OSError, ValueError) as exc:
            infra_notes.append(f"HARNESS_SUITE_INFRA_ERROR: cannot copy daemon WAL: {exc}")

    acceptance_path = _frozen_path(m, m.acceptance, repo_root)
    gate_path = report_dir / "release-gate.json"
    case_dir = realhw_dir / str(workload["engine_case"])
    gate_argv = [sys.executable, "-m", "realhw.acceptance.gate",
                 "--report-dir", str(realhw_dir), "--case-dir", str(case_dir),
                 "--acceptance", str(acceptance_path), "--platform", str(m.platform),
                 "--mode", "baseline" if side.role == "baseline" else "candidate",
                 "--observer", str(observer_path)]
    gate_argv.extend(("--wal-dir", str(wal_dir or (report_dir / "wal"))))
    if baseline_gate is not None:
        gate_argv.extend(("--baseline-gate", str(baseline_gate)))
    if m.platform == "windows":
        gate_argv.extend(("--excl-evidence", str(excl_dir)))
    gate_argv.extend(("--out", str(gate_path)))
    try:
        gate_result = execute(gate_argv, timeout=m.suite.timeout_s)
    except Exception as exc:  # noqa: BLE001
        return SuiteRun("INFRA_ERROR", None, report_dir, {},
                        tuple([*notes, *infra_notes,
                               f"HARNESS_SUITE_INFRA_ERROR: gate launch failed: {type(exc).__name__}: {exc}"]))
    gate_rc = int(getattr(gate_result, "rc", -1))
    release_gate = _read_json_object(gate_path)
    identity = _deployed_identity(report_dir, release_gate, str(m.endpoint or ""),
                                  supervisor_absent=True)
    if gate_rc not in _GATE_NORMAL_RCS or bool(getattr(gate_result, "timed_out", False)):
        infra_notes.append(f"HARNESS_SUITE_INFRA_ERROR: gate rc={gate_rc}")
    if release_gate is None:
        infra_notes.append("HARNESS_SUITE_INFRA_ERROR: release-gate.json is missing or invalid")
    else:
        verdict = release_gate.get("release_gate_result")
        expected_rc = _GATE_EXIT_CODES.get(verdict) if isinstance(verdict, str) else None
        if expected_rc is None or gate_rc != expected_rc:
            infra_notes.append(
                f"HARNESS_SUITE_INFRA_ERROR: gate rc/verdict mismatch: rc={gate_rc} verdict={verdict!r}")

    if infra_notes or release_gate is None:
        status = "INFRA_ERROR"
    else:
        verdict = release_gate.get("release_gate_result")
        if verdict == "NOT_APPLICABLE_BASELINE" and side.role != "baseline":
            status = "INFRA_ERROR"
            infra_notes.append("HARNESS_SUITE_INFRA_ERROR: candidate gate returned baseline-only verdict")
        elif verdict in {"PASS", "NOT_APPLICABLE_BASELINE"}:
            status = "PASS"
        elif verdict == "FAIL":
            status = "FAIL"
        else:
            status = "INFRA_ERROR"
    return SuiteRun(status, gate_rc, report_dir, identity, tuple([*notes, *infra_notes]))


__all__ = ["run"]
