"""L1.5 func-test suite adapter。"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Callable

from . import REPORT_ENDPOINTS, SuiteRun, _read_json_object, _repo_root, _write_json


def _case_restarts_dut(entry: Mapping[str, Any], repo_root: Path) -> bool:
    """只把案例明確宣告的 DUT reboot/restart 視為世代變更例外。"""
    if any(entry.get(key) is True for key in ("dut_restart", "dut_reboot", "restart_dut", "reboot_dut")):
        return True
    source_ref = entry.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref:
        return False
    source = Path(source_ref)
    source = source if source.is_absolute() else repo_root / source
    if source.suffix.casefold() not in {".yaml", ".yml"}:
        return False
    try:
        import yaml
    except ImportError:
        return False
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return False

    def contains_reboot(value: Any) -> bool:
        if isinstance(value, Mapping):
            action = value.get("action")
            names = {"logical_reboot", "dut_reboot", "dut_restart", "dut_spawn",
                     "restart_dut", "reboot_dut"}
            action_name = str(value.get("action_name", "")).casefold()
            action_kind = str(action.get("kind", "")).casefold() if isinstance(action, Mapping) else ""
            if action_name in names or action_kind in names or str(action).casefold() in names:
                return True
            return any(contains_reboot(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(contains_reboot(item) for item in value)
        return False

    return contains_reboot(document)


def _classifiable_endpoint(value: Any) -> bool:
    """只保留釋放探針能檢查的 socket 路徑或 URI。"""
    if not isinstance(value, str) or not value.strip():
        return False
    endpoint = value.strip()
    if endpoint.startswith(("unix://", "tcp://")):
        return True
    return Path(endpoint).is_absolute()


def _case_groups(case_ids: Sequence[str], case_rows: Mapping[str, Any]) -> tuple[list[tuple[str, list[str]]], list[str]]:
    groups: dict[str, list[str]] = {"cases": [], "scenarios": []}
    unsupported: list[str] = []
    for case_id in case_ids:
        row = case_rows.get(case_id)
        source_ref = row.get("source_ref", "") if isinstance(row, Mapping) else ""
        source = str(source_ref)
        if source.startswith("func-test/cases/") and Path(source).suffix.casefold() in {".yaml", ".yml"}:
            groups["cases"].append(case_id)
        elif source.startswith("func-test/scenarios/") and Path(source).suffix.casefold() in {".yaml", ".yml"}:
            groups["scenarios"].append(case_id)
        else:
            unsupported.append(case_id)
    return [(kind, ids) for kind, ids in groups.items() if ids], unsupported


def _ordered_unique(values: Sequence[Any]) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value) if value is not None else ""
        if text and text not in output:
            output.append(text)
    return output


def _identity(artifacts: Mapping[str, Any], transport: Mapping[str, Any],
              tool: Mapping[str, Any], deployed: list[Any], *, evidence_layer: str | None = None) -> dict[str, Any]:
    daemon = dict(artifacts.get("daemon") or {})
    # func-test 舊報告用 package_tree_sha256 表示 Python daemon 套件樹；
    # 共用身分核對介面使用 tree_sha256，保留原欄位供診斷。
    if daemon.get("tree_sha256") is None and daemon.get("package_tree_sha256") is not None:
        daemon["tree_sha256"] = daemon["package_tree_sha256"]
    identity = {
        "cli": dict(artifacts.get("cli") or {}),
        "daemon": daemon,
        "release_manifest_sha256": artifacts.get("release_manifest_sha256"),
        "transport": dict(transport),
        "tool": dict(tool),
        "deployed_content": deployed,
    }
    if evidence_layer is not None:
        identity["evidence_layer"] = evidence_layer
    return identity


def run(m: Any, side: Any, inst: Any, *, run: Callable[..., Any], spawn: Callable[..., Any],
        now: Callable[[], str], probe: Any = None, repo_root: Path | None = None) -> SuiteRun:
    """以隔離 env 執行 bridge 模式，並驗證每案產生的證據樹。"""
    del spawn, now
    repo_root = _repo_root(m, repo_root)
    report_dir = Path(inst.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []

    try:
        from tools.stage3 import ab_manifest
        from tools.stage3.release_check import default_probe

        _, case_rows = ab_manifest.required_cases(m, repo_root=repo_root)
        case_ids = tuple(sorted(m.case_ids))
    except Exception as exc:  # noqa: BLE001 - 缺少凍結案例資料不可當成通過
        return SuiteRun("INFRA_ERROR", None, report_dir, {},
                        (f"HARNESS_SUITE_INFRA_ERROR: cannot load L1.5 case set: {type(exc).__name__}: {exc}",))
    if not case_ids:
        return SuiteRun("INFRA_ERROR", None, report_dir, {}, ("HARNESS_SUITE_INFRA_ERROR: empty L1.5 case set",))

    base_argv = [
        sys.executable,
        str(repo_root / "func-test" / "runner.py"),
        "--mode", "bridge",
        "--dut-mode", "docker" if m.dut.kind == "docker" else "local-process",
        "--report-dir", str(report_dir),
    ]
    selftest = m.suite.selftest_report
    if selftest is None:
        return SuiteRun("INFRA_ERROR", None, report_dir, {},
                        ("HARNESS_SUITE_INFRA_ERROR: L1.5 self-test report is not configured",))
    selftest_path = Path(selftest.path)
    if not selftest_path.is_absolute():
        selftest_path = repo_root / selftest_path
    base_argv.extend(("--selftest-report", str(selftest_path)))

    if m.dut.kind == "docker":
        image = str(m.dut.image or "").strip()
        digest = str(m.dut.image_digest or "").strip()
        if not image or not digest:
            return SuiteRun("INFRA_ERROR", None, report_dir, {},
                            ("HARNESS_SUITE_INFRA_ERROR: docker image and digest are required",))
        digest = digest if digest.startswith("sha256:") else f"sha256:{digest}"
        pinned_image = image if "@" in image else f"{image}@{digest}"
        if "@" in image and image.rsplit("@", 1)[1] != digest:
            return SuiteRun("INFRA_ERROR", None, report_dir, {},
                            ("HARNESS_SUITE_INFRA_ERROR: docker image digest does not match manifest",))
        base_argv.extend(("--dut-image", pinned_image))

    if m.platform == "windows":
        pair = m.dut.com_pair or {}
        daemon_com = pair.get("daemon_side") if isinstance(pair, Mapping) else None
        bridge_com = pair.get("bridge_side") if isinstance(pair, Mapping) else None
        transport_modes = {"com0com-pair": "virtual_com0com", "physical-usb-uart": "physical_usb_uart"}
        transport_mode = transport_modes.get(str(m.dut.transport))
        if (not isinstance(daemon_com, str) or not daemon_com.strip()
                or not isinstance(bridge_com, str) or not bridge_com.strip()
                or transport_mode is None):
            return SuiteRun("INFRA_ERROR", None, report_dir, {},
                            ("HARNESS_SUITE_INFRA_ERROR: Windows L1.5 requires a supported COM pair and transport",))
        base_argv.extend(("--daemon-com", daemon_com, "--bridge-com", bridge_com,
                          "--transport-mode", transport_mode))

    groups, unsupported = _case_groups(case_ids, case_rows)
    infra_notes: list[str] = [f"unsupported L1.5 source_ref: {case_id}" for case_id in unsupported]
    if not groups:
        return SuiteRun("INFRA_ERROR", None, report_dir, {},
                        tuple([*notes, *infra_notes, "HARNESS_SUITE_INFRA_ERROR: no runnable L1.5 cases"]))

    argv = list(base_argv)
    for kind, group_ids in groups:
        selector = "--scenario-case" if kind == "scenarios" else "--case"
        for case_id in group_ids:
            argv.extend((selector, case_id))
    argv.extend(str(arg) for arg in m.suite.extra_args)
    results: list[Any] = []
    try:
        results.append(run(argv, env=inst.env, timeout=m.suite.timeout_s, cwd=repo_root))
    except Exception as exc:  # noqa: BLE001 - 子程序啟動失敗不得當成測試結果
        infra_notes.append(f"func-test launch failed: {type(exc).__name__}: {exc}")
    rcs = [int(getattr(item, "rc", -1)) for item in results]
    timed_out = any(bool(getattr(item, "timed_out", False)) for item in results)
    invalid_rcs = [value for value in rcs if value not in (0, 1)]
    if timed_out or invalid_rcs:
        infra_notes.append(f"func-test rc={rcs} timed_out={timed_out}")
    rc = next((value for value in rcs if value != 0), 0) if results else None

    cases_with_restart = {
        case_id for case_id in case_ids
        if _case_restarts_dut(case_rows.get(case_id, {}), repo_root)
    }
    endpoints: list[str] = []
    device_paths = list(m.release.device_paths)
    lock_paths = list(m.release.lock_paths)
    artifacts_by_case: list[Mapping[str, Any]] = []
    comparison_verdicts: list[str] = []
    identity_signatures: list[str] = []
    deployed: list[Any] = []
    expected_cli_logs: set[Path] = set()

    for case_id in case_ids:
        case_root = report_dir / case_id
        bridge_reports = sorted(case_root.glob("*/*/bridge-manifest.json"))
        if not bridge_reports:
            infra_notes.append(f"missing L1.5 report: {case_id}")
            continue
        generations: set[int] = set()
        for bridge_path in bridge_reports:
            run_dir = bridge_path.parent
            expected_cli_logs.add(run_dir / "cli.ndjson")
            run_generations: set[int] = set()
            bridge = _read_json_object(bridge_path)
            if not bridge or bridge.get("closed_cleanly") is not True or bridge.get("failure") is not None:
                infra_notes.append(f"unclean bridge: {case_id}")
            comparison = _read_json_object(run_dir / "comparison.json")
            verdict = comparison.get("verdict") if comparison is not None else None
            if verdict not in {"PASS", "FAIL"}:
                infra_notes.append(f"non-product comparison verdict: {case_id}: {verdict!r}")
            else:
                comparison_verdicts.append(verdict)
            manifest = _read_json_object(run_dir / "manifest.json")
            if manifest is None:
                infra_notes.append(f"missing L1.5 manifest: {case_id}")
                continue
            artifacts = manifest.get("artifacts")
            if not isinstance(artifacts, Mapping):
                infra_notes.append(f"missing L1.5 artifact identity: {case_id}")
                continue
            transport = manifest.get("transport")
            tool = manifest.get("tool")
            artifacts_by_case.append(artifacts)
            daemon_identity = artifacts.get("daemon")
            daemon_identity = daemon_identity if isinstance(daemon_identity, Mapping) else {}
            transport_identity = transport if isinstance(transport, Mapping) else {}
            tool_identity = tool if isinstance(tool, Mapping) else {}
            identity_artifacts = {
                "cli_sha256": (artifacts.get("cli") or {}).get("sha256")
                if isinstance(artifacts.get("cli"), Mapping) else None,
                "daemon_implementation": daemon_identity.get("implementation"),
                "daemon_sha256": daemon_identity.get("sha256"),
                "daemon_tree_sha256": daemon_identity.get("tree_sha256") or daemon_identity.get("package_tree_sha256"),
                "release_manifest_sha256": artifacts.get("release_manifest_sha256"),
                "transport_mode": transport_identity.get("mode"),
                "tool_tree_sha256": tool_identity.get("tree_sha256"),
            }
            signature = json.dumps(identity_artifacts, sort_keys=True)
            identity_signatures.append(signature)
            deployed.append(artifacts.get("deployed_content"))

            wire_path = run_dir / "wire.ndjson"
            try:
                lines = wire_path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            if not lines:
                infra_notes.append(f"missing L1.5 wire evidence: {case_id}")
            for line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    infra_notes.append(f"malformed L1.5 wire evidence: {case_id}")
                    continue
                if isinstance(record, Mapping) and isinstance(record.get("dut_generation"), int):
                    run_generations.add(record["dut_generation"])
            if not run_generations:
                infra_notes.append(f"missing DUT generation evidence: {case_id}")
            generations.update(run_generations)

        if case_id not in cases_with_restart and generations != {1}:
            infra_notes.append(f"DUT reused across runs: {case_id}")

    if identity_signatures and any(item != identity_signatures[0] for item in identity_signatures[1:]):
        infra_notes.append("L1.5 artifact identity differs between cases")
    expected_rc = 1 if "FAIL" in comparison_verdicts else 0
    if rc is not None and rc != expected_rc:
        infra_notes.append(f"func-test rc/verdict mismatch: rc={rc} expected={expected_rc}")
    if not artifacts_by_case:
        identity: dict[str, Any] = {}
    else:
        first_manifest = next((
            _read_json_object(path.parent / "manifest.json")
            for case_id in case_ids
            for path in sorted((report_dir / case_id).glob("*/*/bridge-manifest.json"))
            if _read_json_object(path.parent / "manifest.json") is not None
        ), {})
        identity = _identity(
            artifacts_by_case[0],
            (first_manifest or {}).get("transport", {}),
            (first_manifest or {}).get("tool", {}),
            deployed,
            evidence_layer="L1.5-docker" if m.dut.kind == "docker" else None,
        )

    cli_logs = sorted(report_dir.rglob("cli.ndjson"))
    for missing_log in sorted(expected_cli_logs - set(cli_logs)):
        infra_notes.append(f"missing L1.5 CLI evidence: {missing_log}")
    for cli_path in cli_logs:
        try:
            cli_lines = cli_path.read_text(encoding="utf-8").split("\n")
        except OSError as exc:
            infra_notes.append(f"cannot read L1.5 CLI evidence: {cli_path}: {exc}")
            continue
        cli_endpoints: list[str] = []
        for line in cli_lines:
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                infra_notes.append(f"malformed L1.5 CLI evidence: {cli_path}")
                continue
            argv = record.get("argv") if isinstance(record, Mapping) else None
            if not isinstance(argv, list):
                infra_notes.append(f"missing L1.5 CLI argv evidence: {cli_path}")
                continue
            for index, token in enumerate(argv):
                if not isinstance(token, str):
                    continue
                if token in {"--socket", "--endpoint"} and index + 1 < len(argv):
                    value = argv[index + 1]
                elif token.startswith(("--socket=", "--endpoint=")):
                    value = token.partition("=")[2]
                else:
                    continue
                if _classifiable_endpoint(value):
                    cli_endpoints.append(value)
        if not cli_endpoints:
            infra_notes.append(f"missing L1.5 RPC endpoint evidence: {cli_path}")
        endpoints.extend(cli_endpoints)

    for manifest_path in sorted(report_dir.rglob("manifest.json")):
        manifest = _read_json_object(manifest_path)
        if manifest is None:
            continue
        transport = manifest.get("transport")
        if isinstance(transport, Mapping):
            for key in ("port_id", "daemon_socket_path", "socket_path"):
                value = transport.get(key)
                if _classifiable_endpoint(value):
                    endpoints.append(str(value))
            for key in ("device_path", "device_paths"):
                value = transport.get(key)
                if isinstance(value, str):
                    device_paths.append(value)
                elif isinstance(value, (list, tuple)):
                    device_paths.extend(str(item) for item in value)
            for key in ("lock_path", "lock_paths"):
                value = transport.get(key)
                if isinstance(value, str):
                    lock_paths.append(value)
                elif isinstance(value, (list, tuple)):
                    lock_paths.extend(str(item) for item in value)
        daemon = manifest.get("daemon")
        if isinstance(daemon, Mapping):
            for key in ("socket_path", "rpc_endpoint", "endpoint"):
                value = daemon.get(key)
                if _classifiable_endpoint(value):
                    endpoints.append(str(value))

    endpoints = [value for value in [*m.release.endpoints, *endpoints] if _classifiable_endpoint(value)]

    try:
        _write_json(report_dir / REPORT_ENDPOINTS, {
            "endpoints": _ordered_unique(endpoints),
            "device_paths": _ordered_unique(device_paths),
            "lock_paths": _ordered_unique(lock_paths),
        })
    except OSError as exc:
        infra_notes.append(f"cannot write endpoints.json: {exc}")

    match_paths: list[str] = [str(Path(side.daemon_exe).resolve()), str(Path(side.cli_exe).resolve())]
    daemon_entry = side.manifest_doc.get("components", {}).get("daemon", {}).get("entry", [])
    if isinstance(daemon_entry, (list, tuple)):
        for item in daemon_entry:
            token = str(item)
            if token.endswith(".py"):
                entry_path = Path(token)
                if not entry_path.is_absolute():
                    entry_path = side.bundle_dir / entry_path
                match_paths.append(str(entry_path.resolve()))
    try:
        actual_probe = probe if probe is not None else default_probe()
        pids = actual_probe.pids_by_exe(tuple(_ordered_unique(match_paths)))
        if pids is None:
            infra_notes.append("leaked daemon probe unknown")
        elif pids:
            infra_notes.append(f"leaked daemon processes: {','.join(str(pid) for pid in pids)}")
    except Exception as exc:  # noqa: BLE001 - probe failure cannot prove release
        infra_notes.append(f"leaked daemon probe unknown: {type(exc).__name__}: {exc}")

    notes.extend(infra_notes)
    if infra_notes:
        status = "INFRA_ERROR"
    else:
        status = "FAIL" if "FAIL" in comparison_verdicts else "PASS"
    return SuiteRun(status, rc, report_dir, identity, tuple(notes))


__all__ = ["run"]
