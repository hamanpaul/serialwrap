"""Canary 干擾／交接週期分類與活躍日判定（stdlib only）。"""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, NamedTuple, Sequence

from tools.stage3 import canary_policy, jsonio


# 與 Task 9 的終態集合一致；unknown 不得作為已完成提交或恢復證據。
TERMINAL_RESULTS: frozenset[str] = frozenset({"done", "error", "timeout"})


@dataclass(frozen=True)
class Episode:
    """由穩態 session 離開後再回穩，或在觀測邊界被切斷的一段紀錄。"""

    host_id: str
    com: str
    daemon_pid: int | None
    states: tuple[str, ...]
    start_wall: str
    end_wall: str
    before_generation: int | None
    after_generation: int | None
    boot_signal: bool
    ended_steady: bool


@dataclass(frozen=True)
class Cycle:
    """可供 Canary 帳本配額核對的單一干擾／交接週期。"""

    type: str
    host_id: str
    platform: str
    com: str | None
    start_wall: str
    end_wall: str
    artifact_id: str | None
    bundle_tree_sha256: str | None
    runtime_tree_sha256: str | None
    result: str
    evidence: tuple[str, ...]


class Attribution(NamedTuple):
    """同時比對 manifest 與部署樹內容的受測物歸屬鍵。"""

    artifact_id: str
    bundle_tree_sha256: str
    runtime_tree_sha256: str


def _time_key(record: Mapping[str, Any], field: str) -> tuple[datetime, str]:
    return jsonio.parse_iso(str(record[field])), str(record.get("record_id", ""))


def _snapshot_time(snapshot: Mapping[str, Any]) -> datetime:
    return jsonio.parse_iso(str(snapshot["t_wall"]))


def _session(snapshot: Mapping[str, Any], com: str) -> Mapping[str, Any] | None:
    sessions = snapshot.get("sessions")
    if not isinstance(sessions, Sequence) or isinstance(sessions, (str, bytes)):
        return None
    for session in sessions:
        if isinstance(session, Mapping) and session.get("com") == com:
            return session
    return None


def _generation(session: Mapping[str, Any] | None) -> int | None:
    value = session.get("bridge_generation") if session is not None else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def steady(sess: Mapping[str, Any]) -> bool:
    """依 Canary 契約判斷 READY 或明確可用的 passthrough ATTACHED 是否穩定。"""
    state = sess.get("state")
    if state == "READY":
        quiet = sess.get("boot_quiet_remaining_s")
        quiet_done = quiet is None or (isinstance(quiet, (int, float)) and quiet <= 0)
        return (
            quiet_done
            and sess.get("ready_reconfirm_pending") is not True
            and sess.get("ready_reconfirm_failed") is not True
        )
    if state == "ATTACHED":
        return sess.get("command_capable") is False or sess.get("platform") == "passthrough"
    return False


def _boot_signal(session: Mapping[str, Any]) -> bool:
    if session.get("state") not in {"READY", "ATTACHED"}:
        return False
    quiet = session.get("boot_quiet_remaining_s")
    return (
        isinstance(quiet, (int, float))
        and quiet > 0
        or session.get("ready_reconfirm_pending") is True
        or session.get("ready_reconfirm_failed") is True
    )


def _make_episode(
    host_id: str,
    com: str,
    before: tuple[Mapping[str, Any], Mapping[str, Any]],
    active: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    end: tuple[Mapping[str, Any], Mapping[str, Any]] | None,
) -> Episode:
    first_snapshot, _ = active[0]
    last_snapshot, _ = active[-1]
    end_snapshot, end_session = end if end is not None else (last_snapshot, None)
    involved = [session for _, session in active]
    if end is not None:
        involved.append(end_session)
    return Episode(
        host_id=host_id,
        com=com,
        daemon_pid=before[0].get("daemon_pid"),
        states=tuple(str(session.get("state", "")) for session in involved[:-1] if session is not None)
        if end is not None
        else tuple(str(session.get("state", "")) for session in involved if session is not None),
        start_wall=str(first_snapshot["t_wall"]),
        end_wall=str(end_snapshot["t_wall"]),
        before_generation=_generation(before[1]),
        after_generation=_generation(end_session) if end is not None else None,
        boot_signal=any(_boot_signal(session) for session in involved if session is not None),
        ended_steady=end is not None,
    )


