"""pytest 外掛（S1X）：把受測 provenance 寫入 JUnit testsuite properties，供 `stage1a_gate.py` 對帳。

寫入的 properties（session 級 autouse fixture，整次執行只寫一次）：
- `HEAD`：本 repo（本檔上兩層）的 `git rev-parse HEAD`；取不到寫空字串，gate 會拒收。
- `SW_REQUIRE_CARGO`：環境變數原值（gate 要求 `"1"`）。
- `git_clean`：工作樹乾淨（含未追蹤檔，排除 `target/`）時為 `"1"`，否則 `"0"`；gate 要求 `"1"`，
  避免「同一 HEAD、髒樹」的執行被當成正式證據（與 `register_p4_evidence.py` 拒收髒樹的規則一致）。
- `effective`：pytest 實際生效的收集／篩選設定（`config.option`，已含 ini `addopts` 與 `PYTEST_ADDOPTS`）的 JSON 物件：
  `keyword`、`markexpr`、`deselect`、`ignore`、`ignore_glob`、`maxfail`、`collectonly`、`lf`、`ff`。
  gate 以此判定是否縮小範圍；單看 argv 會漏掉 `addopts`、`PYTEST_ADDOPTS` 與合併短旗標（如 `-xq`）。
- `argv`：pytest 的 invocation args，JSON 字串陣列（保留供人工檢視；gate 仍要求涵蓋 `tests/` 且不含縮小範圍參數）。

用法（計畫 04 Task 19 Step 5）：
    PYTHONPATH=scripts/rust SW_REQUIRE_CARGO=1 python3 -m pytest -q tests/ -p junit_provenance --junitxml=<path>

本外掛不改變任何測試的收集或判定，只寫 properties；缺少 `-p junit_provenance` 的 JUnit 不具 provenance，gate 一律拒收。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _git_head(root: Path) -> str:
    try:
        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False)
    except OSError:
        return ""
    return res.stdout.strip() if res.returncode == 0 else ""


def _git_clean(root: Path) -> str:
    """工作樹（含未追蹤檔、排除 target/）乾淨為 "1"；髒樹或無法判定為 "0"（gate fail-closed）。"""
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal", "--", ".", ":(exclude)target"],
            cwd=root, capture_output=True, text=True, check=False,
        )
    except OSError:
        return "0"
    if res.returncode != 0:
        return "0"
    return "1" if res.stdout.strip() == "" else "0"


def _effective_options(config: pytest.Config) -> dict[str, object]:
    """pytest 實際生效的收集／篩選設定（已解析 addopts、PYTEST_ADDOPTS 與合併短旗標）。"""
    opt = config.option

    def _strs(value: object) -> list[str]:
        return [str(v) for v in (value or [])]

    return {
        "keyword": str(getattr(opt, "keyword", "") or ""),
        "markexpr": str(getattr(opt, "markexpr", "") or ""),
        "deselect": _strs(getattr(opt, "deselect", None)),
        "ignore": _strs(getattr(opt, "ignore", None)),
        "ignore_glob": _strs(getattr(opt, "ignore_glob", None)),
        "maxfail": int(getattr(opt, "maxfail", 0) or 0),
        "collectonly": bool(getattr(opt, "collectonly", False)),
        "lf": bool(getattr(opt, "lf", False)),
        "ff": bool(getattr(opt, "ff", False)),
    }


@pytest.fixture(autouse=True, scope="session")
def _sw_junit_provenance(request: pytest.FixtureRequest, record_testsuite_property) -> None:
    record_testsuite_property("HEAD", _git_head(REPO_ROOT))
    record_testsuite_property("SW_REQUIRE_CARGO", os.environ.get("SW_REQUIRE_CARGO", ""))
    record_testsuite_property("git_clean", _git_clean(REPO_ROOT))
    record_testsuite_property("effective", json.dumps(_effective_options(request.config), ensure_ascii=False, sort_keys=True))
    record_testsuite_property("argv", json.dumps([str(a) for a in request.config.invocation_params.args]))
