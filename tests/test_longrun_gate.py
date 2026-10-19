"""外層長跑 release gate 的契約測試。"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


pytestmark = [pytest.mark.core_portable, pytest.mark.memory_only]

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE_SOURCE = ROOT / "realhw" / "acceptance" / "longrun_acceptance.yaml"
ENGINE_FILE = ROOT / "realhw" / "cases" / "longrun.py"
START = datetime(2026, 11, 1, tzinfo=timezone.utc)
WORKERS = ("agent:rhw0", "agent:rhw1", "agent:rhw2", "agent:rhw3", "human", "orchestrator", "bg_resolver")


def _gate():
    import importlib

    return importlib.import_module("realhw.acceptance.gate")


def _yaml():
    import yaml

    return yaml


def _jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _wal_record(seq: int, payload: bytes, *, source: str = "agent:rhw0", wall_ts: str = "2026-11-01T00:00:10+00:00"):
    return {
        "cmd_id": f"cmd-{seq}",
        "com": "COM0",
        "crc32": f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}",
        "dir": "TX",
        "len": len(payload),
        "loss_flag": False,
        "meta": {},
        "mono_ts_ns": seq * 1_000_000_000,
        "payload_b64": base64.b64encode(payload).decode("ascii"),
        "seq": seq,
        "source": source,
        "wall_ts": wall_ts,
    }


class LongrunFixture:
    """依 Task 15 介面建立完全合成的長跑證據，不啟動 daemon 或序列埠。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.report_dir = self.root / "report"
        self.case_dir = self.root / "case"
        self.acceptance_path = self.root / "longrun_acceptance.yaml"
        self.observer_path = self.report_dir / "observer.ndjson"
        self.wal_dir = self.report_dir / "wal"
        self.excl_dir = self.report_dir / "excl"
        self.platform = "linux"
        self.started = START
        self.duration_s = 172800
        self.events = []
        self.snapshots = []
        self.observer = []
        self._engine_result = {
            "verdict": "PASS",
            "reason": "符合 lr-mixed 基線",
            "category": "test",
            "reason_code": "ok",
            "duration_s": 172800.0,
            "evidence": {"analysis": "synthetic"},
        }
        self.deployed_content = None
        self._acceptance = _yaml().safe_load(ACCEPTANCE_SOURCE.read_text(encoding="utf-8"))
        self._excl_path = self.excl_dir

    def healthy(self, *, hours: int = 48, platform: str = "linux", started_at: str | None = None):
        self.platform = platform
        self.started = datetime.fromisoformat(started_at) if started_at else START
        self.duration_s = hours * 3600
        self._acceptance = _yaml().safe_load(ACCEPTANCE_SOURCE.read_text(encoding="utf-8"))
        self._write_acceptance(self._acceptance)

        start_event = self._event("start", 0)
        self.events = [start_event]
        for source in WORKERS:
            self.events.append({**self._event("worker_start", 0), "source": source})
        for index in range(100):
            source = "agent:rhw0" if index % 2 == 0 else "human:h1"
            com = "COM0" if index % 2 == 0 else "COM1"
            submitted = self._event("submit", 10 + index)
            submitted.update({"source": source, "com": com, "round": index, "cmd_id": f"cmd-{index}"})
            done = self._event("done", 11 + index)
            done.update({"source": source, "com": com, "round": index, "cmd_id": f"cmd-{index}"})
            self.events.extend((submitted, done))
        for t in range(300, self.duration_s, 300):
            for source in WORKERS:
                alive = self._event("worker_alive", t)
                alive.update({"source": source, "worker": source})
                self.events.append(alive)
        for source in WORKERS:
            stopped = self._event("worker_stop", self.duration_s)
            stopped.update({"source": source, "reason": "completed"})
            self.events.append(stopped)
        self.events.append(self._event("stop", self.duration_s))
        self.events.sort(key=lambda row: (row.get("t", 0), row["kind"] == "start"))

        self.snapshots = []
        for t in range(0, self.duration_s + 1, 300):
            self.snapshots.append({
                "t": t,
                "wall_ts": self._wall(t),
                "daemon": {"pid": 4242, "ok": True},
                "rss_kb": 40000,
                "sessions": {"COM0": "READY", "COM1": "READY"},
            })

        device_binding = {"COM0": "COM5", "COM1": "COM6"} if platform == "windows" else None
        header = {
            "kind": "header",
            "platform": platform,
            "device_binding": device_binding,
            "supervision": {"mode": "systemd-user", "auto_restart_disabled": True, "evidence": "Restart=no"},
        }
        self.observer = [header]
        for t in range(0, self.duration_s + 1, 10):
            daemon = {
                "ok": True,
                "pid": 4242,
                "version": "0.3.2",
                "multi_open": False,
                "foreign_holders": [],
                "holder_unknown": platform == "windows",
                "wal_healthy": True,
            }
            self.observer.append({
                "kind": "sample",
                "wall_ts": self._wall(t),
                "t_mono": float(t),
                "daemon": daemon,
                "sessions": {"COM0": "READY", "COM1": "READY"},
                "procscan": {"other_serialwrapd": []},
            })

        self._write_wal()
        self._write_excl() if platform == "windows" else None
        self._write_all()
        self._write_report()
        return self

    @property
    def engine_result(self):
        return self._engine_result

    @engine_result.setter
    def engine_result(self, value):
        self._engine_result = value
        if hasattr(self, "report_dir"):
            self._write_report()

    def _wall(self, t: int):
        return (self.started + timedelta(seconds=t)).isoformat(timespec="seconds")

    def _event(self, kind: str, t: int):
        return {"kind": kind, "t": t, "wall_ts": self._wall(t)}

    def _write_acceptance(self, value):
        self.acceptance_path.parent.mkdir(parents=True, exist_ok=True)
        self.acceptance_path.write_text(_yaml().safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")

    def _write_wal(self):
        self.wal_dir.mkdir(parents=True, exist_ok=True)
        rows = [
            _wal_record(1, b"echo LR_rhw0_0_0001\n", source="agent:rhw0", wall_ts=self._wall(10)),
            _wal_record(2, b"HUMAN_TICK_1", source="human:h1", wall_ts=self._wall(20)),
        ]
        _jsonl(self.wal_dir / "raw.wal.ndjson", rows)

    def _write_excl(self):
        self.excl_dir.mkdir(parents=True, exist_ok=True)
        handoffs = []
        for com in ("COM0", "COM1"):
            device = "COM5" if com == "COM0" else "COM6"
            for phase, start_offset, end_offset in (("pre", -120, -60), ("post", self.duration_s + 60, self.duration_s + 120)):
                start = self._wall(max(0, start_offset)) if start_offset >= 0 else (self.started + timedelta(seconds=start_offset)).isoformat(timespec="seconds")
                end = self._wall(max(0, end_offset)) if end_offset >= 0 else (self.started + timedelta(seconds=end_offset)).isoformat(timespec="seconds")
                handoffs.append({
                    "kind": "excl_handoff", "phase": phase, "wall_ts_start": start, "wall_ts_end": end,
                    "com": com, "expected_device": device,
                    "daemon": {"pid": 4242, "created_at": 133000000000000000},
                    "verdict": "exclusive", "steps": [],
                })
        probes = []
        for t in range(0, self.duration_s + 1, 300):
            for com in ("COM0", "COM1"):
                device = "COM5" if com == "COM0" else "COM6"
                probes.append({
                    "kind": "excl_probe", "wall_ts": self._wall(t), "t": t, "com": com,
                    "expected_device": device, "before": {"device": device},
                    "after": {"device": device}, "daemon": {"pid": 4242, "created_at": 133000000000000000},
                    "verdict": "exclusive", "attempts": [],
                })
        _jsonl(self.excl_dir / "handoff.ndjson", handoffs)
        _jsonl(self.excl_dir / "probes.ndjson", probes)

    def _write_all(self):
        _jsonl(self.case_dir / "events.ndjson", self.events)
        _jsonl(self.report_dir / "snapshots.ndjson", self.snapshots)
        _jsonl(self.observer_path, self.observer)
        self._write_acceptance(self._acceptance)

    def _write_report(self):
        self.report_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "meta": {
                "started_at_utc": self.started.isoformat(timespec="seconds"),
                "ended_at_utc": self._wall(self.duration_s),
                "cli_identity": {"pinned": True, "resolved": "/test/serialwrap", "sha256": "0" * 64},
            },
            "results": [{"id": "lr-mixed", **self._engine_result}],
        }
        if self.deployed_content is not None:
            report["meta"]["deployed_content"] = copy.deepcopy(self.deployed_content)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        (self.report_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    def inputs(self, *, mode: str, baseline=None, acceptance_path: Path | None = None,
               wal_dir: Path | None | object = ..., excl_evidence_path: Path | None | object = ...):
        gate = _gate()
        return gate.GateInputs(
            report_dir=self.report_dir,
            case_dir=self.case_dir,
            acceptance_path=acceptance_path or self.acceptance_path,
            platform=self.platform,
            mode=mode,
            observer_path=self.observer_path,
            wal_dir=self.wal_dir if wal_dir is ... else wal_dir,
            baseline_gate_path=baseline,
            excl_evidence_path=(self._excl_path if self.platform == "windows" else None)
            if excl_evidence_path is ... else excl_evidence_path,
        )

    def baseline_gate(self):
        out = _gate().evaluate(self.inputs(mode="baseline"))
        path = self.root / "baseline-release-gate.json"
        path.write_text(json.dumps(out, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return path

    def filled_acceptance(self, *, frozen_at: str | None = None):
        value = copy.deepcopy(self._acceptance)
        frozen_at = frozen_at or (self.started - timedelta(days=1)).isoformat(timespec="seconds")
        value["revision"] = "2026-11-01.1"
        fields_by_key = {
            "resources": {
                "linux": {"rss_peak_kb": 60000, "rss_steady_kb": 50000, "rss_slope_kb_per_h": 100},
                "windows": {"rss_peak_kb": 40000, "rss_steady_kb": 35000, "rss_slope_kb_per_h": 100},
            },
            "min_effective_load": {"min_completed_per_board_source": {"COM0|agent:rhw0": 1}, "max_idle_gap_s": 200000},
            "comparison_uncertainty": {"error_rate_abs_tolerance": 0.01, "throughput_ratio_tolerance": 0.02,
                                       "stuck_s_tolerance": 60, "max_reruns": 2},
        }
        for key, fields_by_platform in fields_by_key.items():
            item = value["thresholds"][key]
            item["status"] = "frozen"
            item["frozen_at"] = frozen_at
            item["decision_id"] = "OD-09-F1"
            item["approved_by"] = "test-owner"
            item["overrides"] = {}
            item["proposal_sha256"] = "0" * 64
            item["baseline_evidence"] = {
                platform: {
                    "item_id": (
                        "S0-L3-BASELINE-LINUX-48H"
                        if platform == "linux"
                        else "S0-L3-BASELINE-WINDOWS-48H"
                    ),
                    "artifact_path": f"{platform}/release-gate.json",
                    "sha256": "0" * 64,
                }
                for platform in ("linux", "windows")
            }
            for platform in ("linux", "windows"):
                item["per_platform"][platform] = copy.deepcopy(fields_by_platform.get(platform, fields_by_platform))
        self._acceptance = value
        self._write_acceptance(value)
        return self.acceptance_path

    def set_events(self, events):
        self.events = list(events)
        _jsonl(self.case_dir / "events.ndjson", self.events)

    def set_snapshots(self, snapshots):
        self.snapshots = list(snapshots)
        _jsonl(self.report_dir / "snapshots.ndjson", self.snapshots)

    def set_observer(self, observer):
        self.observer = list(observer)
        _jsonl(self.observer_path, self.observer)

    def replace_command_events(self, command_events):
        preserved = [row for row in self.events if row["kind"] in {"start", "stop", "worker_start", "worker_alive", "worker_stop"}]
        self.set_events(preserved + list(command_events))

    def set_attached(self, com: str, start_t: int, end_t: int, state: str = "ATTACHED"):
        for row in self.snapshots:
            if start_t <= row["t"] < end_t:
                row["sessions"][com] = state
        for row in self.observer:
            if row.get("kind") == "sample" and start_t <= row["t_mono"] < end_t:
                row["sessions"][com] = state
        self.set_snapshots(self.snapshots)
        self.set_observer(self.observer)

    def reset_states(self):
        for row in self.snapshots:
            row["sessions"] = {"COM0": "READY", "COM1": "READY"}
        for row in self.observer:
            if row.get("kind") == "sample":
                row["sessions"] = {"COM0": "READY", "COM1": "READY"}
        self.set_snapshots(self.snapshots)
        self.set_observer(self.observer)

    def drop_excl_evidence(self):
        self._excl_path = None

    def drop_handoff(self, phase: str, com: str):
        path = self.excl_dir / "handoff.ndjson"
        rows = [r for r in _read_jsonl(path) if not (r.get("phase") == phase and r.get("com") == com)]
        _jsonl(path, rows)

    def probe_gap(self, com: str, start_t: int, end_t: int):
        path = self.excl_dir / "probes.ndjson"
        rows = [r for r in _read_jsonl(path) if not (r.get("com") == com and start_t <= r.get("t", -1) <= end_t)]
        _jsonl(path, rows)

    def set_probe(self, index: int, **fields):
        path = self.excl_dir / "probes.ndjson"
        rows = _read_jsonl(path)
        rows[index].update(fields)
        _jsonl(path, rows)

    def set_handoff(self, phase: str, com: str, **fields):
        path = self.excl_dir / "handoff.ndjson"
        rows = _read_jsonl(path)
        for row in rows:
            if row.get("phase") == phase and row.get("com") == com:
                row.update(fields)
        _jsonl(path, rows)

    def set_wal(self, records):
        self.wal_dir.mkdir(parents=True, exist_ok=True)
        _jsonl(self.wal_dir / "raw.wal.ndjson", records)


def _check(out, check_id):
    return next(item for item in out["checks"] if item["id"] == check_id)


def _commands(submitted: int, *, completed: int | None = None, source: str = "agent:rhw0", com: str = "COM0"):
    completed = submitted if completed is None else completed
    rows = []
    for index in range(submitted):
        base = {"source": source, "com": com, "round": index, "cmd_id": f"cmd-{index}", "t": 100 + index}
        rows.append({**base, "kind": "submit"})
        if index < completed:
            rows.append({**base, "kind": "done", "t": 101 + index})
    return rows


def _wal_payload(payload: bytes, *, seq: int = 1, source: str = "agent:rhw0", wall_ts: str = "2026-11-01T00:00:10+00:00"):
    return _wal_record(seq, payload, source=source, wall_ts=wall_ts)


def test_engine_result_preserved_verbatim_and_fail_dominates(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    run.engine_result = {
        "verdict": "FAIL", "reason": "命令錯誤率過高：6/10", "category": "test",
        "reason_code": "cmd_error_rate_high", "duration_s": 172800.0, "evidence": {"analysis": "x"},
    }
    out = gate.evaluate(run.inputs(mode="candidate", baseline=run.baseline_gate()))
    assert out["engine_result"] == run.engine_result
    assert out["release_gate_result"] == "FAIL"


def test_deferred_thresholds_block_candidate_pass(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    out = gate.evaluate(run.inputs(mode="candidate", baseline=run.baseline_gate()))
    assert out["release_gate_result"] == "BLOCKED"
    blocked = {c["id"] for c in out["checks"] if c["result"] == "BLOCKED"}
    assert {"resources", "min_effective_load", "comparison_uncertainty"} <= blocked


def test_baseline_mode_never_claims_non_regression(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="windows")
    out = gate.evaluate(run.inputs(mode="baseline"))
    assert out["release_gate_result"] == "NOT_APPLICABLE_BASELINE"
    assert out["baseline_result"] == "PASS"
    relative = {c["id"]: c["result"] for c in out["checks"] if c["group"] == "relative"}
    assert set(relative.values()) == {"NOT_APPLICABLE_BASELINE"}
    deferred = {c["id"]: c["result"] for c in out["checks"]
                if c["id"] in {"resources", "min_effective_load", "comparison_uncertainty"}}
    assert set(deferred.values()) == {"NOT_APPLICABLE_BASELINE"}
    assert all(c["result"] != "BLOCKED" for c in out["checks"])


def test_all_frozen_filled_candidate_pass(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    base = run.baseline_gate()
    acceptance = run.filled_acceptance()
    out = gate.evaluate(run.inputs(mode="candidate", baseline=base, acceptance_path=acceptance))
    assert out["release_gate_result"] == "PASS"


def test_post_hoc_freeze_is_blocked(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    base = run.baseline_gate()
    acceptance = run.filled_acceptance(frozen_at=(run.started + timedelta(hours=1)).isoformat(timespec="seconds"))
    out = gate.evaluate(run.inputs(mode="candidate", baseline=base, acceptance_path=acceptance))
    for check_id in ("resources", "min_effective_load", "comparison_uncertainty"):
        item = _check(out, check_id)
        assert item["result"] == "BLOCKED"
        assert item["detail"]["reason"] == "NOT_PRE_FROZEN"
    assert out["release_gate_result"] == "BLOCKED"


def test_stitched_segments_fail(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    events = list(run.events)
    events.append({**run._event("start", 90000)})
    run.set_events(events)
    out = gate.evaluate(run.inputs(mode="baseline"))
    assert _check(out, "continuous_duration")["result"] == "FAIL"


def test_short_run_fail(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=47, platform="linux")
    out = gate.evaluate(run.inputs(mode="baseline"))
    assert _check(out, "continuous_duration")["result"] == "FAIL"


def test_sigint_fail(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    run.set_events([*run.events, run._event("sigint", 1000)])
    out = gate.evaluate(run.inputs(mode="baseline"))
    assert _check(out, "continuous_duration")["result"] == "FAIL"


def test_unpinned_cli_insufficient(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    report = json.loads((run.report_dir / "report.json").read_text(encoding="utf-8"))
    report["meta"]["cli_identity"]["pinned"] = False
    (run.report_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    out = gate.evaluate(run.inputs(mode="baseline"))
    assert _check(out, "pinned_cli")["result"] == "INSUFFICIENT_EVIDENCE"


def test_gate_copies_deployed_content(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    run.deployed_content = {"start": {"tree_sha256": "a" * 64}, "end": {"tree_sha256": "b" * 64}}
    run._write_report()
    base = run.baseline_gate()
    out = gate.evaluate(run.inputs(mode="candidate", baseline=base))
    expected_result = out["release_gate_result"]
    assert out["run"]["deployed_content"] == {"start": {"tree_sha256": "a" * 64}, "end": {"tree_sha256": "b" * 64}}

    report = json.loads((run.report_dir / "report.json").read_text(encoding="utf-8"))
    del report["meta"]["deployed_content"]
    (run.report_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    out_without = gate.evaluate(run.inputs(mode="candidate", baseline=base))
    assert out_without["run"]["deployed_content"] is None
    assert out_without["release_gate_result"] == expected_result


def test_observation_gap_insufficient(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    rows = run.observer
    run.set_observer([row for row in rows if not (row.get("kind") == "sample" and 3600 < row.get("t_mono", -1) < 3660)])
    out = gate.evaluate(run.inputs(mode="baseline"))
    check = _check(out, "observation_gaps")
    assert check["result"] == "INSUFFICIENT_EVIDENCE"
    assert check["detail"]["gaps"]


def test_unresolved_stays_in_denominator(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    rows = _commands(100, completed=95)
    out_events = [row for row in run.events if row["kind"] in {"start", "stop", "worker_start", "worker_alive", "worker_stop"}]
    run.set_events(out_events + rows)
    out = gate.evaluate(run.inputs(mode="baseline"))
    counts = out["counts"]["COM0|agent:rhw0"]
    assert counts["unresolved"] == 5
    assert counts["error_rate"] == 0.05
    assert _check(out, "error_rate_absolute")["result"] == "FAIL"


def test_busy_is_expected_reject(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    rows = _commands(100, completed=90)
    for index in range(10):
        rows.append({"kind": "busy", "source": "agent:rhw0", "com": "COM0", "round": 90 + index,
                     "cmd_id": f"busy-{index}", "t": 500 + index})
    out_events = [row for row in run.events if row["kind"] in {"start", "stop", "worker_start", "worker_alive", "worker_stop"}]
    run.set_events(out_events + rows)
    out = gate.evaluate(run.inputs(mode="baseline"))
    counts = out["counts"]["COM0|agent:rhw0"]
    assert counts["accepted"] == 90
    assert counts["error_rate"] == 0


def test_worker_error_counts(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    rows = [
        {"kind": "submit", "source": "agent:rhw0", "com": "COM0", "round": 1, "cmd_id": "c1", "t": 10},
        {"kind": "worker_error", "source": "agent:rhw0", "com": "COM0", "round": 1, "cmd_id": "c1", "t": 11},
    ]
    out_events = [row for row in run.events if row["kind"] in {"start", "stop", "worker_start", "worker_alive", "worker_stop"}]
    run.set_events(out_events + rows)
    out = gate.evaluate(run.inputs(mode="baseline"))
    counts = out["counts"]["COM0|agent:rhw0"]
    assert counts["unexpected_errors"] == 1
    assert counts["unresolved"] == 0


def test_background_counted_by_runtime_result(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    rows = [
        {"kind": "submit", "background": True, "source": "agent:rhw0", "com": "COM0", "round": 1, "cmd_id": "bg1", "t": 10},
        {"kind": "submit_accepted", "source": "agent:rhw0", "com": "COM0", "round": 1, "cmd_id": "bg1", "t": 11},
        {"kind": "submit", "background": True, "source": "agent:rhw0", "com": "COM0", "round": 2, "cmd_id": "bg2", "t": 12},
        {"kind": "submit_accepted", "source": "agent:rhw0", "com": "COM0", "round": 2, "cmd_id": "bg2", "t": 13},
        {"kind": "bg_result", "status": "error", "source": "agent:rhw0", "com": "COM0", "round": 2, "cmd_id": "bg2", "t": 14},
        {"kind": "submit", "background": True, "source": "agent:rhw0", "com": "COM0", "round": 3, "cmd_id": "bg3", "t": 15},
        {"kind": "submit_accepted", "source": "agent:rhw0", "com": "COM0", "round": 3, "cmd_id": "bg3", "t": 16},
        {"kind": "bg_result", "status": "done", "source": "agent:rhw0", "com": "COM0", "round": 3, "cmd_id": "bg3", "t": 17},
    ]
    preserved = [row for row in run.events if row["kind"] in {"start", "stop", "worker_start", "worker_alive", "worker_stop"}]
    run.set_events(preserved + rows)
    out = gate.evaluate(run.inputs(mode="baseline"))
    counts = out["counts"]["COM0|agent:rhw0"]
    assert counts["unresolved"] == 1
    assert counts["unexpected_errors"] == 1
    assert counts["completed"] == 1


def test_bg_lost_unresolved_and_insufficient(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    rows = []
    for index in range(100):
        base = {"source": "agent:rhw0", "com": "COM0", "round": index, "cmd_id": f"bg-{index}", "t": 100 + index}
        rows.extend(({**base, "kind": "submit", "background": True}, {**base, "kind": "submit_accepted"}))
        if index < 97:
            rows.append({**base, "kind": "bg_result", "status": "done", "t": 101 + index})
        else:
            rows.append({**base, "kind": "bg_lost", "reason": "CMD_NOT_FOUND", "t": 101 + index})
    preserved = [row for row in run.events if row["kind"] in {"start", "stop", "worker_start", "worker_alive", "worker_stop"}]
    run.set_events(preserved + rows)
    out = gate.evaluate(run.inputs(mode="baseline"))
    counts = out["counts"]["COM0|agent:rhw0"]
    assert counts["completed"] == 97
    assert counts["unresolved"] == 3
    assert counts["accepted"] == 100
    assert counts["error_rate"] == 0.03
    coverage = _check(out, "background_result_coverage")
    assert coverage["result"] == "INSUFFICIENT_EVIDENCE"
    assert coverage["detail"]["bg_lost"] == 3

    second = LongrunFixture(tmp_path / "drain").healthy(hours=48, platform="linux")
    drain_rows = _commands(2, completed=0)
    drain_rows = [({**row, "background": True} if row["kind"] == "submit" else row)
                  for row in drain_rows if row["kind"] == "submit"]
    for index, row in enumerate(drain_rows):
        row["cmd_id"] = f"drain-{index}"
        row["round"] = index
    drain_rows.extend({"kind": "submit_accepted", "source": "agent:rhw0", "com": "COM0",
                       "round": index, "cmd_id": f"drain-{index}", "t": 110 + index}
                      for index in range(2))
    drain_rows.append({"kind": "bg_drain", "remaining": 2, "t": 500})
    preserved = [row for row in second.events if row["kind"] in {"start", "stop", "worker_start", "worker_alive", "worker_stop"}]
    second.set_events(preserved + drain_rows)
    drained = gate.evaluate(second.inputs(mode="baseline"))
    assert _check(drained, "background_result_coverage")["result"] == "PASS"
    assert drained["counts"]["COM0|agent:rhw0"]["unresolved"] == 2


def test_effective_hour_excludes_non_ready(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    run.set_attached("COM0", 3600, 10800)
    out = gate.evaluate(run.inputs(mode="baseline"))
    assert out["counts"]["COM0|agent:rhw0"]["effective_hour"] == 46


def test_stuck_bounds_open_segment(tmp_path):
    gate = _gate()
    samples = [
        {"t": 0, "sessions": {"COM0": "READY"}},
        {"t": 300, "sessions": {"COM0": "ATTACHED"}},
        {"t": 600, "sessions": {"COM0": "ATTACHED"}, "run_end_t": 700},
    ]
    result = gate.stuck_bounds(samples, interval_s=300)["COM0"]
    assert result["segments"] == [{"lower_s": 300, "upper_s": 700, "open": True}]
    assert result["total_lower_s"] == 300
    assert result["total_upper_s"] == 700


def test_error_rate_per_board_source(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    rows = _commands(200, completed=199, source="agent:rhw0", com="COM0")
    rows.extend(_commands(100, completed=98, source="agent:rhw2", com="COM1"))
    for index in (98, 99):
        rows.append({"kind": "error", "source": "agent:rhw2", "com": "COM1", "round": index,
                     "cmd_id": f"cmd-{index}", "t": 1001 + index, "error_code": "E"})
    preserved = [row for row in run.events if row["kind"] in {"start", "stop", "worker_start", "worker_alive", "worker_stop"}]
    run.set_events(preserved + rows)
    out = gate.evaluate(run.inputs(mode="baseline"))
    assert out["counts"]["COM0|agent:rhw0"]["error_rate"] == 0.005
    assert out["counts"]["COM1|agent:rhw2"]["error_rate"] == 0.02
    assert _check(out, "error_rate_absolute")["result"] == "FAIL"


def test_throughput_relative(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    run.replace_command_events(_commands(100))
    baseline = run.baseline_gate()
    acceptance = run.filled_acceptance()
    value = _yaml().safe_load(acceptance.read_text(encoding="utf-8"))
    value["thresholds"]["comparison_uncertainty"]["per_platform"]["linux"]["throughput_ratio_tolerance"] = 0.0
    run._write_acceptance(value)
    run.replace_command_events(_commands(100, completed=94))
    out_94 = gate.evaluate(run.inputs(mode="candidate", baseline=baseline, acceptance_path=acceptance))
    assert _check(out_94, "throughput_relative")["result"] == "FAIL"

    run.replace_command_events(_commands(100, completed=96))
    out_96 = gate.evaluate(run.inputs(mode="candidate", baseline=baseline, acceptance_path=acceptance))
    assert _check(out_96, "throughput_relative")["result"] == "PASS"


def test_stuck_bounds_math(tmp_path):
    gate = _gate()
    samples = [
        {"t": 0, "sessions": {"COM0": "READY"}},
        {"t": 300, "sessions": {"COM0": "ATTACHED"}},
        {"t": 600, "sessions": {"COM0": "ATTACHED"}},
        {"t": 900, "sessions": {"COM0": "READY"}},
    ]
    result = gate.stuck_bounds(samples, interval_s=300)["COM0"]["segments"][0]
    assert result["lower_s"] == 300
    assert result["upper_s"] == 900


def test_stuck_relative_insufficient_when_overlapping(tmp_path):
    gate = _gate()
    baseline = LongrunFixture(tmp_path / "baseline").healthy(hours=48, platform="linux")
    baseline_path = baseline.baseline_gate()
    baseline_json = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline_json["stuck"] = {"COM0": {"total_lower_s": 600, "total_upper_s": 1200,
                                        "longest_lower_s": 600, "longest_upper_s": 1200}}
    baseline_path.write_text(json.dumps(baseline_json), encoding="utf-8")

    candidate = LongrunFixture(tmp_path / "candidate").healthy(hours=48, platform="linux")
    bounds = {"COM0": {"total_lower_s": 300, "total_upper_s": 900,
                       "longest_lower_s": 300, "longest_upper_s": 900}}
    monkeypatch.setattr(gate, "stuck_bounds", lambda samples, *, interval_s: copy.deepcopy(bounds))
    out = gate.evaluate(candidate.inputs(mode="candidate", baseline=baseline_path,
                                         acceptance_path=candidate.filled_acceptance()))
    assert _check(out, "stuck_relative")["result"] == "INSUFFICIENT_EVIDENCE"

    for candidate_bounds, expected in (
        ({"total_lower_s": 10, "total_upper_s": 20, "longest_lower_s": 10, "longest_upper_s": 20}, "PASS"),
        ({"total_lower_s": 1500, "total_upper_s": 1800, "longest_lower_s": 1500, "longest_upper_s": 1800}, "FAIL"),
    ):
        bounds["COM0"] = candidate_bounds
        result = gate.evaluate(candidate.inputs(mode="candidate", baseline=baseline_path,
                                                acceptance_path=candidate.filled_acceptance()))
        assert _check(result, "stuck_relative")["result"] == expected


def test_safety_unauthorized_tx_fail(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    run.set_wal([_wal_payload(b"unauthorized\n", source="agent:intruder")])
    out = gate.evaluate(run.inputs(mode="baseline"))
    assert _check(out, "safety_unauthorized_tx")["result"] == "FAIL"


def test_safety_missing_wal_insufficient(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    out = gate.evaluate(run.inputs(mode="baseline", wal_dir=None))
    safety = {item["id"]: item["result"] for item in out["checks"] if item["group"] == "safety"}
    assert set(safety) == {"safety_unauthorized_tx", "safety_double_holder", "safety_cross_context_replay",
                          "safety_dangerous_auto_resend"}
    assert set(safety.values()) == {"INSUFFICIENT_EVIDENCE"}


def test_safety_double_holder_fail(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    observer = copy.deepcopy(run.observer)
    observer[1]["daemon"]["multi_open"] = True
    run.set_observer(observer)
    out = gate.evaluate(run.inputs(mode="baseline"))
    assert _check(out, "safety_double_holder")["result"] == "FAIL"


def test_safety_double_holder_windows_exclusive_evidence_pass(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="windows")
    base = gate.evaluate(run.inputs(mode="baseline"))
    check = _check(base, "safety_double_holder")
    assert check["result"] == "PASS"
    assert check["detail"]["holder_unknown_samples"] == len(run.observer) - 1
    assert base["baseline_result"] == "PASS"

    candidate_baseline = run.root / "baseline-gate.json"
    candidate_baseline.write_text(json.dumps(base), encoding="utf-8")
    acceptance = run.filled_acceptance()
    candidate = gate.evaluate(run.inputs(mode="candidate", baseline=candidate_baseline, acceptance_path=acceptance))
    assert candidate["release_gate_result"] == "PASS"


def test_safety_double_holder_windows_missing_evidence_insufficient(tmp_path):
    gate = _gate()
    cases = []

    run = LongrunFixture(tmp_path / "missing-dir").healthy(hours=48, platform="windows")
    run.drop_excl_evidence()
    cases.append((run, "excl_evidence_missing", None))

    run = LongrunFixture(tmp_path / "missing-post").healthy(hours=48, platform="windows")
    run.drop_handoff("post", "COM1")
    cases.append((run, "handoff_missing", ["handoff:post:COM1"]))

    run = LongrunFixture(tmp_path / "late-pre").healthy(hours=48, platform="windows")
    run.set_handoff("pre", "COM0", wall_ts_end=run._wall(1))
    cases.append((run, "handoff_missing", None))

    run = LongrunFixture(tmp_path / "probe-gap").healthy(hours=48, platform="windows")
    run.probe_gap("COM0", 3600, 4500)
    cases.append((run, "probe_gap", [{"com": "COM0", "from_t": 3600, "to_t": 4500}]))

    for index, (run, reason, expected_detail) in enumerate(cases):
        out = gate.evaluate(run.inputs(mode="baseline"))
        check = _check(out, "safety_double_holder")
        assert check["result"] == "INSUFFICIENT_EVIDENCE"
        assert check["detail"]["reason"] == reason
        if expected_detail is not None:
            key = "missing" if index == 1 else "gaps"
            assert check["detail"][key] == expected_detail

    released = LongrunFixture(tmp_path / "released-gap").healthy(hours=48, platform="windows")
    released.probe_gap("COM0", 3600, 4500)
    released.set_attached("COM0", 3600, 4500, state="RELEASED")
    released_result = gate.evaluate(released.inputs(mode="baseline"))
    assert _check(released_result, "safety_double_holder")["result"] == "PASS"

    unbound = LongrunFixture(tmp_path / "unbound").healthy(hours=48, platform="windows")
    unbound.set_probe(5, expected_device="COM9")
    unbound_result = gate.evaluate(unbound.inputs(mode="baseline"))
    assert _check(unbound_result, "safety_double_holder")["detail"]["reason"] == "device_unbound"

    identity = LongrunFixture(tmp_path / "identity").healthy(hours=48, platform="windows")
    identity.set_handoff("post", "COM0", daemon={"pid": 4242, "created_at": 133000000000000001})
    identity_result = gate.evaluate(identity.inputs(mode="baseline"))
    assert _check(identity_result, "safety_double_holder")["detail"]["reason"] == "process_identity_mismatch"

    inconclusive = LongrunFixture(tmp_path / "inconclusive").healthy(hours=48, platform="windows")
    probes = _read_jsonl(inconclusive.excl_dir / "probes.ndjson")
    for probe in probes:
        probe["verdict"] = "inconclusive"
    _jsonl(inconclusive.excl_dir / "probes.ndjson", probes)
    inconclusive_result = gate.evaluate(inconclusive.inputs(mode="baseline"))
    assert _check(inconclusive_result, "safety_double_holder")["detail"]["reason"] == "probe_gap"

    procscan = LongrunFixture(tmp_path / "procscan").healthy(hours=48, platform="windows")
    observer = copy.deepcopy(procscan.observer)
    observer[3]["procscan"] = None
    procscan.set_observer(observer)
    procscan_result = gate.evaluate(procscan.inputs(mode="baseline"))
    assert _check(procscan_result, "safety_double_holder")["result"] == "INSUFFICIENT_EVIDENCE"


def test_safety_double_holder_windows_violation_fail(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="windows")
    run.set_probe(7, verdict="violated")
    out = gate.evaluate(run.inputs(mode="baseline"))
    check = _check(out, "safety_double_holder")
    assert check["result"] == "FAIL"
    assert check["detail"]["events"] == 1

    second = LongrunFixture(tmp_path / "handoff").healthy(hours=48, platform="windows")
    second.set_handoff("pre", "COM1", verdict="violated")
    second.drop_excl_evidence()
    second_out = gate.evaluate(second.inputs(mode="baseline"))
    assert _check(second_out, "safety_double_holder")["result"] == "FAIL"
    assert second_out["release_gate_result"] == "FAIL"


def test_safety_double_holder_linux_ignores_excl_evidence(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    observer = copy.deepcopy(run.observer)
    for row in observer[1:]:
        row["daemon"].pop("holder_unknown", None)
    run.set_observer(observer)
    out = gate.evaluate(run.inputs(mode="baseline", excl_evidence_path=None))
    assert _check(out, "safety_double_holder")["result"] == "PASS"


def test_cross_context_replay_single_occurrence_fail(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    tick = {"kind": "tick", "n": 5, "wall_ts": run._wall(10), "generation": 1, "t": 10}
    dead = {"kind": "minicom_dead", "wall_ts": run._wall(20), "generation": 1, "t": 20}
    run.set_events([*run.events, tick, dead])
    run.set_wal([_wal_payload(b"HUMAN_TICK_5", source="human:h1", wall_ts=run._wall(30))])
    out = gate.evaluate(run.inputs(mode="baseline"))
    check = _check(out, "safety_cross_context_replay")
    assert out["walscan"]["cross_context_replay"] == [
        {"marker": "HUMAN_TICK_5", "count": 1, "reason": "stale_generation"}
    ]
    assert check["result"] == "FAIL"


def test_unauthorized_tx_legit_source_in_forbidden_window(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    observer = copy.deepcopy(run.observer)
    for row in observer:
        if row.get("kind") == "sample" and 100 <= row["t_mono"] <= 200:
            row["sessions"]["COM0"] = "RELEASED"
    run.set_observer(observer)
    run.set_wal([_wal_payload(b"echo x\n", source="agent:rhw1", wall_ts=run._wall(150))])
    out = gate.evaluate(run.inputs(mode="baseline"))
    assert _check(out, "safety_unauthorized_tx")["result"] == "FAIL"

    run.set_wal([_wal_payload(b"echo x\n", source="agent:rhw1", wall_ts=run._wall(99))])
    boundary = gate.evaluate(run.inputs(mode="baseline"))
    assert _check(boundary, "safety_unauthorized_tx")["result"] == "INSUFFICIENT_EVIDENCE"


def test_safety_missing_context_insufficient(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    events = copy.deepcopy(run.events)
    for event in events:
        event.pop("wall_ts", None)
    run.set_events(events)
    out = gate.evaluate(run.inputs(mode="baseline"))
    assert _check(out, "safety_unauthorized_tx")["result"] == "INSUFFICIENT_EVIDENCE"
    assert _check(out, "safety_cross_context_replay")["result"] == "INSUFFICIENT_EVIDENCE"


def test_fault_retention_required(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    observer = copy.deepcopy(run.observer)
    observer[0]["supervision"]["auto_restart_disabled"] = None
    run.set_observer(observer)
    missing = gate.evaluate(run.inputs(mode="baseline"))
    assert _check(missing, "fault_retention_attested")["result"] == "INSUFFICIENT_EVIDENCE"

    observer[0]["supervision"]["auto_restart_disabled"] = True
    run.set_observer(observer)
    attested = gate.evaluate(run.inputs(mode="baseline"))
    assert _check(attested, "fault_retention_attested")["result"] == "PASS"


def test_worker_liveness_gap_insufficient(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    run.set_events([row for row in run.events if not (
        row.get("kind") == "worker_alive" and row.get("source") == "agent:rhw2" and 3600 <= row.get("t", -1) <= 4800
    )])
    out = gate.evaluate(run.inputs(mode="baseline"))
    check = _check(out, "worker_liveness")
    assert check["result"] == "INSUFFICIENT_EVIDENCE"
    assert any("agent:rhw2" in str(item) for item in check["detail"].values())


def test_resources_frozen_rules(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    base = run.baseline_gate()
    acceptance = run.filled_acceptance()

    snapshots = copy.deepcopy(run.snapshots)
    snapshots[-1]["rss_kb"] = 61000
    run.set_snapshots(snapshots)
    over = gate.evaluate(run.inputs(mode="candidate", baseline=base, acceptance_path=acceptance))
    assert _check(over, "resources")["result"] == "FAIL"

    snapshots[-1]["rss_kb"] = 50000
    run.set_snapshots(snapshots)
    within = gate.evaluate(run.inputs(mode="candidate", baseline=base, acceptance_path=acceptance))
    assert _check(within, "resources")["result"] == "PASS"

    sparse = LongrunFixture(tmp_path / "sparse").healthy(hours=48, platform="linux")
    sparse_base = sparse.baseline_gate()
    sparse_acceptance = sparse.filled_acceptance()
    snapshots = copy.deepcopy(sparse.snapshots)
    for index, row in enumerate(snapshots):
        if row["t"] > sparse.duration_s - 86400 and index % 5 == 0:
            row["rss_kb"] = None
    sparse.set_snapshots(snapshots)
    sparse_out = gate.evaluate(sparse.inputs(mode="candidate", baseline=sparse_base, acceptance_path=sparse_acceptance))
    assert _check(sparse_out, "resources")["result"] == "INSUFFICIENT_EVIDENCE"

    windows = LongrunFixture(tmp_path / "windows").healthy(hours=48, platform="windows")
    windows_base = windows.baseline_gate()
    windows_acceptance = windows.filled_acceptance()
    snapshots = copy.deepcopy(windows.snapshots)
    snapshots[-1]["rss_kb"] = 50000
    windows.set_snapshots(snapshots)
    windows_out = gate.evaluate(windows.inputs(mode="candidate", baseline=windows_base, acceptance_path=windows_acceptance))
    assert _check(windows_out, "resources")["result"] == "FAIL"

    invalid = _yaml().safe_load(windows_acceptance.read_text(encoding="utf-8"))
    invalid["thresholds"]["resources"]["per_platform"]["windows"]["rss_peak_kb"] = None
    windows._write_acceptance(invalid)
    with pytest.raises(ValueError):
        gate.load_acceptance(windows.acceptance_path)


def test_min_load_map_keys(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    base = run.baseline_gate()
    acceptance = run.filled_acceptance()
    value = _yaml().safe_load(acceptance.read_text(encoding="utf-8"))
    value["thresholds"]["min_effective_load"]["per_platform"]["linux"] = {
        "min_completed_per_board_source": {"COM0|agent:rhw0": 100, "COM1|human:h1": 10}, "max_idle_gap_s": 3600
    }
    run._write_acceptance(value)
    events = _commands(100, completed=99)
    events.extend(_commands(10, source="human:h1", com="COM1"))
    run.replace_command_events(events)
    low = gate.evaluate(run.inputs(mode="candidate", baseline=base, acceptance_path=acceptance))
    check = _check(low, "min_effective_load")
    assert check["result"] == "INSUFFICIENT_EVIDENCE"
    assert "COM0|agent:rhw0" in str(check["detail"])

    missing = LongrunFixture(tmp_path / "missing").healthy(hours=48, platform="linux")
    missing_base = missing.baseline_gate()
    missing_acceptance = missing.filled_acceptance()
    value = _yaml().safe_load(missing_acceptance.read_text(encoding="utf-8"))
    value["thresholds"]["min_effective_load"]["per_platform"]["linux"][
        "min_completed_per_board_source"
    ] = {"COM0|agent:rhw0": 1, "COM1|human:h1": 10}
    missing._write_acceptance(value)
    only_one_key = _commands(10, source="agent:rhw0", com="COM0")
    missing.replace_command_events(only_one_key)
    absent = gate.evaluate(missing.inputs(mode="candidate", baseline=missing_base, acceptance_path=missing_acceptance))
    assert _check(absent, "min_effective_load")["result"] == "INSUFFICIENT_EVIDENCE"

    good = LongrunFixture(tmp_path / "good").healthy(hours=48, platform="linux")
    good_base = good.baseline_gate()
    good_acceptance = good.filled_acceptance()
    value = _yaml().safe_load(good_acceptance.read_text(encoding="utf-8"))
    value["thresholds"]["min_effective_load"]["per_platform"]["linux"] = {
        "min_completed_per_board_source": {"COM0|agent:rhw0": 1, "COM1|human:h1": 1}, "max_idle_gap_s": 3600
    }
    good._write_acceptance(value)
    both = _commands(2, source="agent:rhw0", com="COM0") + _commands(2, source="human:h1", com="COM1")
    good.replace_command_events(both)
    passed = gate.evaluate(good.inputs(mode="candidate", baseline=good_base, acceptance_path=good_acceptance))
    assert _check(passed, "min_effective_load")["result"] == "PASS"


def test_min_load_too_low_insufficient(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    baseline = run.baseline_gate()
    acceptance = run.filled_acceptance()
    value = _yaml().safe_load(acceptance.read_text(encoding="utf-8"))
    value["thresholds"]["min_effective_load"]["per_platform"]["linux"][
        "min_completed_per_board_source"
    ] = {"COM0|agent:rhw0": 1000}
    run._write_acceptance(value)
    out = gate.evaluate(run.inputs(mode="candidate", baseline=baseline, acceptance_path=acceptance))
    assert _check(out, "min_effective_load")["result"] == "INSUFFICIENT_EVIDENCE"


def test_uncertainty_band_insufficient(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    baseline = run.baseline_gate()
    acceptance = run.filled_acceptance()
    value = _yaml().safe_load(acceptance.read_text(encoding="utf-8"))
    value["thresholds"]["comparison_uncertainty"]["per_platform"]["linux"]["throughput_ratio_tolerance"] = 0.02
    run._write_acceptance(value)
    events = _commands(100, completed=94)
    run.replace_command_events(events)
    band = gate.evaluate(run.inputs(mode="candidate", baseline=baseline, acceptance_path=acceptance))
    assert _check(band, "throughput_relative")["result"] == "INSUFFICIENT_EVIDENCE"
    events = _commands(100, completed=92)
    run.replace_command_events(events)
    below = gate.evaluate(run.inputs(mode="candidate", baseline=baseline, acceptance_path=acceptance))
    assert _check(below, "throughput_relative")["result"] == "FAIL"


def test_frozen_before_baseline_blocked(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    baseline = run.baseline_gate()
    baseline_out = json.loads(baseline.read_text(encoding="utf-8"))
    baseline_out["run"]["ended_at_utc"] = (run.started - timedelta(hours=12)).isoformat(timespec="seconds")
    baseline.write_text(json.dumps(baseline_out), encoding="utf-8")
    acceptance = run.filled_acceptance(frozen_at=(run.started - timedelta(days=2)).isoformat(timespec="seconds"))
    out = gate.evaluate(run.inputs(mode="candidate", baseline=baseline, acceptance_path=acceptance))
    check = _check(out, "frozen_after_baseline")
    assert check["result"] == "BLOCKED"
    for check_id in ("resources", "min_effective_load", "comparison_uncertainty"):
        assert _check(out, check_id)["result"] == "BLOCKED"


def test_baseline_must_pass_for_candidate(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    baseline = run.baseline_gate()
    value = json.loads(baseline.read_text(encoding="utf-8"))
    value["baseline_result"] = "INSUFFICIENT_EVIDENCE"
    baseline.write_text(json.dumps(value), encoding="utf-8")
    out = gate.evaluate(run.inputs(mode="candidate", baseline=baseline, acceptance_path=run.filled_acceptance()))
    assert _check(out, "baseline_compatible")["result"] == "INSUFFICIENT_EVIDENCE"


def test_liveness_restart_fail(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    observer = copy.deepcopy(run.observer)
    observer[-1]["daemon"]["pid"] = 200
    run.set_observer(observer)
    out = gate.evaluate(run.inputs(mode="baseline"))
    assert _check(out, "liveness_restart")["result"] == "FAIL"


def test_baseline_incompatible_insufficient(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    baseline = run.baseline_gate()
    original = json.loads(baseline.read_text(encoding="utf-8"))

    for field, replacement in (("engine_files_sha256", {"realhw/cases/longrun.py": "f" * 64}),
                               ("acceptance_workload", "e" * 64)):
        altered = copy.deepcopy(original)
        if field == "engine_files_sha256":
            altered["run"][field] = replacement
        else:
            altered["acceptance"]["workload_contract_sha256"] = replacement
        baseline.write_text(json.dumps(altered), encoding="utf-8")
        result = gate.evaluate(run.inputs(mode="candidate", baseline=baseline, acceptance_path=run.filled_acceptance()))
        assert _check(result, "baseline_compatible")["result"] == "INSUFFICIENT_EVIDENCE"

    revised = copy.deepcopy(original)
    revised["acceptance"]["revision"] = "2026-10-15.2"
    baseline.write_text(json.dumps(revised), encoding="utf-8")
    compatible = gate.evaluate(run.inputs(mode="candidate", baseline=baseline, acceptance_path=run.filled_acceptance()))
    assert _check(compatible, "baseline_compatible")["result"] == "PASS"


def test_output_schema_and_hashes(tmp_path):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    baseline = run.baseline_gate()
    out = gate.evaluate(run.inputs(mode="candidate", baseline=baseline))
    assert out["schema_version"] == gate.GATE_SCHEMA
    assert out["engine_result"] == run.engine_result
    assert out["release_gate_result"] in {"BLOCKED", "PASS", "FAIL", "INSUFFICIENT_EVIDENCE"}
    acceptance = out["acceptance"]
    raw = run.acceptance_path.read_bytes()
    assert acceptance["sha256"] == hashlib.sha256(raw).hexdigest()
    blob = b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw
    assert acceptance["git_blob_sha1"] == hashlib.sha1(blob).hexdigest()
    assert acceptance["effective_content"] == _yaml().safe_load(raw)
    for name, digest in out["evidence_hashes"].items():
        if name == "wal":
            for wal_name, wal_digest in digest.items():
                assert wal_digest == hashlib.sha256((run.wal_dir / wal_name).read_bytes()).hexdigest()
        else:
            path = {
                "report.json": run.report_dir / "report.json",
                "events.ndjson": run.case_dir / "events.ndjson",
                "snapshots.ndjson": run.report_dir / "snapshots.ndjson",
                "observer.ndjson": run.observer_path,
            }[name]
            assert digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_cli_exit_codes(tmp_path, monkeypatch):
    gate = _gate()
    run = LongrunFixture(tmp_path).healthy(hours=48, platform="linux")
    for verdict, expected in (("PASS", 0), ("NOT_APPLICABLE_BASELINE", 0), ("FAIL", 1),
                              ("INSUFFICIENT_EVIDENCE", 4), ("BLOCKED", 5)):
        monkeypatch.setattr(gate, "evaluate", lambda inputs, result=verdict: {"release_gate_result": result})
        out_path = run.root / f"{verdict}.json"
        code = gate.main([
            "--report-dir", str(run.report_dir), "--case-dir", str(run.case_dir),
            "--acceptance", str(run.acceptance_path), "--platform", "linux", "--mode", "baseline",
            "--out", str(out_path),
        ])
        assert code == expected