def episodes(snapshots: Sequence[dict], policy: canary_policy.CanaryPolicy) -> list[Episode]:
    """依 host／COM 切出穩態離開週期；缺口與 daemon 換代只保留未完成紀錄。"""
    by_host: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for snapshot in snapshots:
        if snapshot.get("daemon_ok") is True:
            by_host[str(snapshot.get("host_id", ""))].append(snapshot)

    max_gap = policy.collector_interval_s * policy.episode_max_gap_factor
    found: list[Episode] = []
    for host_id in sorted(by_host):
        host_rows = sorted(by_host[host_id], key=lambda row: _time_key(row, "t_wall"))
        coms = sorted(
            {
                str(session["com"])
                for snapshot in host_rows
                for session in snapshot.get("sessions", [])
                if isinstance(session, Mapping) and session.get("com") is not None
            }
        )
        for com in coms:
            rows = [(row, _session(row, com)) for row in host_rows]
            previous: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None
            active: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
            active_before: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None
            for current in rows:
                current_snapshot, current_session = current
                if current_session is None:
                    if active:
                        assert active_before is not None
                        found.append(_make_episode(host_id, com, active_before, active, None))
                        active = []
                        active_before = None
                    previous = None
                    continue
                current_time = jsonio.parse_iso(str(current_snapshot["t_wall"]))
                if previous is not None:
                    previous_snapshot, previous_session = previous
                    previous_time = jsonio.parse_iso(str(previous_snapshot["t_wall"]))
                    gap = (current_time - previous_time).total_seconds()
                    same_daemon = previous_snapshot.get("daemon_pid") == current_snapshot.get("daemon_pid")
                else:
                    gap = 0
                    same_daemon = False

                if active and (gap > max_gap or not same_daemon):
                    assert active_before is not None
                    found.append(_make_episode(host_id, com, active_before, active, None))
                    active = []
                    active_before = None
                    previous = current if steady(current_session) else None
                    continue

                if active:
                    if steady(current_session):
                        assert active_before is not None
                        found.append(_make_episode(host_id, com, active_before, active, current))
                        active = []
                        active_before = None
                        previous = current
                    else:
                        active.append(current)
                        previous = current
                    continue

                if previous is None:
                    previous = current if steady(current_session) else None
                    continue

                previous_snapshot, previous_session = previous
                if gap > max_gap or not same_daemon:
                    previous = current if steady(current_session) else None
                elif steady(previous_session) and not steady(current_session):
                    active_before = previous
                    active = [current]
                    previous = current
                elif steady(current_session):
                    previous = current
                else:
                    # 未由穩態開始的非穩態片段不能構成可分類週期。
                    previous = None

            if active:
                assert active_before is not None
                found.append(_make_episode(host_id, com, active_before, active, None))

    return sorted(found, key=lambda ep: (jsonio.parse_iso(ep.start_wall), ep.host_id, ep.com, ep.end_wall))


def classify_episode(ep: Episode, platform: str, recover_tx_seen: bool) -> str | None:
    """依計畫固定優先序把 episode 對應到 Canary 循環類型。"""
    states = set(ep.states)
    if "RELEASED" in states:
        return "handoff_release_attach"
    if "FLASHING" in states:
        return "flash" if platform == "linux" else None
    if (
        "DETACHED" in states
        and ep.before_generation is not None
        and ep.after_generation is not None
        and ep.after_generation > ep.before_generation
    ):
        return "hotplug_reattach"
    if ep.boot_signal:
        return "dut_reboot"
    if "RECOVERING" in states or recover_tx_seen:
        return "recovery"
    return None


