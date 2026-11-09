"""收集 pytest nodeid 與各階段結果，供 Slice 2.4 覆蓋判定使用。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
_STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
_COLLECTED: list[str] = []
_RESULTS: list[dict[str, Any]] = []
_SESSION_FINISHED = False


def _sha256(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _contract_revision() -> str:
    files = (
        "error_codes.yaml", "session_last_errors.yaml", "behavior_changes.yaml", "rpc_schema.json",
        "cli_schema.json", "wal_record.schema.json", "state_file.schema.json", "coverage_map.yaml",
    )
    material = "".join(f"{name}:{_sha256(ROOT / 'contracts' / name) or 'missing'}\n" for name in files)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _git_head() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def _outcome(report: Any) -> str:
    if report.skipped:
        return "xfailed" if hasattr(report, "wasxfail") else "skipped"
    if report.failed:
        return "error" if report.when in {"setup", "teardown"} else "failed"
    return "passed"


def _merge_existing(path: Path, document: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return document
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        document.setdefault("identity_errors", []).append("existing 結果檔無法讀取，拒絕以 pytest 結果覆寫")
        document["finished"] = False
        return document
    if not isinstance(existing, dict) or existing.get("label") != document.get("label"):
        document.setdefault("identity_errors", []).append("同 label 的既有結果檔格式或 label 不符")
        document["finished"] = False
        return document
    identity_fields = ("platform", "contract_revision", "cases_yaml_sha256", "cases_lock_sha256", "cli_sha256", "daemon_sha256")
    conflicts = [field for field in identity_fields if existing.get(field) != document.get(field)]
    merged = dict(document)
    merged["started_at"] = existing.get("started_at", document.get("started_at"))
    merged["collected"] = [*existing.get("collected", []), *document.get("collected", [])]
    merged["results"] = [*existing.get("results", []), *document.get("results", [])]
    merged["exit_code"] = max(int(existing.get("exit_code", 0)), int(document.get("exit_code", 0)))
    merged["finished"] = existing.get("finished") is True and document.get("finished") is True and not conflicts
    for field in ("native_windows", "native_build_link"):
        if field in existing and field in document:
            merged[field] = existing[field] is True and document[field] is True
        else:
            merged[field] = existing.get(field, document.get(field, False)) is True
    merged["identity_errors"] = [*existing.get("identity_errors", [])]
    if conflicts:
        merged["identity_errors"].append("同 label 結果來源身分不同：" + ", ".join(conflicts))
    return merged


def pytest_collection_finish(session: Any) -> None:
    """保存本輪實際收集的 nodeid，包含尚未執行的項目。"""
    global _COLLECTED
    _COLLECTED = [item.nodeid for item in session.items]


def pytest_runtest_logreport(report: Any) -> None:
    """保留 setup、call、teardown 各階段結果，不用最後一列覆蓋前一列。"""
    _RESULTS.append({
        "nodeid": report.nodeid,
        "outcome": _outcome(report),
        "phase": report.when,
        "skip_reason": getattr(report, "longreprtext", None) if report.skipped else None,
        "evidence": [],
    })


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """以穩定 JSON 寫出結果；沒有指定輸出目錄時不產生檔案。"""
    global _SESSION_FINISHED
    output_dir = os.environ.get("SERIALWRAP_COVERAGE_RUN_DIR")
    if not output_dir:
        return
    label = os.environ.get("SERIALWRAP_COVERAGE_LABEL")
    if not label:
        label = "python-windows" if sys.platform == "win32" else "python-linux"
    cli = os.environ.get("SERIALWRAP_CLI_EXE")
    daemon = os.environ.get("SERIALWRAP_DAEMON_EXE")
    document = {
        "label": label,
        "platform": "windows" if sys.platform == "win32" else "linux",
        "native_windows": sys.platform == "win32",
        "cli_exe": cli,
        "cli_sha256": _sha256(Path(cli)) if cli else None,
        "daemon_exe": daemon,
        "daemon_sha256": _sha256(Path(daemon)) if daemon else None,
        "contract_revision": _contract_revision(),
        "cases_yaml_sha256": _sha256(ROOT / "tests/acceptance/cases.yaml"),
        "cases_lock_sha256": _sha256(ROOT / "tests/acceptance/cases.lock.json"),
        "tool": {"name": "coverage_plugin", "revision": _sha256(Path(__file__))},
        "started_at": _STARTED_AT,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "collected": _COLLECTED,
        "exit_code": int(exitstatus),
        "finished": int(exitstatus) == 0 and not session.shouldstop and not session.shouldfail,
        "results": _RESULTS,
    }
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"results-{label}.json"
    document = _merge_existing(path, document)
    path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    _SESSION_FINISHED = True
