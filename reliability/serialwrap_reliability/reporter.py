"""md/json 報表——重用 realhw write_reports 產物＋run meta 烙 deployed serialwrap 版本。

run_loop 的 reporter 契約（契約 C14）＝物件有 ``build_reports(run_result) -> dict``；
本檔以 duck-typing 消費 RunResult 屬性，不 import testpilot（CI 可測）。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from serialwrap_reliability import core


class ReliabilityReporter:
    """寫出 realhw 慣有報告並回傳 run_loop 摘要 payload。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    def build_reports(self, run_result: Any) -> dict[str, Any]:
        core.ensure_realhw_importable()
        from realhw import harness

        registry = {case.id: case for case in core.load_registry()}
        results = list(self._plugin.run_results)
        hints = {
            case_id: (registry[case_id].hints if case_id in registry else ())
            for case_id, _ in results
        }

        artifacts = dict(getattr(run_result, "artifacts", {}) or {})
        meta = dict(artifacts.get("realhw_meta") or self._plugin.run_meta or {})
        meta["fw_ver"] = str(getattr(run_result, "fw_ver", "") or "")
        meta["run_id"] = str(getattr(run_result, "run_id", "") or "")

        ctx = getattr(self._plugin, "ctx", None)
        report_dir = (
            Path(ctx.report_dir)
            if ctx is not None
            else Path(getattr(run_result, "artifact_dir"))
        )
        harness.write_reports(report_dir, meta, results, hints)

        artifact_dir = Path(getattr(run_result, "artifact_dir"))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        reports: dict[str, str] = {}
        for name in ("report.md", "report.json"):
            src = report_dir / name
            dst = artifact_dir / name
            if src.is_file() and src.resolve() != dst.resolve():
                shutil.copy2(src, dst)
            reports[name] = str(dst)

        diagnostic_counts: dict[str, int] = {}
        for record in getattr(run_result, "cases", []) or []:
            retry = getattr(record, "retry", None)
            status = str(getattr(retry, "diagnostic_status", "") or "?")
            diagnostic_counts[status] = diagnostic_counts.get(status, 0) + 1

        return {
            "plugin": "serialwrap_reliability",
            "run_id": meta.get("run_id", ""),
            "deployed_version": meta.get("version", ""),
            "report_dir": str(report_dir),
            "reports": reports,
            "diagnostic_counts": diagnostic_counts,
            "cases": len(results),
        }