def _attribution_fields(snapshot: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return (
        snapshot.get("artifact_id"),
        snapshot.get("bundle_tree_sha256"),
        snapshot.get("runtime_tree_sha256"),
    )


def attribute_artifact(ts: str, host_snapshots_sorted: Sequence[dict]) -> Attribution | None:
    """在已排序快照中以對數時間找鄰居，核對事件時間兩側的部署內容與 daemon 身分。"""
    when = jsonio.parse_iso(ts)
    before_index = bisect_right(host_snapshots_sorted, when, key=_snapshot_time) - 1
    after_index = bisect_left(host_snapshots_sorted, when, key=_snapshot_time)
    if before_index < 0 or after_index == len(host_snapshots_sorted):
        return None
    before, after = host_snapshots_sorted[before_index], host_snapshots_sorted[after_index]
    if before.get("daemon_ok") is not True or after.get("daemon_ok") is not True:
        return None
    if before.get("artifact_check") != "verified" or after.get("artifact_check") != "verified":
        return None
    before_fields = _attribution_fields(before)
    after_fields = _attribution_fields(after)
    artifact_id, bundle_tree, runtime_tree = before_fields
    if (
        not isinstance(artifact_id, str)
        or not artifact_id
        or before_fields != after_fields
        or bundle_tree is None
        or runtime_tree is None
        or before.get("daemon_pid") != after.get("daemon_pid")
    ):
        return None
    return Attribution(artifact_id, str(bundle_tree), str(runtime_tree))


def _cycle(
    cycle_type: str,
    host_id: str,
    platform: str,
    com: str | None,
    start_wall: str,
    end_wall: str,
    snapshots_by_host: Mapping[str, Sequence[dict]],
    result: str,
    evidence: Sequence[str],
) -> Cycle:
    attribution = attribute_artifact(start_wall, snapshots_by_host.get(host_id, ()))
    return Cycle(
        type=cycle_type,
        host_id=host_id,
        platform=platform,
        com=com,
        start_wall=start_wall,
        end_wall=end_wall,
        artifact_id=attribution.artifact_id if attribution else None,
        bundle_tree_sha256=attribution.bundle_tree_sha256 if attribution else None,
        runtime_tree_sha256=attribution.runtime_tree_sha256 if attribution else None,
        result=result,
        evidence=tuple(dict.fromkeys(evidence)),
    )


def _snapshot_evidence(
    host_id: str,
    com: str,
    start_wall: str,
    end_wall: str,
    snapshots_by_host: Mapping[str, Sequence[dict]],
    padding_s: int,
) -> list[str]:
    start = jsonio.parse_iso(start_wall) - timedelta(seconds=padding_s)
    end = jsonio.parse_iso(end_wall)
    refs = []
    rows = snapshots_by_host.get(host_id, ())
    first = bisect_left(rows, start, key=_snapshot_time)
    last = bisect_right(rows, end, key=_snapshot_time)
    for snapshot in rows[first:last]:
        if snapshot.get("daemon_ok") is True and _session(snapshot, com) is not None:
            refs.append(f"snapshot:{snapshot.get('record_id', '')}")
    return refs


def _recovery_confirmation(
    rows: Sequence[dict], com: str, start_wall: str, end_wall: str, max_gap_s: int
) -> Mapping[str, Any] | None:
    """只在同一 daemon 持續可觀測且目標 COM 回穩時確認快速 recovery。"""
    start = jsonio.parse_iso(start_wall)
    end = jsonio.parse_iso(end_wall)
    anchor_index = bisect_left(rows, start, key=_snapshot_time) - 1
    if anchor_index < 0:
        return None
    anchor = rows[anchor_index]
    anchor_time = _snapshot_time(anchor)
    daemon_pid = anchor.get("daemon_pid")
    if (
        anchor.get("daemon_ok") is not True
        or not isinstance(daemon_pid, int)
        or isinstance(daemon_pid, bool)
        or (start - anchor_time).total_seconds() > max_gap_s
        or _session(anchor, com) is None
    ):
        return None

    deadline = end + timedelta(seconds=max_gap_s)
    last_index = bisect_right(rows, deadline, key=_snapshot_time)
    previous_time = anchor_time
    for snapshot in rows[anchor_index + 1:last_index]:
        at = _snapshot_time(snapshot)
        gap = (at - previous_time).total_seconds()
        if gap < 0 or gap > max_gap_s:
            return None
        if snapshot.get("daemon_ok") is not True or snapshot.get("daemon_pid") != daemon_pid:
            return None
        session = _session(snapshot, com)
        if session is None:
            return None
        previous_time = at
        if at > end and steady(session):
            return snapshot
    return None


def _restart_confirmation(
    rows: Sequence[dict], current: Mapping[str, Any], max_gap_s: int
) -> Mapping[str, Any] | None:
    """確認重啟後同一 daemon 世代至少有一個穩態 session。"""
    daemon_pid = current.get("daemon_pid")
    if not isinstance(daemon_pid, int) or isinstance(daemon_pid, bool):
        return None
    start = _snapshot_time(current)
    first = bisect_left(rows, start, key=_snapshot_time)
    while first < len(rows) and _snapshot_time(rows[first]) == start and rows[first] is not current:
        first += 1
    if first == len(rows) or rows[first] is not current:
        return None
    deadline = start + timedelta(seconds=max_gap_s)
    last = bisect_right(rows, deadline, key=_snapshot_time)
    previous_time: datetime | None = None
    for snapshot in rows[first:last]:
        at = _snapshot_time(snapshot)
        if previous_time is not None and (at - previous_time).total_seconds() > max_gap_s:
            return None
        if snapshot.get("daemon_ok") is not True or snapshot.get("daemon_pid") != daemon_pid:
            return None
        sessions = snapshot.get("sessions")
        if isinstance(sessions, Sequence) and not isinstance(sessions, (str, bytes)):
            if any(isinstance(session, Mapping) and steady(session) for session in sessions):
                return snapshot
        previous_time = at
    return None


def _signal_time(signal: Mapping[str, Any]) -> datetime:
    return jsonio.parse_iso(str(signal.get("wall_ts", signal.get("t_wall", ""))))


def _signal_evidence(signal: Mapping[str, Any]) -> str:
    return f"wal:{signal.get('seq')}"


def _countable_submit(submit: Mapping[str, Any], policy: canary_policy.CanaryPolicy) -> bool:
    cmd_id = submit.get("cmd_id")
    source = submit.get("source")
    if not isinstance(cmd_id, str) or re.fullmatch(policy.cmd_id_regex, cmd_id) is None:
        return False
    if not isinstance(source, str) or source.startswith(policy.exclude_source_prefixes):
        return False
    return True


def _unique_submits(
    submits: Sequence[dict], policy: canary_policy.CanaryPolicy, platform: str
) -> list[Mapping[str, Any]]:
    """以政策的 host_id／cmd_id 鍵跨 rotate 去重，保留最早有效提交。"""
    candidates = [
        submit
        for submit in submits
        if submit.get("platform", platform) == platform and _countable_submit(submit, policy)
    ]
    candidates.sort(
        key=lambda submit: (
            _time_key(submit, "wall_ts"),
            str(submit.get("host_id", "")),
            str(submit["cmd_id"]),
        )
    )
    unique: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for submit in candidates:
        key = (str(submit.get("host_id", "")), str(submit["cmd_id"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(submit)
    return unique


def classify(
    snapshots: Sequence[dict],
    submits: Sequence[dict],
    signals: Sequence[dict],
    notes: Sequence[dict],
    policy: canary_policy.CanaryPolicy,
    *,
    platform: str,
) -> list[Cycle]:
    """分類 Canary 帳本中的狀態週期、快速 recovery、人機共用與計畫重啟。"""
    scoped_snapshots = [row for row in snapshots if row.get("platform", platform) == platform]
    snapshots_by_host: dict[str, list[dict]] = defaultdict(list)
    for snapshot in scoped_snapshots:
        snapshots_by_host[str(snapshot.get("host_id", ""))].append(snapshot)
    for rows in snapshots_by_host.values():
        rows.sort(key=lambda row: _time_key(row, "t_wall"))

    cycles: list[Cycle] = []
    all_episodes = episodes(scoped_snapshots, policy)
    recover_signals = [
        (index, signal)
        for index, signal in enumerate(signals)
        if signal.get("kind") == "recover_tx" and signal.get("platform", platform) == platform
    ]
    covered_recoveries: set[int] = set()
    for episode in all_episodes:
        start = jsonio.parse_iso(episode.start_wall) - timedelta(seconds=policy.collector_interval_s)
        end = jsonio.parse_iso(episode.end_wall)
        matching = []
        for index, signal in recover_signals:
            if index in covered_recoveries:
                continue
            if signal.get("host_id") != episode.host_id or signal.get("com") != episode.com:
                continue
            at = _signal_time(signal)
            if start <= at <= end:
                matching.append((index, signal))
                covered_recoveries.add(index)
        cycle_type = classify_episode(episode, platform, bool(matching))
        if cycle_type is None:
            continue
        evidence = _snapshot_evidence(
            episode.host_id,
            episode.com,
            episode.start_wall,
            episode.end_wall,
            snapshots_by_host,
            policy.collector_interval_s,
        )
        evidence.extend(_signal_evidence(signal) for _, signal in matching)
        cycles.append(
            _cycle(
                cycle_type,
                episode.host_id,
                platform,
                episode.com,
                episode.start_wall,
                episode.end_wall,
                snapshots_by_host,
                "recovered_ready" if episode.ended_steady else "ended_non_ready",
                evidence,
            )
        )

    # 沒有被 snapshot episode 覆蓋的 recovery TX 依時間近鄰合併，避免快速恢復漏算。
    remaining: dict[tuple[str, str], list[tuple[int, Mapping[str, Any]]]] = defaultdict(list)
    for index, signal in recover_signals:
        host_id = signal.get("host_id")
        com = signal.get("com")
        if (
            index not in covered_recoveries
            and isinstance(host_id, str)
            and isinstance(com, str)
            and host_id
            and com
        ):
            remaining[(host_id, com)].append((index, signal))
    for (host_id, com), rows in sorted(remaining.items()):
        ordered = sorted(rows, key=lambda pair: _time_key(pair[1], "wall_ts" if "wall_ts" in pair[1] else "t_wall"))
        cluster: list[tuple[int, Mapping[str, Any]]] = []
        for current in ordered:
            if (
                cluster
                and (_signal_time(current[1]) - _signal_time(cluster[-1][1])).total_seconds()
                > policy.recover_cluster_s
            ):
                first, last = cluster[0][1], cluster[-1][1]
                first_wall = str(first.get("wall_ts", first.get("t_wall")))
                last_wall = str(last.get("wall_ts", last.get("t_wall")))
                confirmed = _recovery_confirmation(
                    snapshots_by_host.get(host_id, ()),
                    com,
                    first_wall,
                    last_wall,
                    policy.collector_interval_s * policy.episode_max_gap_factor,
                )
                evidence = [_signal_evidence(signal) for _, signal in cluster]
                if confirmed is not None:
                    evidence.append(f"snapshot:{confirmed.get('record_id', '')}")
                cycles.append(
                    _cycle(
                        "recovery", host_id, platform, com, first_wall, last_wall, snapshots_by_host,
                        "recovered_ready" if confirmed is not None else "ended_non_ready", evidence,
                    )
                )
                cluster = []
            cluster.append(current)
        if cluster:
            first, last = cluster[0][1], cluster[-1][1]
            first_wall = str(first.get("wall_ts", first.get("t_wall")))
            last_wall = str(last.get("wall_ts", last.get("t_wall")))
            confirmed = _recovery_confirmation(
                snapshots_by_host.get(host_id, ()),
                com,
                first_wall,
                last_wall,
                policy.collector_interval_s * policy.episode_max_gap_factor,
            )
            evidence = [_signal_evidence(signal) for _, signal in cluster]
            if confirmed is not None:
                evidence.append(f"snapshot:{confirmed.get('record_id', '')}")
            cycles.append(
                _cycle(
                    "recovery", host_id, platform, com, first_wall, last_wall, snapshots_by_host,
                    "recovered_ready" if confirmed is not None else "ended_non_ready", evidence,
                )
            )

    # 同一 epoch 桶同時含 human TX 與 agent submit 時，至多建立一筆共存循環。
    quota = policy.platforms[platform]
    submit_by_bucket: dict[tuple[str, str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for submit in _unique_submits(submits, policy, platform):
        com = submit.get("com")
        if not isinstance(com, str) or not com:
            continue
        wall = str(submit.get("wall_ts", ""))
        epoch = jsonio.parse_iso(wall).timestamp()
        key = (str(submit.get("host_id", "")), com, math.floor(epoch / policy.coexist_bucket_s))
        submit_by_bucket[key].append(submit)
    human_by_bucket: dict[tuple[str, str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for signal in signals:
        if signal.get("kind") != "human_tx" or signal.get("platform", platform) != platform:
            continue
        com = signal.get("com")
        if not isinstance(com, str) or not com:
            continue
        wall = str(signal.get("wall_ts", ""))
        epoch = jsonio.parse_iso(wall).timestamp()
        key = (str(signal.get("host_id", "")), com, math.floor(epoch / policy.coexist_bucket_s))
        human_by_bucket[key].append(signal)
    coexist_count: dict[tuple[str, str, str], int] = defaultdict(int)
    for key in sorted(submit_by_bucket.keys() & human_by_bucket.keys()):
        host_id, com, _ = key
        events = [*submit_by_bucket[key], *human_by_bucket[key]]
        events.sort(key=lambda event: _time_key(event, "wall_ts"))
        start_wall = str(events[0]["wall_ts"])
        end_wall = str(events[-1]["wall_ts"])
        local_day = canary_policy.local_date(quota, start_wall)
        daily_key = (host_id, com, local_day)
        if coexist_count[daily_key] >= policy.coexist_cap_per_day:
            continue
        coexist_count[daily_key] += 1
        evidence = [f"wal:{event.get('seq')}" for event in events]
        cycles.append(
            _cycle(
                "human_agent_coexist", host_id, platform, com, start_wall, end_wall, snapshots_by_host,
                "recovered_ready", evidence,
            )
        )

    # 只用 daemon_ok 快照相鄰對；中間 collector 失聯列不影響 PID／boot 判定。
    notes_by_host: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for note in notes:
        if note.get("kind") == "planned_restart" and note.get("platform", platform) == platform:
            notes_by_host[str(note.get("host_id", ""))].append(note)
    for host_id, rows in sorted(snapshots_by_host.items()):
        healthy = [row for row in rows if row.get("daemon_ok") is True]
        for previous, current in zip(healthy, healthy[1:]):
            if (
                previous.get("platform", platform) != platform
                or current.get("platform", platform) != platform
                or previous.get("daemon_pid") is None
                or current.get("daemon_pid") is None
                or previous.get("daemon_pid") == current.get("daemon_pid")
                or not previous.get("boot_id")
                or previous.get("boot_id") != current.get("boot_id")
            ):
                continue
            current_wall = str(current["t_wall"])
            at = jsonio.parse_iso(current_wall)
            covered_by_note = any(
                note.get("host_id") == host_id
                and jsonio.parse_iso(str(note["from"])) <= at <= jsonio.parse_iso(str(note["to"]))
                for note in notes_by_host.get(host_id, ())
            )
            if not covered_by_note:
                continue
            confirmation = _restart_confirmation(
                rows,
                current,
                policy.collector_interval_s * policy.episode_max_gap_factor,
            )
            evidence = [f"snapshot:{previous.get('record_id', '')}", f"snapshot:{current.get('record_id', '')}"]
            if confirmation is not None:
                evidence.append(f"snapshot:{confirmation.get('record_id', '')}")
            cycles.append(
                _cycle(
                    "planned_restart", host_id, platform, None, current_wall, current_wall, snapshots_by_host,
                    "recovered_ready" if confirmation is not None else "ended_non_ready", evidence,
                )
            )

    type_order = {cycle_type: index for index, cycle_type in enumerate(canary_policy.CYCLE_TYPES)}
    return sorted(
        cycles,
        key=lambda cycle: (
            jsonio.parse_iso(cycle.start_wall), cycle.host_id, cycle.com or "",
            type_order.get(cycle.type, len(type_order)),
            jsonio.parse_iso(cycle.end_wall),
        ),
    )


def active_days(
    submits: Sequence[dict],
    snapshots: Sequence[dict],
    policy: canary_policy.CanaryPolicy,
    *,
    platform: str,
    content: Attribution,
    results: Sequence[dict] = (),
) -> list[str]:
    """回傳符合命令數與實際內容快照覆蓋門檻的本地活躍日期。"""
    quota = policy.platforms[platform]
    all_by_host: dict[str, list[dict]] = defaultdict(list)
    for snapshot in snapshots:
        if snapshot.get("platform", platform) != platform:
            continue
        host_id = str(snapshot.get("host_id", ""))
        all_by_host[host_id].append(snapshot)
    for rows in all_by_host.values():
        rows.sort(key=lambda row: _time_key(row, "t_wall"))

    terminal_results = {
        (str(result.get("host_id", "")), str(result.get("cmd_id", "")))
        for result in results
        if result.get("platform", platform) == platform and result.get("status") in TERMINAL_RESULTS
    }
    submit_days: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for submit in _unique_submits(submits, policy, platform):
        host_id = str(submit.get("host_id", ""))
        cmd_id = str(submit["cmd_id"])
        # 政策固定要求有終態結果；unknown／無結果不得計入。dedup_key 是 host_id＋cmd_id。
        if (host_id, cmd_id) not in terminal_results:
            continue
        wall = str(submit.get("wall_ts", ""))
        attribution = attribute_artifact(wall, all_by_host.get(host_id, ()))
        if attribution != content:
            continue
        day = canary_policy.local_date(quota, wall)
        submit_days[day].add((host_id, cmd_id))

    coverage_by_day: dict[str, float] = defaultdict(float)
    max_interval = 3 * policy.collector_interval_s
    for host_id, rows in sorted(all_by_host.items()):
        for previous, current in zip(rows, rows[1:]):
            if previous.get("daemon_ok") is not True or current.get("daemon_ok") is not True:
                continue
            if previous.get("artifact_check") != "verified" or current.get("artifact_check") != "verified":
                continue
            if _attribution_fields(previous) != content or _attribution_fields(current) != content:
                continue
            before_time = jsonio.parse_iso(str(previous["t_wall"]))
            after_time = jsonio.parse_iso(str(current["t_wall"]))
            interval = (after_time - before_time).total_seconds()
            if interval < 0 or interval > max_interval:
                continue
            before_day = canary_policy.local_date(quota, str(previous["t_wall"]))
            after_day = canary_policy.local_date(quota, str(current["t_wall"]))
            if before_day == after_day:
                coverage_by_day[before_day] += interval

    return sorted(
        day
        for day, cmd_ids in submit_days.items()
        if len(cmd_ids) >= policy.active_day_min_distinct_submits
        and coverage_by_day.get(day, 0) >= policy.active_day_min_snapshot_coverage_s
    )
