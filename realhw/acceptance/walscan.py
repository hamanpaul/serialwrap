"""獨立 WAL 長跑掃描器（stdlib-only）。

不 import 產品碼與 `uart_stdio_bridge`；只讀 `raw.wal.ndjson` 與其歸檔，逐筆驗 crc32／len，
並以 TX payload 串流計算 agent 重送與 human tick 的重複出現。上下文判定（禁止窗口、
世代失效）只在 `SafetyContext` 存在時進行；缺上下文時回報 `context_missing`，由 gate 判證據不足。
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MARKER_PATTERNS = {
    "agent_line": rb"LR_rhw\d+_\d+_\d{4}",
    "agent_bg": rb"BG_rhw\d+_\d+",
    "human": rb"HUMAN_TICK_\d+",
}
_FORBIDDEN_STATES = frozenset({"RELEASED", "FLASHING", "DETACHED"})
_CURRENT_NAME = "raw.wal.ndjson"


@dataclass(frozen=True)
class SafetyContext:
    """上下文判定資料：由 observer 與 events 推導，不由 WAL 自行猜測。"""

    forbidden_windows: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    human_ticks: list[dict[str, Any]] = field(default_factory=list)
    generation_ends: list[str] = field(default_factory=list)


def _parse_wall(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _all_have_wall(rows: list[dict[str, Any]]) -> bool:
    return all(_parse_wall(row.get("wall_ts")) is not None for row in rows)


def build_safety_context(events: list[dict[str, Any]], observer: list[dict[str, Any]] | None) -> SafetyContext | None:
    """由 events 與 observer 建立上下文；任一缺 wall_ts 回 None（呼叫端判 INSUFFICIENT_EVIDENCE）。"""
    if observer is None:
        return None
    samples = [row for row in observer if row.get("kind") == "sample"]
    if not samples or not _all_have_wall(samples) or not _all_have_wall(events):
        return None

    windows: dict[str, list[tuple[str, str]]] = {}
    ordered = sorted(samples, key=lambda row: _parse_wall(row["wall_ts"]))
    coms = sorted({com for row in ordered for com in (row.get("sessions") or {})})
    for com in coms:
        spans: list[tuple[str, str]] = []
        for left, right in zip(ordered, ordered[1:]):
            left_state = (left.get("sessions") or {}).get(com)
            right_state = (right.get("sessions") or {}).get(com)
            if left_state in _FORBIDDEN_STATES and right_state in _FORBIDDEN_STATES:
                spans.append((left["wall_ts"], right["wall_ts"]))
        if spans:
            windows[com] = spans

    human_ticks = [
        {"n": event.get("n"), "wall_ts": event["wall_ts"], "generation": event.get("generation")}
        for event in events
        if event.get("kind") == "tick"
    ]
    generation_ends = [event["wall_ts"] for event in events if event.get("kind") in {"minicom_dead", "minicom_open"}]
    return SafetyContext(forbidden_windows=windows, human_ticks=human_ticks, generation_ends=generation_ends)


def _wal_files(wal_dir: Path) -> list[Path]:
    archives = sorted(path for path in wal_dir.glob(f"{_CURRENT_NAME}.*") if path.is_file())
    current = wal_dir / _CURRENT_NAME
    files = list(archives)
    if current.is_file():
        files.append(current)
    return files


def _load_records(path: Path, problems: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # 截斷的尾端或損壞位元組：以替代字元續讀並記為 problem（證據不足，不得中止掃描）
        problems.append(f"{path.name}:utf8_decode")
        text = raw.decode("utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"{path.name}:{number}:json_decode")
            continue
        if not isinstance(row, dict):
            problems.append(f"{path.name}:{number}:not_object")
            continue
        rows.append(row)
    return rows


def _payload_problem(row: dict[str, Any]) -> str | None:
    try:
        payload = base64.b64decode(row["payload_b64"], validate=True)
    except (KeyError, binascii.Error, TypeError, ValueError):
        return "payload_b64"
    if row.get("len") != len(payload):
        return "len"
    if f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}" != row.get("crc32"):
        return "crc32"
    return None


def scan_wal(wal_dir: Path, *, since_wall_ts: str, allowed_tx_source_regex: str,
             context: SafetyContext | None = None, tolerance_s: float = 2.0) -> dict[str, Any]:
    """掃描 WAL 目錄；回傳 gate 可直接讀取的摘要（見模組說明）。"""
    wal_dir = Path(wal_dir)
    problems: list[str] = []
    files = _wal_files(wal_dir)
    if not files:
        problems.append("no_wal_files")
    since = _parse_wall(since_wall_ts)
    if since is None:
        problems.append("since_wall_ts_invalid")
    allowed = re.compile(allowed_tx_source_regex)
    tolerance = float(tolerance_s)

    scoped: list[dict[str, Any]] = []
    for path in files:
        for row in _load_records(path, problems):
            wall = _parse_wall(row.get("wall_ts"))
            if wall is None:
                problems.append(f"{path.name}:seq={row.get('seq')}:wall_ts")
                continue
            if since is not None and wall < since:
                continue
            scoped.append(row)
    if not scoped:
        # 有檔案但本 run 區間內沒有任何有效紀錄：不得視為「沒有違規」
        problems.append("no_run_records")

    loss_flags: list[Any] = []
    rotation_failed: list[Any] = []
    unauthorized: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    agent_streams: dict[str, bytearray] = {}
    human_stream = bytearray()
    human_map: list[tuple[int, int, datetime, Any]] = []
    previous_seq: int | None = None

    for row in scoped:
        seq = row.get("seq")
        # seq 連續性：WAL 單一寫入者逐筆遞增；缺號（含未寫入即 loss 的筆）、重號與倒退皆為證據斷裂
        if not isinstance(seq, int) or isinstance(seq, bool):
            problems.append(f"seq_invalid:{seq!r}")
        else:
            if previous_seq is not None and seq != previous_seq + 1:
                problems.append(f"seq_discontinuity:{previous_seq}->{seq}")
            previous_seq = seq
        problem = _payload_problem(row)
        if problem is not None:
            problems.append(f"seq={seq}:{problem}")
        if row.get("loss_flag"):
            loss_flags.append(seq)
        # rotation_failed 為頂層旗標（contracts/wal_record.schema.json；sw_core/wal.py 同）
        if row.get("rotation_failed") is True:
            rotation_failed.append(seq)
        if row.get("dir") == "META":
            meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
            if meta.get("event") == "tx_outcome_unknown":
                # BC-03：寫入結果不明，未確認的尾端可能已送出；不得據此判無違規
                uncertain.append({"seq": seq, "marker": None, "reason": "tx_outcome_unknown", "kind": "tx_unknown"})
            continue
        if row.get("dir") != "TX" or problem is not None:
            continue
        source = str(row.get("source") or "")
        wall = _parse_wall(row.get("wall_ts"))
        payload = base64.b64decode(row["payload_b64"])
        # 每一筆有效 TX 都先檢查來源白名單（含裸 `agent`、`mcu`、`flash`、未知 system 子類），
        # 不得只檢查 `agent:` 前綴；非授權來源不得讓安全檢查 PASS
        source_allowed = allowed.fullmatch(source) is not None
        if not source_allowed:
            unauthorized.append({"seq": seq, "source": source, "reason": "source_not_allowed"})
        if source.startswith("human:"):
            start = len(human_stream)
            human_stream.extend(payload)
            human_map.append((start, len(human_stream), wall, seq))
            continue
        if source == "agent" or source.startswith("agent:"):
            agent_streams.setdefault(source, bytearray()).extend(payload)
        # 禁止窗口只對「來源合法的 agent TX」判定（合法來源在禁止期間送出）
        if not source_allowed or context is None or not source.startswith("agent:"):
            continue
        for window_start, window_end in context.forbidden_windows.get(str(row.get("com")), []):
            start_w = _parse_wall(window_start)
            end_w = _parse_wall(window_end)
            if start_w is None or end_w is None:
                continue
            if start_w <= wall <= end_w:
                unauthorized.append({"seq": seq, "source": source, "reason": "forbidden_window"})
                break
            edge = min(abs((wall - start_w).total_seconds()), abs((wall - end_w).total_seconds()))
            if edge <= tolerance:
                uncertain.append({"seq": seq, "marker": None, "reason": "window_boundary", "kind": "unauthorized"})
                break

    dangerous: dict[str, int] = {}
    for stream in agent_streams.values():
        for pattern in (MARKER_PATTERNS["agent_line"], MARKER_PATTERNS["agent_bg"]):
            for match in re.finditer(pattern, bytes(stream)):
                marker = match.group().decode("ascii")
                dangerous[marker] = dangerous.get(marker, 0) + 1
    dangerous_auto_resend = [{"marker": marker, "count": count}
                             for marker, count in sorted(dangerous.items()) if count > 1]

    human_counts: dict[str, int] = {}
    first_hit: dict[str, tuple[datetime, Any]] = {}
    for match in re.finditer(MARKER_PATTERNS["human"], bytes(human_stream)):
        marker = match.group().decode("ascii")
        human_counts[marker] = human_counts.get(marker, 0) + 1
        if marker in first_hit:
            continue
        for start, end, wall, seq in human_map:
            if start <= match.start() < end:
                first_hit[marker] = (wall, seq)
                break

    cross_context_replay: list[dict[str, Any]] = []
    for marker in sorted(human_counts):
        if human_counts[marker] > 1:
            cross_context_replay.append({"marker": marker, "count": human_counts[marker]})

    ends = sorted(parsed for parsed in (_parse_wall(value) for value in (context.generation_ends if context else []))
                  if parsed is not None)
    for tick in (context.human_ticks if context else []):
        marker = f"HUMAN_TICK_{tick.get('n')}"
        if marker not in first_hit:
            continue
        tick_wall = _parse_wall(tick.get("wall_ts"))
        hit_wall, hit_seq = first_hit[marker]
        if tick_wall is None:
            continue
        following = [end for end in ends if end > tick_wall]
        if not following:
            continue
        end = following[0]
        delta = (hit_wall - end).total_seconds()
        if delta > tolerance:
            cross_context_replay.append({"marker": marker, "count": human_counts[marker],
                                         "reason": "stale_generation"})
        elif abs(delta) <= tolerance:
            uncertain.append({"seq": hit_seq, "marker": marker, "reason": "generation_end_boundary",
                              "kind": "cross_context"})

    return {
        "files": [path.name for path in files],
        "records": len(scoped),
        "problems": problems,
        "unauthorized_tx": unauthorized,
        "loss_flags": loss_flags,
        "rotation_failed": rotation_failed,
        "dangerous_auto_resend": dangerous_auto_resend,
        "cross_context_replay": cross_context_replay,
        "uncertain": uncertain,
        "context_missing": context is None,
    }
