"""P9-T12 Canary 帳本測試用的合成紀錄。"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Mapping

import stage3_fakes as fakes

from tools.stage3 import jsonio


BTREE = fakes.BTREE
RTREE = fakes.RTREE
ts = fakes.ts
snapshot = fakes.snapshot
uuid_cmd_id = fakes.uuid_cmd_id
write_policy = fakes.write_policy
_UTC = timezone.utc
_DAY_S = 24 * 60 * 60
_ACTIVE_S = 12 * 60 * 60
_SNAPSHOT_STEP_S = 30


def _with_id(record: dict) -> dict:
    record["record_id"] = jsonio.record_id(record)
    return record


def _snapshot_time(day: int, offset_s: int) -> str:
    start = datetime(2026, 11, day, 6, tzinfo=_UTC)
    return (start + timedelta(seconds=offset_s)).isoformat()


def _cycle_events(cycles: Mapping[str, int], days: int) -> dict[int, list[tuple[str, int]]]:
    """把每種循環平均分配到日內不重疊的快照位置。"""
    events: dict[int, list[tuple[str, int]]] = defaultdict(list)
    slot_by_day = [0] * days
    for kind, count in cycles.items():
        for index in range(count):
            day_index = index % days
            slot = slot_by_day[day_index]
            slot_by_day[day_index] += 1
            events[day_index].append((kind, 100 + slot * 24))
    return events


def synthetic_canary(
    *,
    days: int,
    submits_per_day: int,
    cycles: Mapping[str, int],
    platform: str,
    artifact: str,
    policy_sha256: str,
    start_day: int = 2,
    host: str = "h1",
    pid: int = 100,
    bundle_tree: str = BTREE,
    runtime_tree: str = RTREE,
) -> list[dict]:
    """建立足以覆蓋活躍日、提交、循環與內容歸屬的 Canary 帳本。"""
    if days < 1 or submits_per_day < 0:
        raise ValueError("days 必須為正數，submits_per_day 不得為負數")

    first = _snapshot_time(start_day, 0)
    records = [
        _with_id(
            {
                "type": "policy_ref",
                "host_id": host,
                "platform": platform,
                "policy_sha256": policy_sha256,
                "status": "frozen",
                "frozen_at": "2026-11-01T00:00:00+00:00",
                "decision_ref": "OD-10-C1",
                "collector_revision": "synthetic-v1",
                "t_wall": first,
            }
        )
    ]

    events_by_day = _cycle_events(cycles, days)
    state_at: dict[tuple[int, int], tuple[str, int, dict]] = {}
    human_signal_at: set[tuple[int, int]] = set()
    for day_index, events in events_by_day.items():
        for kind, sample_index in events:
            slot = sample_index
            if kind == "handoff_release_attach":
                for delta, state in ((1, "RELEASED"), (2, "ATTACHING"), (3, "READY")):
                    state_at[(day_index, slot + delta)] = (state, 1, {})
            elif kind == "hotplug_reattach":
                state_at[(day_index, slot + 1)] = ("DETACHED", 1, {})
                state_at[(day_index, slot + 2)] = ("READY", 2, {})
            elif kind == "dut_reboot":
                state_at[(day_index, slot + 1)] = (
                    "READY",
                    1,
                    {"boot_quiet_remaining_s": 12, "ready_reconfirm_pending": True},
                )
                state_at[(day_index, slot + 2)] = ("READY", 1, {})
            elif kind == "recovery":
                state_at[(day_index, slot + 1)] = ("RECOVERING", 1, {})
                state_at[(day_index, slot + 2)] = ("READY", 1, {})
            elif kind == "human_agent_coexist":
                human_signal_at.add((day_index, sample_index * _SNAPSHOT_STEP_S))

    next_seq = 1
    for day_index in range(days):
        day = start_day + day_index
        offsets = list(range(0, _ACTIVE_S + _SNAPSHOT_STEP_S, _SNAPSHOT_STEP_S))
        # Canary 使用時段外仍保留低頻健康快照，避免合成帳本被解讀成 collector 缺口。
        if day_index + 1 < days:
            offsets.extend(range(_ACTIVE_S + 15 * 60, _DAY_S, 15 * 60))
        for offset_s in offsets:
            state, generation, extra = state_at.get((day_index, offset_s // _SNAPSHOT_STEP_S), ("READY", 1, {}))
            records.append(
                fakes.snapshot(
                    _snapshot_time(day, offset_s),
                    host=host,
                    platform=platform,
                    pid=pid,
                    artifact=artifact,
                    sessions=(("COM0", state, generation, extra),),
                    bundle_tree=bundle_tree,
                    runtime_tree=runtime_tree,
                )
            )

        for submit_index in range(submits_per_day):
            offset_s = submit_index * _ACTIVE_S // submits_per_day if submits_per_day else 0
            wall = _snapshot_time(day, offset_s)
            cmd_id = fakes.uuid_cmd_id(day_index * max(1, submits_per_day) + submit_index + 1)
            records.append(
                _with_id(
                    {
                        "type": "submit",
                        "host_id": host,
                        "platform": platform,
                        "cmd_id": cmd_id,
                        "com": "COM0",
                        "source": "agent:synthetic",
                        "wall_ts": wall,
                        "seq": next_seq,
                        "wal_file": "raw.wal.ndjson",
                    }
                )
            )
            records.append(
                _with_id(
                    {
                        "type": "submit_result",
                        "host_id": host,
                        "platform": platform,
                        "cmd_id": cmd_id,
                        "status": "done",
                        "error_code": None,
                        "t_wall": (datetime.fromisoformat(wall) + timedelta(seconds=1)).isoformat(),
                    }
                )
            )
            next_seq += 1

        for signal_day, offset_s in sorted(human_signal_at):
            if signal_day != day_index:
                continue
            wall = _snapshot_time(day, offset_s)
            records.append(
                _with_id(
                    {
                        "type": "wal_signal",
                        "host_id": host,
                        "platform": platform,
                        "kind": "human_tx",
                        "com": "COM0",
                        "wall_ts": wall,
                        "seq": next_seq,
                        "source": "human:synthetic",
                        "detail": {},
                    }
                )
            )
            next_seq += 1

    def record_time(record: dict) -> datetime:
        field = "wall_ts" if record["type"] in {"submit", "wal_signal"} else "t_wall"
        return jsonio.parse_iso(str(record.get(field, first)))

    # 相同時間先放 policy_ref 和快照，再放提交及其結果，保持 host 帳本可直接重播。
    priority = {"policy_ref": 0, "snapshot": 1, "submit": 2, "wal_signal": 3, "submit_result": 4}
    records.sort(key=lambda row: (record_time(row), priority.get(row["type"], 5), row["record_id"]))
    return records
