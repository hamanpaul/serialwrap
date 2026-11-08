"""OD-09 延後項凍結工具與候選長跑開跑登記（P2-T16）。

本模組是 `longrun_acceptance.yaml` 延後項（`resources`、`min_effective_load`、`comparison_uncertainty`）的唯一寫入者。
門檻判定仍由 `realhw/acceptance/gate.py` 負責，本模組不修改 gate 的判定邏輯，只寫入
`thresholds.<key>.per_platform.<os>.<field>` 與凍結紀錄欄位。

子命令（`python3 -m realhw.acceptance.od09_freeze`）：
  stats             由單一平台的 48 h 基線 case 目錄計算統計，並執行基線違規檢查
  propose           依固定公式由兩平台基線統計產生凍結提案
  freeze            一次凍結兩平台延後項，改寫門檻檔並附加 owner-decisions addenda
  check             檢查門檻檔於指定 run 起點是否已凍結且欄位完整
  register-candidate 候選長跑開跑前登記（append-only ledger）；未凍結者拒絕

本模組可 import yaml 與 `realhw.acceptance.gate`；不得 import 後續階段的模組，也不得讀取後續階段才產生的證據清單。
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import math
import os
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml

from realhw.acceptance import gate

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ACCEPTANCE = Path(__file__).with_name("longrun_acceptance.yaml")
LEDGER_PATH = REPO_ROOT / "realhw" / "acceptance" / "candidate_longrun_starts.ndjson"
CHECKER_PATH = REPO_ROOT / "tests" / "acceptance" / "check_evidence_index.py"

DEFERRED_KEYS: tuple[str, ...] = ("resources", "min_effective_load", "comparison_uncertainty")
PLATFORMS: tuple[str, ...] = ("linux", "windows")
# 必須等於 Task 15「延後項讀寫契約」表（tests/test_od09_freeze.py::test_per_platform_fields_match_acceptance）
PER_PLATFORM_FIELDS: Mapping[str, tuple[str, ...]] = {
    "resources": ("rss_peak_kb", "rss_steady_kb", "rss_slope_kb_per_h"),
    "min_effective_load": ("min_completed_per_board_source", "max_idle_gap_s"),
    "comparison_uncertainty": ("error_rate_abs_tolerance", "throughput_ratio_tolerance", "stuck_s_tolerance", "max_reruns"),
}
BASELINE_ITEMS: Mapping[str, str] = {"linux": "S0-L3-BASELINE-LINUX-48H", "windows": "S0-L3-BASELINE-WINDOWS-48H"}
FREEZE_ITEM: str = "S0-OD09-DEFERRED-FROZEN"
CANDIDATE_LONGRUN_ITEMS: Mapping[str, str] = {
    "S1B-L3-MIXED-48H-WINDOWS": "windows",
    "S24-L3-LRMIXED-48H-LINUX": "linux",
    "S24-L3-LRMIXED-48H-WINDOWS": "windows",
    "S3-L3-AB-48H-LINUX": "linux",
    "S3-L3-AB-48H-WINDOWS": "windows",
}
OD09_CODES: tuple[str, ...] = ("HARNESS_OD09_BASELINE_INVALID", "HARNESS_OD09_TOO_LATE", "HARNESS_OD09_NOT_FROZEN")
ADDENDA_TOPIC = "OD-09 延後項凍結"

# 固定公式（§15.11.4）。提案規則原樣寫入 proposal；擁有者只能在 freeze 時以 overrides 明示覆寫。
PROPOSAL_RULES: Mapping[str, str] = {
    "resources.per_platform.<os>.rss_peak_kb": "ceil(1.25 × 該平台基線 rss_peak_kb)",
    "resources.per_platform.<os>.rss_steady_kb": "ceil(1.25 × 該平台基線穩態中位數 rss_steady_median_kb)",
    "resources.per_platform.<os>.rss_slope_kb_per_h": "max(ceil(2 × 該平台基線穩態斜率), 256)",
    "min_effective_load.per_platform.<os>.min_completed_per_board_source": (
        "逐 (com|source)：max(1, floor(0.5 × 該平台基線全程 completed))，鍵為該平台基線出現的全部 (com, source)"
    ),
    "min_effective_load.per_platform.<os>.max_idle_gap_s": "固定 3600",
    "comparison_uncertainty.per_platform.<os>.error_rate_abs_tolerance": "固定 0.002",
    "comparison_uncertainty.per_platform.<os>.throughput_ratio_tolerance": "固定 0.02",
    "comparison_uncertainty.per_platform.<os>.stuck_s_tolerance": "I = max(linux 與 windows 基線的 snapshot_interval_s)",
    "comparison_uncertainty.per_platform.<os>.max_reruns": "固定 1",
}


class Od09Error(Exception):
    """OD-09 守門失敗；`code` 屬於 `OD09_CODES`，`detail` 列出違規項目。"""

    def __init__(self, code: str, detail: list[str] | None = None) -> None:
        if code not in OD09_CODES:
            raise ValueError(f"未知的 OD-09 原因碼：{code}")
        self.code = code
        self.detail = list(detail or [])
        super().__init__(f"{code}: {'; '.join(self.detail)}")


@dataclass(frozen=True)
class BaselineStats:
    platform: str
    artifact_id: str
    daemon_implementation: str
    duration_s: int
    snapshot_interval_s: int
    max_snapshot_gap_s: int
    rss_peak_kb: int
    rss_steady_median_kb: int
    rss_steady_slope_kb_per_h: float
    hourly_done: Mapping[str, Mapping[str, int]]    # "<com>|<source>" -> {"min": n, "p10": n, "hours": h}
    completed_total: Mapping[str, int]              # "<com>|<source>" -> 全程 completed（counting.definitions.completed）


# --------------------------------------------------------------------------- #
# 共用工具
# --------------------------------------------------------------------------- #
def _canon(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(Path(path).read_bytes())


def _proposal_digest(proposal: Mapping[str, Any]) -> str:
    body = {key: value for key, value in proposal.items() if key != "proposal_sha256"}
    return _sha256_bytes(_canon(body))


def _parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _require_iso(value: str, *, name: str) -> dt.datetime:
    parsed = _parse_iso(value)
    if parsed is None:
        raise ValueError(f"{name} 必須是 ISO 8601 時間：{value!r}")
    return parsed


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _read_rows(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"缺少證據檔：{path}")
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number} 不是 JSON 物件")
        rows.append(value)
    return rows


def _ledger_lines(ledger_path: Path) -> list[str]:
    """ledger 的非空白行（不解析）。非 UTF-8 的行也計入，使解碼失敗無法繞過時序守門。"""
    path = Path(ledger_path)
    if not path.is_file():
        return []
    raw = path.read_bytes()
    return [line for line in raw.decode("utf-8", errors="replace").splitlines() if line.strip()]


@contextlib.contextmanager
def _ledger_lock(ledger_path: Path) -> Iterator[None]:
    """以 sidecar 鎖檔跨程序互斥（POSIX flock、Windows msvcrt.locking），使查重、時序守門與 append 原子化。"""
    ledger_path = Path(ledger_path)
    lock_path = ledger_path.with_name(ledger_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_compact_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _ceil_scaled(value: int, numerator: int, denominator: int) -> int:
    """整數運算的 ceil(numerator × value / denominator)，避免浮點誤差。"""
    return -(-numerator * int(value) // denominator)


def _nearest_rank_p10(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, math.ceil(0.10 * len(ordered)))
    return ordered[rank - 1]


def _verify_index_item(index_path: Path, item_id: str, *, root: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """依路徑載入 tests/acceptance/check_evidence_index.py 的 verify_item（不依賴後續階段的索引）。"""
    spec = importlib.util.spec_from_file_location("od09_check_evidence_index", CHECKER_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入證據索引檢查器：{CHECKER_PATH}")
    module = sys.modules.get(spec.name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module.verify_item(Path(index_path), item_id, root=Path(root))


# --------------------------------------------------------------------------- #
# 工作負載與基線統計
# --------------------------------------------------------------------------- #
def workload(acceptance_path: Path) -> Mapping[str, Any]:
    """回傳門檻檔的 workload 段（時長、快照間隔、缺口係數的唯一來源）。"""
    content, _meta = gate.load_acceptance(Path(acceptance_path))
    return content["workload"]


def _counting(acceptance_path: Path = DEFAULT_ACCEPTANCE) -> Mapping[str, Any]:
    content, _meta = gate.load_acceptance(Path(acceptance_path))
    return content["counting"]


def _snapshot_path(case_dir: Path) -> Path:
    """snapshots.ndjson 優先取 case 目錄；缺則取同層 report/（gate 的 report_dir 佈局）。"""
    for candidate in (case_dir / "snapshots.ndjson", case_dir.parent / "report" / "snapshots.ndjson"):
        if candidate.is_file():
            return candidate
    raise ValueError(f"缺少 snapshots.ndjson：{case_dir}")


def _event_span(events: list[dict[str, Any]]) -> tuple[float, float]:
    """連續時長的唯一定義與 gate `continuous_duration` 相同：恰一個 start、一個 stop、無 sigint。"""
    starts = [row for row in events if row.get("kind") == "start"]
    stops = [row for row in events if row.get("kind") == "stop"]
    sigint = any(row.get("kind") == "sigint" for row in events)
    if len(starts) != 1 or len(stops) != 1 or sigint:
        raise ValueError(
            f"基線 events 必須恰有一個 start、一個 stop 且無 sigint"
            f"（start={len(starts)} stop={len(stops)} sigint={sigint}）"
        )
    return float(starts[0]["t"]), float(stops[0]["t"])


def baseline_stats(case_dir: Path, *, platform: str, release_manifest: Path) -> BaselineStats:
    """由單一平台 48 h 基線 case 目錄計算統計（不做門檻判定；違規檢查見 validate_baseline）。

    時間基準與 gate 相同：時長取 `stop.t - start.t`，快照以相對 start 計，視窗為 `[0, duration]`；
    頭尾缺口（首快照前、末快照後）與相鄰間隔一併計入 `max_snapshot_gap_s`，與 gate 的觀測缺口判定一致。
    """
    if platform not in PLATFORMS:
        raise ValueError(f"未知 platform：{platform}")
    case_dir = Path(case_dir)
    events = _read_rows(case_dir / "events.ndjson")
    start_t, stop_t = _event_span(events)
    duration_s = int(stop_t - start_t)
    if duration_s <= 0:
        raise ValueError("基線時長必須為正")

    window: list[dict[str, Any]] = []
    for row in sorted(_read_rows(_snapshot_path(case_dir)), key=lambda item: float(item["t"])):
        relative = float(row["t"]) - start_t
        if 0.0 <= relative <= duration_s:
            window.append({**row, "t": relative})
    if len(window) < 2:
        raise ValueError("snapshots 不足，無法計算基線")
    times = [float(row["t"]) for row in window]
    gaps = [later - earlier for earlier, later in zip(times, times[1:])]
    interval_s = int(statistics.median(gaps))
    if interval_s < 1:
        raise ValueError("snapshot 間隔無效")
    max_gap_s = int(max([*gaps, times[0], duration_s - times[-1]]))

    resources = gate._stats_resources(window, float(duration_s), interval_s)
    if resources["peak_kb"] is None or resources["steady_kb"] is None or resources["slope_kb_per_h"] is None:
        raise ValueError("snapshots 缺 rss_kb，或穩態窗（最後 24 h）資料不足")

    counts = gate.count_commands(events, _counting())
    done_by_hour: dict[str, dict[int, int]] = {}
    for row in events:
        if row.get("kind") != "done":
            continue
        com, source = row.get("com"), row.get("source")
        if com is None or source is None:
            continue
        key = f"{com}|{source}"
        relative = float(row.get("t", 0) or 0) - start_t
        if relative < 0 or relative > duration_s:
            continue
        hour = int(relative // 3600)
        bucket = done_by_hour.setdefault(key, {})
        bucket[hour] = bucket.get(hour, 0) + 1

    full_hours = duration_s // 3600
    hourly: dict[str, dict[str, int]] = {}
    for key in sorted(counts):
        values = [done_by_hour.get(key, {}).get(hour, 0) for hour in range(full_hours)]
        hourly[key] = {"min": min(values) if values else 0, "p10": _nearest_rank_p10(values), "hours": full_hours}
    completed_total = {key: int(counts[key]["completed"]) for key in sorted(counts)}

    manifest_raw = Path(release_manifest).read_bytes()
    manifest = json.loads(manifest_raw.decode("utf-8"))
    daemon = (manifest.get("components") or {}).get("daemon") or {}
    return BaselineStats(
        platform=platform,
        artifact_id=_sha256_bytes(manifest_raw),
        daemon_implementation=str(daemon.get("implementation") or ""),
        duration_s=duration_s,
        snapshot_interval_s=interval_s,
        max_snapshot_gap_s=max_gap_s,
        rss_peak_kb=int(resources["peak_kb"]),
        rss_steady_median_kb=int(round(resources["steady_kb"])),
        rss_steady_slope_kb_per_h=float(resources["slope_kb_per_h"]),
        hourly_done=hourly,
        completed_total=completed_total,
    )


def validate_baseline(stats: BaselineStats, *, acceptance_path: Path) -> list[str]:
    """基線違規清單（空清單即可用）；時長與係數一律讀 workload（§15.11.4 唯一來源）。"""
    wl = workload(acceptance_path)
    problems: list[str] = []
    if stats.daemon_implementation != "python":
        problems.append(f"daemon_implementation={stats.daemon_implementation!r}，基線必須為 python")
    if stats.duration_s < int(wl["min_continuous_duration_s"]):
        problems.append(f"duration_s={stats.duration_s} 小於 min_continuous_duration_s={wl['min_continuous_duration_s']}")
    limit = float(wl["max_observation_gap_factor"]) * stats.snapshot_interval_s
    if stats.max_snapshot_gap_s > limit:
        problems.append(f"snapshot gap {stats.max_snapshot_gap_s}s 超過 {wl['max_observation_gap_factor']}×"
                        f"{stats.snapshot_interval_s}s（不得拼接 segments）")
    for key in sorted(stats.hourly_done):
        if int(stats.hourly_done[key]["min"]) == 0:
            problems.append(f"{key} 有完整小時 completed 為 0（load stop）")
    return problems


# --------------------------------------------------------------------------- #
# 提案
# --------------------------------------------------------------------------- #
def _interface_problems(thresholds: Any, *, label: str) -> list[str]:
    """延後項的 per_platform 形狀（平台集合與每平台欄位集合）必須與 PER_PLATFORM_FIELDS 完全相同。"""
    if not isinstance(thresholds, Mapping):
        return [f"{label}: thresholds 必須是物件"]
    problems: list[str] = []
    for key in DEFERRED_KEYS:
        item = thresholds.get(key)
        per = item.get("per_platform") if isinstance(item, Mapping) else None
        if not isinstance(per, Mapping):
            problems.append(f"{label} missing thresholds.{key}.per_platform")
            continue
        for extra in sorted(set(per) - set(PLATFORMS)):
            problems.append(f"{label} unexpected thresholds.{key}.per_platform.{extra}")
        expected = set(PER_PLATFORM_FIELDS[key])
        for platform in PLATFORMS:
            fields = per.get(platform)
            if not isinstance(fields, Mapping):
                problems.append(f"{label} missing thresholds.{key}.per_platform.{platform}")
                continue
            for field in sorted(expected - set(fields)):
                problems.append(f"{label} missing thresholds.{key}.per_platform.{platform}.{field}")
            for field in sorted(set(fields) - expected):
                problems.append(f"{label} unexpected thresholds.{key}.per_platform.{platform}.{field}")
    return problems


def _override_problems(overrides: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    for path in sorted(overrides):
        parts = path.split(".")
        valid = (
            len(parts) == 4
            and parts[0] in DEFERRED_KEYS
            and parts[1] == "per_platform"
            and parts[2] in PLATFORMS
            and parts[3] in PER_PLATFORM_FIELDS[parts[0]]
        )
        if not valid:
            problems.append(f"unknown override: {path}")
    return problems


def _read_baseline_gate(platform: str, path: Path) -> dict[str, Any]:
    """讀取單一平台基線 gate，核對平台、模式與判定；錯配即拒絕（不得把 Windows 基線當 Linux 用）。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{platform} 基線 gate 無法讀取：{path}：{exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{platform} 基線 gate 頂層必須是物件：{path}")
    problems: list[str] = []
    if doc.get("platform") != platform:
        problems.append(f"platform={doc.get('platform')!r}")
    if doc.get("mode") != "baseline":
        problems.append(f"mode={doc.get('mode')!r}")
    if doc.get("baseline_result") != "PASS":
        problems.append(f"baseline_result={doc.get('baseline_result')!r}")
    if problems:
        raise ValueError(f"{platform} 基線 gate 不符（{'；'.join(problems)}），拒絕提案")
    return doc


def propose(linux: BaselineStats, windows: BaselineStats, *, baseline_gates: Mapping[str, Path],
            acceptance_path: Path = DEFAULT_ACCEPTANCE) -> dict[str, Any]:
    """依 PROPOSAL_RULES 的固定公式產生凍結提案（不讀取門檻檔的延後項內容）。

    拒絕：統計 platform 標籤與位置不符、任一統計有基線違規、基線 gate 的平台／模式／判定錯配。
    """
    stats = {"linux": linux, "windows": windows}
    for platform in PLATFORMS:
        if platform not in baseline_gates:
            raise ValueError(f"缺少 {platform} 基線 gate 路徑")
        if stats[platform].platform != platform:
            raise ValueError(f"{platform} 位置的統計 platform={stats[platform].platform!r}，兩平台統計不得互換")
    problems = [f"{platform}: {problem}" for platform in PLATFORMS
                for problem in validate_baseline(stats[platform], acceptance_path=acceptance_path)]
    if problems:
        raise ValueError("基線統計違規，拒絕提案：" + "；".join(problems))
    docs = {platform: _read_baseline_gate(platform, Path(baseline_gates[platform])) for platform in PLATFORMS}

    interval = max(linux.snapshot_interval_s, windows.snapshot_interval_s)
    per: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in DEFERRED_KEYS}
    for platform in PLATFORMS:
        base = stats[platform]
        per["resources"][platform] = {
            "rss_peak_kb": _ceil_scaled(base.rss_peak_kb, 5, 4),
            "rss_steady_kb": _ceil_scaled(base.rss_steady_median_kb, 5, 4),
            "rss_slope_kb_per_h": max(math.ceil(2 * base.rss_steady_slope_kb_per_h), 256),
        }
        per["min_effective_load"][platform] = {
            "min_completed_per_board_source": {
                key: max(1, total // 2) for key, total in sorted(base.completed_total.items())
            },
            "max_idle_gap_s": 3600,
        }
        per["comparison_uncertainty"][platform] = {
            "error_rate_abs_tolerance": 0.002,
            "throughput_ratio_tolerance": 0.02,
            "stuck_s_tolerance": interval,
            "max_reruns": 1,
        }

    proposal: dict[str, Any] = {
        "thresholds": {key: {"per_platform": per[key]} for key in DEFERRED_KEYS},
        "baseline_artifacts": {
            platform: {
                "artifact_id": stats[platform].artifact_id,
                "release_gate_sha256": _sha256_file(Path(baseline_gates[platform])),
            }
            for platform in PLATFORMS
        },
        "proposal_rules": dict(PROPOSAL_RULES),
        "generated_at": _now_iso(),
        "acceptance_revision": _baseline_revision(docs),
    }
    proposal["proposal_sha256"] = _proposal_digest(proposal)
    problems = _interface_problems(proposal["thresholds"], label="proposal")
    if problems:
        raise ValueError("提案形狀與 PER_PLATFORM_FIELDS 不一致：" + "；".join(problems))
    return proposal


def _baseline_revision(docs: Mapping[str, Mapping[str, Any]]) -> str:
    revisions: set[str] = set()
    for platform in PLATFORMS:
        revision = (docs[platform].get("acceptance") or {}).get("revision")
        if not isinstance(revision, str) or not revision:
            raise ValueError(f"{platform} 基線 gate 缺 acceptance.revision")
        revisions.add(revision)
    if len(revisions) != 1:
        raise ValueError(f"兩平台基線 gate 的 acceptance.revision 不一致：{sorted(revisions)}")
    return revisions.pop()


# --------------------------------------------------------------------------- #
# 凍結
# --------------------------------------------------------------------------- #
def _cell(value: Any) -> str:
    """Markdown 表格儲存格：換行改空白、管線字元跳脫，避免破壞列結構。"""
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def _addenda_row(*, decision_id: str, approved_by: str, day: str, revision: str, proposal_sha: str,
                 override_keys: set[str]) -> str:
    keys_text = ", ".join(sorted(override_keys)) or "無"
    detail = (f"approved_by={approved_by}；revision={revision}；proposal_sha256={proposal_sha}；"
              f"overrides={keys_text}")
    return "| " + " | ".join([_cell(decision_id), ADDENDA_TOPIC, day, _cell(detail)]) + " |"


def _addenda_has_row(addenda_path: Path, decision_id: str, proposal_sha: str) -> bool:
    path = Path(addenda_path)
    if not path.is_file():
        return False
    prefix = f"| {_cell(decision_id)} | {ADDENDA_TOPIC} |"
    return any(line.startswith(prefix) and f"proposal_sha256={proposal_sha}" in line
               for line in path.read_text(encoding="utf-8").splitlines())


def _check_appendable(addenda_path: Path) -> None:
    """改寫門檻檔之前先確認附加列可寫，避免門檻已凍結卻寫不進紀錄。"""
    addenda_path.parent.mkdir(parents=True, exist_ok=True)
    with addenda_path.open("a", encoding="utf-8"):
        pass


def _append_addenda(addenda_path: Path, row: str) -> None:
    text = addenda_path.read_text(encoding="utf-8") if addenda_path.is_file() else ""
    prefix = "" if (not text or text.endswith("\n")) else "\n"
    addenda_path.parent.mkdir(parents=True, exist_ok=True)
    with addenda_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(prefix + row + "\n")


def _freeze_report(*, revision: str, acceptance_sha: str, values: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    deferred = {key: copy.deepcopy(values[key]) for key in DEFERRED_KEYS}
    first = deferred[DEFERRED_KEYS[0]]
    return {
        "thresholds": deferred,
        "revision": revision,
        "acceptance_sha256": acceptance_sha,
        "frozen_at": first["frozen_at"],
        "baseline_evidence": copy.deepcopy(first["baseline_evidence"]),
        "requires_baseline_gate_rerun": True,
    }


def _baseline_evidence(platform: str, *, stage0_index: Path, evidence_root: Path, acceptance_sha: str,
                       proposal: Mapping[str, Any], now: dt.datetime) -> dict[str, str]:
    item_id = BASELINE_ITEMS[platform]
    item, problems = _verify_index_item(stage0_index, item_id, root=evidence_root)
    if problems or item is None:
        raise Od09Error("HARNESS_OD09_BASELINE_INVALID", [f"{item_id}: {p}" for p in problems or ["item_missing"]])

    gate_path = Path(evidence_root) / item["artifact_path"]
    raw = gate_path.read_bytes()
    issues: list[str] = []
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        doc = {}
        issues.append("release-gate.json 無法解析")
    if doc.get("mode") != "baseline":
        issues.append(f"mode={doc.get('mode')!r}，必須為 baseline")
    if doc.get("platform") != platform:
        issues.append(f"platform={doc.get('platform')!r}，必須為 {platform}")
    if doc.get("baseline_result") != "PASS":
        issues.append(f"baseline_result={doc.get('baseline_result')!r}，必須為 PASS")
    if (doc.get("acceptance") or {}).get("sha256") != acceptance_sha:
        issues.append("acceptance.sha256 與現行門檻檔不符（基線須以凍結前同一 revision 判定）")
    ended = _parse_iso((doc.get("run") or {}).get("ended_at_utc"))
    if ended is None:
        issues.append("run.ended_at_utc 缺漏")
    elif ended >= now:
        issues.append("run.ended_at_utc 不早於凍結時點")
    expected_sha = ((proposal.get("baseline_artifacts") or {}).get(platform) or {}).get("release_gate_sha256")
    if expected_sha != _sha256_bytes(raw):
        issues.append("proposal.baseline_artifacts 的 release_gate_sha256 與基線檔不符")
    if issues:
        raise Od09Error("HARNESS_OD09_BASELINE_INVALID", [f"{item_id}: {msg}" for msg in issues])
    return {"item_id": item_id, "artifact_path": item["artifact_path"], "sha256": item["sha256"]}


def freeze(acceptance_path: Path, proposal: dict, *, decision_id: str, approved_by: str, overrides: Mapping[str, Any],
           stage0_index: Path, evidence_root: Path, ledger_path: Path = LEDGER_PATH, addenda_path: Path, now: str) -> dict:
    """一次凍結兩平台延後項。守門失敗皆不改寫門檻檔與 addenda。

    門檻檔已凍結但附加列寫入失敗（中斷）時，以同一份提案與 decision_id 重跑會補寫附加列並回傳同一份報告；
    其他已凍結情況一律 `HARNESS_OD09_TOO_LATE`。
    """
    acceptance_path = Path(acceptance_path)
    addenda_path = Path(addenda_path)
    ledger_path = Path(ledger_path)
    now_dt = _require_iso(now, name="now")
    overrides = dict(overrides or {})
    if not ledger_path.is_file():
        raise ValueError(f"候選長跑登記 ledger 不存在，無法核對時序：{ledger_path}")
    with _ledger_lock(ledger_path):
        return _freeze_locked(
            acceptance_path, proposal, decision_id=decision_id, approved_by=approved_by, overrides=overrides,
            stage0_index=Path(stage0_index), evidence_root=Path(evidence_root), ledger_path=ledger_path,
            addenda_path=addenda_path, now_dt=now_dt,
        )


def _resume_frozen(acceptance_path: Path, content: Mapping[str, Any], proposal: Mapping[str, Any], *,
                   decision_id: str, addenda_path: Path, already: list[str]) -> dict[str, Any]:
    thresholds = content["thresholds"]
    proposal_sha = proposal.get("proposal_sha256")
    items = {key: thresholds[key] for key in DEFERRED_KEYS if isinstance(thresholds.get(key), Mapping)}
    recoverable = (
        len(already) == len(DEFERRED_KEYS)
        and isinstance(proposal_sha, str)
        and _proposal_digest(proposal) == proposal_sha
        and all(items[key].get("decision_id") == decision_id and items[key].get("proposal_sha256") == proposal_sha
                for key in DEFERRED_KEYS)
        and not _addenda_has_row(addenda_path, decision_id, proposal_sha)
    )
    if not recoverable:
        raise Od09Error("HARNESS_OD09_TOO_LATE", [f"{key} 已凍結" for key in already])
    gate.load_acceptance(acceptance_path)
    first = items[DEFERRED_KEYS[0]]
    frozen_day = _require_iso(first["frozen_at"], name="frozen_at").astimezone(dt.timezone.utc).date().isoformat()
    override_keys = {path for key in DEFERRED_KEYS for path in (items[key].get("overrides") or {})}
    _append_addenda(addenda_path, _addenda_row(
        decision_id=decision_id, approved_by=str(first.get("approved_by", "")), day=frozen_day,
        revision=str(content["revision"]), proposal_sha=str(proposal_sha), override_keys=override_keys,
    ))
    return _freeze_report(revision=str(content["revision"]), acceptance_sha=_sha256_file(acceptance_path), values=items)


def _freeze_locked(acceptance_path: Path, proposal: dict, *, decision_id: str, approved_by: str,
                   overrides: dict[str, Any], stage0_index: Path, evidence_root: Path, ledger_path: Path,
                   addenda_path: Path, now_dt: dt.datetime) -> dict[str, Any]:
    raw = acceptance_path.read_bytes()
    content = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(content, dict):
        raise ValueError(f"門檻檔頂層必須是物件：{acceptance_path}")
    thresholds = content.get("thresholds") or {}
    frozen_at = now_dt.astimezone(dt.timezone.utc).isoformat(timespec="seconds")

    # 1. 時序：ledger 有任何非空白行（可解析與否皆算）即拒絕；已凍結者僅能做同一提案的中斷重試
    ledger = _ledger_lines(ledger_path)
    if ledger:
        raise Od09Error("HARNESS_OD09_TOO_LATE", [f"候選長跑登記已有 {len(ledger)} 筆（ledger 非空）"])
    already = [key for key in DEFERRED_KEYS if isinstance(thresholds.get(key), Mapping)
               and thresholds[key].get("status") == "frozen"]
    if already:
        return _resume_frozen(acceptance_path, content, proposal, decision_id=decision_id,
                              addenda_path=addenda_path, already=already)

    # 2. 介面一致性（門檻檔、提案、overrides）
    problems = (
        _interface_problems(thresholds, label="acceptance")
        + _interface_problems(proposal.get("thresholds"), label="proposal")
        + _override_problems(overrides)
    )
    if problems:
        raise Od09Error("HARNESS_OD09_BASELINE_INVALID", problems)

    # 3. 提案完整性與時點（凍結時點不得早於提案產生時間，防止回填）
    if proposal.get("proposal_sha256") != _proposal_digest(proposal):
        raise Od09Error("HARNESS_OD09_BASELINE_INVALID", ["proposal_sha256 與提案內容不符"])
    if proposal.get("acceptance_revision") != content.get("revision"):
        raise Od09Error("HARNESS_OD09_BASELINE_INVALID", [
            f"proposal.acceptance_revision={proposal.get('acceptance_revision')!r} 與現行 revision "
            f"{content.get('revision')!r} 不符"
        ])
    generated = _parse_iso(proposal.get("generated_at"))
    if generated is None:
        raise Od09Error("HARNESS_OD09_BASELINE_INVALID", ["proposal.generated_at 缺漏或不是 ISO 8601"])
    if now_dt < generated:
        raise Od09Error("HARNESS_OD09_BASELINE_INVALID", [
            f"now 早於 proposal.generated_at（{proposal['generated_at']}），凍結時點不得回填"
        ])

    # 4. 基線證據（只讀 Stage 0 證據索引）
    acceptance_sha = _sha256_bytes(raw)
    evidence = {
        platform: _baseline_evidence(platform, stage0_index=stage0_index, evidence_root=evidence_root,
                                     acceptance_sha=acceptance_sha, proposal=proposal, now=now_dt)
        for platform in PLATFORMS
    }

    # 5. 改寫：只碰三個延後項與 revision
    new = copy.deepcopy(content)
    for key in DEFERRED_KEYS:
        values = copy.deepcopy(proposal["thresholds"][key]["per_platform"])
        key_overrides = {path: value for path, value in overrides.items() if path.split(".")[0] == key}
        for path, value in key_overrides.items():
            _, _, platform, field = path.split(".")
            values[platform][field] = copy.deepcopy(value)
        item = new["thresholds"][key]
        item["status"] = "frozen"
        item["frozen_at"] = frozen_at
        item["decision_id"] = decision_id
        item["approved_by"] = approved_by
        item["overrides"] = copy.deepcopy(key_overrides)
        item["proposal_sha256"] = proposal["proposal_sha256"]
        item["baseline_evidence"] = copy.deepcopy(evidence)
        item["per_platform"] = values

    day = now_dt.astimezone(dt.timezone.utc).date().isoformat()
    previous = str(content.get("revision"))
    same_day, _, suffix = previous.rpartition(".")
    number = int(suffix) + 1 if same_day == day and suffix.isdigit() else 1
    new["revision"] = f"{day}.{number}"

    # 6. 附加列內容與可寫性先行確認；暫存檔以 gate 的 schema 驗證，通過才替換正式門檻檔
    row = _addenda_row(
        decision_id=decision_id, approved_by=approved_by, day=day, revision=str(new["revision"]),
        proposal_sha=str(proposal["proposal_sha256"]), override_keys=set(overrides),
    )
    _check_appendable(addenda_path)
    temp = acceptance_path.with_name(acceptance_path.name + ".od09-tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(yaml.safe_dump(new, sort_keys=False, allow_unicode=True))
    try:
        gate.load_acceptance(temp)
    except ValueError:
        temp.unlink(missing_ok=True)
        raise
    os.replace(temp, acceptance_path)
    acceptance_sha_after = _sha256_file(acceptance_path)

    # 7. 附加列（只追加一列）。若此步失敗，門檻檔已凍結；同一提案重跑會依 _resume_frozen 補寫
    _append_addenda(addenda_path, row)
    return _freeze_report(revision=str(new["revision"]), acceptance_sha=acceptance_sha_after,
                          values=new["thresholds"])


def check_frozen_before(acceptance_path: Path, run_started_at: str) -> list[str]:
    """回傳違規清單（空清單即通過）：延後項須已凍結、凍結時點早於 run 起點、欄位完整、schema 有效。"""
    started = _require_iso(run_started_at, name="run_started_at")
    acceptance_path = Path(acceptance_path)
    try:
        content = yaml.safe_load(acceptance_path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        return [f"acceptance schema invalid: 門檻檔無法解析：{exc}"]
    if not isinstance(content, dict):
        return ["acceptance schema invalid: 門檻檔頂層必須是物件"]
    thresholds = content.get("thresholds") or {}

    violations: list[str] = []
    for key in DEFERRED_KEYS:
        item = thresholds.get(key) if isinstance(thresholds.get(key), Mapping) else {}
        if item.get("status") != "frozen":
            violations.append(f"{key} not frozen")
            continue
        frozen_at = _parse_iso(item.get("frozen_at"))
        if frozen_at is None:
            violations.append(f"{key} frozen_at missing")
        elif frozen_at >= started:
            violations.append(f"{key} frozen after run start")
        per = item.get("per_platform") if isinstance(item.get("per_platform"), Mapping) else {}
        for platform in PLATFORMS:
            fields = per.get(platform) if isinstance(per.get(platform), Mapping) else {}
            for field in PER_PLATFORM_FIELDS[key]:
                if fields.get(field) is None:
                    violations.append(f"{key}.per_platform.{platform}.{field} missing")
    try:
        gate.load_acceptance(acceptance_path)
    except ValueError as exc:
        violations.append(f"acceptance schema invalid: {exc}")
    return violations


def register_candidate_start(acceptance_path: Path, *, item_id: str, platform: str, run_id: str, started_at: str,
                             ledger_path: Path = LEDGER_PATH) -> dict:
    """候選長跑開跑前登記。未凍結者拒絕（ledger 不變）；成功則 append 一行。

    查重、凍結檢查與 append 在同一把 ledger 鎖內完成，併發登記不會寫入重複的 run_id。
    """
    if item_id not in CANDIDATE_LONGRUN_ITEMS:
        raise ValueError(f"未知候選長跑項目：{item_id}")
    if CANDIDATE_LONGRUN_ITEMS[item_id] != platform:
        raise ValueError(f"{item_id} 只能登記於 {CANDIDATE_LONGRUN_ITEMS[item_id]}，不得登記於 {platform}")
    _require_iso(started_at, name="started_at")
    ledger_path = Path(ledger_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with _ledger_lock(ledger_path):
        seen: set[str] = set()
        for number, line in enumerate(_ledger_lines(ledger_path), start=1):
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"ledger 第 {number} 行無法解析，拒絕登記：{exc}") from exc
            if isinstance(row, dict):
                seen.add(str(row.get("run_id")))
        if run_id in seen:
            raise ValueError(f"run_id 已登記：{run_id}")

        violations = check_frozen_before(acceptance_path, started_at)
        if violations:
            raise Od09Error("HARNESS_OD09_NOT_FROZEN", violations)

        content, meta = gate.load_acceptance(Path(acceptance_path))
        record = {
            "item_id": item_id,
            "platform": platform,
            "run_id": run_id,
            "started_at": started_at,
            "acceptance_revision": content["revision"],
            "acceptance_sha256": meta["sha256"],
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with ledger_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
    return record


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _stats_from_json(path: Path) -> BaselineStats:
    return BaselineStats(**json.loads(Path(path).read_text(encoding="utf-8")))


def _parse_pairs(values: list[str] | None, *, name: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for value in values or []:
        key, sep, rest = value.partition("=")
        if not sep or not key:
            raise ValueError(f"{name} 格式應為 KEY=VALUE：{value!r}")
        pairs[key] = rest
    return pairs


def _parse_override_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m realhw.acceptance.od09_freeze",
                                     description="OD-09 延後項凍結工具與候選長跑開跑登記（P2-T16）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_stats = sub.add_parser("stats", help="由單一平台基線 case 目錄計算統計並檢查違規（違規不寫出）")
    p_stats.add_argument("--case-dir", required=True, type=Path)
    p_stats.add_argument("--platform", required=True, choices=PLATFORMS)
    p_stats.add_argument("--release-manifest", required=True, type=Path)
    p_stats.add_argument("--out", required=True, type=Path)

    p_propose = sub.add_parser("propose", help="依固定公式由兩平台統計產生提案")
    p_propose.add_argument("--linux-stats", required=True, type=Path, help="stats --out 產生的 JSON 檔")
    p_propose.add_argument("--windows-stats", required=True, type=Path, help="stats --out 產生的 JSON 檔")
    p_propose.add_argument("--baseline-gate", action="append", default=[], metavar="PLATFORM=PATH", required=True)
    p_propose.add_argument("--out", required=True, type=Path)

    p_freeze = sub.add_parser("freeze", help="一次凍結兩平台延後項")
    p_freeze.add_argument("--acceptance", required=True, type=Path)
    p_freeze.add_argument("--proposal", required=True, type=Path)
    p_freeze.add_argument("--decision-id", required=True)
    p_freeze.add_argument("--approved-by", required=True)
    p_freeze.add_argument("--set", action="append", default=[], metavar="PATH=VALUE",
                          help="覆寫單一欄位，例如 resources.per_platform.linux.rss_peak_kb=60000")
    p_freeze.add_argument("--stage0-index", required=True, type=Path)
    p_freeze.add_argument("--evidence-root", required=True, type=Path)
    p_freeze.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    p_freeze.add_argument("--addenda", required=True, type=Path)
    p_freeze.add_argument("--now", default=None, help="凍結時點（ISO 8601，預設現在 UTC）")
    p_freeze.add_argument("--out", required=True, type=Path)

    p_check = sub.add_parser("check", help="檢查延後項是否於 run 起點前凍結且完整")
    p_check.add_argument("--acceptance", required=True, type=Path)
    p_check.add_argument("--run-started-at", required=True)

    p_register = sub.add_parser("register-candidate", help="候選長跑開跑前登記")
    p_register.add_argument("--acceptance", required=True, type=Path)
    p_register.add_argument("--item", required=True)
    p_register.add_argument("--platform", required=True, choices=PLATFORMS)
    p_register.add_argument("--run-id", required=True)
    p_register.add_argument("--started-at", required=True)
    p_register.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    return parser


def _emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "stats":
            stats = baseline_stats(args.case_dir, platform=args.platform, release_manifest=args.release_manifest)
            problems = validate_baseline(stats, acceptance_path=DEFAULT_ACCEPTANCE)
            if problems:
                # 違規統計不寫出，避免後續 propose 將其當成合格基線使用
                _emit({"ok": False, "violations": problems})
                return 1
            _write_compact_json(args.out, asdict(stats))
            _emit({"ok": True, "violations": [], "out": str(args.out)})
            return 0

        if args.command == "propose":
            linux_stats = _stats_from_json(args.linux_stats)
            windows_stats = _stats_from_json(args.windows_stats)
            problems = [f"{label}: {problem}"
                        for label, stats in (("linux", linux_stats), ("windows", windows_stats))
                        for problem in validate_baseline(stats, acceptance_path=DEFAULT_ACCEPTANCE)]
            if problems:
                _emit({"ok": False, "violations": problems})
                return 1
            gates = _parse_pairs(args.baseline_gate, name="--baseline-gate")
            unknown = sorted(set(gates) - set(PLATFORMS))
            if unknown:
                raise ValueError(f"--baseline-gate 含未知平台：{unknown}")
            proposal = propose(linux_stats, windows_stats,
                               baseline_gates={key: Path(value) for key, value in gates.items()})
            _write_compact_json(args.out, proposal)
            _emit({"ok": True, "proposal_sha256": proposal["proposal_sha256"], "out": str(args.out)})
            return 0

        if args.command == "freeze":
            proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
            overrides = {path: _parse_override_value(raw)
                         for path, raw in _parse_pairs(args.set, name="--set").items()}
            now = args.now or _now_iso()
            report = freeze(args.acceptance, proposal, decision_id=args.decision_id, approved_by=args.approved_by,
                            overrides=overrides, stage0_index=args.stage0_index, evidence_root=args.evidence_root,
                            ledger_path=args.ledger, addenda_path=args.addenda, now=now)
            _write_compact_json(args.out, report)
            _emit({"ok": True, "revision": report["revision"], "out": str(args.out)})
            return 0

        if args.command == "check":
            violations = check_frozen_before(args.acceptance, args.run_started_at)
            _emit({"ok": not violations, "violations": violations})
            return 1 if violations else 0

        if args.command == "register-candidate":
            record = register_candidate_start(args.acceptance, item_id=args.item, platform=args.platform,
                                              run_id=args.run_id, started_at=args.started_at, ledger_path=args.ledger)
            _emit({"ok": True, "record": record})
            return 0
    except Od09Error as exc:
        _emit({"ok": False, "reason_code": exc.code, "detail": exc.detail})
        return 5
    except (ValueError, KeyError, TypeError, OSError, yaml.YAMLError) as exc:
        print(f"od09_freeze 參數或輸入錯誤：{exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"未知子命令：{args.command}")


if __name__ == "__main__":
    sys.exit(main())
