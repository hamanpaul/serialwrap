"""Stage 3 Current／Previous 安裝與 UART 回滾演練。"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import ab_manifest, jsonio
from .proc import Runner, run_cmd, run_json
from .reason_codes import HarnessError
from .release_check import PreStopSnapshot, ReleaseProbe, verify_released


STEP_IDS: tuple[str, ...] = (
    "D0-preflight",
    "D1-record",
    "D2-rollback",
    "D3-verify-previous",
    "D4-smoke-previous",
    "D5-roll-forward",
    "D6-verify-current",
    "D7-invalid-artifact-refusal",
    "D8-start-failure-revert",
)


@dataclass(frozen=True)
class DrillConfig:
    """單次回滾演練的部署、板子與逾時設定。"""

    cli: Path
    platform: str
    boards: tuple[str, ...]
    expect_current: str
    expect_previous: str
    out_dir: Path
    forward_argv: tuple[str, ...] = ()
    corrupt_from: Path | None = None
    start_fail_from: Path | None = None
    ready_timeout_s: float = 180.0
    setup_timeout_s: float = 300.0


@dataclass(frozen=True)
class StepResult:
    """記錄一個 D0～D8 步驟的結果與時間範圍。"""

    step_id: str
    ok: bool
    detail: Mapping[str, Any]
    started_at: str
    finished_at: str


@dataclass(frozen=True)
class DrillResult:
    """單次演練結果；報告檔另含 ingest 所需的內容指紋。"""

    verdict: str
    reason_code: str | None
    steps: tuple[StepResult, ...]
    restore: StepResult | None
    report_path: Path


@dataclass(frozen=True)
class _InstallSnapshot:
    current_dir: Path
    previous_dir: Path
    current_doc: Mapping[str, Any]
    previous_doc: Mapping[str, Any]
    current_manifest_sha256: str
    previous_manifest_sha256: str
    current_bundle_tree_sha256: str
    current_runtime_tree_sha256: str
    previous_bundle_tree_sha256: str
    cli_sha256: str
    old_pid: int
    sessions: Mapping[str, Mapping[str, Any]]
    released_sessions: tuple[tuple[str, str | None], ...]


def _sha256_file(path: Path) -> str:
    try:
        return jsonio.sha256_file(path)
    except OSError as exc:
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"無法讀取檔案：{path}: {exc}") from exc


def _bundle_tree_sha256(bundle_dir: Path) -> str:
    return ab_manifest.bundle_tree_sha256(Path(bundle_dir))


def _runtime_tree_sha256(bundle_dir: Path) -> str:
    return ab_manifest.runtime_tree_sha256(Path(bundle_dir))


def _version_matches(output: str, expected: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9.+-]){re.escape(expected)}(?![A-Za-z0-9.+-])", output) is not None


def _valid_pid(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _run_json(
    run: Runner,
    cli: Path,
    args: Sequence[str],
    *,
    timeout_s: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    argv = [str(cli), *args]
    try:
        result, doc = run_json(run, argv, timeout=timeout_s)
    except Exception as exc:
        return {"argv": argv, "error": str(exc), "rc": None, "stderr": "", "stdout": "", "timed_out": False}, None
    return {
        "argv": argv,
        "rc": result.rc,
        "stderr": result.stderr,
        "stdout": result.stdout,
        "timed_out": result.timed_out,
    }, doc


def _run_text(run: Runner, cli: Path, args: Sequence[str], *, timeout_s: float) -> dict[str, Any]:
    argv = [str(cli), *args]
    try:
        result = run(argv, timeout=timeout_s)
    except Exception as exc:
        return {"argv": argv, "error": str(exc), "rc": None, "stderr": "", "stdout": "", "timed_out": False}
    return {
        "argv": argv,
        "rc": result.rc,
        "stderr": result.stderr,
        "stdout": result.stdout,
        "timed_out": result.timed_out,
    }


def _response_ok(result: Mapping[str, Any], doc: Mapping[str, Any] | None) -> bool:
    return result.get("rc") == 0 and result.get("timed_out") is False and doc is not None and doc.get("ok") is True


def _query_ok(result: Mapping[str, Any], doc: Mapping[str, Any] | None) -> bool:
    return result.get("rc") == 0 and result.get("timed_out") is False and doc is not None


def _sessions(doc: Mapping[str, Any] | None) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if doc is None or not isinstance(doc.get("sessions"), list):
        return {}, ["session list 缺少 sessions 陣列"]
    by_com: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for item in doc["sessions"]:
        if not isinstance(item, dict) or not isinstance(item.get("com"), str) or not item["com"]:
            errors.append("session row 缺少有效 com")
            continue
        com = item["com"]
        if com in by_com:
            errors.append(f"session list 含重複 selector：{com}")
            continue
        by_com[com] = dict(item)
    return by_com, errors


def _board_rows(
    sessions: Mapping[str, Mapping[str, Any]], boards: Sequence[str]
) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    selected: dict[str, Mapping[str, Any]] = {}
    errors: list[str] = []
    for board in boards:
        row = sessions.get(board)
        if row is None:
            errors.append(f"{board}: session 不存在")
        else:
            selected[board] = row
    return selected, errors


def _pairing_check(doc: Mapping[str, Any] | None) -> tuple[bool, str]:
    if doc is None or not isinstance(doc.get("checks"), list):
        return False, "doctor.checks 缺少陣列"
    rows = [row for row in doc["checks"] if isinstance(row, dict) and row.get("check") == "version_pairing"]
    if len(rows) != 1:
        return False, "doctor.checks 必須恰有一筆 version_pairing"
    if rows[0].get("ok") is not True:
        return False, "doctor.checks version_pairing.ok 不是 true"
    return True, "version_pairing 通過"


def _current_bundle_dir(cfg: DrillConfig) -> Path:
    if cfg.platform.lower().startswith("win"):
        layout_path = Path(cfg.cli).parent / "current-release.json"
        try:
            layout = json.loads(layout_path.read_text(encoding="utf-8"))
            directory = layout.get("dir") if isinstance(layout, dict) else None
        except (OSError, ValueError) as exc:
            raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"無法讀取 Windows current-release.json：{exc}") from exc
        if not isinstance(directory, str) or not directory:
            raise HarnessError("HARNESS_ARTIFACT_MISMATCH", "Windows current-release.json 缺少 dir")
        bundle = Path(directory)
    else:
        try:
            resolved = Path(os.path.realpath(Path(cfg.cli), strict=True))
        except (OSError, TypeError) as exc:
            raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"無法解析 Linux 穩定 CLI 入口：{exc}") from exc
        bundle = resolved.parent.parent
    try:
        return bundle.resolve(strict=True)
    except OSError as exc:
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"Current bundle 不存在：{bundle}: {exc}") from exc


def _read_manifest(bundle_dir: Path) -> dict[str, Any]:
    path = Path(bundle_dir) / "release-manifest.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"無法讀取 release manifest：{path}: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("components"), dict):
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"release manifest 結構無效：{path}")
    return doc


def _manifest_file(bundle_dir: Path, manifest: Mapping[str, Any], component: str, *nested: str) -> Path:
    components = manifest.get("components")
    value: Any = components.get(component) if isinstance(components, Mapping) else None
    for key in nested:
        value = value.get(key) if isinstance(value, Mapping) else None
    relative = value.get("path") if isinstance(value, Mapping) else None
    if not isinstance(relative, str) or not relative:
        suffix = ".".join((component, *nested, "path"))
        raise HarnessError("HARNESS_MANIFEST_INVALID", f"manifest 缺少 {suffix}")
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise HarnessError("HARNESS_MANIFEST_INVALID", f"manifest 路徑不在 bundle 內：{relative}")
    root = Path(bundle_dir).resolve(strict=True)
    candidate = Path(bundle_dir) / relative_path
    if candidate.is_symlink():
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"bundle component 不得為 symlink：{relative}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"bundle component 不存在：{relative}: {exc}") from exc
    if root not in resolved.parents or not resolved.is_file():
        raise HarnessError("HARNESS_MANIFEST_INVALID", f"bundle component 不是 bundle 內的一般檔案：{relative}")
    return resolved


def _validate_source_bundle(source: Path, *, label: str) -> list[str]:
    bundle = Path(source)
    try:
        if bundle.is_symlink() or not bundle.is_dir():
            return [f"{label} bundle 不是一般目錄：{bundle}"]
        manifest = _read_manifest(bundle)
        _manifest_file(bundle, manifest, "cli")
        _manifest_file(bundle, manifest, "daemon", "launcher")
    except (HarnessError, OSError, ValueError, TypeError) as exc:
        return [f"{label} bundle 無效：{exc}"]
    return []


def _daemon_path(bundle_dir: Path, manifest: Mapping[str, Any]) -> str:
    components = manifest.get("components")
    daemon = components.get("daemon") if isinstance(components, Mapping) else None
    launcher = daemon.get("launcher") if isinstance(daemon, Mapping) else None
    relative = launcher.get("path") if isinstance(launcher, Mapping) else None
    if not isinstance(relative, str) or not relative:
        return str(bundle_dir)
    return str(Path(bundle_dir) / relative)


def _record_install(cfg: DrillConfig, run: Runner, *, timeout_s: float) -> tuple[_InstallSnapshot | None, dict[str, Any]]:
    detail: dict[str, Any] = {}
    version = _run_text(run, cfg.cli, ("--version",), timeout_s=timeout_s)
    status_result, status = _run_json(run, cfg.cli, ("daemon", "status"), timeout_s=timeout_s)
    doctor_result, doctor = _run_json(run, cfg.cli, ("doctor",), timeout_s=timeout_s)
    sessions_result, sessions_doc = _run_json(run, cfg.cli, ("session", "list"), timeout_s=timeout_s)
    detail.update({"cli_version": version, "daemon_status": status_result, "doctor": doctor_result, "session_list": sessions_result})
    errors: list[str] = []
    if version.get("rc") != 0 or version.get("timed_out") or not _version_matches(str(version.get("stdout", "")), cfg.expect_current):
        errors.append("CLI --version 未顯示預期 Current 版本")
    if not _response_ok(status_result, status) or status is None or status.get("running") is not True:
        errors.append("daemon status 未確認 daemon 正在執行")
    if status is None or not _version_matches(str(status.get("version", "")), cfg.expect_current):
        errors.append("daemon status.version 未顯示預期 Current 版本")
    old_pid = status.get("pid") if status else None
    if not _valid_pid(old_pid):
        errors.append("daemon status.pid 不是有效 PID")
    if not _query_ok(doctor_result, doctor):
        errors.append("doctor 查詢失敗")
    pairing_ok, pairing_detail = _pairing_check(doctor)
    if not pairing_ok:
        errors.append(pairing_detail)
    session_map, session_errors = _sessions(sessions_doc)
    errors.extend(session_errors)
    boards, board_errors = _board_rows(session_map, cfg.boards)
    errors.extend(board_errors)
    for board, row in boards.items():
        if row.get("state") != "READY":
            errors.append(f"{board}: state 不是 READY")
        for field in ("alias", "device_by_id", "attached_real_path"):
            if not isinstance(row.get(field), str) or not row.get(field):
                errors.append(f"{board}: {field} 缺少有效值")
    if not _response_ok(sessions_result, sessions_doc):
        errors.append("session list 查詢失敗")

    current_dir: Path | None = None
    previous_dir: Path | None = None
    current_doc: dict[str, Any] | None = None
    previous_doc: dict[str, Any] | None = None
    current_manifest_sha: str | None = None
    previous_manifest_sha: str | None = None
    current_tree: str | None = None
    current_runtime_tree: str | None = None
    previous_tree: str | None = None
    cli_sha: str | None = None
    try:
        current_dir = _current_bundle_dir(cfg)
        previous_dir = current_dir.parent / cfg.expect_previous
        current_doc = _read_manifest(current_dir)
        previous_doc = _read_manifest(previous_dir)
        if current_doc.get("bundle_version") != cfg.expect_current:
            errors.append("Current bundle manifest version 與 expect_current 不符")
        if previous_doc.get("bundle_version") != cfg.expect_previous:
            errors.append("Previous bundle manifest version 與 expect_previous 不符")
        current_manifest_sha = _sha256_file(current_dir / "release-manifest.json")
        previous_manifest_sha = _sha256_file(previous_dir / "release-manifest.json")
        current_tree = _bundle_tree_sha256(current_dir)
        current_runtime_tree = _runtime_tree_sha256(current_dir)
        previous_tree = _bundle_tree_sha256(previous_dir)
        cli_sha = _sha256_file(cfg.cli)
    except HarnessError as exc:
        errors.append(f"{exc.code}: {exc.detail or str(exc)}")
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f"HARNESS_ARTIFACT_MISMATCH: {exc}")

    released = tuple(
        sorted(
            (com, str(row.get("alias")) if isinstance(row.get("alias"), str) else None)
            for com, row in session_map.items()
            if com not in cfg.boards and row.get("state") == "RELEASED"
        )
    )
    detail["errors"] = errors
    detail["record"] = {
        "old_pid": old_pid,
        "boards": {board: dict(row) for board, row in boards.items()},
        "released_sessions": [{"com": com, "alias": alias, "state": "RELEASED"} for com, alias in released],
        "current_bundle_dir": str(current_dir) if current_dir else None,
        "previous_bundle_dir": str(previous_dir) if previous_dir else None,
        "current_release_manifest_sha256": current_manifest_sha,
        "previous_release_manifest_sha256": previous_manifest_sha,
        "current_bundle_tree_sha256": current_tree,
        "current_runtime_tree_sha256": current_runtime_tree,
        "previous_bundle_tree_sha256": previous_tree,
        "cli_sha256": cli_sha,
    }
    if errors or current_dir is None or previous_dir is None or current_doc is None or previous_doc is None:
        return None, detail
    return (
        _InstallSnapshot(
            current_dir=current_dir,
            previous_dir=previous_dir.resolve(),
            current_doc=current_doc,
            previous_doc=previous_doc,
            current_manifest_sha256=str(current_manifest_sha),
            previous_manifest_sha256=str(previous_manifest_sha),
            current_bundle_tree_sha256=str(current_tree),
            current_runtime_tree_sha256=str(current_runtime_tree),
            previous_bundle_tree_sha256=str(previous_tree),
            cli_sha256=str(cli_sha),
            old_pid=int(old_pid),
            sessions=session_map,
            released_sessions=released,
        ),
        detail,
    )


def _reacquired(
    sessions: Mapping[str, Mapping[str, Any]],
    *,
    boards: Sequence[str],
    previous_sessions: Mapping[str, Mapping[str, Any]],
    since: str,
) -> list[str]:
    errors: list[str] = []
    try:
        cutoff = jsonio.parse_iso(since)
    except (TypeError, ValueError) as exc:
        return [f"reacquire since 時間無效：{exc}"]
    for board in boards:
        current = sessions.get(board)
        original = previous_sessions.get(board)
        if current is None:
            errors.append(f"{board}: D1 session 在切換後消失")
            continue
        if original is None:
            errors.append(f"{board}: session 不存在，無法確認重新取得")
            continue
        if current.get("state") != "READY":
            errors.append(f"{board}.state 不是 READY")
        for field in ("alias", "device_by_id", "attached_real_path"):
            if current.get(field) != original.get(field):
                errors.append(f"{board}.{field} 與 D1 記錄不一致")
        ready_at = current.get("last_ready_at")
        if not isinstance(ready_at, str):
            errors.append(f"{board}.last_ready_at 缺少時間")
        else:
            try:
                if jsonio.parse_iso(ready_at) <= cutoff:
                    errors.append(f"{board}.last_ready_at 未晚於步驟開始時間")
            except (TypeError, ValueError):
                errors.append(f"{board}.last_ready_at 時間格式無效")
    return errors


def _released_sessions(
    sessions: Mapping[str, Mapping[str, Any]],
    expected: Sequence[tuple[str, str | None]],
    *,
    boards: Sequence[str],
) -> list[str]:
    actual = tuple(
        sorted(
            (com, str(row.get("alias")) if isinstance(row.get("alias"), str) else None)
            for com, row in sessions.items()
            if com not in boards and row.get("state") == "RELEASED"
        )
    )
    if actual == tuple(expected):
        return []
    return [f"RELEASED session 集合改變：expected={tuple(expected)!r}, actual={actual!r}"]


def _aliases_unchanged(
    sessions: Mapping[str, Mapping[str, Any]], expected: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    errors: list[str] = []
    for com, before in expected.items():
        after = sessions.get(com)
        if after is None:
            errors.append(f"{com}: D1 session 在切換後消失")
            continue
        if after.get("alias") != before.get("alias"):
            errors.append(f"{com}.alias 與 D1 記錄不一致")
    return errors


def _version_output(run: Runner, cfg: DrillConfig, *, timeout_s: float) -> tuple[dict[str, Any], bool]:
    result = _run_text(run, cfg.cli, ("--version",), timeout_s=timeout_s)
    return result, result.get("rc") == 0 and result.get("timed_out") is False


def _release_old_pid(
    *,
    pid: int,
    current_pid: int | None,
    daemon_path: str,
    probe: ReleaseProbe | None,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[bool, dict[str, Any]]:
    from .ab_manifest import ReleaseSpec
    from .release_check import default_probe

    selected_probe = probe if probe is not None else default_probe()
    if current_pid is not None and current_pid != pid:
        selected_probe = _ExcludePidsProbe(selected_probe, {current_pid})
    snapshot = PreStopSnapshot(
        daemon_pid=pid,
        descendant_pids=(),
        match_paths=(daemon_path,),
        endpoints=(),
        device_paths=(),
        lock_paths=(),
        captured_at=clock(),
    )
    spec = ReleaseSpec(
        timeout_s=0.0,
        poll_s=2.0,
        endpoints=(),
        device_paths=(),
        lock_paths=(),
        device_open_probe=False,
        accept_unreadable_holders=False,
    )
    result = verify_released(snapshot, spec, probe=selected_probe, clock=clock, sleep=sleep)
    return result.released, {
        "checks": [asdict(check) for check in result.checks],
        "elapsed_s": result.elapsed_s,
        "released": result.released,
    }


class _ExcludePidsProbe:
    """在舊 PID 檢查中排除已核實的新 daemon，仍保留其他同路徑行程。"""

    def __init__(self, delegate: ReleaseProbe, excluded: set[int]) -> None:
        self._delegate = delegate
        self._excluded = excluded

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    def pids_by_exe(self, match_paths: Sequence[str]) -> tuple[int, ...] | None:
        pids = self._delegate.pids_by_exe(match_paths)
        if pids is None:
            return None
        return tuple(pid for pid in pids if pid not in self._excluded)


def _query_after_switch(
    cfg: DrillConfig,
    run: Runner,
    *,
    timeout_s: float,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any], dict[str, Any] | None,
           dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    version = _run_text(run, cfg.cli, ("--version",), timeout_s=timeout_s)
    status_result, status = _run_json(run, cfg.cli, ("daemon", "status"), timeout_s=timeout_s)
    doctor_result, doctor = _run_json(run, cfg.cli, ("doctor",), timeout_s=timeout_s)
    sessions_result, sessions_doc = _run_json(run, cfg.cli, ("session", "list"), timeout_s=timeout_s)
    return version, status, status_result, doctor, doctor_result, sessions_doc, sessions_result


def _verify_switched_state(
    cfg: DrillConfig,
    run: Runner,
    *,
    expected_version: str,
    old_pid: int,
    since: str,
    snapshot: _InstallSnapshot,
    probe: ReleaseProbe | None,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    ready_timeout_s: float,
    daemon_path: str,
    expect_bundle_dir: Path,
    expect_tree: bool,
) -> tuple[bool, dict[str, Any]]:
    deadline = clock() + max(0.0, ready_timeout_s)
    last_detail: dict[str, Any] = {}
    while True:
        version, status, status_result, doctor, doctor_result, sessions_doc, sessions_result = _query_after_switch(
            cfg, run, timeout_s=min(15.0, max(1.0, ready_timeout_s))
        )
        session_map, session_errors = _sessions(sessions_doc)
        errors: list[str] = []
        if version.get("rc") != 0 or version.get("timed_out") or not _version_matches(str(version.get("stdout", "")), expected_version):
            errors.append(f"CLI --version 未顯示預期版本 {expected_version}")
        if not _response_ok(status_result, status) or status is None or status.get("running") is not True:
            errors.append("daemon status 未確認 daemon 正在執行")
        if status is None or not _version_matches(str(status.get("version", "")), expected_version):
            errors.append(f"daemon status.version 未顯示預期版本 {expected_version}")
        current_pid = status.get("pid") if status else None
        if not _valid_pid(current_pid) or current_pid == old_pid:
            errors.append("daemon pid 未更換為有效的新 PID")
        if not _query_ok(doctor_result, doctor):
            errors.append("doctor 查詢失敗")
        pairing_ok, pairing_detail = _pairing_check(doctor)
        if not pairing_ok:
            errors.append(pairing_detail)
        errors.extend(session_errors)
        if not _response_ok(sessions_result, sessions_doc):
            errors.append("session list 查詢失敗")
        errors.extend(_reacquired(session_map, boards=cfg.boards, previous_sessions=snapshot.sessions, since=since))
        errors.extend(_released_sessions(session_map, snapshot.released_sessions, boards=cfg.boards))
        errors.extend(_aliases_unchanged(session_map, snapshot.sessions))
        foreign_holder_details: list[dict[str, Any]] = []
        if status is not None:
            if status.get("multi_open") is not False:
                errors.append("daemon status.multi_open 不是 false")
            if cfg.platform.lower().startswith("linux"):
                foreign = status.get("foreign_holders")
                holder_errors, foreign_holder_details = _foreign_holder_errors(foreign, current_pid)
                errors.extend(holder_errors)
        released, release_detail = _release_old_pid(
            pid=old_pid,
            current_pid=current_pid if _valid_pid(current_pid) else None,
            daemon_path=daemon_path,
            probe=probe,
            clock=clock,
            sleep=sleep,
        )
        if not released:
            errors.append("release_check 尚未確認舊 daemon PID 結束")
        detail: dict[str, Any] = {
            "cli_version": version,
            "daemon_status": status_result,
            "daemon_status_doc": status,
            "daemon_pid": current_pid,
            "foreign_holders_detail": foreign_holder_details,
            "doctor": doctor_result,
            "session_list": sessions_result,
            "old_pid_release": release_detail,
            "errors": errors,
        }
        try:
            current_dir = _current_bundle_dir(cfg)
            detail["installed_bundle_dir"] = str(current_dir)
            if current_dir.resolve() != expect_bundle_dir.resolve():
                errors.append(f"安裝佈局未指向預期 bundle：{current_dir}")
            if expect_tree:
                bundle_tree = _bundle_tree_sha256(current_dir)
                runtime_tree = _runtime_tree_sha256(current_dir)
                detail["current_bundle_tree_sha256"] = bundle_tree
                detail["current_runtime_tree_sha256"] = runtime_tree
                if bundle_tree != snapshot.current_bundle_tree_sha256:
                    errors.append("deployed tree changed: bundle_tree_sha256")
                if runtime_tree != snapshot.current_runtime_tree_sha256:
                    errors.append("deployed tree changed: runtime_tree_sha256")
        except (OSError, HarnessError, ValueError) as exc:
            errors.append(f"HARNESS_ARTIFACT_MISMATCH: {exc}")
        detail["errors"] = errors
        if not errors:
            return True, detail
        last_detail = detail
        now_mono = clock()
        if now_mono >= deadline:
            return False, last_detail
        sleep(min(2.0, max(0.0, deadline - now_mono)))


def _foreign_holder_errors(value: object, own_pid: object) -> tuple[list[str], list[dict[str, Any]]]:
    """依 RPC 契約檢查 holder map，並保留掃描不完整項目的診斷資訊。"""
    if not isinstance(value, Mapping):
        return ["daemon status.foreign_holders 缺少物件"], []
    errors: list[str] = []
    details: list[dict[str, Any]] = []
    for device, holder in value.items():
        if isinstance(holder, int) and not isinstance(holder, bool):
            if holder != own_pid:
                errors.append(f"daemon status.foreign_holders[{device!r}] 含其他 PID")
            continue
        if not isinstance(holder, Mapping) or holder.get("holder_unknown") is not True:
            errors.append(f"daemon status.foreign_holders[{device!r}] 格式無效")
            continue
        holder_device = holder.get("device")
        if not isinstance(holder_device, str) or not holder_device or set(holder) - {"device", "holder_unknown", "pids"}:
            errors.append(f"daemon status.foreign_holders[{device!r}] 格式無效")
            continue
        pids = holder.get("pids")
        if pids is not None and (
            not isinstance(pids, list)
            or any(not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 for pid in pids)
        ):
            errors.append(f"daemon status.foreign_holders[{device!r}].pids 格式無效")
            continue
        foreign_pids = [pid for pid in pids or [] if pid != own_pid]
        if foreign_pids:
            errors.append(f"daemon status.foreign_holders[{device!r}] 含其他 PID")
        details.append(
            {
                "tty": device,
                "device": holder_device,
                "holder_unknown": True,
                "pids": list(pids or []),
                "foreign_pids": foreign_pids,
            }
        )
    return errors, details


def _smoke(
    cfg: DrillConfig,
    run: Runner,
    *,
    boards: Sequence[str],
    marker: Callable[[], str],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[bool, dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for board in boards:
        gate_ok, gate_detail = _wait_smoke_gate(cfg, run, board, clock=clock, sleep=sleep)
        if not gate_ok:
            return False, {"boards": details, "errors": [f"{board}: smoke 前 agent gate 未解除"], "gate": gate_detail}
        token = marker()
        if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9]{1,32}", token) is None:
            return False, {"errors": ["smoke marker 格式無效"], "boards": details}
        command = f"echo SW''DRILL_{token}"
        submit_result, submit = _run_json(
            run,
            cfg.cli,
            ("cmd", "submit", "--selector", board, "--cmd", command, "--cmd-timeout", "12"),
            timeout_s=15.0,
        )
        if not _response_ok(submit_result, submit):
            details.append({"board": board, "submit": submit_result, "errors": ["cmd submit 失敗"]})
            return False, {"boards": details, "errors": [f"{board}: cmd submit 失敗"]}
        command_id = submit.get("cmd_id") if submit else None
        if not isinstance(command_id, str) or not command_id:
            details.append({"board": board, "submit": submit_result, "errors": ["cmd submit 缺少 cmd_id"]})
            return False, {"boards": details, "errors": [f"{board}: cmd submit 缺少 cmd_id"]}
        deadline = clock() + 17.0
        last: dict[str, Any] = {}
        while True:
            status_result, status = _run_json(
                run,
                cfg.cli,
                ("cmd", "status", "--cmd-id", command_id),
                timeout_s=5.0,
            )
            command_doc = status.get("command") if status else None
            stdout = command_doc.get("stdout") if isinstance(command_doc, dict) else None
            done = isinstance(command_doc, dict) and command_doc.get("status") == "done"
            found = isinstance(stdout, str) and f"SWDRILL_{token}" in stdout
            last = {"command_status": status_result, "command": command_doc, "marker": token}
            if isinstance(command_doc, dict) and command_doc.get("status") in {"error", "canceled", "cancelled"}:
                details.append({"board": board, "submit": submit_result, **last,
                                "errors": [f"cmd status 終態為 {command_doc.get('status')}"]})
                return False, {"boards": details, "errors": [f"{board}: cmd status 為終態錯誤"]}
            if _response_ok(status_result, status) and done:
                if found:
                    details.append({"board": board, "submit": submit_result, **last})
                    break
                details.append({"board": board, "submit": submit_result, **last, "errors": ["命令完成但 stdout 缺少 smoke marker"]})
                return False, {"boards": details, "errors": [f"{board}: stdout 缺少 smoke marker"]}
            now_mono = clock()
            if now_mono >= deadline or status_result.get("timed_out"):
                details.append({"board": board, "submit": submit_result, **last, "errors": ["cmd status 未在逾時內回報 done"]})
                return False, {"boards": details, "errors": [f"{board}: cmd status 未在逾時內回報 done"]}
            sleep(min(0.5, max(0.0, deadline - now_mono)))
    return True, {"boards": details, "errors": []}


def _wait_smoke_gate(
    cfg: DrillConfig,
    run: Runner,
    board: str,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[bool, dict[str, Any]]:
    """在單次 submit 前等 READY、boot quiet 與 READY 再確認 gate 全部解除。"""
    deadline = clock() + max(0.0, cfg.ready_timeout_s)
    last: dict[str, Any] = {}
    while True:
        result, doc = _run_json(run, cfg.cli, ("session", "list"), timeout_s=15.0)
        sessions, errors = _sessions(doc)
        row = sessions.get(board)
        if not _response_ok(result, doc) or errors or row is None:
            return False, {"session_list": result, "errors": errors or [f"{board}: session list 查詢失敗"]}
        if row.get("state") != "READY":
            return False, {"session_list": result, "errors": [f"{board}.state 不是 READY"]}
        if row.get("ready_reconfirm_failed") is True:
            return False, {"session_list": result, "errors": [f"{board}.ready_reconfirm_failed 為 true"]}
        quiet = row.get("boot_quiet_remaining_s")
        if quiet is not None and (not isinstance(quiet, (int, float)) or isinstance(quiet, bool)):
            return False, {"session_list": result, "errors": [f"{board}.boot_quiet_remaining_s 格式無效"]}
        pending = row.get("ready_reconfirm_pending") is True or (isinstance(quiet, (int, float)) and quiet > 0)
        last = {"session_list": result, "session": row, "pending": pending}
        if not pending:
            return True, last
        now_mono = clock()
        if now_mono >= deadline:
            return False, {**last, "errors": [f"{board}: agent gate 逾時未解除"]}
        sleep(min(0.5, max(0.0, deadline - now_mono)))


def _setup_result(run: Runner, argv: Sequence[str], *, timeout_s: float) -> tuple[dict[str, Any], dict[str, Any] | None]:
    try:
        result, doc = run_json(run, list(argv), timeout=timeout_s)
    except Exception as exc:
        return {"argv": list(argv), "error": str(exc), "rc": None, "stderr": "", "stdout": "", "timed_out": False}, None
    return {
        "argv": list(argv),
        "rc": result.rc,
        "stderr": result.stderr,
        "stdout": result.stdout,
        "timed_out": result.timed_out,
    }, doc


def _setup_refused(result: Mapping[str, Any], doc: Mapping[str, Any] | None) -> bool:
    rc = result.get("rc")
    if not isinstance(rc, int) or isinstance(rc, bool) or "error" in result or result.get("timed_out") is not False:
        return False
    if doc is not None and doc.get("ok") is False:
        return True
    return rc != 0


def _ensure_destination_absent(destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"bundle 目的地已存在，拒絕覆寫：{destination}")


def _remove_created_bundle(destination: Path, *, created: bool) -> None:
    if created:
        shutil.rmtree(destination, ignore_errors=True)


def _make_corrupted_bundle(src: Path, dst: Path) -> Path:
    source = Path(src)
    destination = Path(dst)
    if not source.is_dir():
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"corrupt source bundle 不存在：{source}")
    _ensure_destination_absent(destination)
    created = False
    try:
        destination.mkdir(parents=True)
        created = True
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
        manifest_path = destination / "release-manifest.json"
        doc = _read_manifest(destination)
        cli = doc["components"].get("cli")
        digest = cli.get("sha256") if isinstance(cli, dict) else None
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise HarnessError("HARNESS_MANIFEST_INVALID", "components.cli.sha256 格式無效")
        replacement = "0" if digest[-1] != "0" else "1"
        manifest_text = manifest_path.read_bytes().decode("utf-8")
        components_match = re.search(r'"components"\s*:\s*\{', manifest_text)
        if components_match is None:
            raise HarnessError("HARNESS_MANIFEST_INVALID", "release manifest 缺少 components 物件")
        cli_match = re.compile(r'"cli"\s*:').search(manifest_text, components_match.end())
        if cli_match is None:
            raise HarnessError("HARNESS_MANIFEST_INVALID", "release manifest 缺少 components.cli")
        value_start = cli_match.end()
        while value_start < len(manifest_text) and manifest_text[value_start].isspace():
            value_start += 1
        _, component_end = json.JSONDecoder().raw_decode(manifest_text, value_start)
        cli_text = manifest_text[value_start:component_end]
        digest_match = re.search(r'"sha256"\s*:\s*"([0-9a-f]{64})"', cli_text)
        if digest_match is None or digest_match.group(1) != digest:
            raise HarnessError("HARNESS_MANIFEST_INVALID", "components.cli.sha256 無法對應原始 manifest")
        digest_start = value_start + digest_match.start(1)
        digest_end = value_start + digest_match.end(1)
        updated_text = manifest_text[:digest_start] + digest[:-1] + replacement + manifest_text[digest_end:]
        jsonio.atomic_write_text(manifest_path, updated_text)
        return destination
    except HarnessError:
        _remove_created_bundle(destination, created=created)
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _remove_created_bundle(destination, created=created)
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"無法建立 corrupted bundle：{exc}") from exc


def _make_start_fail_bundle(src: Path, dst: Path) -> Path:
    source = Path(src)
    destination = Path(dst)
    if not source.is_dir():
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"start-fail source bundle 不存在：{source}")
    _ensure_destination_absent(destination)
    created = False
    try:
        destination.mkdir(parents=True)
        created = True
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
        manifest_path = destination / "release-manifest.json"
        doc = _read_manifest(destination)
        daemon = doc["components"].get("daemon")
        launcher = daemon.get("launcher") if isinstance(daemon, dict) else None
        relative = launcher.get("path") if isinstance(launcher, dict) else None
        if not isinstance(relative, str) or not relative:
            raise HarnessError("HARNESS_MANIFEST_INVALID", "components.daemon.launcher.path 缺少有效路徑")
        launcher_path = (destination / relative).resolve()
        if destination.resolve() not in launcher_path.parents:
            raise HarnessError("HARNESS_MANIFEST_INVALID", "daemon launcher 路徑跳出 bundle")
        if not launcher_path.is_file():
            raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"daemon launcher 不存在：{relative}")
        target = str(doc.get("target", "" )).lower()
        if "windows" in target or "win32" in target or relative.lower().endswith((".exe", ".cmd")):
            launcher_path.unlink()
            launcher_path = destination / "bin" / "serialwrapd-start-fail.cmd"
            launcher_path.write_bytes(b"@echo off\r\nexit /b 3\r\n")
            launcher["path"] = launcher_path.relative_to(destination).as_posix()
        else:
            launcher_path.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8", newline="\n")
            launcher_path.chmod(0o755)
        launcher["sha256"] = _sha256_file(launcher_path)
        jsonio.atomic_write_text(manifest_path, jsonio.dumps_report(doc))
        sums: list[str] = []
        for path in sorted(destination.rglob("*"), key=lambda item: item.relative_to(destination).as_posix()):
            if path.is_symlink():
                raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"bundle 含 symlink：{path.relative_to(destination).as_posix()}")
            if path.is_file() and path.name not in {"SHA256SUMS", "release-manifest.json"}:
                sums.append(f"{_sha256_file(path)}  {path.relative_to(destination).as_posix()}\n")
        (destination / "SHA256SUMS").write_text("".join(sums), encoding="utf-8", newline="\n")
        return destination
    except HarnessError:
        _remove_created_bundle(destination, created=created)
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _remove_created_bundle(destination, created=created)
        raise HarnessError("HARNESS_ARTIFACT_MISMATCH", f"無法建立 start-fail bundle：{exc}") from exc


def make_corrupted_bundle(src: Path, dst: Path) -> Path:
    """複製 bundle，只篡改 manifest 宣告的 CLI digest，供 D7 拒絕測試。"""
    return _make_corrupted_bundle(src, dst)


def make_start_fail_bundle(src: Path, dst: Path) -> Path:
    """複製 bundle，替換 daemon launcher 並重算其 digest 與 SHA256SUMS。"""
    return _make_start_fail_bundle(src, dst)


def _step(step_id: str, started: str, now: Callable[[], str], ok: bool, detail: Mapping[str, Any]) -> StepResult:
    return StepResult(step_id=step_id, ok=bool(ok), detail=dict(detail), started_at=started, finished_at=now())


def _write_report(
    cfg: DrillConfig,
    *,
    verdict: str,
    reason_code: str | None,
    started_at: str,
    finished_at: str,
    steps: Sequence[StepResult],
    restore: StepResult | None,
    record: Mapping[str, Any] | None,
    limitations: Sequence[str],
) -> Path:
    path = Path(cfg.out_dir) / "drill-report.json"
    report: dict[str, Any] = {
        "schema": "drill-report.v1",
        "verdict": verdict,
        "reason_code": reason_code,
        "started_at": started_at,
        "finished_at": finished_at,
        "platform": cfg.platform,
        "cli": str(cfg.cli),
        "expect_current": cfg.expect_current,
        "expect_previous": cfg.expect_previous,
        "steps": [asdict(step) for step in steps],
        "restore": asdict(restore) if restore is not None else None,
        "limitations": list(limitations),
    }
    if record is not None:
        report.update(
            {
                "current_release_manifest_sha256": record.get("current_release_manifest_sha256"),
                "previous_release_manifest_sha256": record.get("previous_release_manifest_sha256"),
                "current_bundle_tree_sha256": record.get("current_bundle_tree_sha256"),
                "current_runtime_tree_sha256": record.get("current_runtime_tree_sha256"),
                "previous_bundle_tree_sha256": record.get("previous_bundle_tree_sha256"),
                "cli_sha256": record.get("cli_sha256"),
                "current_bundle_dir": record.get("current_bundle_dir"),
                "previous_bundle_dir": record.get("previous_bundle_dir"),
            }
        )
    jsonio.atomic_write_text(path, jsonio.dumps_report(report))
    return path


def _resolve_forward_argv(cfg: DrillConfig, snapshot: _InstallSnapshot) -> tuple[str, ...]:
    if cfg.forward_argv:
        return tuple(str(value) for value in cfg.forward_argv)
    manifest = snapshot.current_doc
    components = manifest.get("components")
    cli = components.get("cli") if isinstance(components, Mapping) else None
    relative = cli.get("path") if isinstance(cli, Mapping) else None
    if not isinstance(relative, str) or not relative:
        raise HarnessError("HARNESS_MANIFEST_INVALID", "Current manifest 缺少 components.cli.path")
    return (str(snapshot.current_dir / relative), "setup")


def _should_restore(cfg: DrillConfig, snapshot: _InstallSnapshot) -> bool:
    try:
        return _current_bundle_dir(cfg).resolve() == snapshot.previous_dir.resolve()
    except (HarnessError, OSError):
        return False


def _run_drill_impl(
    cfg: DrillConfig,
    *,
    run: Runner = run_cmd,
    probe: ReleaseProbe | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], str] = jsonio.utc_now_iso,
    marker: Callable[[], str] = lambda: secrets.token_hex(3),
    _state: dict[str, Any] | None = None,
) -> DrillResult:
    """依 D0～D8 執行回滾、UART 重新取得、roll-forward 與自動回復檢查。"""
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    steps: list[StepResult] = []
    restore: StepResult | None = None
    snapshot: _InstallSnapshot | None = None
    record: Mapping[str, Any] | None = None
    limitations: list[str] = []
    verdict = "FAIL"
    reason_code: str | None = "HARNESS_DRILL_STEP_FAILED"
    state = _state if _state is not None else {}
    state.update(
        steps=steps,
        snapshot=snapshot,
        record=record,
        restore=restore,
        limitations=limitations,
        verdict=verdict,
        reason_code=reason_code,
    )

    def add(step_id: str, started: str, ok: bool, detail: Mapping[str, Any]) -> StepResult:
        result = _step(step_id, started, now, ok, detail)
        steps.append(result)
        state["steps"] = steps
        return result

    def phase(step_id: str, started: str) -> None:
        state["phase"] = step_id
        state["phase_started_at"] = started

    # D0：只讀前置條件；此步失敗時不得啟動任何 setup。
    started = now()
    state["started_at"] = started
    phase("D0-preflight", started)
    version = _run_text(run, cfg.cli, ("--version",), timeout_s=15.0)
    status_result, status = _run_json(run, cfg.cli, ("daemon", "status"), timeout_s=15.0)
    mcu_result, mcu = _run_json(run, cfg.cli, ("mcu", "status"), timeout_s=15.0)
    sessions_result, sessions_doc = _run_json(run, cfg.cli, ("session", "list"), timeout_s=15.0)
    session_map, session_errors = _sessions(sessions_doc)
    board_rows, board_errors = _board_rows(session_map, cfg.boards)
    preflight_errors = list(session_errors) + board_errors
    if not cfg.boards:
        preflight_errors.append("boards 不可為空")
    if len(set(cfg.boards)) != len(cfg.boards):
        preflight_errors.append("boards 不可含重複 selector")
    if cfg.platform.lower() not in {"linux", "windows"}:
        preflight_errors.append("platform 必須是 linux 或 windows")
    if not cfg.expect_current or not cfg.expect_previous or cfg.expect_current == cfg.expect_previous:
        preflight_errors.append("expect_current 與 expect_previous 必須是不同的非空版本")
    if cfg.ready_timeout_s <= 0 or cfg.setup_timeout_s <= 0:
        preflight_errors.append("ready_timeout_s 與 setup_timeout_s 必須大於零")
    if cfg.forward_argv and any(str(argument).startswith("--force") for argument in cfg.forward_argv):
        preflight_errors.append("forward_argv 不得包含 --force 類旗標")
    if cfg.corrupt_from is not None:
        preflight_errors.extend(_validate_source_bundle(cfg.corrupt_from, label="corrupt source"))
        if (out_dir / "corrupt").exists() or (out_dir / "corrupt").is_symlink():
            preflight_errors.append(f"corrupt bundle 目的地已存在：{out_dir / 'corrupt'}")
    if cfg.start_fail_from is not None:
        preflight_errors.extend(_validate_source_bundle(cfg.start_fail_from, label="start-fail source"))
        if (out_dir / "startfail").exists() or (out_dir / "startfail").is_symlink():
            preflight_errors.append(f"start-fail bundle 目的地已存在：{out_dir / 'startfail'}")
    if version.get("rc") != 0 or version.get("timed_out") or not _version_matches(str(version.get("stdout", "")), cfg.expect_current):
        preflight_errors.append("CLI --version 未顯示 expect_current")
    if not _response_ok(status_result, status) or status is None or status.get("running") is not True:
        preflight_errors.append("daemon status 未確認 daemon running")
    if not _response_ok(mcu_result, mcu) or mcu is None or mcu.get("flashing") is not False:
        preflight_errors.append("mcu status 顯示 flashing 或查詢失敗")
    if not _response_ok(sessions_result, sessions_doc):
        preflight_errors.append("session list 查詢失敗")
    for board, row in board_rows.items():
        if row.get("state") != "READY":
            preflight_errors.append(f"{board}: state 不是 READY")
    detail = {
        "cli_version": version,
        "daemon_status": status_result,
        "mcu_status": mcu_result,
        "session_list": sessions_result,
        "errors": preflight_errors,
    }
    preflight_ok = not preflight_errors
    add("D0-preflight", started, preflight_ok, detail)
    if not preflight_ok:
        verdict = "PRECONDITION_FAILED"
        reason_code = "HARNESS_DRILL_PRECONDITION"
    else:
        # D1：記錄實際安裝目錄的 manifest 與兩種內容指紋。
        started = now()
        phase("D1-record", started)
        snapshot, detail = _record_install(cfg, run, timeout_s=15.0)
        record = detail.get("record") if isinstance(detail, dict) else None
        state.update(snapshot=snapshot, record=record)
        d1_ok = snapshot is not None
        add("D1-record", started, d1_ok, detail)
        if not d1_ok:
            verdict = "FAIL"
            reason_code = "HARNESS_DRILL_STEP_FAILED"
        else:
            # D2：切回完整 Previous 組合。
            t2 = now()
            started = t2
            phase("D2-rollback", started)
            result, doc = _run_json(run, cfg.cli, ("setup", "--rollback"), timeout_s=cfg.setup_timeout_s)
            d2_ok = _response_ok(result, doc)
            add("D2-rollback", started, d2_ok, {"setup": result, "response": doc, "errors": [] if d2_ok else ["setup --rollback 未成功"]})
            if not d2_ok:
                verdict = "FAIL"
                reason_code = "HARNESS_DRILL_STEP_FAILED"
            else:
                # D3：確認新 daemon、版本配對、原板綁定與每塊板重新 READY。
                started = now()
                phase("D3-verify-previous", started)
                daemon_path = _daemon_path(snapshot.current_dir, snapshot.current_doc)
                d3_ok, d3_detail = _verify_switched_state(
                    cfg,
                    run,
                    expected_version=cfg.expect_previous,
                    old_pid=snapshot.old_pid,
                    since=t2,
                    snapshot=snapshot,
                    probe=probe,
                    clock=clock,
                    sleep=sleep,
                    ready_timeout_s=cfg.ready_timeout_s,
                    daemon_path=daemon_path,
                    expect_bundle_dir=snapshot.previous_dir,
                    expect_tree=False,
                )
                d3 = add("D3-verify-previous", started, d3_ok, d3_detail)
                if not d3.ok:
                    verdict = "FAIL"
                    reason_code = "HARNESS_DRILL_STEP_FAILED"
                else:
                    # D4：以一次性輸出確認 rollback 後 UART 可實際使用。
                    started = now()
                    phase("D4-smoke-previous", started)
                    d4_ok, d4_detail = _smoke(cfg, run, boards=cfg.boards, marker=marker, clock=clock, sleep=sleep)
                    add("D4-smoke-previous", started, d4_ok, d4_detail)
                    if not d4_ok:
                        verdict = "FAIL"
                        reason_code = "HARNESS_DRILL_STEP_FAILED"
                    else:
                        previous_pid = d3_detail.get("daemon_pid")
                        # D5：以 D1 記錄的 Current bundle 重新啟用。
                        t5 = now()
                        started = t5
                        phase("D5-roll-forward", started)
                        try:
                            forward_argv = _resolve_forward_argv(cfg, snapshot)
                        except HarnessError as exc:
                            forward_argv = ()
                            d5_ok = False
                            d5_detail = {"errors": [f"{exc.code}: {exc.detail}"], "setup": None, "response": None}
                        else:
                            setup_result, setup_doc = _setup_result(run, forward_argv, timeout_s=cfg.setup_timeout_s)
                            d5_ok = _response_ok(setup_result, setup_doc)
                            d5_detail = {"setup": setup_result, "response": setup_doc, "errors": [] if d5_ok else ["Current setup 未成功"]}
                        add("D5-roll-forward", started, d5_ok, d5_detail)
                        if not d5_ok:
                            verdict = "FAIL"
                            reason_code = "HARNESS_DRILL_STEP_FAILED"
                        else:
                            # D6：Current 版本、程序退出、UART 重新取得及實際部署樹均須吻合 D1。
                            started = now()
                            phase("D6-verify-current", started)
                            if not _valid_pid(previous_pid):
                                d6_ok = False
                                d6_detail = {"errors": ["D3 未記錄有效 Previous daemon PID"]}
                            else:
                                daemon_path = _daemon_path(snapshot.previous_dir, snapshot.previous_doc)
                                d6_ok, d6_detail = _verify_switched_state(
                                    cfg,
                                    run,
                                    expected_version=cfg.expect_current,
                                    old_pid=int(previous_pid),
                                    since=t5,
                                    snapshot=snapshot,
                                    probe=probe,
                                    clock=clock,
                                    sleep=sleep,
                                    ready_timeout_s=cfg.ready_timeout_s,
                                    daemon_path=daemon_path,
                                    expect_bundle_dir=snapshot.current_dir,
                                    expect_tree=True,
                                )
                                if d6_ok:
                                    d6_smoke_ok, d6_smoke_detail = _smoke(
                                        cfg, run, boards=cfg.boards, marker=marker, clock=clock, sleep=sleep
                                    )
                                    d6_detail["smoke"] = d6_smoke_detail
                                    d6_ok = d6_smoke_ok
                                    if not d6_smoke_ok:
                                        d6_detail.setdefault("errors", []).extend(d6_smoke_detail.get("errors", []))
                            d6 = add("D6-verify-current", started, d6_ok, d6_detail)
                            if not d6.ok:
                                verdict = "FAIL"
                                reason_code = "HARNESS_DRILL_STEP_FAILED"
                            else:
                                # D7：錯誤 CLI digest 必須在啟用前遭拒，PID 與 generation 不得改變。
                                if cfg.corrupt_from is not None:
                                    started = now()
                                    phase("D7-invalid-artifact-refusal", started)
                                    before_result, before_status = _run_json(run, cfg.cli, ("daemon", "status"), timeout_s=15.0)
                                    before_sessions_result, before_sessions_doc = _run_json(
                                        run, cfg.cli, ("session", "list"), timeout_s=15.0
                                    )
                                    before_sessions, before_session_errors = _sessions(before_sessions_doc)
                                    before_rows, before_board_errors = _board_rows(before_sessions, cfg.boards)
                                    try:
                                        corrupted = make_corrupted_bundle(cfg.corrupt_from, out_dir / "corrupt")
                                        corrupt_doc = _read_manifest(corrupted)
                                        cli_component = corrupt_doc["components"]["cli"]
                                        corrupt_argv = (str(corrupted / cli_component["path"]), "setup")
                                        corrupt_result, corrupt_response = _setup_result(run, corrupt_argv, timeout_s=cfg.setup_timeout_s)
                                        refusal = _setup_refused(corrupt_result, corrupt_response)
                                        after_version, after_version_ok = _version_output(run, cfg, timeout_s=15.0)
                                        after_status_result, after_status = _run_json(run, cfg.cli, ("daemon", "status"), timeout_s=15.0)
                                        after_sessions_result, after_sessions_doc = _run_json(run, cfg.cli, ("session", "list"), timeout_s=15.0)
                                        after_sessions, after_session_errors = _sessions(after_sessions_doc)
                                        after_rows, after_board_errors = _board_rows(after_sessions, cfg.boards)
                                        d7_errors: list[str] = []
                                        if not refusal:
                                            d7_errors.append("corrupt artifact 未被拒絕")
                                        if not after_version_ok or not _version_matches(str(after_version.get("stdout", "")), cfg.expect_current):
                                            d7_errors.append("D7 後 CLI 版本不再是 expect_current")
                                        if not _response_ok(before_result, before_status) or before_status is None:
                                            d7_errors.append("D7 前 daemon status 查詢失敗")
                                        if not _response_ok(after_status_result, after_status) or after_status is None:
                                            d7_errors.append("D7 後 daemon status 查詢失敗")
                                        elif before_status is not None and (
                                            after_status.get("version") != before_status.get("version")
                                            or after_status.get("pid") != before_status.get("pid")
                                        ):
                                            d7_errors.append("D7 拒絕前後 daemon version／pid 改變")
                                        elif after_status is not None and not _version_matches(
                                            str(after_status.get("version", "")), cfg.expect_current
                                        ):
                                            d7_errors.append("D7 後 daemon status.version 不再是 expect_current")
                                        d7_errors.extend(before_session_errors + before_board_errors + after_session_errors + after_board_errors)
                                        for board in cfg.boards:
                                            if board in before_rows and board in after_rows:
                                                if before_rows[board].get("bridge_generation") != after_rows[board].get("bridge_generation"):
                                                    d7_errors.append(f"{board}.bridge_generation 在同一 PID 內改變")
                                                if after_rows[board].get("state") != "READY":
                                                    d7_errors.append(f"{board}.state 在 D7 後不是 READY")
                                        try:
                                            current_dir = _current_bundle_dir(cfg)
                                            current_bundle_tree = _bundle_tree_sha256(current_dir)
                                            current_runtime_tree = _runtime_tree_sha256(current_dir)
                                            if current_dir.resolve() != snapshot.current_dir.resolve():
                                                d7_errors.append("D7 後安裝佈局不再指向 D1 Current")
                                            if current_bundle_tree != snapshot.current_bundle_tree_sha256:
                                                d7_errors.append("D7 後 Current bundle_tree_sha256 改變")
                                            if current_runtime_tree != snapshot.current_runtime_tree_sha256:
                                                d7_errors.append("D7 後 Current runtime_tree_sha256 改變")
                                        except (HarnessError, OSError, ValueError) as exc:
                                            current_bundle_tree = None
                                            current_runtime_tree = None
                                            d7_errors.append(f"D7 後無法確認 Current 內容：{exc}")
                                        d7_ok = not d7_errors
                                        d7_detail = {
                                            "setup": corrupt_result,
                                            "response": corrupt_response,
                                            "before_daemon_status": before_result,
                                            "before_session_list": before_sessions_result,
                                            "after_cli_version": after_version,
                                            "after_daemon_status": after_status_result,
                                            "after_session_list": after_sessions_result,
                                            "current_bundle_dir": str(current_dir) if 'current_dir' in locals() else None,
                                            "current_bundle_tree_sha256": current_bundle_tree,
                                            "current_runtime_tree_sha256": current_runtime_tree,
                                            "errors": d7_errors,
                                        }
                                    except (HarnessError, OSError, ValueError, KeyError, TypeError) as exc:
                                        d7_ok = False
                                        d7_detail = {"errors": [str(exc)]}
                                    add("D7-invalid-artifact-refusal", started, d7_ok, d7_detail)
                                else:
                                    limitations.append("step1_refusal_not_exercised")

                                if cfg.start_fail_from is not None and (not steps or steps[-1].ok):
                                    # D8：新版 daemon 無法啟動時，Previous 必須完整回復並再次可操作。
                                    t8 = now()
                                    started = t8
                                    phase("D8-start-failure-revert", started)
                                    before_result, before_status = _run_json(run, cfg.cli, ("daemon", "status"), timeout_s=15.0)
                                    old_d8_pid = before_status.get("pid") if before_status else None
                                    try:
                                        start_fail = make_start_fail_bundle(cfg.start_fail_from, out_dir / "startfail")
                                        manifest = _read_manifest(start_fail)
                                        cli_component = manifest["components"]["cli"]
                                        start_argv = (str(start_fail / cli_component["path"]), "setup")
                                        start_result, start_response = _setup_result(run, start_argv, timeout_s=cfg.setup_timeout_s)
                                        refusal = _setup_refused(start_result, start_response)
                                        if not _valid_pid(old_d8_pid):
                                            d8_ok = False
                                            d8_detail = {"setup": start_result, "response": start_response, "errors": ["D8 前缺少有效 daemon PID"]}
                                        elif not refusal:
                                            d8_ok = False
                                            d8_detail = {"setup": start_result, "response": start_response, "errors": ["start-fail artifact 未回報啟動失敗"]}
                                        else:
                                            daemon_path = _daemon_path(snapshot.current_dir, snapshot.current_doc)
                                            d8_ok, d8_detail = _verify_switched_state(
                                                cfg,
                                                run,
                                                expected_version=cfg.expect_current,
                                                old_pid=int(old_d8_pid),
                                                since=t8,
                                                snapshot=snapshot,
                                                probe=probe,
                                                clock=clock,
                                                sleep=sleep,
                                                ready_timeout_s=cfg.ready_timeout_s,
                                                daemon_path=daemon_path,
                                                expect_bundle_dir=snapshot.current_dir,
                                                expect_tree=True,
                                            )
                                            d8_detail.update({"setup": start_result, "response": start_response})
                                            try:
                                                current_dir = _current_bundle_dir(cfg)
                                                if current_dir.resolve() == start_fail.resolve():
                                                    d8_detail.setdefault("errors", []).append("start-fail 版本成為 Current")
                                                    d8_ok = False
                                            except (HarnessError, OSError) as exc:
                                                d8_detail.setdefault("errors", []).append(f"無法確認 Current 佈局：{exc}")
                                                d8_ok = False
                                            if d8_ok:
                                                smoke_ok, smoke_detail = _smoke(cfg, run, boards=cfg.boards, marker=marker,
                                                                                clock=clock, sleep=sleep)
                                                d8_detail["smoke"] = smoke_detail
                                                d8_ok = smoke_ok
                                                if not smoke_ok:
                                                    d8_detail.setdefault("errors", []).extend(smoke_detail.get("errors", []))
                                    except (HarnessError, OSError, ValueError, KeyError, TypeError) as exc:
                                        d8_ok = False
                                        d8_detail = {"errors": [str(exc)]}
                                    add("D8-start-failure-revert", started, d8_ok, d8_detail)
                                elif cfg.start_fail_from is None:
                                    limitations.append("step7_revert_not_exercised")

                                failed = any(not step.ok for step in steps)
                                if failed:
                                    verdict = "FAIL"
                                    reason_code = "HARNESS_DRILL_STEP_FAILED"
                                elif limitations:
                                    verdict = "INCOMPLETE"
                                    reason_code = "HARNESS_DRILL_INCOMPLETE"
                                else:
                                    verdict = "PASS"
                                    reason_code = None

    if verdict == "FAIL" and snapshot is not None:
        restore_started = now()
        if _should_restore(cfg, snapshot):
            try:
                forward_argv = _resolve_forward_argv(cfg, snapshot)
                result, doc = _setup_result(run, forward_argv, timeout_s=cfg.setup_timeout_s)
                restore_ok = _response_ok(result, doc)
                restore_detail = {"action": "roll-forward", "setup": result, "response": doc,
                                  "errors": [] if restore_ok else ["restore Current 失敗"]}
            except Exception as exc:
                restore_ok = False
                restore_detail = {"action": "roll-forward", "errors": [f"{type(exc).__name__}: {exc}"]}
        else:
            try:
                layout: Any = str(_current_bundle_dir(cfg))
            except (HarnessError, OSError, ValueError) as exc:
                layout = {"error": f"{type(exc).__name__}: {exc}"}
            restore_ok = False
            restore_detail = {"action": "none", "layout": layout,
                              "errors": ["目前安裝佈局不是 Previous；依程序不自動變更"]}
        restore = _step("restore", restore_started, now, restore_ok, restore_detail)
        state["restore"] = restore

    try:
        finished_at = now()
        report_path = _write_report(
            cfg,
            verdict=verdict,
            reason_code=reason_code,
            started_at=str(state.get("started_at", started)),
            finished_at=finished_at,
            steps=steps,
            restore=restore,
            record=record,
            limitations=limitations,
        )
    except (OSError, TypeError, ValueError) as exc:
        # 報告無法落盤時不可宣稱演練成功。
        verdict = "FAIL"
        reason_code = "HARNESS_DRILL_STEP_FAILED"
        report_path = out_dir / "drill-report.json"
        try:
            jsonio.atomic_write_text(
                report_path,
                jsonio.dumps_report({"schema": "drill-report.v1", "verdict": verdict, "reason_code": reason_code,
                                     "started_at": str(state.get("started_at", started)), "finished_at": jsonio.utc_now_iso(),
                                     "report_error": str(exc), "steps": [asdict(step) for step in steps]}),
            )
        except OSError:
            pass
    state.update(steps=steps, snapshot=snapshot, record=record, restore=restore,
                 limitations=limitations, verdict=verdict, reason_code=reason_code)
    return DrillResult(
        verdict=verdict,
        reason_code=reason_code,
        steps=tuple(steps),
        restore=restore,
        report_path=report_path,
    )


def run_drill(
    cfg: DrillConfig,
    *,
    run: Runner = run_cmd,
    probe: ReleaseProbe | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], str] = jsonio.utc_now_iso,
    marker: Callable[[], str] = lambda: secrets.token_hex(3),
) -> DrillResult:
    """執行 D0～D8；未預期例外也轉成失敗步驟、復原與報告。"""
    state: dict[str, Any] = {}
    try:
        return _run_drill_impl(cfg, run=run, probe=probe, clock=clock, sleep=sleep, now=now,
                               marker=marker, _state=state)
    except Exception as exc:
        def timestamp() -> str:
            try:
                return now()
            except Exception:
                return jsonio.utc_now_iso()

        started_at = state.get("started_at")
        if not isinstance(started_at, str):
            started_at = timestamp()
        finished_at = timestamp()
        phase_id = str(state.get("phase", "D1-record"))
        phase_started = state.get("phase_started_at")
        if not isinstance(phase_started, str):
            phase_started = started_at
        steps = list(state.get("steps", []))
        step_id = phase_id if not steps or steps[-1].step_id != phase_id else f"{phase_id}-exception"
        steps.append(
            StepResult(
                step_id=step_id,
                ok=False,
                detail={"errors": [f"未預期例外：{type(exc).__name__}: {exc}"]},
                started_at=phase_started,
                finished_at=finished_at,
            )
        )
        snapshot = state.get("snapshot")
        record = state.get("record")
        limitations = list(state.get("limitations", []))
        restore: StepResult | None = None
        if isinstance(snapshot, _InstallSnapshot):
            restore_started = timestamp()
            try:
                layout = _current_bundle_dir(cfg)
                should_restore = layout.resolve() == snapshot.previous_dir.resolve()
            except Exception as layout_exc:
                layout = {"error": f"{type(layout_exc).__name__}: {layout_exc}"}
                should_restore = False
            if should_restore:
                try:
                    argv = _resolve_forward_argv(cfg, snapshot)
                    result, response = _setup_result(run, argv, timeout_s=cfg.setup_timeout_s)
                    restored = _response_ok(result, response)
                    detail = {"action": "roll-forward", "setup": result, "response": response,
                              "errors": [] if restored else ["restore Current 失敗"]}
                except Exception as restore_exc:
                    restored = False
                    detail = {"action": "roll-forward", "errors": [f"{type(restore_exc).__name__}: {restore_exc}"]}
            else:
                restored = False
                detail = {"action": "none", "layout": str(layout) if isinstance(layout, Path) else layout,
                          "errors": ["目前安裝佈局不是 Previous；依程序不自動變更"]}
            restore = StepResult("restore", restored, detail, restore_started, timestamp())
        try:
            out_dir = Path(cfg.out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            report_path = _write_report(
                cfg,
                verdict="FAIL",
                reason_code="HARNESS_DRILL_STEP_FAILED",
                started_at=started_at,
                finished_at=finished_at,
                steps=steps,
                restore=restore,
                record=record,
                limitations=limitations,
            )
        except Exception:
            report_path = Path(cfg.out_dir) / "drill-report.json"
        return DrillResult(
            verdict="FAIL",
            reason_code="HARNESS_DRILL_STEP_FAILED",
            steps=tuple(steps),
            restore=restore,
            report_path=report_path,
        )
