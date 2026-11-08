"""OD-09 凍結測試使用的合成 Stage 0 證據。"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def _yaml():
    import yaml

    return yaml


def _longrun_api():
    from test_longrun_gate import LongrunFixture, _gate

    return LongrunFixture, _gate


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _rewrite_hourly_workload(run, *, completed_scale: float = 1.0) -> None:
    preserved = [
        row for row in run.events
        if row["kind"] in {"start", "stop", "worker_start", "worker_alive", "worker_stop"}
    ]
    commands = []
    for hour in range(48):
        for com, source in (("COM0", "agent:rhw0"), ("COM1", "human:h1")):
            submitted_at = hour * 3600 + 10
            base = {
                "source": source,
                "com": com,
                "round": hour,
                "cmd_id": f"{com}-{source}-{hour}",
            }
            commands.append({
                **base,
                "kind": "submit",
                "t": submitted_at,
                "wall_ts": (run.started + timedelta(seconds=submitted_at)).isoformat(timespec="seconds"),
            })
            if hour < int(48 * completed_scale):
                completed_at = submitted_at + 1
                commands.append({
                    **base,
                    "kind": "done",
                    "t": completed_at,
                    "wall_ts": (run.started + timedelta(seconds=completed_at)).isoformat(timespec="seconds"),
                })
    run.set_events(sorted(preserved + commands, key=lambda row: (row.get("t", 0), row["kind"] == "start")))


def _checker_module(repo: Path):
    import importlib.util
    import sys

    checker_path = repo / "tests" / "acceptance" / "check_evidence_index.py"
    name = "od09_test_check_evidence_index"
    spec = importlib.util.spec_from_file_location(name, checker_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入證據索引檢查器：{checker_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def evidence_index_checker(env):
    """載入替身 repo 內的證據索引檢查器。"""
    return _checker_module(env.repo)


def ts(day: int, hour: int) -> str:
    return f"2026-11-{day:02d}T{hour:02d}:00:00+00:00"


def od09_env(
    root: Path,
    *,
    linux_rss_peak: int = 40000,
    linux_slope: float = 10.0,
    interval: int = 300,
    duration_s: int | None = None,
    rust_baseline: bool = False,
):
    """建立只含 Task 12、15 檔案的隔離 repo 與兩平台基線。"""
    from realhw.acceptance import od09_freeze

    root = Path(root)
    repo = root / "repo"
    source_files = (
        "realhw/acceptance/longrun_acceptance.yaml",
        "realhw/acceptance/longrun_acceptance.schema.json",
        "tests/acceptance/stage0_evidence_index.yaml",
        "tests/acceptance/check_evidence_index.py",
        "tests/acceptance/evidence_index.schema.json",
        # checker 依同目錄載入的證據層級 helper（Task 17）；缺它則替身 repo 內的 checker 無法執行。
        "tests/acceptance/evidence_labels.py",
    )
    for relative in source_files:
        source = ROOT / relative
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    acceptance = repo / "realhw" / "acceptance" / "longrun_acceptance.yaml"
    stage0_index = repo / "tests" / "acceptance" / "stage0_evidence_index.yaml"
    evidence_root = repo / "evidence"
    ledger = repo / "realhw" / "acceptance" / "candidate_longrun_starts.ndjson"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"")
    addenda = repo / "docs" / "rust-refactor" / "owner-decisions-addenda.md"
    addenda.parent.mkdir(parents=True, exist_ok=True)
    addenda.write_text("| decision_id | 主題 | 日期 | 內容 |\n", encoding="utf-8")

    LongrunFixture, gate_loader = _longrun_api()
    gate = gate_loader()
    index_value = _yaml().safe_load(stage0_index.read_text(encoding="utf-8"))
    runs = {}
    manifests = {}
    baseline_gates = {}
    stats = {}
    for platform in od09_freeze.PLATFORMS:
        run = LongrunFixture(root / "runs" / platform).healthy(
            hours=48,
            platform=platform,
            started_at=ts(10, 0),
        )
        _rewrite_hourly_workload(run)
        manifest = repo / "release-manifests" / f"{platform}.json"
        implementation = "rust" if rust_baseline and platform == "linux" else "python"
        _write_json(manifest, {"components": {"daemon": {"implementation": implementation}}})
        manifests[platform] = manifest

        gate_result = gate.evaluate(
            run.inputs(mode="baseline", acceptance_path=acceptance)
        )
        item_id = od09_freeze.BASELINE_ITEMS[platform]
        gate_path = evidence_root / item_id / "release-gate.json"
        _write_json(gate_path, gate_result)
        baseline_gates[platform] = gate_path

        item = next(item for item in index_value["items"] if item["id"] == item_id)
        item["status"] = "present"
        item["artifact_path"] = f"{item_id}/release-gate.json"
        item["sha256"] = hashlib.sha256(gate_path.read_bytes()).hexdigest()

        result = od09_freeze.baseline_stats(
            run.case_dir,
            platform=platform,
            release_manifest=manifest,
        )
        peak = linux_rss_peak if platform == "linux" else result.rss_peak_kb
        slope = linux_slope if platform == "linux" else result.rss_steady_slope_kb_per_h
        stats[platform] = replace(
            result,
            daemon_implementation=implementation,
            duration_s=duration_s if duration_s is not None else result.duration_s,
            snapshot_interval_s=interval,
            rss_peak_kb=peak,
            rss_steady_median_kb=peak if platform == "linux" else result.rss_steady_median_kb,
            rss_steady_slope_kb_per_h=slope,
        )
        runs[platform] = run

    stage0_index.write_text(
        _yaml().safe_dump(index_value, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return SimpleNamespace(
        repo=repo,
        acceptance=acceptance,
        stage0_index=stage0_index,
        evidence_root=evidence_root,
        ledger=ledger,
        addenda=addenda,
        linux_stats=stats["linux"],
        windows_stats=stats["windows"],
        linux_case_dir=runs["linux"].case_dir,
        windows_case_dir=runs["windows"].case_dir,
        linux_manifest=manifests["linux"],
        windows_manifest=manifests["windows"],
        baseline_gates=baseline_gates,
        linux_run=runs["linux"],
        windows_run=runs["windows"],
    )


def longrun_case(
    directory: Path,
    *,
    platform: str,
    started_at: str,
    rss_peak_kb: int | None = None,
    completed_scale: float = 1.0,
):
    """以長跑 fixture 建候選證據，不啟動 daemon 或序列埠。"""
    LongrunFixture, _gate = _longrun_api()
    run = LongrunFixture(Path(directory)).healthy(
        hours=48,
        platform=platform,
        started_at=started_at,
    )
    _rewrite_hourly_workload(run, completed_scale=completed_scale)
    if rss_peak_kb is not None:
        snapshots = [dict(row, rss_kb=rss_peak_kb) for row in run.snapshots]
        run.set_snapshots(snapshots)
    return run


def rerun_baseline_gate(env, *, platform: str) -> Path:
    """以凍結後 acceptance 重跑合成的指定平台基線 gate。"""
    _LongrunFixture, gate_loader = _longrun_api()
    gate = gate_loader()
    run = getattr(env, f"{platform}_run")
    result = gate.evaluate(
        run.inputs(mode="baseline", acceptance_path=env.acceptance)
    )
    item_id = {
        "linux": "S0-L3-BASELINE-LINUX-48H",
        "windows": "S0-L3-BASELINE-WINDOWS-48H",
    }[platform]
    path = env.evidence_root / item_id / "release-gate.json"
    _write_json(path, result)
    return path
