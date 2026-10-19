"""外層長跑發版判定器（`release_gate_result`）。

本模組可 import yaml；引擎模組（`realhw/cases/longrun.py`）不得 import 本檔。
門檻唯一來源為 `longrun_acceptance.yaml`；引擎結果以 `engine_result` 原樣保留，任何規則都不會把引擎 FAIL 轉成 PASS。
判定原則：證據不足一律 INSUFFICIENT_EVIDENCE 或 BLOCKED，不得算成 PASS（§11.3、§11.4、§15.12）。
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import functools
import hashlib
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .walscan import build_safety_context, scan_wal

GATE_SCHEMA = "release-gate.v1"
RESULTS = ("PASS", "FAIL", "INSUFFICIENT_EVIDENCE", "BLOCKED", "NOT_APPLICABLE_BASELINE")
EXIT_CODES = {"PASS": 0, "NOT_APPLICABLE_BASELINE": 0, "FAIL": 1, "INSUFFICIENT_EVIDENCE": 4, "BLOCKED": 5}
# 判定器自身失敗（門檻檔無法解析、輸入無法讀取等）；與 FAIL（1）區分，避免被誤讀為發版違規
EXIT_ERROR = 3

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).with_name("longrun_acceptance.schema.json")
PLATFORMS = ("linux", "windows")
ENGINE_ID = "lr-mixed"
READY = "READY"
NA = "NOT_APPLICABLE_BASELINE"
_DAY_S = 86400.0
_TOTAL_KEY = "*"
SAFETY_IDS = ("safety_unauthorized_tx", "safety_double_holder", "safety_cross_context_replay",
              "safety_dangerous_auto_resend")
_DEPENDENTS = {
    "unexpected_cmd_error_rate": ("error_rate_absolute", "error_rate_relative"),
    "effective_throughput": ("throughput_relative",),
    "stuck_attached": ("stuck_relative",),
    "resources": ("resources",),
    "min_effective_load": ("min_effective_load",),
    "comparison_uncertainty": ("comparison_uncertainty", "error_rate_relative", "throughput_relative",
                               "stuck_relative"),
    "counting": ("error_rate_absolute", "error_rate_relative", "throughput_relative"),
    "safety": SAFETY_IDS,
    "liveness": ("liveness_daemon_exit", "liveness_restart"),
}
_BASELINE_GROUPS = ("evidence", "integrity", "safety", "liveness")
_BASELINE_IDS = ("error_rate_absolute", "engine_verdict")


@dataclass(frozen=True)
class GateInputs:
    report_dir: Path
    case_dir: Path
    acceptance_path: Path
    platform: str                 # "linux" | "windows"
    mode: str                     # "baseline" | "candidate"
    observer_path: Path | None
    wal_dir: Path | None
    baseline_gate_path: Path | None
    excl_evidence_path: Path | None = None   # winexcl 證據目錄 `excl/`（probes.ndjson＋handoff.ndjson）；windows 必要


# --------------------------------------------------------------------------- #
# 基本工具
# --------------------------------------------------------------------------- #
def _canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _num(value: float) -> int | float:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _parse_wall(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _read_text(path: Path) -> tuple[str, bool]:
    """UTF-8 讀取；非法位元組以替代字元續讀並回報 (text, bad)，不得中止判定。"""
    raw = Path(path).read_bytes()
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), True


def _read_ndjson(path: Path | None) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """回 (rows, problems)；檔案不存在回 (None, [])。"""
    if path is None or not Path(path).is_file():
        return None, []
    rows: list[dict[str, Any]] = []
    text, bad = _read_text(Path(path))
    problems: list[str] = [f"{Path(path).name}:utf8_decode"] if bad else []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"{Path(path).name}:{number}")
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            problems.append(f"{Path(path).name}:{number}:not_object")
    return rows, problems


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not Path(path).is_file():
        return None
    try:
        value = json.loads(_read_text(Path(path))[0])
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _row_pid(row: dict[str, Any]) -> Any:
    """observer 樣本為 `daemon.pid`；engine snapshots 為頂層 `pid`。"""
    daemon = row.get("daemon")
    if isinstance(daemon, dict) and "pid" in daemon:
        return daemon.get("pid")
    return row.get("pid")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@functools.lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_acceptance(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """讀取並以 schema 驗證門檻檔；回 (內容, {path, revision, sha256, git_blob_sha1})。"""
    raw = Path(path).read_bytes()
    try:
        content = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"門檻檔無法解析：{path}：{exc}") from exc
    if not isinstance(content, dict):
        raise ValueError(f"門檻檔頂層必須是物件：{path}")
    errors = sorted(Draft202012Validator(_schema()).iter_errors(content), key=lambda err: list(err.path))
    if errors:
        details = "；".join(f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}" for err in errors[:5])
        raise ValueError(f"門檻檔不符 schema：{details}")
    meta = {
        "path": str(path),
        "revision": content["revision"],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "git_blob_sha1": hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest(),
    }
    return content, meta


def workload_contract_sha256(acceptance: dict[str, Any]) -> str:
    return hashlib.sha256(_canon({"workload": acceptance["workload"],
                                  "counting": acceptance["counting"]})).hexdigest()


# --------------------------------------------------------------------------- #
# 指令計數（counting）
# --------------------------------------------------------------------------- #
def _zero_bucket() -> dict[str, Any]:
    return {"submitted": 0, "accepted": 0, "completed": 0, "failed": 0, "unresolved": 0,
            "expected_rejects": 0, "unexpected_errors": 0, "bg_lost": 0}


def _tally(events: list[dict[str, Any]], counting: dict[str, Any]) -> dict[str, Any]:
    reject_kinds = set(counting["expected_reject_kinds"])
    reject_codes = set(counting["expected_reject_error_codes"])
    counts: dict[str, dict[str, Any]] = {}
    completed_t: dict[str, list[float]] = {}
    pairs: dict[tuple[Any, Any], dict[str, Any]] = {}
    background = {"bg_lost": 0, "bg_slow": 0, "bg_backpressure_s": 0.0, "bg_drain_remaining": 0}

    def bucket(key: str) -> dict[str, Any]:
        return counts.setdefault(key, _zero_bucket())

    def close(pair_id: tuple[Any, Any] | None, state: str) -> dict[str, Any] | None:
        pair = pairs.get(pair_id) if pair_id is not None else None
        if pair is None or not pair["open"]:
            return None
        pair["open"] = False
        pair["state"] = state
        return pair

    for event in events:
        kind = event.get("kind")
        source = event.get("source")
        com = event.get("com")
        round_ = event.get("round")
        key = f"{com}|{source}" if com is not None and source is not None else None
        pair_id = (source, round_) if source is not None and round_ is not None else None
        t_value = float(event.get("t", 0) or 0)

        if kind == "bg_backpressure":
            background["bg_backpressure_s"] += float(event.get("wait_s", 0) or 0)
        elif kind == "bg_slow":
            background["bg_slow"] += 1
        elif kind == "bg_drain":
            background["bg_drain_remaining"] += int(event.get("remaining", 0) or 0)
        elif kind == "bg_lost":
            background["bg_lost"] += 1
            if key is not None:
                bucket(key)["bg_lost"] += 1
        elif kind == "submit":
            if key is None:
                continue
            previous = pairs.get(pair_id) if pair_id is not None else None
            if previous is not None and previous["open"]:
                previous["open"] = False
                previous["state"] = "superseded"
                bucket(previous["key"])["unresolved"] += 1
            if pair_id is not None:
                pairs[pair_id] = {"key": key, "open": True, "state": "open"}
            bucket(key)["submitted"] += 1
        elif kind == "done":
            if key is None:
                continue
            pair = close(pair_id, "done") if pair_id is not None else None
            if pair_id is None or pair is not None:
                bucket(key)["completed"] += 1
                completed_t.setdefault(key, []).append(t_value)
        elif kind == "bg_result":
            status = event.get("status")
            if key is None or status not in {"done", "error", "canceled"}:
                continue
            pair = close(pair_id, str(status)) if pair_id is not None else None
            if pair_id is not None and pair is None:
                continue
            if status == "done":
                bucket(key)["completed"] += 1
                completed_t.setdefault(key, []).append(t_value)
            else:
                bucket(key)["failed"] += 1
                bucket(key)["unexpected_errors"] += 1
        elif kind in reject_kinds or (kind == "error" and (event.get("error_code") in reject_codes
                                                           or event.get("detail") in reject_codes)):
            if key is None:
                continue
            pair = close(pair_id, "expected_reject") if pair_id is not None else None
            if pair_id is None or pair is not None:
                bucket(key)["expected_rejects"] += 1
        elif kind == "error":
            if key is None:
                continue
            pair = close(pair_id, "error") if pair_id is not None else None
            if pair_id is None or pair is not None:
                bucket(key)["failed"] += 1
                bucket(key)["unexpected_errors"] += 1
        elif kind == "worker_error":
            if key is None or pair_id is None:
                continue
            pair = close(pair_id, "worker_error")
            if pair is not None:
                bucket(key)["unexpected_errors"] += 1

    for pair in pairs.values():
        if pair["open"]:
            bucket(pair["key"])["unresolved"] += 1

    for key in counts:
        entry = counts[key]
        entry["accepted"] = entry["submitted"] - entry["expected_rejects"]
        denominator = entry["accepted"]
        numerator = entry["unexpected_errors"] + entry["unresolved"]
        if denominator > 0:
            entry["error_rate"] = numerator / denominator
        else:
            entry["error_rate"] = 0.0 if numerator == 0 else 1.0
    return {"counts": counts, "completed_t": completed_t, "background": background}


def count_commands(events: list[dict[str, Any]], counting: dict[str, Any]) -> dict[str, dict[str, int]]:
    """依 `counting` 定義逐 (com, source) 計數；鍵為 `f"{com}|{source}"`。"""
    return _tally(events, counting)["counts"]


def _add_total(counts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    total = _zero_bucket()
    for entry in counts.values():
        for field in total:
            total[field] += entry[field]
    accepted = total["accepted"]
    numerator = total["unexpected_errors"] + total["unresolved"]
    total["error_rate"] = (numerator / accepted) if accepted > 0 else (0.0 if numerator == 0 else 1.0)
    return total


# --------------------------------------------------------------------------- #
# stuck 區段與非 READY 時間
# --------------------------------------------------------------------------- #
def stuck_bounds(samples: list[dict[str, Any]], *, interval_s: float) -> dict[str, dict[str, Any]]:
    """每板非 READY 區段的上下界（秒）。最後一筆樣本可帶 `run_end_t` 作為 run 結束時間。"""
    if not samples:
        return {}
    ordered = sorted(samples, key=lambda row: float(row["t"]))
    times = [float(row["t"]) for row in ordered]
    run_end = float(ordered[-1].get("run_end_t", times[-1]))
    boards = sorted({com for row in ordered for com in (row.get("sessions") or {})})
    result: dict[str, dict[str, Any]] = {}
    for com in boards:
        states = [(row.get("sessions") or {}).get(com) for row in ordered]
        segments: list[dict[str, Any]] = []
        total = len(ordered)
        k = 0
        while k < total:
            if states[k] == READY:
                k += 1
                continue
            start = k
            j = k
            while j < total and states[j] != READY:
                j += 1
            prev_t = times[start - 1] if start > 0 else times[start]
            if j < total:
                segments.append({"lower_s": _num(times[j - 1] - times[start]),
                                 "upper_s": _num(times[j] - prev_t)})
            else:
                segments.append({"lower_s": _num(times[total - 1] - times[start]),
                                 "upper_s": _num(run_end - prev_t), "open": True})
            k = j
        result[com] = {
            "segments": segments,
            "total_lower_s": _num(sum(seg["lower_s"] for seg in segments)),
            "total_upper_s": _num(sum(seg["upper_s"] for seg in segments)),
            "longest_lower_s": _num(max((seg["lower_s"] for seg in segments), default=0)),
            "longest_upper_s": _num(max((seg["upper_s"] for seg in segments), default=0)),
        }
    return result


def _cover_intervals(samples: list[dict[str, Any]], com: str) -> list[tuple[float, float]]:
    """兩端樣本皆為確定非 READY 的區間（觀測到的非 READY 時間，保守計入）。"""
    cover: list[tuple[float, float]] = []
    ordered = sorted(samples, key=lambda row: float(row["t"]))
    for left, right in zip(ordered, ordered[1:]):
        left_state = (left.get("sessions") or {}).get(com)
        right_state = (right.get("sessions") or {}).get(com)
        if left_state is None or right_state is None:
            continue
        if left_state != READY and right_state != READY:
            cover.append((float(left["t"]), float(right["t"])))
    return cover


def _uncovered(lo: float, hi: float, cover: list[tuple[float, float]]) -> list[tuple[float, float]]:
    gaps: list[tuple[float, float]] = []
    cursor = lo
    for start, end in sorted(cover):
        if end <= cursor:
            continue
        if start >= hi:
            break
        if start > cursor:
            gaps.append((cursor, min(start, hi)))
        cursor = max(cursor, end)
        if cursor >= hi:
            break
    if cursor < hi:
        gaps.append((cursor, hi))
    return [(a, b) for a, b in gaps if b > a]


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #
def _check(check_id: str, group: str, result: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": check_id, "group": group,
            "status": "not_applicable" if result == NA else "evaluated",
            "result": result, "detail": detail or {}}


def _engine_result(report: dict[str, Any]) -> dict[str, Any] | None:
    for item in report.get("results") or []:
        if isinstance(item, dict) and item.get("id") == ENGINE_ID:
            return {key: value for key, value in item.items() if key != "id"}
    return None


def _frozen_items(acceptance: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    items = [(key, value) for key, value in acceptance["thresholds"].items()]
    items.extend((name, acceptance[name]) for name in ("counting", "safety", "liveness"))
    return items


def _stats_resources(snapshots: list[dict[str, Any]], duration_s: float | None, interval_s: int) -> dict[str, Any]:
    rows = sorted((row for row in snapshots if row.get("rss_kb") is not None), key=lambda row: float(row["t"]))
    peak = max((float(row["rss_kb"]) for row in rows), default=None)
    first_kb = _num(float(rows[0]["rss_kb"])) if rows else None
    last_kb = _num(float(rows[-1]["rss_kb"])) if rows else None
    if duration_s is None:
        return {"peak_kb": peak, "steady_kb": None, "slope_kb_per_h": None, "valid_last_24h": 0,
                "expected_last_24h": int(_DAY_S // interval_s), "first_kb": first_kb, "last_kb": last_kb}
    last = [row for row in rows if float(row["t"]) >= duration_s - _DAY_S]
    expected = int(_DAY_S // interval_s)
    steady = statistics.median(float(row["rss_kb"]) for row in last) if last else None
    slope = None
    if len(last) >= 2:
        xs = [float(row["t"]) / 3600.0 for row in last]
        ys = [float(row["rss_kb"]) for row in last]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        var = sum((x - mean_x) ** 2 for x in xs)
        slope = (sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var) if var > 0 else 0.0
    return {"peak_kb": peak, "steady_kb": steady, "slope_kb_per_h": slope,
            "valid_last_24h": len(last), "expected_last_24h": expected,
            "first_kb": first_kb, "last_kb": last_kb}


def _pid_changes(values: list[Any]) -> int:
    seen = [int(value) for value in values if value is not None]
    return sum(1 for left, right in zip(seen, seen[1:]) if left != right)


def _run_stuck_samples(observer_samples: list[dict[str, Any]] | None,
                       snapshots: list[dict[str, Any]], duration_s: float | None) -> list[dict[str, Any]]:
    source = observer_samples if observer_samples else snapshots
    rows = [{"t": float(row["t"]), "sessions": dict(row.get("sessions") or {})}
            for row in source if row.get("t") is not None]
    # 只計入 run 視窗 [0, duration] 內的樣本：pre／post handoff 的 RELEASED 不屬於負載 run
    if duration_s is not None:
        rows = [row for row in rows if 0.0 <= row["t"] <= float(duration_s)]
    rows.sort(key=lambda row: row["t"])
    if rows and duration_s is not None:
        rows[-1]["run_end_t"] = float(duration_s)
    return rows


def _windows_double_holder(inputs: GateInputs, acceptance: dict[str, Any], observer_rows: list[dict[str, Any]],
                           observer_samples: list[dict[str, Any]], start_wall: dt.datetime | None,
                           stop_wall: dt.datetime | None, duration_s: float | None) -> tuple[str, dict[str, Any]]:
    """Windows 獨占證據契約（R1-F7）。回 (result, detail)。"""
    header = observer_rows[0] if observer_rows and observer_rows[0].get("kind") == "header" else {}
    binding = header.get("device_binding") or {}
    if not binding:
        return "INSUFFICIENT_EVIDENCE", {"reason": "device_unbound", "detail": "observer header 缺 device_binding"}
    excl = inputs.excl_evidence_path
    if excl is None or not Path(excl).is_dir():
        return "INSUFFICIENT_EVIDENCE", {"reason": "excl_evidence_missing"}
    handoffs, _ = _read_ndjson(Path(excl) / "handoff.ndjson")
    probes, _ = _read_ndjson(Path(excl) / "probes.ndjson")
    handoffs = handoffs or []
    probes = probes or []
    interval = float(acceptance["workload"]["windows_excl_probe_interval_s"])
    max_gap = float(acceptance["workload"]["max_observation_gap_factor"]) * interval
    samples = observer_samples

    # (1) pre／post handoff 存在且時序正確
    missing: list[str] = []
    if start_wall is None or stop_wall is None:
        return "INSUFFICIENT_EVIDENCE", {"reason": "handoff_missing", "missing": ["events:start_or_stop"]}
    for com in sorted(binding):
        for phase in ("pre", "post"):
            ok = False
            for row in handoffs:
                if row.get("phase") != phase or row.get("com") != com or row.get("verdict") != "exclusive":
                    continue
                if phase == "pre":
                    end = _parse_wall(row.get("wall_ts_end"))
                    ok = end is not None and end <= start_wall
                else:
                    begin = _parse_wall(row.get("wall_ts_start"))
                    ok = begin is not None and begin >= stop_wall
                if ok:
                    break
            if not ok:
                missing.append(f"handoff:{phase}:{com}")
    if missing:
        return "INSUFFICIENT_EVIDENCE", {"reason": "handoff_missing", "missing": missing}

    valid = [row for row in handoffs if row.get("verdict") == "exclusive"]
    valid.extend(row for row in probes if row.get("verdict") == "exclusive")

    # (2) 同一 daemon 程序身分（pid＋建立時間）
    observer_pids = {row["daemon"].get("pid") for row in samples if (row.get("daemon") or {}).get("pid") is not None}
    identities = {(row["daemon"].get("pid"), row["daemon"].get("created_at")) for row in valid if "daemon" in row}
    if len(identities) != 1 or len(observer_pids) != 1 or {pid for pid, _ in identities} != observer_pids:
        return "INSUFFICIENT_EVIDENCE", {"reason": "process_identity_mismatch",
                                         "identities": sorted(str(item) for item in identities)}

    # (3) 裝置綁定：只約束有判定力的紀錄（exclusive／violated）；not_ready 等 inconclusive 不因 device 缺而失效
    for row in handoffs + probes:
        com = row.get("com")
        if com not in binding or row.get("verdict") not in ("exclusive", "violated"):
            continue
        before_device = (row.get("before") or {}).get("device", binding[com])
        if row.get("expected_device") != binding[com] or before_device != binding[com]:
            return "INSUFFICIENT_EVIDENCE", {"reason": "device_unbound", "com": com}

    # (4) 探測時間序列：未覆蓋的缺口需 ≤ max_gap（或位於確定非 READY 區間內）
    gaps: list[dict[str, Any]] = []
    for com in sorted(binding):
        series = []
        for row in probes:
            if row.get("com") != com or row.get("verdict") != "exclusive":
                continue
            wall = _parse_wall(row.get("wall_ts"))
            if wall is None or start_wall is None:
                continue
            offset = (wall - start_wall).total_seconds()
            # 只計入 run 視窗內的探測（pre handoff 前的探測不得造成假缺口）
            if 0.0 <= offset <= float(duration_s or 0.0):
                series.append(offset)
        series.sort()
        points = [0.0] + series + [float(duration_s or 0.0)]
        cover = _cover_intervals(samples, com)
        for left, right in zip(points, points[1:]):
            if right - left <= max_gap:
                continue
            for lo, hi in _uncovered(left, right, cover):
                if hi - lo > max_gap:
                    gaps.append({"com": com, "from_t": _num(lo + interval), "to_t": _num(hi - interval)})
    if gaps:
        return "INSUFFICIENT_EVIDENCE", {"reason": "probe_gap", "gaps": gaps}
    return "PASS", {}


def evaluate(inputs: GateInputs) -> dict[str, Any]:
    if inputs.mode not in ("baseline", "candidate"):
        raise ValueError(f"未知 mode：{inputs.mode}")
    if inputs.platform not in PLATFORMS:
        raise ValueError(f"未知 platform：{inputs.platform}")
    is_candidate = inputs.mode == "candidate"
    acceptance, acc_meta = load_acceptance(inputs.acceptance_path)
    report_path = Path(inputs.report_dir) / "report.json"
    report = _read_json(report_path) or {}
    meta = report.get("meta") or {}
    engine = _engine_result(report)

    events, event_problems = _read_ndjson(Path(inputs.case_dir) / "events.ndjson")
    events = events or []
    snapshot_path = Path(inputs.report_dir) / "snapshots.ndjson"
    if not snapshot_path.is_file():
        snapshot_path = Path(inputs.case_dir) / "snapshots.ndjson"
    snapshots, snapshot_problems = _read_ndjson(snapshot_path)
    snapshots = snapshots or []
    observer_rows, observer_problems = _read_ndjson(inputs.observer_path)
    baseline = _read_json(inputs.baseline_gate_path) if inputs.baseline_gate_path else None
    parse_problems = event_problems + snapshot_problems + observer_problems

    workload = acceptance["workload"]
    starts = [row for row in events if row.get("kind") == "start"]
    stops = [row for row in events if row.get("kind") == "stop"]
    start_ev = starts[0] if len(starts) == 1 else None
    stop_ev = stops[0] if len(stops) == 1 else None
    start_wall = _parse_wall(start_ev.get("wall_ts")) if start_ev else None
    stop_wall = _parse_wall(stop_ev.get("wall_ts")) if stop_ev else None
    duration_s: float | None = None
    if start_ev is not None and stop_ev is not None:
        duration_s = float(stop_ev.get("t", 0)) - float(start_ev.get("t", 0))

    def rel_time(row: dict[str, Any]) -> float | None:
        wall = _parse_wall(row.get("wall_ts"))
        if start_wall is not None and wall is not None:
            return (wall - start_wall).total_seconds()
        if row.get("t_mono") is not None:
            return float(row["t_mono"])
        if row.get("t") is not None:
            return float(row["t"])
        return None

    observer_samples = None
    if observer_rows is not None:
        observer_samples = []
        for row in observer_rows:
            if row.get("kind") != "sample":
                continue
            stamp = rel_time(row)
            if stamp is None:
                continue
            observer_samples.append({**row, "t": stamp})
        observer_samples.sort(key=lambda row: row["t"])
    observer_header = observer_rows[0] if observer_rows and observer_rows[0].get("kind") == "header" else None

    stuck_rows = _run_stuck_samples(observer_samples, snapshots, duration_s)
    stuck = stuck_bounds(stuck_rows, interval_s=float(workload["observer_interval_s"]))

    tally = _tally(events, acceptance["counting"])
    counts = tally["counts"]
    completed_t = tally["completed_t"]
    total_entry = _add_total(counts)
    counts_out: dict[str, dict[str, Any]] = {}
    boards_seen = {key.split("|", 1)[0] for key in counts} | set(stuck)
    for key in sorted(counts):
        entry = dict(counts[key])
        board = key.split("|", 1)[0]
        eff_hour = _effective_hour(stuck_rows, board, duration_s)
        entry["effective_hour"] = eff_hour
        entry["throughput_per_h"] = (entry["completed"] / eff_hour) if eff_hour else None
        counts_out[key] = entry
    total_entry["effective_hour"] = (sum(_effective_hour(stuck_rows, board, duration_s) or 0.0
                                         for board in boards_seen) / max(len(boards_seen), 1)
                                     if boards_seen else None)
    counts_out[_TOTAL_KEY] = total_entry

    checks: list[dict[str, Any]] = []
    # 真實 run 的 report.json 不寫 started／ended（只有 fixture 寫）：回退到 start／stop 事件的 wall_ts
    started_at = meta.get("started_at_utc") or (start_ev.get("wall_ts") if start_ev else None)
    ended_at = meta.get("ended_at_utc") or (stop_ev.get("wall_ts") if stop_ev else None)
    started_dt = _parse_wall(started_at)

    # --- evidence -------------------------------------------------------- #
    checks.append(_check("evidence_parse", "evidence",
                         "PASS" if not parse_problems else "INSUFFICIENT_EVIDENCE",
                         {"problems": parse_problems[:20]}))
    checks.append(_check("engine_report_present", "evidence",
                         "PASS" if engine is not None else "INSUFFICIENT_EVIDENCE",
                         {"id": ENGINE_ID} if engine is not None else {"missing": ENGINE_ID}))
    pinned = (meta.get("cli_identity") or {}).get("pinned") is True
    checks.append(_check("pinned_cli", "evidence", "PASS" if pinned else "INSUFFICIENT_EVIDENCE",
                         {"pinned": (meta.get("cli_identity") or {}).get("pinned")}))
    if engine is None:
        engine_result_check = _check("engine_verdict", "engine", "INSUFFICIENT_EVIDENCE", {"reason": "engine_missing"})
    elif engine.get("verdict") == "PASS":
        engine_result_check = _check("engine_verdict", "engine", "PASS", {"verdict": "PASS"})
    elif engine.get("verdict") == "FAIL":
        engine_result_check = _check("engine_verdict", "engine", "FAIL", {"verdict": "FAIL"})
    else:
        engine_result_check = _check("engine_verdict", "engine", "INSUFFICIENT_EVIDENCE",
                                     {"verdict": engine.get("verdict")})
    checks.append(engine_result_check)

    checks.append(_observation_gaps_check(observer_samples, snapshots, duration_s, workload))

    # pre_frozen
    frozen_violations: list[str] = []
    pre_frozen_missing = started_dt is None
    for name, item in _frozen_items(acceptance):
        if item.get("status") != "frozen":
            continue
        frozen_at = _parse_wall(item.get("frozen_at"))
        if frozen_at is None or started_dt is None or frozen_at >= started_dt:
            frozen_violations.append(name)
    if pre_frozen_missing:
        checks.append(_check("pre_frozen", "evidence", "INSUFFICIENT_EVIDENCE", {"reason": "started_at_missing"}))
    elif frozen_violations:
        checks.append(_check("pre_frozen", "evidence", "BLOCKED" if is_candidate else "INSUFFICIENT_EVIDENCE",
                             {"reason": "NOT_PRE_FROZEN", "items": sorted(frozen_violations)}))
    else:
        checks.append(_check("pre_frozen", "evidence", "PASS", {}))

    # frozen_after_baseline
    after_violations: list[str] = []
    if not is_candidate:
        checks.append(_check("frozen_after_baseline", "evidence", NA, {}))
    elif baseline is None:
        checks.append(_check("frozen_after_baseline", "evidence", "INSUFFICIENT_EVIDENCE",
                             {"reason": "baseline_missing"}))
    else:
        base_content = (baseline.get("acceptance") or {}).get("effective_content") or {}
        base_end = _parse_wall((baseline.get("run") or {}).get("ended_at_utc"))
        base_thresholds = base_content.get("thresholds") if isinstance(base_content, dict) else None
        if base_end is None or not isinstance(base_thresholds, dict):
            checks.append(_check("frozen_after_baseline", "evidence", "INSUFFICIENT_EVIDENCE",
                                 {"reason": "baseline_content_missing"}))
        else:
            for key, item in acceptance["thresholds"].items():
                base_item = base_thresholds.get(key) or {}
                if base_item.get("status") == "deferred" and item.get("status") == "frozen":
                    frozen_at = _parse_wall(item.get("frozen_at"))
                    if frozen_at is None or frozen_at <= base_end:
                        after_violations.append(key)
            if after_violations:
                checks.append(_check("frozen_after_baseline", "evidence", "BLOCKED",
                                     {"reason": "FROZEN_BEFORE_BASELINE", "items": sorted(after_violations)}))
            else:
                checks.append(_check("frozen_after_baseline", "evidence", "PASS", {}))

    # fault retention
    retention = ((observer_header or {}).get("supervision") or {}).get("auto_restart_disabled") \
        if observer_header else None
    if observer_header is None:
        checks.append(_check("fault_retention_attested", "evidence", "INSUFFICIENT_EVIDENCE",
                             {"reason": "observer_header_missing"}))
    elif retention is True:
        checks.append(_check("fault_retention_attested", "evidence", "PASS", {"auto_restart_disabled": True}))
    else:
        checks.append(_check("fault_retention_attested", "evidence", "INSUFFICIENT_EVIDENCE",
                             {"auto_restart_disabled": retention}))

    checks.append(_worker_liveness_check(events, duration_s, acceptance["liveness"], start_ev))
    checks.append(_background_check(tally["background"]))

    # --- integrity ------------------------------------------------------- #
    checks.append(_continuous_duration_check(events, acceptance))

    # --- safety ---------------------------------------------------------- #
    walscan_summary = _walscan_summary(inputs, acceptance, events, observer_rows, started_at, start_wall)
    checks.extend(_safety_checks(inputs, acceptance, events, observer_rows, observer_samples, meta, started_at,
                                 start_wall, stop_wall, duration_s, walscan_summary))

    # --- liveness -------------------------------------------------------- #
    checks.append(_liveness_exit_check(events, snapshots, observer_samples))
    checks.append(_liveness_restart_check(observer_samples, snapshots))

    # --- load ------------------------------------------------------------ #
    checks.append(_error_rate_absolute_check(counts_out, acceptance))
    thresholds = acceptance["thresholds"]
    load_min = thresholds["min_effective_load"]
    # OD-09 校準資料：每板每來源每小時 completed 分布與最大閒置間隔（供延後項凍結）
    load_calibration = {"per_board_hour": _per_board_hour(counts_out),
                        "max_idle_gap_s": _max_idle_gaps(completed_t)}
    if not is_candidate:
        checks.append(_check("min_effective_load", "load", NA, {"per_platform": load_min["per_platform"],
                                                                 "calibration": load_calibration}))
    elif load_min["status"] == "deferred":
        checks.append(_check("min_effective_load", "load", "BLOCKED", {"reason": "deferred",
                                                                        **load_calibration}))
    else:
        checks.append(_min_effective_load_check(load_min["per_platform"][inputs.platform], counts_out, completed_t))

    # --- resources ------------------------------------------------------- #
    resources_cfg = thresholds["resources"]
    stats = _stats_resources(snapshots, duration_s, int(workload["snapshot_interval_s"]))
    if not is_candidate:
        checks.append(_check("resources", "resources", NA, {"calibration": stats}))
    elif resources_cfg["status"] == "deferred":
        checks.append(_check("resources", "resources", "BLOCKED", {"reason": "deferred", "calibration": stats}))
    else:
        checks.append(_resources_check(resources_cfg["per_platform"][inputs.platform], stats))

    # --- relative -------------------------------------------------------- #
    uncertainty = thresholds["comparison_uncertainty"]
    tolerance = uncertainty["per_platform"][inputs.platform] if uncertainty["status"] == "frozen" else None
    if not is_candidate:
        for check_id in ("error_rate_relative", "throughput_relative", "stuck_relative"):
            checks.append(_check(check_id, "relative", NA, {}))
        checks.append(_check("comparison_uncertainty", "relative", NA, {}))
        checks.append(_check("baseline_compatible", "evidence", NA, {}))
    else:
        checks.append(_baseline_compatible_check(baseline, acceptance, inputs.platform, engine_files_sha(acceptance)))
        if uncertainty["status"] == "deferred":
            checks.append(_check("comparison_uncertainty", "relative", "BLOCKED", {"reason": "deferred"}))
            for check_id in ("error_rate_relative", "throughput_relative", "stuck_relative"):
                checks.append(_check(check_id, "relative", "BLOCKED", {"reason": "comparison_uncertainty_deferred"}))
        else:
            checks.append(_check("comparison_uncertainty", "relative", "PASS", {"tolerance": tolerance}))
            checks.append(_error_rate_relative_check(counts_out, baseline, tolerance))
            board_hours = {board: _effective_hour(stuck_rows, board, duration_s) for board in boards_seen}
            checks.append(_throughput_relative_check(counts_out, baseline, tolerance, thresholds, board_hours))
            checks.append(_stuck_relative_check(stuck, baseline, tolerance))

    # dependency blocks from pre_frozen / frozen_after_baseline
    blocked: dict[str, str] = {}
    for name in frozen_violations:
        for check_id in _DEPENDENTS.get(name, ()):
            blocked[check_id] = "NOT_PRE_FROZEN"
    for name in after_violations:
        for check_id in _DEPENDENTS.get(name, ()):
            blocked.setdefault(check_id, "FROZEN_BEFORE_BASELINE")
    for check in checks:
        # 實際違規（FAIL）不得被凍結時點問題覆寫成 BLOCKED／INSUFFICIENT（§14.2：安全群組不可豁免）
        if check["id"] in blocked and check["result"] not in (NA, "FAIL"):
            check["result"] = "BLOCKED" if is_candidate else "INSUFFICIENT_EVIDENCE"
            check["detail"] = {**check["detail"], "reason": blocked[check["id"]]}

    release = _summarize(checks, is_candidate)
    baseline_result = None
    if not is_candidate:
        baseline_result = _baseline_summary(checks)

    wal_hashes: dict[str, str] = {}
    if inputs.wal_dir is not None and Path(inputs.wal_dir).is_dir():
        for path in sorted(Path(inputs.wal_dir).iterdir()):
            if path.is_file():
                wal_hashes[path.name] = _sha256_file(path)
    evidence_hashes: dict[str, Any] = {}
    for name, path in (("report.json", report_path),
                       ("events.ndjson", Path(inputs.case_dir) / "events.ndjson"),
                       ("snapshots.ndjson", snapshot_path),
                       ("observer.ndjson", inputs.observer_path)):
        if path is not None and Path(path).is_file():
            evidence_hashes[name] = _sha256_file(Path(path))
    evidence_hashes["wal"] = wal_hashes
    excl_hashes: dict[str, str] = {}
    if inputs.excl_evidence_path is not None and Path(inputs.excl_evidence_path).is_dir():
        for path in sorted(Path(inputs.excl_evidence_path).glob("*.ndjson")):
            excl_hashes[path.name] = _sha256_file(path)

    return {
        "schema_version": GATE_SCHEMA,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "platform": inputs.platform,
        "mode": inputs.mode,
        "engine_result": engine,
        "release_gate_result": release,
        "baseline_result": baseline_result,
        "acceptance": {
            "path": acc_meta["path"],
            "revision": acc_meta["revision"],
            "sha256": acc_meta["sha256"],
            "git_blob_sha1": acc_meta["git_blob_sha1"],
            "workload_contract_sha256": workload_contract_sha256(acceptance),
            "effective_content": acceptance,
        },
        "run": {
            "started_at_utc": started_at,
            "ended_at_utc": ended_at,
            "duration_s": duration_s,
            "cli_identity": meta.get("cli_identity"),
            "deployed_content": copy.deepcopy(meta.get("deployed_content")) if meta.get("deployed_content") is not None else None,
            "daemon_version": (meta.get("daemon_identity") or {}).get("version"),
            "engine_files_sha256": engine_files_sha(acceptance),
            "contract_revision": meta.get("contract_revision"),
            "cases_yaml_sha256": meta.get("cases_yaml_sha256"),
        },
        "checks": checks,
        "counts": counts_out,
        "stuck": stuck,
        "walscan": walscan_summary,
        "evidence_hashes": evidence_hashes,
        "excl_evidence_hashes": excl_hashes,
    }


def engine_files_sha(acceptance: dict[str, Any]) -> dict[str, str | None]:
    """門檻檔 `workload.engine_files` 列出的引擎檔（相對 repo 根）之 sha256；缺檔為 None。"""
    files = list(acceptance["workload"]["engine_files"])
    return {rel: (_sha256_file(REPO_ROOT / rel) if (REPO_ROOT / rel).is_file() else None) for rel in files}


def _effective_hour(rows: list[dict[str, Any]], board: str, duration_s: float | None) -> float | None:
    if duration_s is None:
        return None
    cover = _cover_intervals(rows, board) if rows else []
    non_ready = 0.0
    for start, end in cover:
        lo = max(0.0, start)
        hi = min(float(duration_s), end)
        if hi > lo:
            non_ready += hi - lo
    effective = max(0.0, float(duration_s) - non_ready)
    return effective / 3600.0


def _max_idle_gaps(completed_t: dict[str, list[float]]) -> dict[str, Any]:
    """每個 `com|source` 相鄰 completed 事件的最大間隔（秒）；不足兩筆記 0。"""
    result: dict[str, Any] = {}
    for key in sorted(completed_t):
        times = sorted(completed_t[key])
        result[key] = _num(max((right - left for left, right in zip(times, times[1:])), default=0.0))
    return result


def _per_board_hour(counts_out: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {key: {"completed": entry["completed"], "effective_hour": entry["effective_hour"]}
            for key, entry in counts_out.items() if key != _TOTAL_KEY}


def _observation_gaps_check(observer_samples: list[dict[str, Any]] | None, snapshots: list[dict[str, Any]],
                            duration_s: float | None, workload: dict[str, Any]) -> dict[str, Any]:
    factor = float(workload["max_observation_gap_factor"])
    gaps: list[dict[str, Any]] = []
    missing: list[str] = []

    def in_window(t: float) -> bool:
        return 0.0 <= t <= float(duration_s) if duration_s is not None else True

    def scan(label: str, times: list[float], interval: float) -> None:
        limit = factor * interval
        times = [t for t in times if in_window(t)]
        if not times:
            missing.append(label)
            return
        if times[0] > limit:
            gaps.append({"source": label, "from_t": 0, "to_t": _num(times[0])})
        if duration_s is not None and duration_s - times[-1] > limit:
            gaps.append({"source": label, "from_t": _num(times[-1]), "to_t": _num(duration_s)})
        for left, right in zip(times, times[1:]):
            if right - left > limit:
                gaps.append({"source": label, "from_t": _num(left), "to_t": _num(right)})

    def null_pid_runs(label: str, rows: list[tuple[float, Any]]) -> None:
        """daemon pid 連續為 null 超過 factor × snapshot_interval_s 即為觀測缺口（無法辨識 daemon）。"""
        limit = factor * float(workload["snapshot_interval_s"])
        ordered = sorted((t, pid) for t, pid in rows if in_window(t))
        run_start: float | None = None
        for t, pid in ordered:
            if pid is None:
                if run_start is None:
                    run_start = t
                continue
            if run_start is not None and t - run_start > limit:
                gaps.append({"source": label, "reason": "pid_null", "from_t": _num(run_start), "to_t": _num(t)})
            run_start = None
        if run_start is not None and ordered and ordered[-1][0] - run_start > limit:
            gaps.append({"source": label, "reason": "pid_null", "from_t": _num(run_start),
                         "to_t": _num(ordered[-1][0])})

    if observer_samples is None:
        missing.append("observer")
    else:
        scan("observer", [float(row["t"]) for row in observer_samples], float(workload["observer_interval_s"]))
        null_pid_runs("observer", [(float(row["t"]), (row.get("daemon") or {}).get("pid"))
                                   for row in observer_samples])
    scan("snapshots", sorted(float(row["t"]) for row in snapshots if row.get("t") is not None),
         float(workload["snapshot_interval_s"]))
    null_pid_runs("snapshots", [(float(row["t"]), _row_pid(row)) for row in snapshots if row.get("t") is not None])
    if missing:
        return _check("observation_gaps", "evidence", "INSUFFICIENT_EVIDENCE", {"missing": missing, "gaps": gaps})
    if gaps:
        return _check("observation_gaps", "evidence", "INSUFFICIENT_EVIDENCE", {"gaps": gaps})
    return _check("observation_gaps", "evidence", "PASS", {})


def _continuous_duration_check(events: list[dict[str, Any]], acceptance: dict[str, Any]) -> dict[str, Any]:
    starts = [row for row in events if row.get("kind") == "start"]
    stops = [row for row in events if row.get("kind") == "stop"]
    sigint = any(row.get("kind") == "sigint" for row in events)
    minimum = float(acceptance["workload"]["min_continuous_duration_s"])
    detail: dict[str, Any] = {"starts": len(starts), "stops": len(stops), "sigint": sigint}
    if len(starts) > 1:
        return _check("continuous_duration", "integrity", "FAIL", {**detail, "reason": "stitched"})
    if sigint:
        return _check("continuous_duration", "integrity", "FAIL", {**detail, "reason": "sigint"})
    if len(starts) != 1 or len(stops) != 1:
        return _check("continuous_duration", "integrity", "INSUFFICIENT_EVIDENCE", {**detail, "reason": "start_stop_missing"})
    duration = float(stops[0]["t"]) - float(starts[0]["t"])
    detail["duration_s"] = _num(duration)
    if duration < minimum:
        return _check("continuous_duration", "integrity", "FAIL", {**detail, "reason": "too_short"})
    return _check("continuous_duration", "integrity", "PASS", detail)


def _premature_stop(stop: dict[str, Any], target_s: float | None, slack_s: float) -> bool:
    """worker 提前結束：reason=error，或在負載目標時長前超過心跳容差即停止。

    引擎在 deadline 後才 join 並收尾（背景 final drain 另有最長 30 s），正常收尾的 worker_stop
    可能略早於 `stop` 事件，因此不得以 `stop.t` 為基準判提前結束。
    """
    if stop.get("reason") == "error":
        return True
    if target_s is None or stop.get("t") is None:
        return False
    return float(stop["t"]) < target_s - slack_s


def _worker_liveness_check(events: list[dict[str, Any]], duration_s: float | None,
                           liveness: dict[str, Any], start_ev: dict[str, Any] | None) -> dict[str, Any]:
    max_gap = float(liveness["worker_heartbeat_max_gap_s"])
    starts = {row.get("source"): row for row in events if row.get("kind") == "worker_start"}
    declared = start_ev.get("workers") if start_ev else None
    if not isinstance(declared, int) or isinstance(declared, bool) or declared < 0:
        return _check("worker_liveness", "evidence", "INSUFFICIENT_EVIDENCE", {"reason": "start_workers_missing"})
    # 預期 agent 集合由引擎 `start.workers` 宣告；不由已觀測的事件反推（整個 worker 消失也必須被發現）
    expected = {"human", "orchestrator", "bg_resolver"} | {f"agent:rhw{index}" for index in range(declared)}
    missing = sorted(worker for worker in expected if worker not in starts)
    gaps: list[dict[str, Any]] = []
    premature: list[str] = []
    stops = {row.get("source"): row for row in events if row.get("kind") == "worker_stop"}
    end_t = float(duration_s) if duration_s is not None else None
    target_s = start_ev.get("duration_s") if start_ev else None
    target_s = float(target_s) if isinstance(target_s, (int, float)) and not isinstance(target_s, bool) else end_t
    for worker in sorted(expected):
        if worker not in starts:
            continue
        alive = sorted(float(row["t"]) for row in events
                       if row.get("kind") == "worker_alive" and (row.get("source") == worker or row.get("worker") == worker)
                       and row.get("t") is not None)
        points = [float(starts[worker].get("t", 0))] + alive + ([end_t] if end_t is not None else [])
        for left, right in zip(points, points[1:]):
            if right - left > max_gap:
                gaps.append({"worker": worker, "from_t": _num(left), "to_t": _num(right)})
        stop = stops.get(worker)
        if stop is not None and _premature_stop(stop, target_s, max_gap):
            premature.append(worker)
    if missing or gaps or premature:
        return _check("worker_liveness", "evidence", "INSUFFICIENT_EVIDENCE",
                      {"missing": missing, "gaps": gaps, "premature_stop": premature})
    return _check("worker_liveness", "evidence", "PASS", {"workers": sorted(expected)})


def _background_check(background: dict[str, Any]) -> dict[str, Any]:
    detail = {"bg_lost": background["bg_lost"], "bg_backpressure_s": _num(float(background["bg_backpressure_s"])),
              "bg_slow": background["bg_slow"], "bg_drain_remaining": background["bg_drain_remaining"]}
    if background["bg_lost"] > 0:
        return _check("background_result_coverage", "evidence", "INSUFFICIENT_EVIDENCE", detail)
    return _check("background_result_coverage", "evidence", "PASS", detail)


def _safety_checks(inputs: GateInputs, acceptance: dict[str, Any], events: list[dict[str, Any]],
                   observer_rows: list[dict[str, Any]] | None, observer_samples: list[dict[str, Any]] | None,
                   meta: dict[str, Any], started_at: Any, start_wall: dt.datetime | None,
                   stop_wall: dt.datetime | None, duration_s: float | None,
                   scan: dict[str, Any] | None) -> list[dict[str, Any]]:

    checks: list[dict[str, Any]] = []
    if scan is None:
        # require_wal_dir：安全群組必須有 WAL 證據；缺 WAL 時整組不得 PASS（僅既有違規 FAIL 可優先呈現）
        for check_id in ("safety_unauthorized_tx", "safety_cross_context_replay", "safety_dangerous_auto_resend"):
            checks.append(_check(check_id, "safety", "INSUFFICIENT_EVIDENCE", {"reason": "wal_missing"}))
    else:
        # 結果不明的寫入（tx_outcome_unknown）可能已送出尾端位元組，無法證明無違規，不得判 PASS（§11.3、§15.12）
        uncertain_tx = [item for item in scan["uncertain"] if item.get("kind") == "tx_unknown"]
        unresolved_problem = bool(scan["problems"] or scan["loss_flags"] or uncertain_tx)
        uncertain_unauth = [item for item in scan["uncertain"] if item.get("kind") == "unauthorized"]
        uncertain_cross = [item for item in scan["uncertain"] if item.get("kind") == "cross_context"]
        if scan["unauthorized_tx"]:
            checks.append(_check("safety_unauthorized_tx", "safety", "FAIL",
                                 {"count": len(scan["unauthorized_tx"]), "items": scan["unauthorized_tx"][:20]}))
        elif unresolved_problem or scan["context_missing"] or uncertain_unauth:
            checks.append(_check("safety_unauthorized_tx", "safety", "INSUFFICIENT_EVIDENCE",
                                 {"problems": scan["problems"][:20], "loss_flags": scan["loss_flags"],
                                  "context_missing": scan["context_missing"], "uncertain": uncertain_unauth}))
        else:
            checks.append(_check("safety_unauthorized_tx", "safety", "PASS", {"records": scan["records"]}))

        if scan["cross_context_replay"]:
            checks.append(_check("safety_cross_context_replay", "safety", "FAIL",
                                 {"items": scan["cross_context_replay"]}))
        elif unresolved_problem or scan["context_missing"] or uncertain_cross:
            checks.append(_check("safety_cross_context_replay", "safety", "INSUFFICIENT_EVIDENCE",
                                 {"problems": scan["problems"][:20], "context_missing": scan["context_missing"],
                                  "uncertain": uncertain_cross}))
        else:
            checks.append(_check("safety_cross_context_replay", "safety", "PASS", {}))

        if scan["dangerous_auto_resend"]:
            checks.append(_check("safety_dangerous_auto_resend", "safety", "FAIL",
                                 {"items": scan["dangerous_auto_resend"]}))
        elif unresolved_problem:
            checks.append(_check("safety_dangerous_auto_resend", "safety", "INSUFFICIENT_EVIDENCE",
                                 {"problems": scan["problems"][:20]}))
        else:
            checks.append(_check("safety_dangerous_auto_resend", "safety", "PASS", {}))

    holder = _double_holder_check(inputs, acceptance, observer_rows, observer_samples, start_wall,
                                  stop_wall, duration_s, events)
    if scan is None and holder["result"] != "FAIL":
        holder = _check("safety_double_holder", "safety", "INSUFFICIENT_EVIDENCE",
                        {**holder["detail"], "reason": "wal_missing"})
    checks.insert(1, holder)
    return checks


def _foreign_holder_violations(holders: Any, own_pid: Any) -> tuple[int, int]:
    """回 (違規數, 無法分類數)。

    `daemon status.foreign_holders` 為 `{tty: 持有者 pid（int，可能就是本 daemon）}`，
    或 `{tty: {device, holder_unknown:true, pids?}}`。int 值與本 daemon pid 相同不算違規；
    holder_unknown 物件只有 `pids` 含非本 daemon 的 pid 才算違規，其餘僅為診斷。
    """
    if not isinstance(holders, dict):
        return 0, 0
    violations = 0
    unclassified = 0
    for entry in holders.values():
        if isinstance(entry, dict):
            for pid in entry.get("pids") or []:
                if own_pid is None:
                    unclassified += 1
                elif int(pid) != int(own_pid):
                    violations += 1
        elif entry is not None:
            if own_pid is None:
                unclassified += 1
            elif int(entry) != int(own_pid):
                violations += 1
    return violations, unclassified


def _double_holder_check(inputs: GateInputs, acceptance: dict[str, Any], observer_rows: list[dict[str, Any]] | None,
                         observer_samples: list[dict[str, Any]] | None, start_wall: dt.datetime | None,
                         stop_wall: dt.datetime | None, duration_s: float | None,
                         events: list[dict[str, Any]]) -> dict[str, Any]:
    """雙重持有：觀測到的多 daemon／多持有者為 FAIL；Windows 另需獨占開啟證據（R1-F7）。"""
    if observer_samples is None:
        return _check("safety_double_holder", "safety", "INSUFFICIENT_EVIDENCE", {"reason": "observer_missing"})
    violations = 0
    unknown_samples = 0
    procscan_missing = 0
    unclassified = 0
    for row in observer_samples:
        daemon = row.get("daemon") or {}
        own_pid = daemon.get("pid")
        if daemon.get("multi_open") is True:
            violations += 1
        found, unknown_pid = _foreign_holder_violations(daemon.get("foreign_holders"), own_pid)
        violations += found
        unclassified += unknown_pid
        procscan = row.get("procscan")
        others = procscan.get("other_serialwrapd") if isinstance(procscan, dict) else None
        if others is None:
            procscan_missing += 1
        elif others:
            violations += 1
        if daemon.get("holder_unknown") is True:
            unknown_samples += 1
    excl_dir = inputs.excl_evidence_path
    if inputs.platform == "windows" and excl_dir is not None and Path(excl_dir).is_dir():
        for name in ("probes.ndjson", "handoff.ndjson"):
            rows, _ = _read_ndjson(Path(excl_dir) / name)
            violations += sum(1 for row in rows or [] if row.get("verdict") == "violated")
    detail: dict[str, Any] = {"events": violations, "holder_unknown_samples": unknown_samples}
    if violations > 0:
        return _check("safety_double_holder", "safety", "FAIL", detail)
    if unclassified:
        return _check("safety_double_holder", "safety", "INSUFFICIENT_EVIDENCE",
                      {**detail, "reason": "daemon_pid_unknown", "samples": unclassified})
    if procscan_missing:
        return _check("safety_double_holder", "safety", "INSUFFICIENT_EVIDENCE",
                      {**detail, "reason": "procscan_missing", "samples": procscan_missing})
    if inputs.platform != "windows":
        return _check("safety_double_holder", "safety", "PASS", detail)
    result, extra = _windows_double_holder(inputs, acceptance, observer_rows or [], observer_samples,
                                           start_wall, stop_wall, duration_s)
    detail.update(extra)
    return _check("safety_double_holder", "safety", result, detail)


def _liveness_exit_check(events: list[dict[str, Any]], snapshots: list[dict[str, Any]],
                         observer_samples: list[dict[str, Any]] | None) -> dict[str, Any]:
    deaths = [row for row in events if row.get("kind") == "major" and row.get("detail") == "daemon_death"]
    if deaths:
        return _check("liveness_daemon_exit", "liveness", "FAIL", {"events": len(deaths)})
    if not observer_samples:
        return _check("liveness_daemon_exit", "liveness", "INSUFFICIENT_EVIDENCE", {"reason": "observer_missing"})
    run = 0
    run_flags: list[Any] = []
    for row in observer_samples:
        if (row.get("daemon") or {}).get("ok") is False:
            run += 1
            run_flags.append((row.get("daemon") or {}).get("pid_alive"))
            if run >= 3 and any(flag is False for flag in run_flags):
                return _check("liveness_daemon_exit", "liveness", "FAIL", {"consecutive_failures": run})
        else:
            run = 0
            run_flags = []
    unknown = any(row.get("daemon", {}).get("ok") is False and row.get("daemon", {}).get("pid_alive") is None
                  for row in observer_samples)
    if unknown:
        return _check("liveness_daemon_exit", "liveness", "INSUFFICIENT_EVIDENCE", {"reason": "pid_alive_unknown"})
    return _check("liveness_daemon_exit", "liveness", "PASS", {})


def _liveness_restart_check(observer_samples: list[dict[str, Any]] | None,
                            snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    changes = 0
    if observer_samples:
        changes += _pid_changes([(row.get("daemon") or {}).get("pid") for row in observer_samples])
    changes += _pid_changes([(row.get("daemon") or {}).get("pid", row.get("pid")) for row in snapshots])
    if changes > 0:
        return _check("liveness_restart", "liveness", "FAIL", {"changes": changes})
    return _check("liveness_restart", "liveness", "PASS", {"changes": 0})


def _error_rate_absolute_check(counts_out: dict[str, dict[str, Any]], acceptance: dict[str, Any]) -> dict[str, Any]:
    limit = float(acceptance["thresholds"]["unexpected_cmd_error_rate"]["max_ratio"])
    rates = {key: entry["error_rate"] for key, entry in counts_out.items() if entry.get("accepted", 0) > 0}
    if not rates:
        return _check("error_rate_absolute", "load", "INSUFFICIENT_EVIDENCE", {"reason": "no_accepted_commands"})
    over = {key: rate for key, rate in rates.items() if rate > limit}
    if over:
        return _check("error_rate_absolute", "load", "FAIL", {"limit": limit, "over": over})
    return _check("error_rate_absolute", "load", "PASS", {"limit": limit, "rates": rates})


def _min_effective_load_check(per: dict[str, Any], counts_out: dict[str, dict[str, Any]],
                              completed_t: dict[str, list[float]]) -> dict[str, Any]:
    need = per["min_completed_per_board_source"] or {}
    max_idle = float(per["max_idle_gap_s"])
    shortfalls: list[dict[str, Any]] = []
    for key, required in sorted(need.items()):
        completed = counts_out.get(key, {}).get("completed", 0)
        if completed < required:
            shortfalls.append({"key": key, "completed": completed, "required": required})
        times = sorted(completed_t.get(key, []))
        idle = max((right - left for left, right in zip(times, times[1:])), default=0.0)
        if len(times) >= 2 and idle > max_idle:
            shortfalls.append({"key": key, "max_idle_gap_s": _num(idle), "limit": _num(max_idle)})
    if shortfalls:
        return _check("min_effective_load", "load", "INSUFFICIENT_EVIDENCE", {"shortfalls": shortfalls})
    return _check("min_effective_load", "load", "PASS", {"keys": sorted(need)})


def _resources_check(per: dict[str, Any], stats: dict[str, Any]) -> dict[str, Any]:
    peak_limit = per["rss_peak_kb"]
    steady_limit = per["rss_steady_kb"]
    slope_limit = per["rss_slope_kb_per_h"]
    over = []
    if stats["peak_kb"] is not None and stats["peak_kb"] > peak_limit:
        over.append("peak")
    if stats["steady_kb"] is not None and stats["steady_kb"] > steady_limit:
        over.append("steady")
    if stats["slope_kb_per_h"] is not None and stats["slope_kb_per_h"] > slope_limit:
        over.append("slope")
    detail = {"stats": stats, "limits": per, "over": over}
    if over:
        return _check("resources", "resources", "FAIL", detail)
    if stats["valid_last_24h"] < 0.9 * stats["expected_last_24h"]:
        return _check("resources", "resources", "INSUFFICIENT_EVIDENCE", detail)
    return _check("resources", "resources", "PASS", detail)


def _baseline_compatible_check(baseline: dict[str, Any] | None, acceptance: dict[str, Any], platform: str,
                               engine_hashes: dict[str, Any]) -> dict[str, Any]:
    if baseline is None:
        return _check("baseline_compatible", "evidence", "INSUFFICIENT_EVIDENCE", {"reason": "baseline_missing"})
    reasons = []
    if baseline.get("platform") != platform:
        reasons.append("platform")
    if (baseline.get("run") or {}).get("engine_files_sha256") != engine_hashes:
        reasons.append("engine_files_sha256")
    if (baseline.get("acceptance") or {}).get("workload_contract_sha256") != workload_contract_sha256(acceptance):
        reasons.append("workload_contract_sha256")
    if baseline.get("baseline_result") != "PASS":
        reasons.append("baseline_result")
    if reasons:
        return _check("baseline_compatible", "evidence", "INSUFFICIENT_EVIDENCE", {"reasons": reasons})
    return _check("baseline_compatible", "evidence", "PASS", {})


def _error_rate_relative_check(counts_out: dict[str, dict[str, Any]], baseline: dict[str, Any] | None,
                               tolerance: dict[str, Any]) -> dict[str, Any]:
    base_counts = (baseline or {}).get("counts") or {}
    band = float(tolerance["error_rate_abs_tolerance"])
    worst = "PASS"
    detail: dict[str, Any] = {}
    for key, entry in counts_out.items():
        cand = entry.get("error_rate")
        base = (base_counts.get(key) or {}).get("error_rate")
        if cand is None or base is None:
            worst = "INSUFFICIENT_EVIDENCE"
            detail[key] = {"reason": "baseline_or_candidate_missing"}
            continue
        if cand <= base:
            verdict = "PASS"
        elif cand <= base + band:
            verdict = "INSUFFICIENT_EVIDENCE"
        else:
            verdict = "FAIL"
        detail[key] = {"candidate": cand, "baseline": base, "result": verdict}
        if verdict == "FAIL":
            worst = "FAIL"
        elif verdict == "INSUFFICIENT_EVIDENCE" and worst == "PASS":
            worst = "INSUFFICIENT_EVIDENCE"
    # 基線有、候選完全沒有的 (board, source)：無法比較，不得略過
    for key in sorted(set(base_counts) - set(counts_out)):
        if worst == "PASS":
            worst = "INSUFFICIENT_EVIDENCE"
        detail[key] = {"reason": "candidate_missing"}
    return _check("error_rate_relative", "relative", worst, detail)


def _throughput_relative_check(counts_out: dict[str, dict[str, Any]], baseline: dict[str, Any] | None,
                               tolerance: dict[str, Any], thresholds: dict[str, Any],
                               board_hours: dict[str, float | None]) -> dict[str, Any]:
    base_counts = (baseline or {}).get("counts") or {}
    minimum = float(thresholds["effective_throughput"]["min_ratio_of_baseline"])
    band = float(tolerance["throughput_ratio_tolerance"])
    worst = "PASS"
    detail: dict[str, Any] = {}
    for key, cand_entry in counts_out.items():
        # 只比對候選實際出現的 (board, source)；候選缺鍵的完成數下限由 min_effective_load 判定
        if key == _TOTAL_KEY:
            continue
        base_entry = base_counts.get(key) or {}
        base_rate = (base_entry.get("completed", 0) / base_entry["effective_hour"]
                     if base_entry.get("effective_hour") else None)
        cand_hour = cand_entry.get("effective_hour") or board_hours.get(key.split("|", 1)[0])
        if base_rate in (None, 0) or cand_hour in (None, 0):
            worst = "INSUFFICIENT_EVIDENCE"
            detail[key] = {"reason": "effective_hour_missing"}
            continue
        ratio = (cand_entry.get("completed", 0) / cand_hour) / base_rate
        if ratio >= minimum:
            verdict = "PASS"
        elif ratio >= minimum - band:
            verdict = "INSUFFICIENT_EVIDENCE"
        else:
            verdict = "FAIL"
        detail[key] = {"ratio": ratio, "result": verdict}
        if verdict == "FAIL":
            worst = "FAIL"
        elif verdict == "INSUFFICIENT_EVIDENCE" and worst == "PASS":
            worst = "INSUFFICIENT_EVIDENCE"
    for key in sorted(set(base_counts) - set(counts_out) - {_TOTAL_KEY}):
        if worst == "PASS":
            worst = "INSUFFICIENT_EVIDENCE"
        detail[key] = {"reason": "candidate_missing"}
    return _check("throughput_relative", "relative", worst, detail)


def _stuck_relative_check(stuck: dict[str, dict[str, Any]], baseline: dict[str, Any] | None,
                          tolerance: dict[str, Any]) -> dict[str, Any]:
    if baseline is None or "stuck" not in baseline:
        return _check("stuck_relative", "relative", "INSUFFICIENT_EVIDENCE", {"reason": "baseline_stuck_missing"})
    base = baseline.get("stuck") or {}
    band = float(tolerance["stuck_s_tolerance"])
    worst = "PASS"
    detail: dict[str, Any] = {}
    for board in sorted(set(stuck) | set(base)):
        cand = stuck.get(board, {})
        ref = base.get(board, {})
        for prefix in ("total", "longest"):
            c_low = float(cand.get(f"{prefix}_lower_s", 0) or 0)
            c_up = float(cand.get(f"{prefix}_upper_s", 0) or 0)
            b_low = float(ref.get(f"{prefix}_lower_s", 0) or 0)
            b_up = float(ref.get(f"{prefix}_upper_s", 0) or 0)
            if c_up <= b_low + band:
                verdict = "PASS"
            elif c_low > b_up + band:
                verdict = "FAIL"
            else:
                verdict = "INSUFFICIENT_EVIDENCE"
            detail[f"{board}|{prefix}"] = {"candidate": [c_low, c_up], "baseline": [b_low, b_up], "result": verdict}
            if verdict == "FAIL":
                worst = "FAIL"
            elif verdict == "INSUFFICIENT_EVIDENCE" and worst == "PASS":
                worst = "INSUFFICIENT_EVIDENCE"
    return _check("stuck_relative", "relative", worst, detail)


def _walscan_summary(inputs: GateInputs, acceptance: dict[str, Any], events: list[dict[str, Any]],
                     observer_rows: list[dict[str, Any]] | None, started_at: Any,
                     start_wall: dt.datetime | None) -> dict[str, Any] | None:
    if inputs.wal_dir is None or not Path(inputs.wal_dir).is_dir():
        return None
    context = build_safety_context(events, observer_rows)
    since = started_at or (start_wall.isoformat() if start_wall else "")
    return scan_wal(Path(inputs.wal_dir), since_wall_ts=since,
                    allowed_tx_source_regex=acceptance["safety"]["allowed_tx_source_regex"], context=context,
                    tolerance_s=float(acceptance["evidence"]["context_time_tolerance_s"]))


def _summarize(checks: list[dict[str, Any]], is_candidate: bool) -> str:
    results = {check["result"] for check in checks}
    if "FAIL" in results:
        return "FAIL"
    if "INSUFFICIENT_EVIDENCE" in results:
        return "INSUFFICIENT_EVIDENCE"
    if "BLOCKED" in results:
        return "BLOCKED"
    return "PASS" if is_candidate else NA


def _baseline_summary(checks: list[dict[str, Any]]) -> str:
    relevant = [check["result"] for check in checks
                if (check["group"] in _BASELINE_GROUPS or check["id"] in _BASELINE_IDS) and check["result"] != NA]
    if "FAIL" in relevant:
        return "FAIL"
    if "INSUFFICIENT_EVIDENCE" in relevant or "BLOCKED" in relevant:
        return "INSUFFICIENT_EVIDENCE"
    return "PASS"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="長跑外層發版判定器（release_gate_result）")
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--case-dir", required=True, type=Path)
    parser.add_argument("--acceptance", required=True, type=Path)
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--mode", required=True, choices=("baseline", "candidate"))
    parser.add_argument("--observer", type=Path, default=None)
    parser.add_argument("--wal-dir", type=Path, default=None)
    parser.add_argument("--baseline-gate", type=Path, default=None)
    parser.add_argument("--excl-evidence", type=Path, default=None)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    inputs = GateInputs(
        report_dir=args.report_dir, case_dir=args.case_dir, acceptance_path=args.acceptance,
        platform=args.platform, mode=args.mode, observer_path=args.observer, wal_dir=args.wal_dir,
        baseline_gate_path=args.baseline_gate, excl_evidence_path=args.excl_evidence,
    )
    try:
        out = evaluate(inputs)
    except Exception as exc:  # noqa: BLE001 - 判定器自身失敗不得以 FAIL（1）退出
        print(f"release gate 判定器失敗：{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
                        encoding="utf-8")
    return EXIT_CODES[out["release_gate_result"]]


if __name__ == "__main__":
    sys.exit(main())
