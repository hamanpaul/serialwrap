"""Canary collector、維護 note 與事故帳本的命令列入口。"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import threading
from pathlib import Path
from typing import Sequence

from tools.stage3 import canary_collect, canary_incidents, canary_policy, jsonio
from tools.stage3.reason_codes import HarnessError


def _json_print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sw-canary")
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="收集一次或持續收集 Canary 資料")
    collect.add_argument("--cli", required=True, type=Path, help="serialwrap CLI 執行檔路徑")
    collect.add_argument("--ledger-dir", required=True, type=Path, help="Canary 帳本目錄")
    collect.add_argument("--policy", required=True, type=Path, help="已凍結的 Canary policy JSON")
    collect.add_argument("--platform", required=True, choices=canary_policy.PLATFORMS)
    collect.add_argument("--once", action="store_true", help="只執行一次收集")

    note = commands.add_parser("note", help="登錄計畫停機或重啟時段")
    note.add_argument("--ledger-dir", required=True, type=Path)
    note.add_argument("--kind", required=True, choices=canary_incidents.NOTE_KINDS)
    note.add_argument("--from", dest="start", required=True)
    note.add_argument("--to", dest="end", required=True)
    note.add_argument("--reason", required=True)
    note.add_argument("--author", required=True)
    note.add_argument("--host-id", required=True)
    note.add_argument("--platform", required=True, choices=canary_policy.PLATFORMS)

    incident = commands.add_parser("incident", help="管理人工 Canary 事故紀錄")
    incident_commands = incident.add_subparsers(dest="incident_command", required=True)

    opened = incident_commands.add_parser("open", help="新增人工事故")
    opened.add_argument("--ledger-dir", required=True, type=Path)
    opened.add_argument("--kind", required=True, choices=canary_incidents.INCIDENT_KINDS)
    opened.add_argument("--platform", required=True, choices=canary_policy.PLATFORMS)
    opened.add_argument("--host-id", required=True)
    opened.add_argument("--artifact-id")
    opened.add_argument("--summary", required=True)
    opened.add_argument("--evidence", action="append", default=[])

    closed = incident_commands.add_parser("close", help="結案既有事故")
    closed.add_argument("--ledger-dir", required=True, type=Path)
    closed.add_argument("--policy", required=True, type=Path)
    closed.add_argument("--incident-id", required=True)
    closed.add_argument("--resolution", required=True, choices=canary_incidents.CLOSE_RESOLUTIONS)
    closed.add_argument("--issue-ref")
    closed.add_argument("--note", required=True)
    closed.add_argument("--closed-by", required=True)

    listed = incident_commands.add_parser("list", help="列出目前自動與人工事故")
    listed.add_argument("--ledger-dir", required=True, type=Path)
    listed.add_argument("--policy", required=True, type=Path)

    return parser


def _read_records(ledger_dir: Path) -> list[dict]:
    path = Path(ledger_dir) / "ledger.ndjson"
    if not path.exists():
        return []
    records, malformed = jsonio.read_ndjson(path)
    if malformed:
        raise HarnessError("HARNESS_LEDGER_CORRUPT", f"malformed ledger lines: {malformed[:10]}")
    return records


def _detected(records: list[dict], policy: canary_policy.CanaryPolicy):
    return canary_incidents.detect(
        [row for row in records if row.get("type") == "snapshot"],
        [row for row in records if row.get("type") == "wal_signal"],
        [row for row in records if row.get("type") == "note"],
        policy,
        [row for row in records if row.get("type") == "harvest_anomaly"],
    )


def _execute(args: argparse.Namespace) -> int:
    if args.command == "collect":
        policy = canary_policy.load_policy(args.policy)
        if args.once:
            _json_print(canary_collect.collect_once(args.cli, args.ledger_dir, policy, platform=args.platform))
            return 0
        stop = threading.Event()
        try:
            canary_collect.collect_loop(args.cli, args.ledger_dir, policy, platform=args.platform, stop=stop)
        except KeyboardInterrupt:
            stop.set()
        _json_print({"ok": True, "stopped": True})
        return 0

    if args.command == "note":
        record = canary_incidents.write_note(
            args.ledger_dir,
            kind=args.kind,
            start=args.start,
            end=args.end,
            reason=args.reason,
            author=args.author,
            host_id=args.host_id,
            platform=args.platform,
        )
        _json_print({"ok": True, "record": record})
        return 0

    if args.command == "incident":
        records = _read_records(args.ledger_dir)
        if args.incident_command == "open":
            incident = canary_incidents.open_manual(
                args.ledger_dir,
                kind=args.kind,
                platform=args.platform,
                host_id=args.host_id,
                artifact_id=args.artifact_id,
                summary=args.summary,
                evidence=args.evidence,
                now=jsonio.utc_now_iso(),
            )
            _json_print({"ok": True, "incident": dataclasses.asdict(incident)})
            return 0

        policy = canary_policy.load_policy(args.policy)
        incidents = _detected(records, policy)
        known = canary_incidents.current_incidents(records, incidents)
        if args.incident_command == "close":
            record = canary_incidents.close(
                args.ledger_dir,
                args.incident_id,
                resolution=args.resolution,
                issue_ref=args.issue_ref,
                note=args.note,
                closed_by=args.closed_by,
                now=jsonio.utc_now_iso(),
                known=known,
            )
            _json_print({"ok": True, "record": record})
            return 0
        rows = [dataclasses.asdict(row) for row in sorted(known.values(), key=lambda item: (item.opened_at, item.incident_id))]
        _json_print({"ok": True, "incidents": rows})
        return 0

    raise AssertionError(f"unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    """解析命令並以穩定 JSON 回報 Canary 工具錯誤。"""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    canary_collect.collector_revision()
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _execute(args)
    except HarnessError as exc:
        _json_print({"ok": False, "reason_code": exc.code, "detail": exc.detail})
        return 2
    except (OSError, ValueError) as exc:
        _json_print(
            {
                "ok": False,
                "reason_code": "HARNESS_CLI_ERROR",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

