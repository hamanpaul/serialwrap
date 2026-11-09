"""收集 pytest nodeid 與各階段結果，供 Slice 2.4 覆蓋判定使用。

本模組以具名 plugin（`PLUGIN_NAME`）註冊一次：根 conftest 與黑箱 conftest 都可能載入，
`register()` 依名稱去重。以 plugin（而非子目錄 conftest hook）註冊，才能記錄
`tests/contract_blackbox` 以外測項的 setup／call／teardown 結果（P8X-24c6b39-6）。
受測 artifact 取自根 conftest 清除環境變數前的 `ab_env.AB_ENV` 快照，並與黑箱 harness
實際使用的 CLI／daemon 入口交叉核對（P8X-24c6b39-2）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "sw_contract_coverage"
# 擷取後立即清除：受測項內的巢狀 pytest（`--collect-only`、子測項）繼承後會把自身收集結果
# 併進外層 run 的結果檔，使整輪變成 incomplete。本 module 每行程只載入一次（sys.modules 重用）。
_RUN_DIR = os.environ.pop("SERIALWRAP_COVERAGE_RUN_DIR", None)
_LABEL = os.environ.pop("SERIALWRAP_COVERAGE_LABEL", None)
_STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
_COLLECTED: list[str] = []
_RESULTS: list[dict[str, Any]] = []
_SESSION_FINISHED = False


def register(config: Any) -> None:
    """依名稱註冊本 plugin；重複呼叫（根與黑箱 conftest 皆載入）只註冊一次。"""
    if not config.pluginmanager.has_plugin(PLUGIN_NAME):
        config.pluginmanager.register(sys.modules[__name__], PLUGIN_NAME)


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


def _ab_env() -> dict[str, str]:
    """A/B 選擇器快照：根 conftest 會在 import `ab_env` 之後清除這些環境變數。"""
    module = sys.modules.get("ab_env")
    if module is None:
        tests_dir = str(ROOT / "tests")
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        try:
            import ab_env as module  # noqa: PLC0415
        except ImportError:
            module = None
    if module is not None:
        return dict(getattr(module, "AB_ENV", {}))
    keys = ("SERIALWRAP_CLI_EXE", "SERIALWRAP_DAEMON_EXE", "SERIALWRAP_AB_LABEL", "SW_BASELINE_PYTHON")
    return {key: os.environ[key] for key in keys if key in os.environ}


def _harness() -> Any | None:
    """黑箱 harness（解析實際受測入口）；stdlib-only，不得 import 產品碼。"""
    module = sys.modules.get("bb_harness")
    if module is not None:
        return module
    path = Path(__file__).with_name("bb_harness.py")
    spec = importlib.util.spec_from_file_location("sw_coverage_bb_harness", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析註記時需要在 sys.modules 找到模組本身。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - harness 無法載入時以身分錯誤呈現
        sys.modules.pop(spec.name, None)
        return None
    return module


def _real(value: str) -> str:
    return os.path.realpath(value)


def _artifact_identity(label: str) -> tuple[str | None, str | None, list[str]]:
    """回傳 (cli, daemon, identity_errors)；Rust label 必須有可驗證且與受測入口一致的 artifact。"""
    env = _ab_env()
    cli = env.get("SERIALWRAP_CLI_EXE")
    if cli == "/bin/serialwrap" and not Path("/bin/serialwrap").exists() and os.environ.get("SW_BUNDLE"):
        cli = str(Path(os.environ["SW_BUNDLE"]) / "bin" / "serialwrap")
    daemon = env.get("SERIALWRAP_DAEMON_EXE")
    ab_label = env.get("SERIALWRAP_AB_LABEL", "python-baseline")
    errors: list[str] = []
    if label.startswith("rust-"):
        if not ab_label.startswith("rust"):
            errors.append(f"coverage label {label} 為 Rust，但 SERIALWRAP_AB_LABEL={ab_label}（受測入口不是 Rust artifact）")
        for name, value in (("SERIALWRAP_CLI_EXE", cli), ("SERIALWRAP_DAEMON_EXE", daemon)):
            if not value or not Path(value).is_file():
                errors.append(f"{name} 未指向可驗證的 artifact 檔案")
        harness = _harness()
        if harness is None:
            errors.append("無法載入 bb_harness，無法交叉核對實際受測入口")
        elif cli and daemon:
            try:
                cli_entry = harness.cli_prefix()[-1]
                daemon_entry = harness.daemon_prefix()[-1]
            except Exception as exc:  # noqa: BLE001
                errors.append(f"解析實際受測入口失敗：{type(exc).__name__}: {exc}")
            else:
                if _real(cli_entry) != _real(cli):
                    errors.append(f"CLI 受測入口 {cli_entry} 與記錄的 artifact {cli} 不符")
                if _real(daemon_entry) != _real(daemon):
                    errors.append(f"daemon 受測入口 {daemon_entry} 與記錄的 artifact {daemon} 不符")
    elif ab_label.startswith("rust"):
        errors.append(f"coverage label {label} 為 Python，但 SERIALWRAP_AB_LABEL={ab_label} 指向 Rust artifact")
    return cli, daemon, errors


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
    merged["identity_errors"] = [*existing.get("identity_errors", []), *document.get("identity_errors", [])]
    if conflicts:
        merged["identity_errors"].append("同 label 結果來源身分不同：" + ", ".join(conflicts))
    if not merged["identity_errors"]:
        merged.pop("identity_errors")
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


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    """以穩定 JSON 寫出結果；沒有指定輸出目錄時不產生檔案。

    trylast：根與黑箱 conftest 的 session 防線可能升高 `session.exitstatus`，須在其後讀取。
    """
    global _SESSION_FINISHED
    output_dir = _RUN_DIR
    if not output_dir or _SESSION_FINISHED:
        return
    label = _LABEL
    if not label:
        label = "python-windows" if sys.platform == "win32" else "python-linux"
    final_status = int(getattr(session, "exitstatus", exitstatus))
    cli, daemon, identity_errors = _artifact_identity(label)
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
        "exit_code": final_status,
        "finished": final_status == 0 and not session.shouldstop and not session.shouldfail,
        "results": _RESULTS,
    }
    if identity_errors:
        document["identity_errors"] = identity_errors
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"results-{label}.json"
    document = _merge_existing(path, document)
    path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    _SESSION_FINISHED = True
