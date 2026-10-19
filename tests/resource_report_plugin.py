"""pytest 資源報告外掛：報告 hook 的唯一宿主（P2-T13，TST-D-4、R5-F3、R5-F4）。

- `SW_RESOURCE_REPORT=<path>` 時於 `pytest_sessionfinish`（trylast，P0 live-guard 之後）寫出
  `l1-results.v1` 報告（`schema_version` 2）；報告同時是 L1 判定的輸入（見 tests/acceptance/l1_judge.py）。
- `core_portable` 項目若帶平台條件 SKIP，session exit status 升為 1（§11.2）；此閘門與是否寫報告無關。
- `SERIALWRAP_CLI_EXE` 有值時量測 `deployed_content`（start／end，R5-F4）。

只 import stdlib、`tests/resource_report.py` 與 `realhw/deployed_content.py`（皆以路徑載入），
不得 import `sw_core` 或 root conftest。可由 root conftest 或任何 conftest（如 P3 黑箱）以
`config.pluginmanager.register(<本模組>, "sw_resource_report")` 註冊；同名只註冊一次。
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
from collections.abc import Mapping
from typing import Any

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
REPORT_ENV = "SW_RESOURCE_REPORT"
CLI_ENV = "SERIALWRAP_CLI_EXE"
DAEMON_ENV = "SERIALWRAP_DAEMON_EXE"
PLUGIN_NAME = "sw_resource_report"
FORMAT = "l1-results.v1"
SCHEMA_VERSION = 2

# P1 T01 的能力式自動 SKIP（conftest 的 pytest_collection_modifyitems 設定），供 skip_kind 判定。
CAPABILITY_SKIP = pytest.StashKey[bool]()


def _load_shared(module_name: str, path: pathlib.Path) -> Any:
    """以路徑載入並登記到 sys.modules，確保同一行程只有一份模組狀態（例：`_FORCE_OS`）。"""
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


rr = _load_shared("tests.resource_report", _HERE / "resource_report.py")

_ENV_SNAPSHOT: dict[str, str | None] | None = None
_state: dict[str, Any] = {}


def _reset() -> None:
    _state.clear()
    _state.update(
        collected=[],
        deselected=[],
        not_collected=[],
        rows=[],
        pending={},
        meta={},
        report_path=None,
        deployed_start=None,
    )


_reset()


def capture_deployed_env(env: Mapping[str, str]) -> None:
    """root conftest 在清除 env 隔離前呼叫，保留 `SERIALWRAP_CLI_EXE`／`SERIALWRAP_DAEMON_EXE`（R5-F4）。"""
    global _ENV_SNAPSHOT
    _ENV_SNAPSHOT = {CLI_ENV: env.get(CLI_ENV), DAEMON_ENV: env.get(DAEMON_ENV)}


def _env(name: str) -> str | None:
    if _ENV_SNAPSHOT is not None and name in _ENV_SNAPSHOT:
        return _ENV_SNAPSHOT[name]
    return os.environ.get(name)


def _deployed_module() -> Any:
    return _load_shared("realhw.deployed_content", _HERE.parent / "realhw" / "deployed_content.py")


_DIGEST_KEYS = (
    "cli_sha256",
    "release_manifest_sha256",
    "bundle_tree_sha256",
    "runtime_tree_sha256",
    "entry_dir_sha256",
    "daemon_sha256",
)


def _measure_deployed() -> dict[str, Any] | None:
    cli = _env(CLI_ENV)
    if not cli:
        return None
    dc = _deployed_module()
    daemon = _env(DAEMON_ENV)
    if not daemon:
        # 黑箱會退回 repo 內的 Python daemon；未設 daemon 即無法證明受測內容，digest 一律不輸出。
        measured = dc.measure(pathlib.Path(cli), daemon_argv=None)
        measured["error"] = "daemon_env_missing"
        for key in _DIGEST_KEYS:
            measured[key] = None
        return measured
    return dc.measure(pathlib.Path(cli), daemon_argv=[daemon])


def _static_skip(item: pytest.Item) -> bool:
    path = pathlib.Path(str(item.path))
    if path.suffix != ".py":
        return False
    cls = getattr(item, "cls", None)
    cls_name = cls.__name__ if cls is not None else None
    return rr.static_platform_skip(path, cls_name, getattr(item, "originalname", None) or item.name)


def _item_meta(item: pytest.Item) -> dict[str, Any]:
    marks = list(item.iter_markers())
    names = {mark.name for mark in marks}
    # 全部資源 marker 都要計入（P2-T13 審查修正：原實作只取第一個，多資源項目會漏計）。
    resources = [r for r in rr.RESOURCE_MARKERS if r in names]
    return {
        "platform": next((p for p in rr.PLATFORM_MARKERS if p in names), None),
        "resource": resources[0] if resources else None,
        "resources": resources,
        "is_platform_skip": rr.is_platform_skip(marks) or _static_skip(item),
    }


def ignore_on_windows(collection_path: pathlib.Path, config: pytest.Config) -> bool:
    """Windows collection 前排除模組層為 `posix_only` 的測試檔，並記入 `not_collected_platform`。"""
    if rr.effective_os_name() != "nt":
        return False
    path = pathlib.Path(collection_path)
    if path.suffix != ".py" or not path.name.startswith("test_"):
        return False
    if not rr.should_ignore_on_windows(path):
        return False
    rel = os.path.relpath(path, config.rootpath).replace(os.sep, "/")
    if rel not in _state["not_collected"]:
        _state["not_collected"].append(rel)
    return True


def pytest_sessionstart(session: pytest.Session) -> None:
    _reset()
    path = os.environ.get(REPORT_ENV)
    if not path:
        return
    _state["report_path"] = path
    # 先移除上一次執行留下的報告：被 kill 的本次執行不得讓舊的 finished:true 報告冒充結果（P2-T13 審查修正）。
    pathlib.Path(path).unlink(missing_ok=True)
    _state["deployed_start"] = _measure_deployed()


def pytest_collection_finish(session: pytest.Session) -> None:
    _state["collected"] = [item.nodeid for item in session.items]
    for item in session.items:
        _state["meta"][item.nodeid] = _item_meta(item)


def pytest_deselected(items: list[pytest.Item]) -> None:
    for item in items:
        _state["deselected"].append(item.nodeid)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: Any):
    report = yield
    if report.skipped:
        meta = _state["meta"].get(item.nodeid) or _item_meta(item)
        if meta["is_platform_skip"]:
            report.sw_skip_kind = "platform"
        elif item.stash.get(CAPABILITY_SKIP, False):
            report.sw_skip_kind = "capability"
        else:
            report.sw_skip_kind = "other"
    return report


def _phase_outcome(row: dict[str, Any]) -> str | None:
    """彙總一列的結果；回傳 None 表示該次執行沒有可記錄的結果（例如 --setup-only 未進入 call）。

    `passed` 只在 setup／call／teardown 都實際 passed 時才給（P2-T13 審查修正：原實作把未執行的 call 記為 passed）。
    subtest 失敗會把整列記為 failed（SubtestReport 不覆寫 call phase）。
    """
    phases = row["phases"]
    if phases["setup"] == "failed" or phases["teardown"] == "failed":
        return "error"
    if row.get("subtest_failed"):
        return "failed"
    if row["xfail"]:
        if phases["call"] == "skipped":
            return "xfailed"
        if phases["call"] == "passed":
            return "xpassed"
    if phases["call"] == "failed":
        return "failed"
    if "skipped" in (phases["setup"], phases["call"]) or row.get("subtest_skipped"):
        return "skipped"
    if phases["setup"] == "passed" and phases["call"] == "passed" and phases["teardown"] == "passed":
        return "passed"
    return None


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "setup":
        # 每次執行嘗試（含重跑外掛）各自一列，不以 nodeid 合併。
        _state["pending"][report.nodeid] = {
            "nodeid": report.nodeid,
            "phases": {"setup": None, "call": None, "teardown": None},
            "xfail": False,
            "skip_kind": None,
            "subtest_failed": False,
            "subtest_skipped": False,
        }
    row = _state["pending"].get(report.nodeid)
    if row is None:
        return
    if getattr(report, "context", None) is not None:
        # pytest 9 subtests：SubtestReport 另列報告，不得覆寫父測試的 call phase（否則失敗的 subtest 會記成 passed）。
        if report.outcome == "failed":
            row["subtest_failed"] = True
        elif report.skipped:
            row["subtest_skipped"] = True
            if row["skip_kind"] is None:
                row["skip_kind"] = getattr(report, "sw_skip_kind", "other")
        return
    row["phases"][report.when] = report.outcome
    if hasattr(report, "wasxfail"):
        row["xfail"] = True
    if report.skipped and row["skip_kind"] is None:
        row["skip_kind"] = getattr(report, "sw_skip_kind", "other")
    if report.when == "teardown":
        del _state["pending"][report.nodeid]
        outcome = _phase_outcome(row)
        if outcome is None:
            return  # 沒有可記錄的結果：collected 但無 results 列，由 l1_judge 判為 not_run
        _state["rows"].append(
            {
                "nodeid": row["nodeid"],
                "outcome": outcome,
                "skip_kind": row["skip_kind"] if outcome in ("skipped", "xfailed") else None,
                "phases": dict(row["phases"]),
            }
        )


def _gate_records() -> list[dict[str, Any]]:
    rows_by_id: dict[str, list[dict[str, Any]]] = {}
    for row in _state["rows"]:
        rows_by_id.setdefault(row["nodeid"], []).append(row)
    records: list[dict[str, Any]] = []
    for nodeid in _state["collected"]:
        meta = _state["meta"].get(nodeid, {})
        base = {
            "nodeid": nodeid,
            "platform": meta.get("platform"),
            "resource": meta.get("resource"),
            "resources": list(meta.get("resources", [])),
            "is_platform_skip": bool(meta.get("is_platform_skip", False)),
        }
        executed = rows_by_id.get(nodeid, [])
        if not executed:
            records.append(dict(base, outcome=None))
        for row in executed:
            records.append(dict(base, outcome=row["outcome"]))
    return records


def _write_report(path: str, report: dict[str, Any]) -> None:
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    target.write_text(text, encoding="utf-8", newline="\n")


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    summary = rr.summarize(
        _gate_records(),
        ignored=list(_state["not_collected"]),
        platform=rr.effective_platform(),
    )
    # §11.2：core_portable 禁止以平台原因 SKIP；帶平台條件 SKIP 即升為 1（不蓋掉更高語意的 exit code）。
    if summary["core_portable_platform_skips"] and session.exitstatus == 0:
        session.exitstatus = 1

    path = _state.get("report_path")
    if not path:
        return
    deployed: dict[str, Any] | None = None
    if _state.get("deployed_start") is not None:
        deployed = _deployed_module().bracket(_state["deployed_start"], _measure_deployed() or {})
    config = session.config
    # argv：sys.argv[0] 加上 pytest 實際收到的參數。以 pytest.main() 啟動時 sys.argv 為 ['-c']，
    # 只記 sys.argv 會使 selector 比對失效（P2-T13 審查修正；`python -m pytest` 下結果與原來相同）。
    invocation = [str(a) for a in config.invocation_params.args]
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "format": FORMAT,
        "runner": "pytest",
        "platform": summary["platform"],
        "argv": [sys.argv[0], *invocation],
        "markexpr": config.option.markexpr or "",
        "counts": summary["counts"],
        "resources": summary["resources"],
        "not_collected_platform": summary["not_collected_platform"],
        "core_portable_platform_skips": summary["core_portable_platform_skips"],
        "collected": list(_state["collected"]),
        "deselected": list(_state["deselected"]),
        "results": list(_state["rows"]),
        "exit_code": session.exitstatus,
        "finished": True,
        "deployed_content": deployed,
    }
    _write_report(path, report)
