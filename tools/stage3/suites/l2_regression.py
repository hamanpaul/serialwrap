"""L2 TestPilot regression suite adapter。"""

from __future__ import annotations

import shutil
import yaml
from pathlib import Path
from typing import Any, Callable

from . import SuiteRun, _frozen_path, _read_json_object, _repo_root


def _report_dirs(root: Path) -> set[Path]:
    try:
        return {path.resolve() for path in root.glob("tp-*") if path.is_dir()}
    except OSError:
        return set()


def _home_path(value: Any) -> str:
    text = str(value)
    return str(Path(text).expanduser()) if text.startswith("~") else text


def run(m: Any, side: Any, inst: Any, *, run: Callable[..., Any], spawn: Callable[..., Any],
        now: Callable[[], str], repo_root: Path | None = None) -> SuiteRun:
    """複製單次 TestPilot 新報告，並保留其逐案結果供 Task 7 判定。"""
    del spawn, now
    repo_root = _repo_root(m, repo_root)
    report_dir = Path(inst.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    notes: list[str] = []
    template = m.suite.testbed_template
    if template is None:
        return SuiteRun("INFRA_ERROR", None, report_dir, {}, ("HARNESS_SUITE_INFRA_ERROR: testbed template is missing",))

    try:
        source = _frozen_path(m, template, repo_root)
        testbed = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(testbed, dict):
            raise ValueError("testbed template must be a YAML object")
        testbed = dict(testbed)
        testbed["serialwrap_exe"] = str(side.cli_exe)
        if m.endpoint:
            testbed["endpoint"] = str(m.endpoint)
        configured_lock = m.platform_settings.get("bench_lock_path")
        if configured_lock is None:
            configured_lock = testbed.get("bench_lock_path")
        if configured_lock is not None and str(configured_lock).strip():
            testbed["bench_lock_path"] = _home_path(configured_lock)
        elif m.platform == "windows":
            raise ValueError("Windows L2 requires the shared bench_lock_path")
        authorization = m.authorization
        allow_destructive = bool(authorization.allow_destructive) if authorization is not None else False
        authorized_boards = list(authorization.boards) if authorization is not None else []
        testbed["allow_destructive"] = allow_destructive
        testbed["authorized_boards"] = authorized_boards
        if allow_destructive and not authorized_boards:
            raise ValueError("destructive authorization requires at least one authorized board")
        testbed_path = Path(inst.root) / "testbed.yaml"
        testbed_path.parent.mkdir(parents=True, exist_ok=True)
        testbed_path.write_text(yaml.safe_dump(testbed, allow_unicode=True, sort_keys=True),
                                encoding="utf-8", newline="\n")
    except Exception as exc:  # noqa: BLE001 - 無法建立凍結 testbed 不可執行
        return SuiteRun("INFRA_ERROR", None, report_dir, {},
                        (f"HARNESS_SUITE_INFRA_ERROR: invalid testbed template: {type(exc).__name__}: {exc}",))

    env = dict(inst.env)
    home_value = env.get("USERPROFILE") if m.platform == "windows" else env.get("HOME")
    report_home = Path(home_value).expanduser() if isinstance(home_value, str) and home_value else Path.home()
    reports_root = report_home / "b-log" / "regression-reports"
    before = _report_dirs(reports_root)
    argv = ["testpilot", "run", "serialwrap_regression"]
    case_ids = tuple(m.case_ids)
    for case_id in case_ids:
        argv.extend(("--case", str(case_id)))
    argv.extend(str(arg) for arg in m.suite.extra_args)
    env["SERIALWRAP_REGRESSION_TESTBED"] = str(testbed_path)

    try:
        result = run(argv, env=env, timeout=m.suite.timeout_s, cwd=repo_root)
    except Exception as exc:  # noqa: BLE001 - 啟動錯誤屬 harness 失敗
        return SuiteRun("INFRA_ERROR", None, report_dir, {},
                        tuple(notes + [f"HARNESS_SUITE_INFRA_ERROR: TestPilot launch failed: {type(exc).__name__}: {exc}"]))
    rc = int(getattr(result, "rc", -1))
    if bool(getattr(result, "timed_out", False)):
        return SuiteRun("INFRA_ERROR", rc, report_dir, {},
                        tuple(notes + ["HARNESS_SUITE_INFRA_ERROR: TestPilot timed out"]))

    after = _report_dirs(reports_root)
    new_dirs = sorted(after - before, key=lambda path: path.name)
    if len(new_dirs) != 1:
        note = f"testpilot report dir ambiguous: expected one new tp-* directory, found {len(new_dirs)}"
        return SuiteRun("INFRA_ERROR", rc, report_dir, {}, tuple(notes + [note]))

    copied = report_dir / "testpilot"
    try:
        if copied.exists():
            raise FileExistsError(str(copied))
        shutil.copytree(new_dirs[0], copied)
    except OSError as exc:
        return SuiteRun("INFRA_ERROR", rc, report_dir, {},
                        tuple(notes + [f"HARNESS_SUITE_INFRA_ERROR: cannot copy TestPilot report: {exc}"]))

    report = _read_json_object(copied / "report.json")
    if report is None:
        return SuiteRun("INFRA_ERROR", rc, report_dir, {},
                        tuple(notes + ["HARNESS_SUITE_INFRA_ERROR: new TestPilot report has no valid report.json"]))

    meta = report.get("meta") if isinstance(report.get("meta"), dict) else {}
    authorization = m.authorization
    expected_allow_destructive = bool(authorization.allow_destructive) if authorization is not None else False
    expected_authorized_boards = list(authorization.boards) if authorization is not None else []
    if (meta.get("allow_destructive") is not expected_allow_destructive
            or meta.get("authorized_boards") != expected_authorized_boards):
        return SuiteRun("INFRA_ERROR", rc, report_dir, {},
                        tuple(notes + ["HARNESS_SUITE_INFRA_ERROR: TestPilot authorization mismatch"]))
    identity = {
        "cli": dict(meta.get("cli_identity") or {}),
        "daemon_version": (meta.get("daemon_identity") or {}).get("version")
        if isinstance(meta.get("daemon_identity"), dict) else meta.get("version"),
        "release_manifest_sha256": meta.get("release_manifest_sha256"),
        "deployed_content": meta.get("deployed_content"),
    }
    if rc != 0 and not (copied / "report.json").is_file():
        return SuiteRun("INFRA_ERROR", rc, report_dir, identity,
                        tuple(notes + [f"HARNESS_SUITE_INFRA_ERROR: TestPilot rc={rc} without report.json"]))
    return SuiteRun("PASS" if rc == 0 else "FAIL", rc, report_dir, identity, tuple(notes))


__all__ = ["run"]
