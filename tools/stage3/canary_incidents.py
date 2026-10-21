"""Canary 事故追蹤：自動偵測、人工開案、結案與目前狀態彙總（P9-T11，stdlib only）。

自動事故（`detect`）每次由快照、WAL 訊號、擷取異常與 note 重算，不寫入帳本，因此同輸入永遠得到相同的
`incident_id`。人工開案與結案則以附加式紀錄 `incident_open`／`incident_close` 寫入 `ledger.ndjson`，
不改寫既有紀錄。非預期重啟即使已被 supervisor 自動拉起仍開事故（OD-10、§15.11.2）；已佐證的主機重開
本身不是 daemon 事故。

本模組限 stdlib，會被打包進 Task 9 的 collector zipapp。
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from tools.stage3 import canary_classify, canary_policy, jsonio
from tools.stage3.reason_codes import HarnessError


INCIDENT_KINDS: tuple[str, ...] = (
    "unexpected_restart",
    "unexplained_pid_change",
    "supervisor_failed_to_start",
    "daemon_unreachable",
    "stuck_attached",
    "wal_unhealthy",
    "wal_loss",
    "harvest_integrity",
    "multi_open",
    "collector_gap",
    "safety_violation",
    "other",
)
CLOSE_RESOLUTIONS: tuple[str, ...] = ("fixed_in_artifact", "not_product_defect", "environment", "duplicate")
SAFETY_ALLOWED_RESOLUTIONS: tuple[str, ...] = ("fixed_in_artifact", "duplicate")
NOTE_KINDS: tuple[str, ...] = ("planned_restart", "planned_downtime")

LEDGER_FILE: str = "ledger.ndjson"

# 只有明確表示 daemon 確定不存在的錯誤才能排除 PID 重用的可能（見 unexplained_pid_change）。
_DEAD_ERRORS = frozenset({"not_running", "connect_refused"})
# stuck_attached 排除的狀態：這些不是「卡住」而是預期中的離開或燒錄流程。
_STUCK_EXCLUDED_STATES = frozenset({"RELEASED", "FLASHING", "DETACHED"})
# 安全規則（§11.3、§14.2）：跨上下文的同 cmd_id 重送與人機回放。
_RESEND_DISRUPTED_STATES = frozenset({"RECOVERING", "RELEASED", "DETACHED"})
_REPLAY_DISRUPTED_STATES = frozenset({"RECOVERING", "RELEASED"})
# 擷取異常的完整性類別：只記錄而不開事故即屬忽略證據完整性問題。
_INTEGRITY_KINDS = frozenset({"malformed_line", "crc_mismatch", "seq_regress", "truncated", "replaced", "vanished"})
_WAL_LOSS_SIGNALS = frozenset({"wal_loss", "wal_recreated"})
# 會產生事故的 WAL 訊號種類；其餘（如 human_tx）僅供帳本記錄，不必解析快照即可略過。
_SIGNAL_INCIDENT_KINDS = _WAL_LOSS_SIGNALS | frozenset({"unauthorized_tx", "resend_suspect", "human_replay_suspect"})


@dataclass(frozen=True)
class Incident:
    incident_id: str            # sha256(f"{kind}|{host_id}|{key}")[:12]
    kind: str
    platform: str
    host_id: str
    artifact_id: str | None
    opened_at: str
    detected_by: str            # "auto" | "manual"
    summary: str
    evidence: tuple[str, ...]
    status: str                 # "open" | "closed"
    closed_at: str | None
    resolution: str | None
    issue_ref: str | None
    closed_by: str | None
    note: str | None


Window = tuple[datetime, datetime]


@dataclass(frozen=True)
class _Host:
    """單一 host 的快照（依 (t_wall, record_id) 排序）與對應的已解析時間，供二分搜尋使用。"""

    rows: list[dict]
    times: list[datetime]


_EMPTY_HOST = _Host(rows=[], times=[])


def _invalid_close(detail: str) -> HarnessError:
    return HarnessError("HARNESS_INCIDENT_INVALID_CLOSE", detail)


def _incident_id(kind: str, host_id: str, key: str) -> str:
    return hashlib.sha256(f"{kind}|{host_id}|{key}".encode("utf-8")).hexdigest()[:12]


def _make(
    kind: str,
    host_id: str,
    platform: str,
    artifact_id: str | None,
    opened_at: str,
    key: str,
    summary: str,
    evidence: Sequence[str],
) -> Incident:
    """建立自動偵測的未結案事故；自動事故 `detected_by` 固定為 `auto`。"""
    return Incident(
        incident_id=_incident_id(kind, host_id, key),
        kind=kind,
        platform=platform,
        host_id=host_id,
        artifact_id=artifact_id,
        opened_at=opened_at,
        detected_by="auto",
        summary=summary,
        evidence=tuple(evidence),
        status="open",
        closed_at=None,
        resolution=None,
        issue_ref=None,
        closed_by=None,
        note=None,
    )


def _t(record: Mapping[str, Any], field: str = "t_wall") -> datetime:
    return jsonio.parse_iso(str(record[field]))


def _parse_or_none(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return jsonio.parse_iso(value)
    except ValueError:
        return None


def _platform(snapshot: Mapping[str, Any]) -> str:
    return str(snapshot.get("platform") or "unknown")


def _snap_ref(snapshot: Mapping[str, Any]) -> str:
    return f"snapshot:{snapshot.get('record_id') or snapshot.get('t_wall')}"


def _session_row(snapshot: Mapping[str, Any], com: str) -> Mapping[str, Any] | None:
    rows = snapshot.get("sessions")
    if not isinstance(rows, (list, tuple)):
        return None
    for row in rows:
        if isinstance(row, Mapping) and row.get("com") == com:
            return row
    return None


def _windows(notes: Sequence[Mapping[str, Any]], host_id: str, kind: str) -> list[Window]:
    """同 host、同 kind 的 note 時間窗；`from`／`to` 不合法或倒置者略過。"""
    out: list[Window] = []
    for note in notes:
        if note.get("kind") != kind or note.get("host_id") != host_id:
            continue
        start, end = _parse_or_none(note.get("from")), _parse_or_none(note.get("to"))
        if start is None or end is None or end < start:
            continue
        out.append((start, end))
    return out


def _covered(windows: Sequence[Window], when: datetime) -> bool:
    return any(start <= when <= end for start, end in windows)


def _group_snapshots(snapshots: Sequence[Mapping[str, Any]]) -> dict[str, _Host]:
    """依 host 分組並以 (t_wall, record_id) 排序；同時間的次序由 record_id 決定，保證確定性。

    時間只解析一次，之後的窗口查詢以二分搜尋完成（避免每筆訊號都線性掃描並重複解析）。
    """
    grouped: dict[str, list[tuple[datetime, str, dict]]] = defaultdict(list)
    for snapshot in snapshots:
        row = dict(snapshot)
        grouped[str(row.get("host_id", ""))].append((_t(row), str(row.get("record_id", "")), row))
    hosts: dict[str, _Host] = {}
    for host_id, items in grouped.items():
        items.sort(key=lambda item: (item[0], item[1]))
        hosts[host_id] = _Host(rows=[row for _, _, row in items], times=[when for when, _, _ in items])
    return hosts


def _runs(rows: Sequence[dict], predicate) -> list[list[int]]:
    """回傳 rows 中連續滿足 predicate 的位置區段（依原序）。"""
    runs: list[list[int]] = []
    current: list[int] | None = None
    for index, row in enumerate(rows):
        if predicate(row):
            if current is None:
                current = []
            current.append(index)
        elif current is not None:
            runs.append(current)
            current = None
    if current is not None:
        runs.append(current)
    return runs


def _artifact_near(host: _Host, when: datetime) -> str | None:
    """取時間不晚於 `when` 的最後快照之 artifact_id；無則取第一筆。"""
    if not host.rows:
        return None
    index = max(bisect_right(host.times, when) - 1, 0)
    return host.rows[index].get("artifact_id")


def _host_incidents(
    host_id: str,
    rows: Sequence[dict],
    notes: Sequence[Mapping[str, Any]],
    policy: canary_policy.CanaryPolicy,
) -> list[Incident]:
    """單一 host 的快照類事故（PID、supervisor、不可達、卡住、WAL、多開與缺口）。"""
    interval = policy.collector_interval_s
    grace = timedelta(seconds=policy.post_boot_grace_s)
    planned = _windows(notes, host_id, "planned_restart")
    downtime = _windows(notes, host_id, "planned_downtime")
    out: list[Incident] = []
    count = len(rows)

    # 已佐證的主機重開：boot_id 皆非 None 且不同、中間無其他快照、間隔 ≥ reboot_corroboration_s。
    corroborated_at: dict[int, bool] = {}
    boot_started: dict[str, datetime] = {}
    for index in range(count - 1):
        before, after = rows[index], rows[index + 1]
        before_boot, after_boot = before.get("boot_id"), after.get("boot_id")
        if before_boot is None or after_boot is None or before_boot == after_boot:
            continue
        if (_t(after) - _t(before)).total_seconds() >= policy.reboot_corroboration_s:
            corroborated_at[index] = True
            boot_started.setdefault(str(after_boot), _t(after))

    # PID 規則：以相鄰的 daemon_ok 快照 p、c 比較，中間失聯的快照略過。
    daemon_index = [index for index, row in enumerate(rows) if row.get("daemon_ok") is True]
    for left, right in zip(daemon_index, daemon_index[1:]):
        before, after = rows[left], rows[right]
        if corroborated_at.get(left) and after.get("boot_id") == rows[left + 1].get("boot_id"):
            continue  # 已佐證的主機重開不是 daemon 事故。
        pid_changed = before.get("daemon_pid") != after.get("daemon_pid")
        dead_between = any(rows[j].get("error") in _DEAD_ERRORS for j in range(left + 1, right))
        if not (pid_changed or dead_between):
            continue
        when = _t(after)
        if _covered(planned, when):
            continue
        gap = (when - _t(before)).total_seconds()
        if pid_changed and gap <= 3 * interval:
            kind = "unexpected_restart"
            summary = f"daemon pid {before.get('daemon_pid')}→{after.get('daemon_pid')} 於 {gap:.0f}s 內變更且無計畫重啟覆蓋（非預期重啟）"
        else:
            kind = "unexplained_pid_change"
            summary = f"daemon pid {before.get('daemon_pid')}→{after.get('daemon_pid')}（間隔 {gap:.0f}s，中間{'曾' if dead_between else '未'}確認 daemon 不存在）無計畫重啟覆蓋"
        out.append(
            _make(
                kind,
                host_id,
                _platform(after),
                after.get("artifact_id"),
                str(after["t_wall"]),
                str(after["t_wall"]),
                summary,
                [_snap_ref(before), _snap_ref(after)],
            )
        )

    # supervisor 未於已佐證重開後的 post_boot_grace_s 內恢復 daemon。窗口只取同一 boot 的快照：
    # 之後另一個 boot 的健康快照不能代表前一個 boot 已成功啟動；窗口若因新 boot 提早結束，
    # 該 boot 已結束，視為已完整觀測。
    for index in sorted(corroborated_at):
        first = rows[index + 1]
        deadline = _t(first) + grace
        tail = rows[index + 1 :]
        window: list[dict] = []
        for row in tail:
            if row.get("boot_id") != first.get("boot_id") or _t(row) > deadline:
                break
            window.append(row)
        observed = len(window) < len(tail) or _t(rows[-1]) >= deadline
        if not observed:
            continue  # 尚未觀測滿寬限期，不能判定。
        if any(row.get("daemon_ok") is True for row in window):
            continue
        out.append(
            _make(
                "supervisor_failed_to_start",
                host_id,
                _platform(first),
                first.get("artifact_id"),
                str(first["t_wall"]),
                str(first.get("boot_id")),
                f"已佐證主機重開後 {policy.post_boot_grace_s}s 內 daemon 未恢復（boot {first.get('boot_id')}）",
                [_snap_ref(first)],
            )
        )

    # daemon 不可達：同 boot 連續 daemon_ok=false。post-boot grace 內與計畫停機內的快照會切開區段，
    # 各保留段獨立判定；如此維護窗或 grace 結束後仍持續的失聯不會因區段起點落在豁免內而被整段遮蔽。
    grace_end = {boot: start + grace for boot, start in boot_started.items()}

    def _exempt_poll(row: Mapping[str, Any]) -> bool:
        when = _t(row)
        boot = row.get("boot_id")
        end = grace_end.get(str(boot)) if boot is not None else None
        if end is not None and when <= end:
            return True
        return _covered(downtime, when)

    down_runs = _split_by_boot(rows, _runs(rows, lambda row: row.get("daemon_ok") is not True))
    for run in down_runs:
        for segment in _split_kept(run, lambda index: not _exempt_poll(rows[index])):
            if len(segment) < policy.unreachable_incident_polls:
                continue
            first, last = rows[segment[0]], rows[segment[-1]]
            span = (_t(last) - _t(first)).total_seconds()
            if span < 2 * interval:
                continue
            out.append(
                _make(
                    "daemon_unreachable",
                    host_id,
                    _platform(first),
                    first.get("artifact_id"),
                    str(first["t_wall"]),
                    str(first["t_wall"]),
                    f"連續 {len(segment)} 個快照 daemon 不可達（跨度 {span:.0f}s）",
                    [_snap_ref(first), _snap_ref(last)],
                )
            )

    # 卡住：同 com、同 daemon pid、連續非穩態（排除 RELEASED／FLASHING／DETACHED）達 stuck_incident_s。
    # 單次 daemon 不可達不重置卡住時鐘（不可達由 daemon_unreachable 規則處理）；但曾確認 daemon 不存在
    # （not_running／connect_refused）則切斷，因重新啟動後的狀態已不是同一段連續觀測。
    coms = sorted(
        {
            str(row["com"])
            for snapshot in rows
            for row in (snapshot.get("sessions") or ())
            if isinstance(row, Mapping) and row.get("com") is not None
        }
    )
    for com in coms:
        segments: list[tuple[int, int]] = []
        start: int | None = None
        last: int | None = None
        pid: Any = None
        dead_since_last = False
        for index, snapshot in enumerate(rows):
            if snapshot.get("daemon_ok") is not True:
                if snapshot.get("error") in _DEAD_ERRORS:
                    dead_since_last = True
                continue
            session = _session_row(snapshot, com)
            eligible = (
                session is not None
                and session.get("state") not in _STUCK_EXCLUDED_STATES
                and not canary_classify.steady(session)
            )
            continues = eligible and start is not None and not dead_since_last and pid == snapshot.get("daemon_pid")
            dead_since_last = False
            if continues:
                last = index
                continue
            if start is not None and last is not None:
                segments.append((start, last))
            start = last = None
            if eligible:
                start = last = index
                pid = snapshot.get("daemon_pid")
        if start is not None and last is not None:
            segments.append((start, last))
        for first_index, last_index in segments:
            first, last_row = rows[first_index], rows[last_index]
            duration = (_t(last_row) - _t(first)).total_seconds()
            if duration < policy.stuck_incident_s:
                continue
            out.append(
                _make(
                    "stuck_attached",
                    host_id,
                    _platform(first),
                    first.get("artifact_id"),
                    str(first["t_wall"]),
                    f"{com}|{first['t_wall']}",
                    f"{com} 於 daemon pid {first.get('daemon_pid')} 下非穩態持續 {duration:.0f}s",
                    [_snap_ref(first), _snap_ref(last_row)],
                )
            )

    # WAL 不健康、同機多開：各段第一個快照為事故起點。
    for predicate, kind, text in (
        (lambda row: row.get("wal_healthy") is False, "wal_unhealthy", "WAL 健康檢查失敗"),
        (lambda row: row.get("multi_open") is True, "multi_open", "偵測到同機多開（two-reader）"),
    ):
        for run in _runs(rows, predicate):
            first = rows[run[0]]
            out.append(
                _make(
                    kind,
                    host_id,
                    _platform(first),
                    first.get("artifact_id"),
                    str(first["t_wall"]),
                    str(first["t_wall"]),
                    text,
                    [_snap_ref(first)],
                )
            )

    # collector 缺口：同 boot 相鄰快照間隔超過 gap_incident_s。
    for index in range(count - 1):
        before, after = rows[index], rows[index + 1]
        if before.get("boot_id") != after.get("boot_id"):
            continue
        gap = (_t(after) - _t(before)).total_seconds()
        if gap > policy.gap_incident_s:
            out.append(
                _make(
                    "collector_gap",
                    host_id,
                    _platform(before),
                    before.get("artifact_id"),
                    str(before["t_wall"]),
                    str(before["t_wall"]),
                    f"collector 缺口 {gap:.0f}s（同 boot）",
                    [_snap_ref(before), _snap_ref(after)],
                )
            )
    return out


def _split_by_boot(rows: Sequence[dict], runs: list[list[int]]) -> list[list[int]]:
    """boot_id 變更即切段，使一段不可達區間只屬於同一個 boot。"""
    split: list[list[int]] = []
    for run in runs:
        current: list[int] = []
        for index in run:
            if current and rows[current[-1]].get("boot_id") != rows[index].get("boot_id"):
                split.append(current)
                current = []
            current.append(index)
        if current:
            split.append(current)
    return split


def _split_kept(run: Sequence[int], keep: Callable[[int], bool]) -> list[list[int]]:
    """依 keep 把連續區段切成保留的子區段；被排除的快照會切斷區段。"""
    out: list[list[int]] = []
    current: list[int] = []
    for index in run:
        if keep(index):
            current.append(index)
        elif current:
            out.append(current)
            current = []
    if current:
        out.append(current)
    return out


def _states_between(host: _Host, com: str, lo: datetime, hi: datetime) -> list[Mapping[str, Any]]:
    """回傳時間落在 [lo, hi] 內、該 com 的 session 列（二分搜尋時間窗）。"""
    start = bisect_left(host.times, lo)
    end = bisect_right(host.times, hi)
    out: list[Mapping[str, Any]] = []
    for snapshot in host.rows[start:end]:
        session = _session_row(snapshot, com)
        if session is not None:
            out.append(session)
    return out


def _previous_wall(signal: Mapping[str, Any]) -> datetime | None:
    detail = signal.get("detail")
    if not isinstance(detail, Mapping):
        return None
    return _parse_or_none(detail.get("previous_wall_ts"))


def _crossing_window(signal: Mapping[str, Any]) -> tuple[str, datetime, datetime] | None:
    """回傳 (com, 前次 wall 時間, 本次 wall 時間)；前次時間或 COM 不可得時無法判定跨上下文，回傳 None。"""
    previous = _previous_wall(signal)
    com = signal.get("com")
    if previous is None or not isinstance(com, str):
        return None
    return com, previous, jsonio.parse_iso(str(signal["wall_ts"]))


def _resend_crosses(window: tuple[str, datetime, datetime], host: _Host) -> bool:
    """同 cmd_id 重送是否跨越 RECOVERING／RELEASED／DETACHED、generation 或 daemon pid 變更。"""
    com, previous, now = window
    if any(row.get("state") in _RESEND_DISRUPTED_STATES for row in _states_between(host, com, previous, now)):
        return True
    lo = bisect_left(host.times, previous)
    hi = bisect_right(host.times, now)
    scope = ([host.rows[lo - 1]] if lo > 0 else []) + host.rows[lo:hi]
    generations = {
        session.get("bridge_generation")
        for snapshot in scope
        for session in [_session_row(snapshot, com)]
        if session is not None and session.get("bridge_generation") is not None
    }
    pids = {snapshot.get("daemon_pid") for snapshot in scope if snapshot.get("daemon_ok") is True}
    return len(generations) > 1 or len(pids) > 1


def _replay_crosses(window: tuple[str, datetime, datetime], host: _Host) -> bool:
    """人機回放是否發生在 RECOVERING 或 RELEASED 期間（§11.3、§14.2）。"""
    com, previous, now = window
    return any(row.get("state") in _REPLAY_DISRUPTED_STATES for row in _states_between(host, com, previous, now))


def _signal_incident(signal: Mapping[str, Any], hosts: Mapping[str, _Host]) -> Incident | None:
    kind = str(signal.get("kind", ""))
    if kind not in _SIGNAL_INCIDENT_KINDS:
        return None
    host_id = str(signal.get("host_id", ""))
    host = hosts.get(host_id, _EMPTY_HOST)
    rows = host.rows
    wall = str(signal["wall_ts"])
    when = jsonio.parse_iso(wall)
    seq = signal.get("seq")
    platform = str(signal.get("platform") or (_platform(rows[0]) if rows else "unknown"))
    artifact = _artifact_near(host, when)
    evidence = [f"wal:{seq}"]

    if kind in _WAL_LOSS_SIGNALS:
        # wal_loss 與 wal_recreated 共用同一個 key，同一筆 META 只會產生一個事故。
        return _make(
            "wal_loss", host_id, platform, artifact, wall, f"wal_loss:{seq}:{wall}",
            f"WAL 遺失或重建（{kind}，seq {seq}）", evidence,
        )
    if kind == "unauthorized_tx":
        return _make(
            "safety_violation", host_id, platform, artifact, wall, f"unauthorized_tx:{seq}:{wall}",
            f"未授權 TX 來源 {signal.get('source')}", evidence,
        )
    window = _crossing_window(signal)
    if kind == "resend_suspect":
        if window is None:
            # 前次時間或 COM 不可得：無法排除跨上下文重送，保守歸為安全不變量（不可以環境原因結案）。
            return _make(
                "safety_violation", host_id, platform, artifact, wall, f"resend_suspect:{seq}:{wall}",
                "同 cmd_id 重送但缺少可判定的前次時間或 COM，無法排除跨上下文（保守歸為安全不變量）", evidence,
            )
        if _resend_crosses(window, host):
            return _make(
                "safety_violation", host_id, platform, artifact, wall, f"resend_suspect:{seq}:{wall}",
                "同 cmd_id 同 payload 重送且跨越 RECOVERING／RELEASED／DETACHED、generation 或 daemon pid 變更", evidence,
            )
        return _make(
            "other", host_id, platform, artifact, wall, f"resend_suspect:{seq}:{wall}",
            "同 cmd_id 同 payload 重送，未跨越上下文，仍須分診", evidence,
        )
    # human_replay_suspect
    if window is None:
        return _make(
            "other", host_id, platform, artifact, wall, f"human_replay_suspect:{seq}:{wall}",
            "人機回放訊號缺少可判定的前次時間或 COM，仍須分診", evidence,
        )
    if not _replay_crosses(window, host):
        return None
    return _make(
        "safety_violation", host_id, platform, artifact, wall, f"human_replay_suspect:{seq}:{wall}",
        "人機回放疑似跨越 RECOVERING／RELEASED 期間（安全不變量）", evidence,
    )


def _order_key(incident: Incident) -> tuple:
    """同一 incident_id 出現多筆候選時的確定性排序鍵：最早開案者優先，其餘欄位僅作為穩定的次序。"""
    return (
        jsonio.parse_iso(incident.opened_at),
        incident.evidence,
        incident.summary,
        incident.platform,
        incident.artifact_id or "",
    )


def detect(
    snapshots: Sequence[dict],
    signals: Sequence[dict],
    notes: Sequence[dict],
    policy: canary_policy.CanaryPolicy,
    anomalies: Sequence[dict] = (),
) -> list[Incident]:
    """由帳本快照、WAL 訊號、擷取異常與 note 重算所有自動事故（純函式、確定性）。

    同一 incident_id 若由多筆輸入產生，保留最早開案者，結果不依輸入順序而定。
    回傳依 `(opened_at, incident_id)` 排序的未結案事故；結案狀態由 `current_incidents` 套用。
    """
    hosts = _group_snapshots(snapshots)
    found: dict[str, Incident] = {}

    def keep(incident: Incident | None) -> None:
        if incident is None:
            return
        current = found.get(incident.incident_id)
        if current is None or _order_key(incident) < _order_key(current):
            found[incident.incident_id] = incident

    for host_id, host in hosts.items():
        for incident in _host_incidents(host_id, host.rows, notes, policy):
            keep(incident)
    for signal in signals:
        keep(_signal_incident(signal, hosts))
    for anomaly in anomalies:
        detail = str(anomaly.get("detail", ""))
        if detail.split(":", 1)[0] not in _INTEGRITY_KINDS:
            continue
        host_id = str(anomaly.get("host_id", ""))
        host = hosts.get(host_id, _EMPTY_HOST)
        when = _t(anomaly)
        keep(
            _make(
                "harvest_integrity",
                host_id,
                str(anomaly.get("platform") or (_platform(host.rows[0]) if host.rows else "unknown")),
                _artifact_near(host, when),
                str(anomaly["t_wall"]),
                detail,
                f"擷取異常：{detail}",
                [f"harvest_anomaly:{anomaly.get('record_id') or detail}"],
            )
        )

    return sorted(found.values(), key=lambda incident: (jsonio.parse_iso(incident.opened_at), incident.incident_id))


def _read_ledger(ledger_dir: Path) -> list[dict]:
    path = Path(ledger_dir) / LEDGER_FILE
    if not path.exists():
        return []
    records, malformed = jsonio.read_ndjson(path)
    if malformed:
        raise HarnessError("HARNESS_LEDGER_CORRUPT", f"{path.name} malformed lines {malformed[:5]}")
    return records


def _append(ledger_dir: Path, record: dict) -> dict:
    """補上 record_id 後附加到帳本；既有紀錄不被修改。"""
    Path(ledger_dir).mkdir(parents=True, exist_ok=True)
    record["record_id"] = jsonio.record_id(record)
    jsonio.append_ndjson(Path(ledger_dir) / LEDGER_FILE, record)
    return record


def _check_scope(platform: str, host_id: str) -> None:
    """人工開案與 note 的平台／主機欄位須合乎帳本 schema（Task 12 依 platform 過濾 gate）。"""
    if platform not in canary_policy.PLATFORMS:
        raise ValueError(f"unknown platform: {platform!r}")
    if not isinstance(host_id, str) or not host_id.strip():
        raise ValueError("host_id must be a non-empty string")


def write_note(
    ledger_dir: Path,
    *,
    kind: str,
    start: str,
    end: str,
    reason: str,
    author: str,
    host_id: str,
    platform: str,
) -> dict:
    """寫入計畫停機或計畫重啟 note；`planned_restart` 只抑制 PID 類事故，`planned_downtime` 只抑制不可達。"""
    if kind not in NOTE_KINDS:
        raise ValueError(f"unknown note kind: {kind!r}")
    _check_scope(platform, host_id)
    start_dt, end_dt = jsonio.parse_iso(start), jsonio.parse_iso(end)
    if end_dt <= start_dt:
        raise ValueError("note window requires start < end")
    if not reason.strip() or not author.strip():
        raise ValueError("note reason and author are required")
    record = {
        "type": "note",
        "host_id": host_id,
        "platform": platform,
        "kind": kind,
        "from": start,
        "to": end,
        "reason": reason,
        "author": author,
        "t_wall": jsonio.utc_now_iso(),
    }
    return _append(Path(ledger_dir), record)


def open_manual(
    ledger_dir: Path,
    *,
    kind: str,
    platform: str,
    host_id: str,
    artifact_id: str | None,
    summary: str,
    evidence: Sequence[str],
    now: str,
) -> Incident:
    """人工開案並寫入 `incident_open` 紀錄；incident_id 由 kind、host、開案時間與摘要決定。"""
    if kind not in INCIDENT_KINDS:
        raise ValueError(f"unknown incident kind: {kind!r}")
    _check_scope(platform, host_id)
    jsonio.parse_iso(now)
    incident_id = _incident_id(kind, host_id, f"{now}|{summary}")
    record = {
        "type": "incident_open",
        "host_id": host_id,
        "platform": platform,
        "incident_id": incident_id,
        "kind": kind,
        "artifact_id": artifact_id,
        "opened_at": now,
        "detected_by": "manual",
        "summary": summary,
        "evidence": list(evidence),
    }
    _append(Path(ledger_dir), record)
    return Incident(
        incident_id=incident_id,
        kind=kind,
        platform=platform,
        host_id=host_id,
        artifact_id=artifact_id,
        opened_at=now,
        detected_by="manual",
        summary=summary,
        evidence=tuple(evidence),
        status="open",
        closed_at=None,
        resolution=None,
        issue_ref=None,
        closed_by=None,
        note=None,
    )


def close(
    ledger_dir: Path,
    incident_id: str,
    *,
    resolution: str,
    issue_ref: str | None,
    note: str,
    closed_by: str,
    now: str,
    known: Mapping[str, Incident],
) -> dict:
    """結案並寫入 `incident_close` 紀錄。違反規則一律丟出 `HARNESS_INCIDENT_INVALID_CLOSE`。

    安全不變量（`safety_violation`）只能以 `fixed_in_artifact` 或 `duplicate` 結案，不得以環境或非產品缺陷豁免。
    """
    incident = known.get(incident_id)
    if incident is None:
        raise _invalid_close(f"unknown incident {incident_id}")
    if incident.status == "closed" or any(
        record.get("type") == "incident_close" and record.get("incident_id") == incident_id
        for record in _read_ledger(Path(ledger_dir))
    ):
        raise _invalid_close(f"incident {incident_id} already closed")
    if resolution not in CLOSE_RESOLUTIONS:
        raise _invalid_close(f"resolution {resolution!r} not allowed")
    if incident.kind == "safety_violation" and resolution not in SAFETY_ALLOWED_RESOLUTIONS:
        raise _invalid_close(f"safety_violation cannot close as {resolution!r}")
    if resolution == "fixed_in_artifact" and not issue_ref:
        raise _invalid_close("fixed_in_artifact requires issue_ref")
    if not note or not note.strip():
        raise _invalid_close("note must be non-empty")
    if not closed_by or not closed_by.strip():
        raise _invalid_close("closed_by must be non-empty")
    try:
        jsonio.parse_iso(now)
    except ValueError as exc:
        raise _invalid_close(f"closed_at must be tz-aware ISO: {exc}") from exc

    record = {
        "type": "incident_close",
        "host_id": incident.host_id,
        "platform": incident.platform,
        "incident_id": incident_id,
        "resolution": resolution,
        "issue_ref": issue_ref,
        "note": note,
        "closed_by": closed_by,
        "closed_at": now,
    }
    return _append(Path(ledger_dir), record)


def current_incidents(records: Sequence[dict], detected: Sequence[Incident]) -> dict[str, Incident]:
    """合併自動事故、人工開案與結案紀錄，回傳 incident_id 到目前狀態的對應。"""
    current: dict[str, Incident] = {}
    for incident in detected:
        current.setdefault(incident.incident_id, incident)
    for record in records:
        if record.get("type") != "incident_open":
            continue
        incident_id = str(record["incident_id"])
        if incident_id in current:
            continue
        current[incident_id] = Incident(
            incident_id=incident_id,
            kind=str(record["kind"]),
            platform=str(record.get("platform", "unknown")),
            host_id=str(record.get("host_id", "")),
            artifact_id=record.get("artifact_id"),
            opened_at=str(record["opened_at"]),
            detected_by=str(record.get("detected_by", "manual")),
            summary=str(record.get("summary", "")),
            evidence=tuple(str(item) for item in record.get("evidence", [])),
            status="open",
            closed_at=None,
            resolution=None,
            issue_ref=None,
            closed_by=None,
            note=None,
        )
    closes = sorted(
        (record for record in records if record.get("type") == "incident_close"),
        key=lambda record: (jsonio.parse_iso(str(record["closed_at"])), str(record.get("record_id", ""))),
    )
    for record in closes:
        incident_id = str(record["incident_id"])
        incident = current.get(incident_id)
        if incident is None or incident.status == "closed":
            continue
        current[incident_id] = replace(
            incident,
            status="closed",
            closed_at=str(record["closed_at"]),
            resolution=record.get("resolution"),
            issue_ref=record.get("issue_ref"),
            closed_by=record.get("closed_by"),
            note=record.get("note"),
        )
    return current
