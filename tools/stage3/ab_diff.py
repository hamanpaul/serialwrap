"""L1.5 A/B 報告差分工具。

只讀取兩側報告目錄。正規化依據為 ``contracts/ab_normalization.yaml``；不載入
runner、daemon、P2 bridge 或測試模組。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from tools.stage3.jsonio import sha256_file
from tools.stage3.reason_codes import HarnessError


DIFF_SCHEMA = "stage3-ab-diff.v1"

_RUNNERS = ("bridge", "contract-blackbox", "windows-native", "rust-l1")
_BRIDGE_FILES = (
    "manifest.json",
    "comparison.json",
    "bridge-manifest.json",
    "wire.ndjson",
    "cli.ndjson",
    "daemon.log",
    "wal",
)
_RUNNER_FILES: dict[str, tuple[str, ...]] = {
    "contract-blackbox": (
        "source/release-manifest.json",
        "source/l1-results.json",
        "source/junit.xml",
        "source/cli-observations.ndjson",
        "source/invariant-evidence.json",
    ),
    "windows-native": (
        "source/release-manifest.json",
        "source/l1-results.json",
        "source/junit.xml",
        "source/windows-transport-context.json",
        "source/cli-observations.ndjson",
        "source/invariant-evidence.json",
    ),
    "rust-l1": (
        "source/release-manifest.json",
        "source/l1-results.json",
        "source/libtest.stdout.txt",
        "source/cli-observations.ndjson",
        "source/invariant-evidence.json",
    ),
}
_VALID_VERDICTS = frozenset({"PASS", "FAIL"})
_VALID_RUNNER_OUTCOMES = frozenset({"passed", "failed"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
_EXCEPTION = "EXCEPTION"
_ROOT_SEGMENT_CHARS = "A-Za-z0-9._-"
_INSTANCE_ENDPOINT_FLAGS = frozenset({"--socket", "--endpoint", "--bench"})
_ENDPOINT_PATH_FLAGS = frozenset({"--socket", "--endpoint"})
_ROOT_VALUE_OPTIONS = frozenset({"--timeout", "--retries"})
_ROOT_SWITCH_OPTIONS = frozenset({"-h", "--help", "--version", "-v", "--verbose"})
_ALLOWED_RULE_KEYS = frozenset({
    "approved_revision",
    "reviewed_by",
    "mappings",
    "g1_exception_message",
    "g2_set_paths",
    "volatile_keys",
    "instance_root_placeholder",
})

# 擁有者追認前為候選規則：只處理成功 status 回應的自身 PID 掃描快照。
_B11_STATUS_COMMANDS = frozenset({("daemon", "status"), ("health.status",)})
_B11_FOREIGN_HOLDERS_KEY = "foreign_holders"


@dataclass
class _Call:
    argv: list[str]
    rc: int | None
    stdout: str | None
    response: Any = None
    response_valid: bool = False
    source_ref: str = ""


@dataclass
class _Run:
    case_id: str
    runner: str
    path: Path
    report_root: Path
    verdict: str | None = None
    reason: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    invariants: list[dict[str, Any]] = field(default_factory=list)
    calls: list[_Call] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    verdict_ref: str = ""
    l1_document: dict[str, Any] | None = None
    l1_report_valid: bool = False
    instance_roots: tuple[str, ...] = ()
    identity_verified: bool = False
    case_identity_verified: bool = False
    verified_sources: set[str] = field(default_factory=set)
    failure_ref: str = ""
    failure_reason: str = ""
    failure_verified: bool = False
    failure_records: list[dict[str, Any]] = field(default_factory=list)
    invariant_failures: list[dict[str, Any]] = field(default_factory=list)
    unverified_invariant_failures: list[dict[str, Any]] = field(default_factory=list)
    case_map: dict[str, str] | None = None

    @property
    def run_index(self) -> str:
        return self.path.name

    def ref(self, relative: str, fragment: str = "") -> str:
        root_relative = self.path.relative_to(self.report_root).as_posix()
        result = f"{root_relative}/{relative}" if relative else root_relative
        return f"{result}{fragment}"


def _harness(detail: str) -> HarnessError:
    return HarnessError("HARNESS_COMPARATOR_ERROR", detail)


def _read_bytes(path: Path) -> bytes:
    return Path(path).read_bytes()


def _json_document(path: Path, run: _Run, relative: str, *, required: bool = True) -> Any:
    if not path.is_file():
        if required:
            _add_problem(run.problems, f"missing source file: {relative}")
        return None
    try:
        return json.loads(_read_bytes(path).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        _add_problem(run.problems, f"invalid JSON: {relative}")
        return None


def _json_object(path: Path, run: _Run, relative: str, *, required: bool = True) -> dict[str, Any] | None:
    document = _json_document(path, run, relative, required=required)
    if document is None:
        return None
    if not isinstance(document, dict):
        _add_problem(run.problems, f"invalid JSON object: {relative}")
        return None
    return document


def _add_problem(problems: list[str], message: str) -> None:
    if message not in problems:
        problems.append(message)


def _run_key(name: str) -> tuple[int, int, str]:
    if name.isascii() and name.isdecimal():
        return (0, int(name), name)
    return (1, 0, name)


def load_case_runs(report_dir: Path) -> dict[str, list[Path]]:
    """載入 bridge 與三種 runner 的 run 目錄，並依 case 及 run 序號排序。"""
    try:
        root = Path(report_dir)
    except (TypeError, ValueError) as exc:
        raise _harness(f"報告目錄參數無效：{exc}") from exc
    if not root.exists():
        return {}
    if not root.is_dir():
        raise _harness("報告目錄不是目錄")
    found: dict[str, list[Path]] = {}
    try:
        case_dirs = sorted((entry for entry in root.iterdir() if entry.is_dir()), key=lambda item: item.name)
        for case_dir in case_dirs:
            case_runs: list[Path] = []
            for runner in _RUNNERS:
                runner_dir = case_dir / runner
                if runner_dir.is_dir():
                    case_runs.extend(path for path in runner_dir.iterdir() if path.is_dir())
            if case_runs:
                found[case_dir.name] = sorted(case_runs, key=lambda item: (_run_key(item.name), item.parent.name))
    except OSError as exc:
        raise _harness(f"無法列舉報告目錄：{exc}") from exc
    return found


def _runner_for_path(path: Path) -> str:
    return path.parent.name


def _safe_relative(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        return None
    return parsed.as_posix()


def _ensure_required_files(run: _Run, files: Iterable[str]) -> None:
    for relative in files:
        candidate = run.path / Path(*PurePosixPath(relative).parts)
        if relative == "wal":
            exists = candidate.is_dir()
        else:
            exists = candidate.is_file()
        if not exists:
            _add_problem(run.problems, f"missing source file: {relative}")


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _nested(value: Any, *parts: str) -> Any:
    current = value
    for part in parts:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _check_identity_fields(run: _Run) -> bool:
    """依 daemon implementation 驗證適用的 artifact 身分欄位。"""
    valid = True
    for dotted in ("cli.sha256", "release_manifest_sha256"):
        value = _nested(run.artifacts, *dotted.split("."))
        if not _valid_sha256(value):
            _add_problem(run.problems, f"missing artifact identity: artifacts.{dotted}")
            valid = False
    implementation = _nested(run.artifacts, "daemon", "implementation")
    if not isinstance(implementation, str) or implementation not in {"python", "rust"}:
        _add_problem(run.problems, "missing artifact identity: artifacts.daemon.implementation")
        valid = False
    if implementation == "python":
        tree_sha = _nested(run.artifacts, "daemon", "tree_sha256")
        if not _valid_sha256(tree_sha):
            _add_problem(run.problems, "missing artifact identity: artifacts.daemon.tree_sha256")
            valid = False
    elif implementation == "rust":
        daemon_sha = _nested(run.artifacts, "daemon", "sha256")
        if not _valid_sha256(daemon_sha):
            _add_problem(run.problems, "missing artifact identity: artifacts.daemon.sha256")
            valid = False
    return valid


def _bridge_artifacts(manifest: Mapping[str, Any]) -> dict[str, Any]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return {}
    cli = artifacts.get("cli")
    daemon = artifacts.get("daemon")
    return {
        "cli": {"sha256": _nested(cli, "sha256")},
        "daemon": {
            "implementation": _nested(daemon, "implementation"),
            "sha256": _nested(daemon, "sha256"),
            "tree_sha256": _nested(daemon, "package_tree_sha256"),
        },
        "release_manifest_sha256": artifacts.get("release_manifest_sha256"),
    }


def _read_ndjson(path: Path, run: _Run, relative: str) -> list[tuple[int, dict[str, Any]]]:
    if not path.is_file():
        return []
    try:
        raw = _read_bytes(path)
    except OSError:
        _add_problem(run.problems, f"unreadable source file: {relative}")
        return []
    rows: list[tuple[int, dict[str, Any]]] = []
    for line_number, raw_line in enumerate(raw.split(b"\n"), start=1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError):
            _add_problem(run.problems, f"invalid JSON: {relative}#L{line_number}")
            continue
        if not isinstance(value, dict):
            _add_problem(run.problems, f"invalid JSON object: {relative}#L{line_number}")
            continue
        rows.append((line_number, value))
    return rows


def _parse_stdout(run: _Run, stdout: Any, relative_ref: str) -> tuple[Any, bool]:
    if not isinstance(stdout, str):
        return None, False
    try:
        return json.loads(stdout), True
    except (ValueError, json.JSONDecodeError, RecursionError):
        _add_problem(run.problems, f"invalid JSON: {relative_ref}")
        return None, False


def _parse_bridge_calls(run: _Run) -> None:
    relative = "cli.ndjson"
    rows = _read_ndjson(run.path / relative, run, relative)
    for line_number, row in rows:
        ref = run.ref(relative, f"#L{line_number}")
        argv = row.get("argv")
        rc = row.get("rc")
        stdout = row.get("stdout")
        argv_valid = isinstance(argv, list) and all(isinstance(token, str) for token in argv)
        if not argv_valid or not argv or not _has_logical_command(argv):
            _add_problem(
                run.problems,
                f"missing required observation: cli_observations[{len(run.calls)}].argv ({ref})",
            )
        if not isinstance(rc, int) or isinstance(rc, bool):
            _add_problem(run.problems, f"missing required observation: cli_observations[{len(run.calls)}].rc")
            rc = None
        if not isinstance(stdout, str):
            _add_problem(run.problems, f"missing required observation: cli_observations[{len(run.calls)}].stdout")
        response, response_valid = _parse_stdout(run, stdout, ref)
        run.calls.append(_Call(
            argv=list(argv) if argv_valid else [],
            rc=rc,
            stdout=stdout if isinstance(stdout, str) else None,
            response=response,
            response_valid=response_valid,
            source_ref=ref,
        ))
    if not run.calls:
        _add_problem(run.problems, "missing required observation: cli.ndjson")
    run.instance_roots = _instance_roots(run)


def _manifest_evidence(manifest: Mapping[str, Any], run: _Run) -> None:
    evidence = manifest.get("evidence")
    if not isinstance(evidence, Mapping):
        _add_problem(run.problems, "invalid manifest evidence")
        return
    required_paths = {
        "wire": "wire.ndjson",
        "control": "control.ndjson",
        "correlation": "correlation.ndjson",
        "dut_stderr": "dut.stderr.log",
        "cli_log": "cli.ndjson",
        "wal_dir": "wal",
    }
    for key, expected_path in required_paths.items():
        if key not in evidence:
            _add_problem(run.problems, f"missing manifest evidence: evidence.{key}")
            continue
        item = evidence[key]
        declared_path = item if key == "wal_dir" else item.get("path") if isinstance(item, Mapping) else None
        if declared_path != expected_path:
            _add_problem(run.problems, f"invalid manifest path: evidence.{key}")
    for key, item in evidence.items():
        if key == "wal_dir":
            relative = _safe_relative(item)
            if relative is None:
                _add_problem(run.problems, "invalid manifest path: evidence.wal_dir")
            elif not (run.path / Path(*PurePosixPath(relative).parts)).is_dir():
                _add_problem(run.problems, f"missing source file: {relative}")
            else:
                run.verified_sources.add(relative)
            continue
        if not isinstance(item, Mapping):
            _add_problem(run.problems, f"invalid manifest evidence: {key}")
            continue
        relative = _safe_relative(item.get("path"))
        if relative is None:
            _add_problem(run.problems, f"invalid manifest path: evidence.{key}.path")
            continue
        candidate = run.path / Path(*PurePosixPath(relative).parts)
        if not candidate.is_file():
            _add_problem(run.problems, f"missing source file: {relative}")
            continue
        expected = item.get("sha256")
        try:
            actual = sha256_file(candidate)
        except OSError:
            _add_problem(run.problems, f"unreadable source file: {relative}")
            continue
        if not _valid_sha256(expected) or actual != expected:
            _add_problem(run.problems, f"source hash mismatch: {relative}")
        else:
            run.verified_sources.add(relative)


def _comparison_integrity(comparison: Mapping[str, Any], run: _Run) -> None:
    integrity = comparison.get("integrity")
    if not isinstance(integrity, Mapping):
        _add_problem(run.problems, "invalid comparison integrity")
        return
    wire_sha = integrity.get("wire_sha256")
    expected_wire_sha = integrity.get("wire_sha256_expected")
    if not _valid_sha256(wire_sha):
        _add_problem(run.problems, "invalid comparison integrity: wire_sha256")
    if not _valid_sha256(expected_wire_sha):
        _add_problem(run.problems, "invalid comparison integrity: wire_sha256_expected")
    elif wire_sha != expected_wire_sha:
        _add_problem(run.problems, "comparison integrity mismatch: wire_sha256")
    wire_path = run.path / "wire.ndjson"
    if wire_path.is_file():
        try:
            actual_wire_sha = sha256_file(wire_path)
        except OSError:
            actual_wire_sha = ""
            _add_problem(run.problems, "unreadable source file: wire.ndjson")
        if actual_wire_sha and (wire_sha != actual_wire_sha or expected_wire_sha != actual_wire_sha):
            _add_problem(run.problems, "comparison integrity mismatch: wire_sha256")
    wal_files = integrity.get("wal_files")
    if not isinstance(wal_files, list) or not all(isinstance(item, str) and item for item in wal_files):
        _add_problem(run.problems, "invalid comparison integrity: wal_files")
    else:
        for item in wal_files:
            relative = _safe_relative(item)
            if relative is None:
                _add_problem(run.problems, "invalid comparison integrity: wal_files")
                continue
            if not relative.startswith("wal/"):
                relative = f"wal/{relative}"
            if not (run.path / Path(*PurePosixPath(relative).parts)).is_file():
                _add_problem(run.problems, f"missing source file: {relative}")
    wal_records = integrity.get("wal_records")
    if not isinstance(wal_records, int) or isinstance(wal_records, bool) or wal_records < 0:
        _add_problem(run.problems, "invalid comparison integrity: wal_records")
    reported_problems = integrity.get("problems")
    if not isinstance(reported_problems, list):
        _add_problem(run.problems, "invalid comparison integrity: problems")
    elif reported_problems:
        _add_problem(run.problems, "comparison reports integrity problems")


def _load_bridge_run(run: _Run) -> None:
    _ensure_required_files(run, _BRIDGE_FILES)
    manifest = _json_object(run.path / "manifest.json", run, "manifest.json")
    comparison = _json_object(run.path / "comparison.json", run, "comparison.json")
    bridge_manifest = _json_object(run.path / "bridge-manifest.json", run, "bridge-manifest.json")

    if manifest is not None:
        if manifest.get("schema_version") != "l15-manifest.v1":
            _add_problem(run.problems, "invalid manifest schema_version")
        run.artifacts = _bridge_artifacts(manifest)
        run.identity_verified = _check_identity_fields(run)
        _manifest_evidence(manifest, run)
        expected_bridge_hash = manifest.get("bridge_manifest_sha256")
        bridge_path = run.path / "bridge-manifest.json"
        if bridge_path.is_file():
            try:
                if not isinstance(expected_bridge_hash, str) or sha256_file(bridge_path) != expected_bridge_hash:
                    _add_problem(run.problems, "source hash mismatch: bridge-manifest.json")
            except OSError:
                _add_problem(run.problems, "unreadable source file: bridge-manifest.json")

    if comparison is not None:
        run.verdict_ref = run.ref("comparison.json")
        if comparison.get("schema_version") != "comparison.v1":
            _add_problem(run.problems, "invalid comparison schema_version")
        _comparison_integrity(comparison, run)
        verdict = comparison.get("verdict")
        run.verdict = verdict if isinstance(verdict, str) else None
        if verdict == _INSUFFICIENT:
            _add_problem(run.problems, f"invalid comparison verdict: {_INSUFFICIENT}")
        elif not isinstance(verdict, str) or verdict not in _VALID_VERDICTS:
            _add_problem(run.problems, f"invalid comparison verdict: {verdict!r}")
        elif verdict == "FAIL":
            run.failure_ref = run.verdict_ref
            reasons = comparison.get("reasons")
            run.failure_reason = "; ".join(item for item in reasons if isinstance(item, str)) if isinstance(reasons, list) else ""
            run.failure_records.append({
                "source_ref": run.verdict_ref,
                "reason": run.failure_reason or "bridge comparison verdict FAIL",
                "verified": False,
            })
        reasons = comparison.get("reasons")
        if isinstance(reasons, list):
            run.reason = "; ".join(str(reason) for reason in reasons if isinstance(reason, str))
        if not isinstance(comparison.get("invariants"), list):
            _add_problem(run.problems, "invalid comparison invariants")
        else:
            for index, invariant in enumerate(comparison["invariants"]):
                if not isinstance(invariant, Mapping):
                    _add_problem(run.problems, f"invalid comparison invariant: {index}")
                    continue
                invariant_id = invariant.get("id")
                invariant_verdict = invariant.get("verdict")
                if not _nonempty_string(invariant_id) or not isinstance(invariant_verdict, str) or invariant_verdict not in _VALID_VERDICTS:
                    _add_problem(run.problems, f"invalid comparison invariant: {index}")
                    if invariant_verdict == "FAIL":
                        run.unverified_invariant_failures.append({
                            "id": invariant_id,
                            "source_ref": run.ref("comparison.json", f"#/invariants/{index}"),
                            "reason": "invariant FAIL has a missing or invalid id",
                        })
                    continue
                run.invariants.append({"id": invariant_id, "result": invariant_verdict})
                if invariant_verdict == "FAIL":
                    run.invariant_failures.append({
                        "id": invariant_id,
                        "source_ref": run.ref("comparison.json", f"#/invariants/{index}"),
                        "reason": f"invariant {invariant_id} FAIL",
                    })

    if bridge_manifest is not None:
        if bridge_manifest.get("closed_cleanly") is not True:
            _add_problem(run.problems, "bridge not closed cleanly: bridge-manifest.json.closed_cleanly")
            _add_problem(run.problems, "runner incomplete: bridge")
        if bridge_manifest.get("failure") is not None:
            _add_problem(run.problems, "bridge failure: bridge-manifest.json.failure")
            _add_problem(run.problems, "bridge-manifest.failure")
            _add_problem(run.problems, "runner incomplete: bridge")

    _parse_bridge_calls(run)
    if manifest is not None and comparison is not None:
        manifest_case = manifest.get("case_id")
        comparison_case = comparison.get("case_id")
        manifest_run = manifest.get("run_id")
        comparison_run = comparison.get("run_id")
        identity_ok = (
            isinstance(run.run_index, str)
            and run.run_index.isascii()
            and run.run_index.isdecimal()
            and int(run.run_index) > 0
            and manifest_case == run.case_id
            and comparison_case == run.case_id
            and _nonempty_string(manifest_run)
            and comparison_run == manifest_run
        )
        if not identity_ok:
            _add_problem(run.problems, "bridge case/run identity mismatch")
        run.case_identity_verified = bool(identity_ok)
        run.failure_verified = run.identity_verified and run.case_identity_verified
        for failure in run.failure_records:
            failure["verified"] = run.failure_verified
    else:
        _add_problem(run.problems, "bridge case/run identity unavailable")


def _source_file_map(run: _Run, envelope: Mapping[str, Any]) -> dict[str, str]:
    rows = envelope.get("source_files")
    if not isinstance(rows, list):
        _add_problem(run.problems, "invalid source_files")
        return {}
    hashes: dict[str, str] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            _add_problem(run.problems, f"invalid source_files[{index}]")
            continue
        relative = _safe_relative(row.get("path"))
        digest = row.get("sha256")
        if relative is None:
            _add_problem(run.problems, f"invalid source file path: source_files[{index}]")
            continue
        if not _valid_sha256(digest):
            _add_problem(run.problems, f"missing source hash: {relative}")
            continue
        if relative in hashes:
            _add_problem(run.problems, f"duplicate source file: {relative}")
            continue
        hashes[relative] = digest
        candidate = run.path / Path(*PurePosixPath(relative).parts)
        try:
            candidate.resolve().relative_to(run.path.resolve())
        except (OSError, ValueError):
            _add_problem(run.problems, f"invalid source file path: {relative}")
            continue
        if not candidate.is_file():
            _add_problem(run.problems, f"missing source file: {relative}")
            continue
        try:
            if sha256_file(candidate) != digest:
                _add_problem(run.problems, f"source hash mismatch: {relative}")
            else:
                run.verified_sources.add(relative)
        except OSError:
            _add_problem(run.problems, f"unreadable source file: {relative}")
    for relative in _RUNNER_FILES[run.runner]:
        if relative not in hashes:
            _add_problem(run.problems, f"missing source file: {relative}")
        candidate = run.path / Path(*PurePosixPath(relative).parts)
        if not candidate.is_file():
            _add_problem(run.problems, f"missing source file: {relative}")
    return hashes


def _release_identity(run: _Run, release: Mapping[str, Any], digest: str) -> bool:
    cli = _nested(release, "components", "cli")
    daemon = _nested(release, "components", "daemon")
    implementation = _nested(run.artifacts, "daemon", "implementation")
    expected = {
        "cli.sha256": _nested(cli, "sha256"),
        "daemon.implementation": _nested(daemon, "implementation"),
        "release_manifest_sha256": digest,
    }
    if implementation == "python":
        expected["daemon.tree_sha256"] = _nested(daemon, "package", "tree_sha256")
    elif implementation == "rust":
        expected["daemon.sha256"] = _nested(daemon, "launcher", "sha256")
    else:
        _add_problem(run.problems, "missing artifact identity: artifacts.daemon.implementation")
        return False
    valid = True
    for dotted, source_value in expected.items():
        envelope_value = _nested(run.artifacts, *dotted.split("."))
        if dotted == "daemon.implementation":
            values_valid = isinstance(source_value, str) and isinstance(envelope_value, str)
        else:
            values_valid = _valid_sha256(source_value) and _valid_sha256(envelope_value)
        if not values_valid or source_value != envelope_value:
            _add_problem(run.problems, f"artifact identity mismatch: artifacts.{dotted}")
            valid = False
    return valid


def _source_ref_path(report_root: Path, source_ref: str) -> tuple[Path | None, int | None]:
    path_text, marker, suffix = source_ref.partition("#")
    relative = _safe_relative(path_text)
    if relative is None:
        return None, None
    line_number: int | None = None
    if marker:
        match = re.fullmatch(r"L([1-9][0-9]*)", suffix)
        if match is None:
            return None, None
        line_number = int(match.group(1))
    candidate = report_root / Path(*PurePosixPath(relative).parts)
    try:
        candidate.resolve().relative_to(report_root.resolve())
    except (OSError, ValueError):
        return None, None
    if not candidate.is_file():
        return None, None
    if line_number is not None:
        try:
            lines = _read_bytes(candidate).split(b"\n")
        except OSError:
            return None, None
        if line_number > len(lines) or not lines[line_number - 1].strip():
            return None, None
    return candidate, line_number


def _validate_cli_source_line(
    run: _Run, source_ref: Any, index: int, observation: Mapping[str, Any], raw_rows: Mapping[int, dict[str, Any]],
) -> str:
    fallback = run.ref("source/cli-observations.ndjson")
    if not isinstance(source_ref, str):
        _add_problem(run.problems, f"missing required observation: cli_observations[{index}].source_ref")
        return fallback
    source_path, line_number = _source_ref_path(run.report_root, source_ref)
    if source_path is None:
        _add_problem(run.problems, f"invalid source_ref: cli_observations[{index}].source_ref")
        return fallback
    if line_number is None:
        _add_problem(run.problems, f"invalid source_ref: cli_observations[{index}].source_ref")
        return fallback
    try:
        relative = source_path.relative_to(run.path).as_posix()
    except ValueError:
        _add_problem(run.problems, f"invalid source_ref: cli_observations[{index}].source_ref")
        return fallback
    if relative != "source/cli-observations.ndjson":
        _add_problem(run.problems, f"invalid source_ref: cli_observations[{index}].source_ref")
        return fallback
    row = raw_rows.get(line_number)
    if row is None:
        _add_problem(run.problems, f"invalid source_ref: cli_observations[{index}].source_ref")
        return fallback
    if row.get("call_index") != index:
        _add_problem(run.problems, f"source observation index mismatch: {relative}#L{line_number}")
    for field_name in ("argv", "rc", "stdout"):
        if field_name in observation and row.get(field_name) != observation[field_name]:
            _add_problem(run.problems, f"source observation mismatch: cli_observations[{index}].{field_name}")
    return run.ref(relative, f"#L{line_number}")


def _parse_runner_calls(run: _Run, envelope: Mapping[str, Any]) -> None:
    observations = envelope.get("cli_observations")
    raw_relative = "source/cli-observations.ndjson"
    raw_rows_list = _read_ndjson(run.path / raw_relative, run, raw_relative)
    raw_rows = {line: row for line, row in raw_rows_list}
    if not isinstance(observations, list):
        _add_problem(run.problems, "missing required observation: cli_observations")
        return
    if not observations:
        _add_problem(run.problems, "missing required observation: cli_observations[0]")
    if len(observations) != len(raw_rows_list):
        _add_problem(run.problems, "source observation count mismatch: source/cli-observations.ndjson")
    for index, observation in enumerate(observations):
        if not isinstance(observation, Mapping):
            _add_problem(run.problems, f"missing required observation: cli_observations[{index}]")
            continue
        argv = observation.get("argv")
        rc = observation.get("rc")
        stdout = observation.get("stdout")
        argv_valid = isinstance(argv, list) and all(isinstance(token, str) for token in argv)
        if not argv_valid or not argv or not _has_logical_command(argv):
            _add_problem(run.problems, f"missing required observation: cli_observations[{index}].argv")
        if not isinstance(rc, int) or isinstance(rc, bool):
            _add_problem(run.problems, f"missing required observation: cli_observations[{index}].rc")
            rc = None
        if not isinstance(stdout, str):
            _add_problem(run.problems, f"missing required observation: cli_observations[{index}].stdout")
        source_ref = _validate_cli_source_line(
            run, observation.get("source_ref"), index, observation, raw_rows,
        )
        response, response_valid = _parse_stdout(run, stdout, source_ref or f"{raw_relative}#L{index + 1}")
        run.calls.append(_Call(
            argv=list(argv) if argv_valid else [],
            rc=rc,
            stdout=stdout if isinstance(stdout, str) else None,
            response=response,
            response_valid=response_valid,
            source_ref=source_ref,
        ))
    run.instance_roots = _instance_roots(run)


def _instance_roots(run: _Run) -> tuple[str, ...]:
    """從本 run 的 endpoint 與 status 路徑擷取實例根目錄。"""
    roots: set[str] = set()
    for call in run.calls:
        argv = call.argv
        endpoint_values: list[str] = []
        for index, token in enumerate(argv[:-1]):
            if token in _ENDPOINT_PATH_FLAGS:
                endpoint_values.append(argv[index + 1])
            else:
                endpoint_values.extend(
                    token.partition("=")[2]
                    for flag in _ENDPOINT_PATH_FLAGS
                    if token.startswith(f"{flag}=")
                )
        for raw_endpoint in endpoint_values:
            endpoint = raw_endpoint.replace("\\", "/")
            parts = PurePosixPath(endpoint).parts
            if len(parts) >= 3 and parts[-2:] == ("run", "serialwrapd.sock"):
                root = str(PurePosixPath(*parts[:-2]))
                if root and root != ".":
                    roots.add(root)
        if not isinstance(call.response, Mapping):
            continue
        wal = call.response.get("wal")
        wal_dir = wal.get("wal_dir") if isinstance(wal, Mapping) else None
        if isinstance(wal_dir, str):
            normalized = wal_dir.replace("\\", "/")
            parts = PurePosixPath(normalized).parts
            if parts and parts[-1] == "wal":
                root = str(PurePosixPath(*parts[:-1]))
                if root and root != ".":
                    roots.add(root)
        mirror = call.response.get("mirror_path")
        if isinstance(mirror, str):
            parts = PurePosixPath(mirror.replace("\\", "/")).parts
            if len(parts) >= 2 and parts[-2] == "wal":
                root = str(PurePosixPath(*parts[:-2]))
                if root and root != ".":
                    roots.add(root)
    return tuple(sorted(roots, key=lambda root: (-len(root), root)))


def _load_l1_report(run: _Run) -> None:
    relative = "source/l1-results.json"
    document = _json_object(run.path / relative, run, relative)
    if document is None:
        return
    run.l1_document = document
    if document.get("schema_version") != 2 or document.get("format") != "l1-results.v1":
        _add_problem(run.problems, f"invalid runner source: {relative}")
    else:
        run.l1_report_valid = True


def _load_runner_invariants(run: _Run, envelope: Mapping[str, Any] | None) -> None:
    """獨立保全原始 invariant；envelope 僅作一致性核對。"""
    source = _json_object(run.path / "source/invariant-evidence.json", run, "source/invariant-evidence.json")
    source_items = source.get("invariants") if source is not None else None
    normalized_source: list[dict[str, str]] = []
    if not isinstance(source_items, list):
        if source is not None:
            _add_problem(run.problems, "invalid invariant evidence")
    else:
        if source.get("schema_version") != "stage3-invariant-evidence.v1":
            _add_problem(run.problems, "invalid invariant evidence schema_version")
        for index, item in enumerate(source_items):
            if not isinstance(item, Mapping):
                _add_problem(run.problems, f"invalid invariant evidence item: {index}")
                continue
            invariant_id = item.get("id")
            result = item.get("result")
            if not _nonempty_string(invariant_id) or not isinstance(result, str) or result not in _VALID_VERDICTS:
                _add_problem(run.problems, f"invalid invariant evidence item: {index}")
                if result == "FAIL":
                    run.unverified_invariant_failures.append({
                        "id": invariant_id,
                        "source_ref": run.ref("source/invariant-evidence.json", f"#/invariants/{index}"),
                        "reason": "invariant FAIL has a missing or invalid id",
                    })
                continue
            normalized_source.append({"id": invariant_id, "result": result})
            if result == "FAIL":
                run.invariant_failures.append({
                    "id": invariant_id,
                    "source_ref": run.ref("source/invariant-evidence.json", f"#/invariants/{index}"),
                    "reason": f"invariant {invariant_id} FAIL",
                })
    run.invariants = normalized_source

    if envelope is None:
        return
    envelope_items = envelope.get("invariants")
    if not isinstance(envelope_items, list):
        _add_problem(run.problems, "invalid runner invariants")
        return
    normalized_envelope: list[dict[str, str]] = []
    for index, item in enumerate(envelope_items):
        if not isinstance(item, Mapping):
            _add_problem(run.problems, f"invalid runner invariant: {index}")
            continue
        invariant_id = item.get("id")
        result = item.get("result")
        if not _nonempty_string(invariant_id) or not isinstance(result, str) or result not in _VALID_VERDICTS:
            _add_problem(run.problems, f"invalid runner invariant: {index}")
            if result == "FAIL":
                run.unverified_invariant_failures.append({
                    "id": invariant_id,
                    "source_ref": run.ref("runner-observation.json", f"#/invariants/{index}"),
                    "reason": "envelope invariant FAIL has a missing or invalid id",
                })
            continue
        normalized_envelope.append({"id": invariant_id, "result": result})
        if result == "FAIL" and {"id": invariant_id, "result": result} not in normalized_source:
            run.unverified_invariant_failures.append({
                "id": invariant_id,
                "source_ref": run.ref("runner-observation.json", f"#/invariants/{index}"),
                "reason": f"invariant {invariant_id} FAIL is not corroborated by raw invariant evidence",
            })
    if normalized_source != normalized_envelope:
        _add_problem(run.problems, "invariant evidence mismatch")


def _load_runner_case_results(run: _Run, envelope: Mapping[str, Any]) -> None:
    case_identity_ok = (
        envelope.get("case_id") == run.case_id
        and envelope.get("runner") == run.runner
        and str(envelope.get("run_index")) == run.run_index
        and run.run_index.isascii()
        and run.run_index.isdecimal()
        and int(run.run_index) > 0
    )
    if not case_identity_ok:
        _add_problem(run.problems, "runner observation identity mismatch")

    case_map = envelope.get("case_map")
    case_map_ok = False
    if not isinstance(case_map, Mapping):
        _add_problem(run.problems, "invalid case_map")
    else:
        map_path = _safe_relative(case_map.get("path"))
        map_sha = case_map.get("sha256")
        if map_path is None or not _valid_sha256(map_sha):
            _add_problem(run.problems, "invalid case_map")
        else:
            run.case_map = {"path": map_path, "sha256": map_sha}
            case_map_ok = True

    rows = envelope.get("case_results")
    if not isinstance(rows, list):
        _add_problem(run.problems, "invalid case_results")
        rows = []
    if not rows:
        _add_problem(run.problems, "missing required observation: case_results")

    document = run.l1_document
    raw_results = document.get("results") if isinstance(document, Mapping) else None
    collected = document.get("collected") if isinstance(document, Mapping) else None
    if not isinstance(raw_results, list):
        _add_problem(run.problems, "invalid runner results: source/l1-results.json")
        raw_results = []
    if not isinstance(collected, list) or not all(isinstance(item, str) for item in collected):
        _add_problem(run.problems, "invalid runner collected: source/l1-results.json")
        collected = []

    mapped_indices: set[int] = set()
    verified_mapped_indices: set[int] = set()
    executed_outcomes: list[str] = []
    for map_index, mapping in enumerate(rows):
        if not isinstance(mapping, Mapping):
            _add_problem(run.problems, f"invalid case_results[{map_index}]")
            continue
        nodeid = mapping.get("nodeid")
        source_ref = mapping.get("source_ref")
        if not _nonempty_string(nodeid) or not isinstance(source_ref, str):
            _add_problem(run.problems, f"invalid case_results[{map_index}]")
            continue
        match = re.fullmatch(
            re.escape(run.ref("source/l1-results.json")) + r"#results\[([0-9]+)\]",
            source_ref,
        )
        if match is None:
            _add_problem(run.problems, f"invalid case result source_ref: case_results[{map_index}]")
            continue
        result_index = int(match.group(1))
        if result_index >= len(raw_results):
            _add_problem(run.problems, f"missing case result: case_results[{map_index}]")
            continue
        if result_index in mapped_indices:
            _add_problem(run.problems, f"duplicate case result: case_results[{map_index}]")
            continue
        mapped_indices.add(result_index)
        raw_row = raw_results[result_index]
        if not isinstance(raw_row, Mapping):
            _add_problem(run.problems, f"invalid runner result: results[{result_index}]")
            continue
        if raw_row.get("nodeid") != nodeid:
            _add_problem(run.problems, f"case result nodeid mismatch: results[{result_index}]")
            continue
        if nodeid not in collected:
            _add_problem(run.problems, f"case result was not collected: results[{result_index}]")
            continue
        outcome = raw_row.get("outcome")
        if not isinstance(outcome, str):
            _add_problem(run.problems, f"invalid runner result outcome: results[{result_index}]")
            continue
        if outcome not in _VALID_RUNNER_OUTCOMES:
            _add_problem(run.problems, f"required case result not executed: {outcome}")
            continue
        verified_mapped_indices.add(result_index)
        executed_outcomes.append(outcome)

    mapped_failed_indices: set[int] = set()
    for result_index, raw_row in enumerate(raw_results):
        if not isinstance(raw_row, Mapping) or raw_row.get("outcome") != "failed":
            continue
        if result_index in verified_mapped_indices:
            mapped_failed_indices.add(result_index)
        source_ref = run.ref("source/l1-results.json", f"#results[{result_index}]")
        verified = (
            result_index in verified_mapped_indices
            and case_identity_ok
            and case_map_ok
            and run.identity_verified
            and run.l1_report_valid
            and "source/l1-results.json" in run.verified_sources
        )
        reason = (
            run.reason if verified and run.reason
            else "原始 runner FAIL 尚未核實對應此 required case"
        )
        run.failure_records.append({"source_ref": source_ref, "reason": reason, "verified": verified})
    if run.failure_records:
        run.failure_ref = run.failure_records[0]["source_ref"]
        run.failure_reason = run.failure_records[0]["reason"]
        run.failure_verified = bool(run.failure_records[0]["verified"])

    if not executed_outcomes:
        _add_problem(run.problems, "required case has no executed result: source/l1-results.json")
        run.verdict = None
    else:
        raw_verdict = "FAIL" if "failed" in executed_outcomes else "PASS"
        envelope_verdict = envelope.get("verdict")
        if not isinstance(envelope_verdict, str) or envelope_verdict not in _VALID_VERDICTS:
            _add_problem(run.problems, f"invalid runner verdict: {envelope_verdict!r}")
        elif envelope_verdict != raw_verdict:
            _add_problem(run.problems, "runner result mismatch: verdict")
        run.verdict = raw_verdict
    run.case_identity_verified = bool(case_identity_ok and case_map_ok)

    if envelope.get("verdict") == "FAIL" and not (
        case_identity_ok and case_map_ok and mapped_failed_indices
    ):
        run.failure_records.append({
            "source_ref": run.ref("runner-observation.json"),
            "reason": run.reason or "runner envelope verdict FAIL is not corroborated by raw L1 results",
            "verified": False,
        })


def _runner_completion(run: _Run, envelope: Mapping[str, Any]) -> None:
    completion = envelope.get("completion")
    if not isinstance(completion, Mapping):
        _add_problem(run.problems, f"runner incomplete: {run.runner}")
        completion = {}
    status = completion.get("status")
    finished = completion.get("finished")
    exit_code = completion.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        _add_problem(run.problems, "missing required observation: completion.exit_code")
    elif exit_code != 0:
        _add_problem(run.problems, f"runner exit code nonzero: {run.runner}")
    if status != "completed" or finished is not True:
        _add_problem(run.problems, f"runner incomplete: {run.runner}")
    if run.l1_document is not None:
        if run.l1_document.get("finished") is not True:
            _add_problem(run.problems, f"runner incomplete: {run.runner}")
        source_exit = run.l1_document.get("exit_code")
        if not isinstance(source_exit, int) or isinstance(source_exit, bool):
            _add_problem(run.problems, "missing required observation: source/l1-results.json.exit_code")
        else:
            if source_exit != 0:
                _add_problem(run.problems, f"runner source exit code nonzero: {run.runner}")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool) and source_exit != exit_code:
                _add_problem(run.problems, "runner exit code mismatch: source/l1-results.json")
    if run.runner == "rust-l1":
        provenance = run.l1_document.get("provenance") if run.l1_document is not None else None
        if not _valid_cargo_provenance(provenance, run.l1_document):
            _add_problem(run.problems, f"runner provenance mismatch: source/l1-results.json")


def _valid_cargo_provenance(provenance: Any, report: Mapping[str, Any] | None) -> bool:
    """驗證 cargo_l1_results._provenance() 真正輸出的欄位，不檢查虛構完成欄位。"""
    if not isinstance(provenance, Mapping) or report is None:
        return False
    if provenance.get("schema") != "cargo-l1-provenance.v1":
        return False
    platform = provenance.get("platform")
    if not isinstance(platform, str) or platform not in {"linux", "windows"} or platform != report.get("platform"):
        return False
    if provenance.get("build_target") is not None and not _nonempty_string(provenance.get("build_target")):
        return False
    git = provenance.get("git")
    if not isinstance(git, Mapping) or not isinstance(git.get("clean"), bool):
        return False
    if git.get("head") is not None and not isinstance(git.get("head"), str):
        return False
    end = git.get("end")
    if not isinstance(end, Mapping) or not isinstance(end.get("clean"), bool):
        return False
    if end.get("head") is not None and not isinstance(end.get("head"), str):
        return False
    toolchain = provenance.get("toolchain")
    if not isinstance(toolchain, Mapping) or not {"rustc_vv", "rustc_vv_sha256", "error"} <= set(toolchain):
        return False
    if toolchain.get("rustc_vv") is not None and not isinstance(toolchain.get("rustc_vv"), str):
        return False
    if toolchain.get("rustc_vv_sha256") is not None and not _valid_sha256(toolchain.get("rustc_vv_sha256")):
        return False
    if toolchain.get("error") is not None and not isinstance(toolchain.get("error"), str):
        return False
    rustflags = provenance.get("rustflags")
    env_names = ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS")
    if not isinstance(rustflags, Mapping):
        return False
    for name in env_names:
        value = rustflags.get(name)
        if not isinstance(value, Mapping) or not isinstance(value.get("set"), bool):
            return False
        digest = value.get("sha256")
        if digest is not None and not _valid_sha256(digest):
            return False
    executables = provenance.get("executables")
    if not isinstance(executables, list):
        return False
    for executable in executables:
        if not isinstance(executable, Mapping):
            return False
        if not all(_nonempty_string(executable.get(key)) for key in ("crate", "target", "path")):
            return False
        digest = executable.get("sha256")
        if digest is not None and not _valid_sha256(digest):
            return False
        nodeids = executable.get("nodeids")
        if not isinstance(nodeids, list) or not all(isinstance(nodeid, str) for nodeid in nodeids):
            return False
    return True


def _load_runner_run(run: _Run) -> None:
    _ensure_required_files(run, ("runner-observation.json", *_RUNNER_FILES[run.runner]))
    envelope = _json_object(run.path / "runner-observation.json", run, "runner-observation.json")
    _load_l1_report(run)
    if envelope is None:
        _load_runner_invariants(run, None)
        _runner_completion(run, {})
        if run.l1_document is not None:
            raw_results = run.l1_document.get("results")
            if isinstance(raw_results, list):
                for index, item in enumerate(raw_results):
                    if isinstance(item, Mapping) and item.get("outcome") == "failed":
                        run.failure_records.append({
                            "source_ref": run.ref("source/l1-results.json", f"#results[{index}]"),
                            "reason": "原始 runner FAIL 尚未核實對應此 required case",
                            "verified": False,
                        })
        return
    if envelope.get("schema_version") != "stage3-runner-observation.v1":
        _add_problem(run.problems, "invalid runner observation schema_version")
    run.verdict_ref = run.ref("source/l1-results.json")
    reason = envelope.get("reason")
    if isinstance(reason, str):
        run.reason = reason
    else:
        _add_problem(run.problems, "missing runner observation field: reason")
    artifacts = envelope.get("artifacts")
    run.artifacts = dict(artifacts) if isinstance(artifacts, Mapping) else {}
    if not isinstance(artifacts, Mapping):
        _add_problem(run.problems, "missing runner observation field: artifacts")
    # 身分預設未核實；envelope 的 SHA 格式本身不是原始來源證據。
    run.identity_verified = False

    source_hashes = _source_file_map(run, envelope)
    release_path = run.path / "source/release-manifest.json"
    release = _json_object(release_path, run, "source/release-manifest.json")
    release_digest = ""
    if release is not None:
        try:
            release_digest = sha256_file(release_path)
        except OSError:
            release_digest = ""
            _add_problem(run.problems, "unreadable source file: source/release-manifest.json")
        if release.get("schema_version") != 1:
            _add_problem(run.problems, "invalid release manifest schema_version")
        if source_hashes.get("source/release-manifest.json") != release_digest:
            _add_problem(run.problems, "source hash mismatch: source/release-manifest.json")
    release_source_verified = (
        release is not None
        and release.get("schema_version") == 1
        and source_hashes.get("source/release-manifest.json") == release_digest
        and "source/release-manifest.json" in run.verified_sources
    )
    if release_source_verified:
        fields_valid = _check_identity_fields(run)
        identity_matches = _release_identity(run, release, release_digest)
        run.identity_verified = fields_valid and identity_matches
    else:
        _add_problem(run.problems, "artifact identity source unverified: source/release-manifest.json")

    _load_runner_invariants(run, envelope)
    _parse_runner_calls(run, envelope)
    _load_runner_case_results(run, envelope)
    _runner_completion(run, envelope)

    if run.l1_document is not None:
        l1_runner = run.l1_document.get("runner")
        expected_runner = "cargo" if run.runner == "rust-l1" else "pytest"
        if l1_runner != expected_runner:
            _add_problem(run.problems, "runner source identity mismatch: source/l1-results.json")
        if run.runner == "windows-native" and run.l1_document.get("platform") != "windows":
            _add_problem(run.problems, "runner platform mismatch: source/l1-results.json")
    if run.runner == "windows-native":
        context = _json_object(
            run.path / "source/windows-transport-context.json",
            run,
            "source/windows-transport-context.json",
        )
        if context is not None:
            if context.get("schema_version") != "stage3-windows-transport.v1":
                _add_problem(run.problems, "invalid Windows transport context schema_version")
            for field_name in ("platform", "transport", "serial_backend", "port_id"):
                if not _nonempty_string(context.get(field_name)):
                    _add_problem(run.problems, f"missing Windows transport context field: {field_name}")
            if context.get("platform") != "windows":
                _add_problem(run.problems, "Windows transport context platform mismatch")
            if not isinstance(context.get("opened"), bool):
                _add_problem(run.problems, "missing Windows transport context field: opened")
            context_sha = context.get("candidate_context_sha256")
            if not isinstance(context_sha, str) or re.fullmatch(r"[0-9a-f]{64}", context_sha) is None:
                _add_problem(run.problems, "missing Windows transport context field: candidate_context_sha256")


def _load_run(case_id: str, path: Path, report_root: Path) -> _Run:
    runner = _runner_for_path(path)
    run = _Run(case_id=case_id, runner=runner, path=path, report_root=report_root)
    try:
        if runner == "bridge":
            _load_bridge_run(run)
        else:
            _load_runner_run(run)
    except Exception as exc:
        _add_problem(run.problems, f"invalid report data: {type(exc).__name__}")
    return run


def _str_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"正規化規則 {name} 必須為字串清單")
    return list(value)


def _g2_entries(rules: Mapping[str, Any]) -> list[tuple[tuple[str, ...], str]]:
    entries: list[tuple[tuple[str, ...], str]] = []
    value = rules.get("g2_set_paths", [])
    if not isinstance(value, list):
        raise ValueError("g2_set_paths 必須為清單")
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("g2_set_paths 項目必須為對映")
        command = item.get("command")
        json_path = item.get("json_path")
        if not isinstance(command, list) or not command or not all(isinstance(token, str) and token for token in command):
            raise ValueError("g2_set_paths.command 必須為非空字串清單")
        if not isinstance(json_path, str) or not json_path or any(not part for part in json_path.split(".")):
            raise ValueError("g2_set_paths.json_path 必須為非空點路徑")
        entries.append((tuple(command), json_path))
    return entries


def _validate_rules(rules: Mapping[str, Any]) -> None:
    unknown = sorted(str(key) for key in rules if key not in _ALLOWED_RULE_KEYS)
    if unknown:
        raise ValueError(f"正規化規則含未知鍵：{unknown}")
    if "g1_exception_message" in rules and not isinstance(rules["g1_exception_message"], bool):
        raise ValueError("g1_exception_message 必須為布林值")
    _str_list(rules.get("volatile_keys"), "volatile_keys")
    _g2_entries(rules)
    if "instance_root_placeholder" in rules and not isinstance(rules["instance_root_placeholder"], str):
        raise ValueError("instance_root_placeholder 必須為字串")


def load_rules(rules_path: Path) -> dict[str, Any]:
    """以 bytes 讀取並驗證 A/B 正規化契約。"""
    try:
        raw = _read_bytes(Path(rules_path))
        document = yaml.safe_load(raw.decode("utf-8"))
        if not isinstance(document, Mapping):
            raise ValueError("正規化規則必須為對映")
        _validate_rules(document)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise _harness(f"A/B 正規化規則無法載入：{exc}") from exc
    return dict(document)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=True)


def _sort_json_path(value: Any, parts: Sequence[str]) -> Any:
    if not parts:
        if isinstance(value, list):
            return sorted(value, key=_canonical)
        return value
    if isinstance(value, Mapping):
        result = {key: _copy_json(child) for key, child in value.items()}
        first = parts[0]
        if first in result:
            result[first] = _sort_json_path(result[first], parts[1:])
        return result
    return value


def _copy_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy_json(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_copy_json(child) for child in value]
    return value


def _root_substitute(value: str, root: str, placeholder: str) -> str:
    if not root:
        return value
    variants = {root, root.replace("/", "\\"), root.replace("\\", "/")}
    for variant in sorted(variants, key=lambda item: (-len(item), item)):
        pattern = re.compile(
            rf"(?<![{_ROOT_SEGMENT_CHARS}]){re.escape(variant)}(?=[/\\]|\Z)"
        )
        value = pattern.sub(lambda _match: placeholder, value)
    return value


def _normalize_tree(value: Any, *, volatile: frozenset[str], g1: bool, roots: Sequence[str], placeholder: str) -> Any:
    if isinstance(value, Mapping):
        exception_message = g1 and value.get("error_code") == _EXCEPTION
        grouped: dict[Any, list[Any]] = {}
        for key, child in value.items():
            if isinstance(key, str) and key in volatile:
                continue
            if exception_message and key == "message":
                continue
            new_key = key
            preserve_holder_subtree = key == _B11_FOREIGN_HOLDERS_KEY
            if isinstance(key, str) and not preserve_holder_subtree:
                for root in roots:
                    new_key = _root_substitute(new_key, root, placeholder)
            if preserve_holder_subtree:
                normalized_child = _copy_json(child)
            else:
                normalized_child = _normalize_tree(
                    child, volatile=volatile, g1=g1, roots=roots, placeholder=placeholder,
                )
            grouped.setdefault(new_key, []).append(normalized_child)
        return {
            key: values[0] if len(values) == 1 else sorted(values, key=_canonical)
            for key, values in grouped.items()
        }
    if isinstance(value, list):
        return [
            _normalize_tree(child, volatile=volatile, g1=g1, roots=roots, placeholder=placeholder)
            for child in value
        ]
    if isinstance(value, str):
        for root in roots:
            value = _root_substitute(value, root, placeholder)
        return value
    return value


def normalize_response(obj: Any, *, rules: Mapping[str, Any], instance_root: str) -> Any:
    """套用 G1、G2、volatile 欄位與實例根目錄替換規則。"""
    if not isinstance(rules, Mapping):
        raise _harness("normalize_response 的 rules 必須為對映")
    if not isinstance(instance_root, str):
        raise _harness("normalize_response 的 instance_root 必須為字串")
    try:
        _validate_rules(rules)
    except ValueError as exc:
        raise _harness(f"A/B 正規化規則無效：{exc}") from exc
    volatile = frozenset(_str_list(rules.get("volatile_keys"), "volatile_keys"))
    placeholder = rules.get("instance_root_placeholder", "<ROOT>")
    normalized = _normalize_tree(
        obj,
        volatile=volatile,
        g1=bool(rules.get("g1_exception_message", False)),
        roots=(instance_root,) if instance_root else (),
        placeholder=placeholder,
    )
    for _, json_path in _g2_entries(rules):
        normalized = _sort_json_path(normalized, json_path.split("."))
    return normalized


def _logical_argv(
    argv: Sequence[str], *, roots: Sequence[str] = (), placeholder: str = "<ROOT>",
) -> tuple[str, ...]:
    """依 root parser 邊界移除 CLI 路徑及實例路由選項，保留其餘 token。"""
    tokens = _argv_without_cli_path(argv)
    output: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            output.extend(tokens[index:])
            break
        if token in _INSTANCE_ENDPOINT_FLAGS:
            index += 2 if index + 1 < len(tokens) else 1
            continue
        option, separator, _ = token.partition("=")
        if separator and option in _INSTANCE_ENDPOINT_FLAGS:
            index += 1
            continue
        if token in _ROOT_VALUE_OPTIONS:
            output.append(token)
            if index + 1 < len(tokens):
                output.append(tokens[index + 1])
                index += 2
            else:
                index += 1
            continue
        if separator and option in _ROOT_VALUE_OPTIONS:
            output.append(token)
            index += 1
            continue
        if token in _ROOT_SWITCH_OPTIONS or (token.startswith("-") and len(token) > 2 and set(token[1:]) == {"v"}):
            output.append(token)
            index += 1
            continue
        if token.startswith("-"):
            # 未知選項可能是子命令語法或 root parser 縮寫；保守保留後續全部輸入。
            output.extend(tokens[index:])
            break
        # root parser 在第一個位置參數處進入子命令；後續 token 都由子命令解析。
        output.extend(tokens[index:])
        break
    normalized: list[str] = []
    for token in output:
        value = token
        for root in roots:
            value = _root_substitute(value, root, placeholder)
        normalized.append(value)
    return tuple(normalized)


def _argv_without_cli_path(argv: Sequence[str]) -> list[str]:
    """移除 argv[0] 的 serialwrap 執行檔或帶路徑的執行檔。"""
    tokens = list(argv)
    if tokens:
        first = tokens[0]
        basename = Path(first.replace("\\", "/")).name.lower()
        if (basename in {"serialwrap", "serialwrap.exe", "serialwrap.py"}
                or "/" in first or "\\" in first):
            tokens = tokens[1:]
    return tokens


def _has_logical_command(argv: Sequence[str]) -> bool:
    """判斷移除 CLI 路徑與全域選項值後是否仍有 root 子命令位置參數。"""
    tokens = _argv_without_cli_path(argv)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return any(value.strip() for value in tokens[index + 1:])
        if token in _INSTANCE_ENDPOINT_FLAGS:
            index += 2 if index + 1 < len(tokens) else 1
            continue
        option, separator, _ = token.partition("=")
        if separator and option in _INSTANCE_ENDPOINT_FLAGS:
            index += 1
            continue
        if token in _ROOT_VALUE_OPTIONS:
            index += 2 if index + 1 < len(tokens) else 1
            continue
        if separator and option in _ROOT_VALUE_OPTIONS:
            index += 1
            continue
        if token in _ROOT_SWITCH_OPTIONS or (token.startswith("-") and len(token) > 2 and set(token[1:]) == {"v"}):
            index += 1
            continue
        if token.startswith("-"):
            return False
        return bool(token.strip())
    return False


def _rules_for_calls(
    rules: Mapping[str, Any], left: _Call, right: _Call,
    roots_a: Sequence[str] = (), roots_b: Sequence[str] = (),
) -> dict[str, Any]:
    placeholder = rules.get("instance_root_placeholder", "<ROOT>")
    left_command = _logical_argv(left.argv, roots=roots_a, placeholder=placeholder)
    right_command = _logical_argv(right.argv, roots=roots_b, placeholder=placeholder)
    if left_command != right_command:
        return {**rules, "g2_set_paths": []}
    selected = [
        item for item in rules.get("g2_set_paths", [])
        if isinstance(item, Mapping)
        and isinstance(item.get("command"), list)
        and tuple(left_command[:len(item["command"])]) == tuple(item["command"])
    ]
    return {**rules, "g2_set_paths": selected}


def _b11_candidate_applies(call: _Call) -> bool:
    """檢查擁有者追認前的 B11 窄版候選條件。"""
    return (
        _logical_argv(call.argv) in _B11_STATUS_COMMANDS
        and isinstance(call.rc, int)
        and not isinstance(call.rc, bool)
        and call.rc == 0
        and call.response_valid
        and isinstance(call.response, Mapping)
        and call.response.get("ok") is True
    )


def _b11_foreign_holders_is_safe(response: Mapping[str, Any]) -> bool:
    """只允許空快照或 /dev/ 絕對裝置路徑對應該 daemon 自身 PID。"""
    holders = response.get(_B11_FOREIGN_HOLDERS_KEY)
    if not isinstance(holders, Mapping):
        return False
    if not holders:
        return True
    pid = response.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    return all(
        isinstance(name, str)
        and name.startswith("/dev/")
        and isinstance(holder_pid, int)
        and not isinstance(holder_pid, bool)
        and holder_pid == pid
        for name, holder_pid in holders.items()
    )


def _call_response(call: _Call) -> Any:
    if not call.response_valid:
        return None
    response = call.response
    if _b11_candidate_applies(call) and isinstance(response, Mapping) and _b11_foreign_holders_is_safe(response):
        response = {key: value for key, value in response.items() if key != _B11_FOREIGN_HOLDERS_KEY}
    return response


def _first_difference(left: Any, right: Any, path: str) -> str | None:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        for key in sorted(set(left) | set(right), key=str):
            child_path = f"{path}.{key}"
            if key not in left or key not in right:
                return child_path
            difference = _first_difference(left[key], right[key], child_path)
            if difference is not None:
                return difference
        return None
    if isinstance(left, list) and isinstance(right, list):
        for index, (a_item, b_item) in enumerate(zip(left, right)):
            difference = _first_difference(a_item, b_item, f"{path}.{index}")
            if difference is not None:
                return difference
        return None if len(left) == len(right) else path
    return None if _canonical(left) == _canonical(right) else path


def _top_level_differences(left: Any, right: Any, path: str) -> list[str]:
    """回傳各頂層欄位的第一個差異路徑，保留同一回應內彼此獨立的差異。"""
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        found: list[str] = []
        for key in sorted(set(left) | set(right), key=str):
            child_path = f"{path}.{key}"
            if key not in left or key not in right:
                found.append(child_path)
                continue
            child = _first_difference(left[key], right[key], child_path)
            if child is not None:
                found.append(child)
        return found
    difference = _first_difference(left, right, path)
    return [difference] if difference is not None else []


def _aggregate_verdict(runs: Sequence[_Run]) -> str:
    values = {run.verdict or "MISSING" for run in runs}
    if not values:
        return "MISSING"
    for value in ("FAIL", _INSUFFICIENT, "MISSING"):
        if value in values:
            return value
    if values == {"PASS"}:
        return "PASS"
    return "|".join(sorted(values))


def _failure_reason(run: _Run) -> str:
    if run.reason:
        return run.reason
    return ""


def _collect_failures_and_safety(
    runs_by_side: Mapping[str, Mapping[str, Sequence[_Run]]],
    required_case_ids: set[str],
) -> tuple[list[dict[str, str]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    baseline_failures: list[dict[str, str]] = []
    product_failures: list[dict[str, str]] = []
    unverified_failures: list[dict[str, Any]] = []
    safety: list[str] = []
    for side, label in (("a", "A"), ("b", "B")):
        for case_id, runs in sorted(runs_by_side[side].items()):
            for run in runs:
                failure_records = list(run.failure_records)
                if not failure_records and run.failure_ref:
                    failure_records.append({
                        "source_ref": run.failure_ref,
                        "reason": run.failure_reason or _failure_reason(run),
                        "verified": run.failure_verified,
                    })
                for failure in failure_records:
                    failure_ref = failure.get("source_ref")
                    if not isinstance(failure_ref, str) or not failure_ref:
                        continue
                    row = {
                        "side": side,
                        "case": case_id,
                        "source_ref": failure_ref,
                        "reason": str(failure.get("reason") or _failure_reason(run)),
                    }
                    if failure.get("verified") is True:
                        if side == "a":
                            baseline_failures.append(row)
                        elif case_id in required_case_ids:
                            product_failures.append(row)
                        else:
                            problem = f"unverified failure source: {failure_ref}"
                            _add_problem(run.problems, problem)
                            unverified_failures.append({
                                **row,
                                "problems": list(dict.fromkeys([*run.problems, "FAIL case is not in required_case_ids"])),
                            })
                    else:
                        problem = f"unverified failure source: {failure_ref}"
                        _add_problem(run.problems, problem)
                        unverified_failures.append({
                            **row,
                            "problems": list(dict.fromkeys(run.problems)),
                        })

                for invariant_failure in run.invariant_failures:
                    verified = run.identity_verified and run.case_identity_verified
                    if run.runner != "bridge":
                        verified = verified and "source/invariant-evidence.json" in run.verified_sources
                    token = f"{label}:{case_id}:{invariant_failure['id']}"
                    if verified:
                        if token not in safety:
                            safety.append(token)
                    else:
                        _add_problem(run.problems, "invariant FAIL source identity unverified")
                        _add_problem(run.problems, f"unverified invariant failure source: {invariant_failure['source_ref']}")
                        unverified_failures.append({
                            "side": side,
                            "case": case_id,
                            "source_ref": invariant_failure["source_ref"],
                            "reason": invariant_failure["reason"],
                            "problems": list(dict.fromkeys(run.problems)),
                        })
                for invariant_failure in run.unverified_invariant_failures:
                    _add_problem(
                        run.problems,
                        f"unverified invariant failure source: {invariant_failure['source_ref']}",
                    )
                    unverified_failures.append({
                        "side": side,
                        "case": case_id,
                        "source_ref": invariant_failure["source_ref"],
                        "reason": invariant_failure["reason"],
                        "problems": list(dict.fromkeys(run.problems)) or ["invariant source mismatch"],
                    })
    return baseline_failures, product_failures, unverified_failures, safety


def _run_pair_key(run: _Run) -> tuple[str, str]:
    return (run.runner, run.run_index)


def _pair_problems(
    a_runs: Sequence[_Run], b_runs: Sequence[_Run], problems: dict[str, list[str]], case_id: str,
) -> list[tuple[_Run, _Run]]:
    a_by_key = {_run_pair_key(run): run for run in a_runs}
    b_by_key = {_run_pair_key(run): run for run in b_runs}
    pairs: list[tuple[_Run, _Run]] = []
    for runner, index in sorted(set(a_by_key) | set(b_by_key), key=lambda item: (_run_key(item[1]), item[0])):
        key = (runner, index)
        if key not in a_by_key:
            _add_problem(problems["a"], f"missing corresponding run: {case_id}/{runner}/{index}")
        elif key not in b_by_key:
            _add_problem(problems["b"], f"missing corresponding run: {case_id}/{runner}/{index}")
        else:
            pairs.append((a_by_key[key], b_by_key[key]))
    return pairs


def _difference(
    run_a: _Run,
    run_b: _Run,
    case_id: str,
    path: str,
    kind: str,
    case_changes: Mapping[str, str],
    source_a: str | None,
    source_b: str | None,
) -> dict[str, Any]:
    return {
        "case": case_id,
        "path": path,
        "kind": kind,
        "registered_change": case_changes.get(case_id),
        "source_refs": {"a": source_a, "b": source_b},
    }


def _compare_run_pair(
    run_a: _Run,
    run_b: _Run,
    *,
    rules: Mapping[str, Any],
    case_changes: Mapping[str, str],
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    case_id = run_a.case_id
    if run_a.verdict != run_b.verdict:
        differences.append(_difference(
            run_a, run_b, case_id, "comparison.verdict", "verdict", case_changes,
            run_a.verdict_ref or run_a.ref("comparison.json"),
            run_b.verdict_ref or run_b.ref("comparison.json"),
        ))
    placeholder = rules.get("instance_root_placeholder", "<ROOT>")
    left_logical: list[tuple[str, ...]] = []
    right_logical: list[tuple[str, ...]] = []
    for call in run_a.calls:
        left_logical.append(_logical_argv(call.argv, roots=run_a.instance_roots, placeholder=placeholder))
    for call in run_b.calls:
        right_logical.append(_logical_argv(call.argv, roots=run_b.instance_roots, placeholder=placeholder))

    paired_count = min(len(run_a.calls), len(run_b.calls))
    for index in range(paired_count, max(len(run_a.calls), len(run_b.calls))):
        call_a = run_a.calls[index] if index < len(run_a.calls) else None
        call_b = run_b.calls[index] if index < len(run_b.calls) else None
        differences.append(_difference(
            run_a, run_b, case_id, f"call[{index}]", "call_count", case_changes,
            call_a.source_ref if call_a is not None else None,
            call_b.source_ref if call_b is not None else None,
        ))

    for index in range(paired_count):
        call_a = run_a.calls[index]
        call_b = run_b.calls[index]
        if left_logical[index] != right_logical[index]:
            differences.append(_difference(
                run_a, run_b, case_id, f"call[{index}].argv", "argv", case_changes,
                call_a.source_ref, call_b.source_ref,
            ))
        if call_a.rc != call_b.rc:
            differences.append(_difference(
                run_a, run_b, case_id, f"call[{index}].rc", "rc", case_changes,
                call_a.source_ref, call_b.source_ref,
            ))
        if not call_a.response_valid or not call_b.response_valid:
            continue
        call_rules = _rules_for_calls(
            rules, call_a, call_b, run_a.instance_roots, run_b.instance_roots,
        )
        left = normalize_response(
            _call_response(call_a), rules=call_rules,
            instance_root=run_a.instance_roots[0] if run_a.instance_roots else "",
        )
        right = normalize_response(
            _call_response(call_b), rules=call_rules,
            instance_root=run_b.instance_roots[0] if run_b.instance_roots else "",
        )
        value_paths = _top_level_differences(left, right, f"call[{index}]")
        for value_path in value_paths:
            differences.append(_difference(
                run_a, run_b, case_id, value_path, "value", case_changes,
                call_a.source_ref, call_b.source_ref,
            ))
    return differences


def _load_side(report_root: Path) -> dict[str, list[_Run]]:
    loaded: dict[str, list[_Run]] = {}
    for case_id, paths in load_case_runs(report_root).items():
        loaded[case_id] = [_load_run(case_id, path, report_root) for path in paths]
    return loaded


def diff_l15(
    a_dir: Path,
    b_dir: Path,
    *,
    registered: Mapping[str, str],
    case_changes: Mapping[str, str],
    required_case_ids: set[str],
    rules_path: Path,
) -> dict[str, Any]:
    """比較 A/B 報告；完整性、產品 FAIL 與安全 FAIL 同時收集。"""
    if not isinstance(registered, Mapping) or not all(isinstance(key, str) and isinstance(value, str) for key, value in registered.items()):
        raise _harness("registered 必須為字串對映")
    if not isinstance(case_changes, Mapping) or not all(isinstance(key, str) and isinstance(value, str) for key, value in case_changes.items()):
        raise _harness("case_changes 必須為字串對映")
    if not isinstance(required_case_ids, set) or not all(isinstance(case, str) and case for case in required_case_ids):
        raise _harness("required_case_ids 必須為非空字串集合")
    try:
        a_root = Path(a_dir).resolve()
        b_root = Path(b_dir).resolve()
        rules_file = Path(rules_path)
    except (TypeError, ValueError, OSError) as exc:
        raise _harness(f"A/B 報告或規則路徑無效：{exc}") from exc
    if a_root == b_root or a_root in b_root.parents or b_root in a_root.parents:
        raise _harness("A、B 報告目錄相同或互為祖先")
    rules = load_rules(rules_file)

    try:
        runs_a = _load_side(a_root)
        runs_b = _load_side(b_root)
    except OSError as exc:
        raise _harness(f"無法讀取 A/B 報告目錄：{exc}") from exc

    problems: dict[str, list[str]] = {"a": [], "b": []}
    # case_changes 引用的變更 ID 必須存在於 registered；對照表不一致時兩側完整性失敗，不靜默忽略。
    for case_id, change_id in sorted(case_changes.items()):
        if change_id not in registered:
            for side in ("a", "b"):
                _add_problem(problems[side], f"unregistered case change: {case_id} -> {change_id}")
    for side, runs_by_case in (("a", runs_a), ("b", runs_b)):
        if not required_case_ids:
            _add_problem(problems[side], "required_case_ids 為空，無法判定完整性")
        for case_id in sorted(required_case_ids):
            if not runs_by_case.get(case_id):
                _add_problem(problems[side], f"missing required case: {case_id}")

    differences: list[dict[str, Any]] = []
    cli_pairing: list[dict[str, Any]] = []
    for case_id in sorted(set(runs_a) | set(runs_b)):
        a_runs = runs_a.get(case_id, [])
        b_runs = runs_b.get(case_id, [])
        pairs = _pair_problems(a_runs, b_runs, problems, case_id)
        for run_a, run_b in pairs:
            cli_a = _nested(run_a.artifacts, "cli", "sha256")
            cli_b = _nested(run_b.artifacts, "cli", "sha256")
            cli_match = cli_a == cli_b if _valid_sha256(cli_a) and _valid_sha256(cli_b) else None
            cli_pairing.append({
                "case": case_id,
                "runner": run_a.runner,
                "run_index": run_a.run_index,
                "matched": cli_match,
                "source_refs": {
                    "a": run_a.ref("manifest.json" if run_a.runner == "bridge" else "runner-observation.json"),
                    "b": run_b.ref("manifest.json" if run_b.runner == "bridge" else "runner-observation.json"),
                },
            })
            if cli_match is False:
                _add_problem(problems["b"], "artifact identity mismatch: artifacts.cli.sha256")
                _add_problem(run_b.problems, "artifact identity mismatch: artifacts.cli.sha256")
            differences.extend(_compare_run_pair(run_a, run_b, rules=rules, case_changes=case_changes))

    baseline_failures, product_failures, unverified_failures, safety_violations = _collect_failures_and_safety(
        {"a": runs_a, "b": runs_b}, required_case_ids,
    )

    for side, runs_by_case in (("a", runs_a), ("b", runs_b)):
        for case_runs in runs_by_case.values():
            for run in case_runs:
                for issue in run.problems:
                    _add_problem(problems[side], issue)

    integrity = {"a": not problems["a"], "b": not problems["b"]}
    a_cases = {
        case_id: _aggregate_verdict(case_runs)
        for case_id, case_runs in sorted(runs_a.items())
    }
    b_cases = {
        case_id: _aggregate_verdict(case_runs)
        for case_id, case_runs in sorted(runs_b.items())
    }
    ok = (
        integrity["a"]
        and integrity["b"]
        and not safety_violations
        and not differences
        and not product_failures
    )
    return {
        "schema_version": DIFF_SCHEMA,
        "ok": bool(ok),
        "integrity": integrity,
        "integrity_problems": {"a": problems["a"], "b": problems["b"]},
        "safety_violations": safety_violations,
        "differences": differences,
        "a_cases": a_cases,
        "b_cases": b_cases,
        "baseline_failures": baseline_failures,
        "unverified_failures": unverified_failures,
        "product_failures": product_failures,
        "artifact_identity": [
            {
                "side": side,
                "case": case_id,
                "runner": run.runner,
                "run_index": run.run_index,
                "verified": run.identity_verified,
                "source_ref": run.ref("manifest.json" if run.runner == "bridge" else "source/release-manifest.json"),
            }
            for side, runs_by_case in (("a", runs_a), ("b", runs_b))
            for case_id, case_runs in sorted(runs_by_case.items())
            for run in case_runs
        ],
        "cli_pairing": cli_pairing,
        "case_maps": [
            {
                "side": side,
                "case": case_id,
                "runner": run.runner,
                "path": run.case_map["path"],
                "sha256": run.case_map["sha256"],
            }
            for side, runs_by_case in (("a", runs_a), ("b", runs_b))
            for case_id, case_runs in sorted(runs_by_case.items())
            for run in case_runs
            if run.case_map is not None
        ],
    }


def safety_tokens(report_dir: Path, label: str) -> list[str]:
    """回傳報告內所有安全 invariant FAIL；供後續 runner 共用。"""
    tokens: list[str] = []
    side_runs = _load_side(Path(report_dir).resolve())
    for case_id, runs in sorted(side_runs.items()):
        for run in runs:
            verified = run.identity_verified and run.case_identity_verified
            if run.runner != "bridge":
                verified = verified and "source/invariant-evidence.json" in run.verified_sources
            if not verified:
                continue
            for invariant in run.invariant_failures:
                token = f"{label}:{case_id}:{invariant['id']}"
                if token not in tokens:
                    tokens.append(token)
    return tokens


__all__ = [
    "DIFF_SCHEMA",
    "diff_l15",
    "load_case_runs",
    "load_rules",
    "normalize_response",
    "safety_tokens",
]
