"""Stage 3 命令列入口（`python3 -m tools.stage3 <子命令>`，P9-T17）。

子命令群組與各自委派的模組：

- `ab`：`verify-manifest` → `ab_manifest.load_manifest`＋`verify_frozen`；`run` → `run_ab`（P9-T6）；
- `canary`：`policy freeze`（P9-T12）、`collect`（P9-T9）、`note`／`incident`（P9-T11）、`report`（P9-T12）、`pack-collector`；
- `rollback-drill`：`run`、`make-corrupted`、`make-start-fail` → `rollback_drill`（P9-T13）；
- `evidence`：`target`、`env-id`、`ingest`、`add`、`status`、`report` → `evidence_index`／`evidence_ingest`（P9-T14、T14B）；
- `od09 check`：唯讀委派 `od09_consume.check_main`（P9-T15）；
- `retirement`：整段 argv 原樣交給 `retirement.main`（P9-T16）；`release.yml` 與 `mkbundle.sh` 的既有呼叫方式不變。

結束碼契約：0＝PASS／成功；1＝FAIL／INCOMPLETE／BLOCKED；2＝參數錯誤或 `HarnessError`；3＝INFRA_ERROR／BASELINE_INVALID／ABORTED。
`HarnessError` 時 stdout 印 `{"ok":false,"reason_code":…,"detail":…}`。`ValueError`、`OSError`、argparse 參數錯誤與未預期例外同形輸出 `HARNESS_CLI_ERROR`（exit 2）；未預期例外的堆疊寫到 stderr，不以 exit 1 偽裝成判定結果。`ab run` 被中斷（`KeyboardInterrupt`）時讀回 `<out>/ab-report.json`：已記錄的 FAIL 維持 exit 1；報告不可讀或為 PASS 時輸出 `HARNESS_ABORTED` 並 exit 3。
stdout 強制 UTF-8，以緊湊 JSON 輸出（`ensure_ascii=False`），僅 `evidence report` 與 `canary report --md` 輸出 Markdown。

本模組不 import `sw_core`，也不寫入 `od09` 凍結（唯一寫入者為 P2 `realhw.acceptance.od09_freeze`）。`build_parser()` 不匯入 `realhw`、`yaml` 或 `jsonschema`（`canary collect` 以 `/usr/bin/python3` 常駐，須維持 stdlib-only 路徑）。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import threading
import traceback
from pathlib import Path, PurePath
from typing import Any, Callable, Mapping, Sequence

from tools.stage3 import canary_policy, jsonio
from tools.stage3.reason_codes import HarnessError
from tools.stage3.retirement import main as retirement_main

REPO_ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE_DIR = REPO_ROOT / "tests" / "acceptance"
_AB_EXIT: Mapping[str, int] = {
    "PASS": 0,
    "FAIL": 1,
    "INCOMPLETE": 1,
    "INFRA_ERROR": 3,
    "BASELINE_INVALID": 3,
    "ABORTED": 3,
}
_AB_EXIT_UNKNOWN = 3


class _Parser(argparse.ArgumentParser):
    """關閉前綴縮寫：`--i-under` 不得代替 `--i-understand-this-restarts-the-daemon`（破壞性旗標）。子解析器同樣套用。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)


def run_ab(manifest_path: Path, *, out_dir: Path, repo_root: Path) -> Any:
    """Sequential A/B 執行器（P9-T6）的延後匯入包裝；測試可替換此模組層級名稱。"""
    from tools.stage3 import ab_runner  # noqa: PLC0415

    return ab_runner.run_ab(manifest_path, out_dir=out_dir, repo_root=repo_root)


def _owner_pair(value: str) -> tuple[str, str]:
    key, sep, name = value.partition("=")
    if not sep or not key or not name.strip():
        raise argparse.ArgumentTypeError("必須為 PLATFORM=NAME")
    return key, name


def _add_od09_check_arguments(parser: argparse.ArgumentParser) -> None:
    """與 `od09_consume.add_check_arguments` 同形的引數。本地定義以免 `build_parser()` 匯入 realhw／yaml；
    `tests/test_stage3_cli.py::test_od09_check_arguments_match_consumer` 核對兩者一致。"""
    parser.add_argument("--acceptance", required=True, type=Path)
    parser.add_argument("--run-started-at", required=True)


def build_parser() -> argparse.ArgumentParser:
    """登錄所有子命令（供說明與參數驗證）；`retirement` 的實際解析由 `retirement.main` 負責。"""
    from tools.stage3 import canary_incidents, evidence_index  # noqa: PLC0415

    parser = _Parser(prog="python3 -m tools.stage3", description="Stage 3 測試工具入口（P9）。")
    commands = parser.add_subparsers(dest="command", required=True)

    ab = commands.add_parser("ab", help="Sequential A/B：manifest 核對與執行（P9-T3、T6）")
    ab_actions = ab.add_subparsers(dest="action", required=True)
    verify = ab_actions.add_parser("verify-manifest", help="載入並核對凍結 manifest（不執行任何受測命令）")
    verify.add_argument("--manifest", required=True, type=Path)
    verify.add_argument("--repo-root", required=True, type=Path)
    run = ab_actions.add_parser("run", help="執行 Sequential A/B 並寫出 ab-report.json")
    run.add_argument("--manifest", required=True, type=Path)
    run.add_argument("--out", required=True, type=Path)
    run.add_argument("--repo-root", required=True, type=Path)

    canary = commands.add_parser("canary", help="Canary policy、收集、事故與帳本報告（P9-T8～T12）")
    canary_actions = canary.add_subparsers(dest="action", required=True)
    policy = canary_actions.add_parser("policy", help="Canary policy 凍結（BO-P9-06）")
    policy_actions = policy.add_subparsers(dest="policy_action", required=True)
    freeze = policy_actions.add_parser("freeze", help="凍結 draft policy 並附加 addenda 列")
    freeze.add_argument("--policy", required=True, type=Path)
    freeze.add_argument("--decision-id", required=True)
    freeze.add_argument("--frozen-by", required=True)
    freeze.add_argument("--evidence-owner", required=True, action="append", type=_owner_pair,
                        help="PLATFORM=NAME，兩個平台各給一次")
    freeze.add_argument("--addenda", required=True, type=Path)
    collect = canary_actions.add_parser("collect", help="收集 Canary 快照（只接受已凍結 policy）")
    collect.add_argument("--cli", required=True, type=Path)
    collect.add_argument("--ledger-dir", required=True, type=Path)
    collect.add_argument("--policy", required=True, type=Path)
    collect.add_argument("--platform", required=True, choices=canary_policy.PLATFORMS)
    collect.add_argument("--once", action="store_true", help="只收集一次")
    note = canary_actions.add_parser("note", help="登錄計畫停機或計畫重啟時段")
    note.add_argument("--ledger-dir", required=True, type=Path)
    note.add_argument("--kind", required=True, choices=canary_incidents.NOTE_KINDS)
    note.add_argument("--from", dest="start", required=True, help="ISO 8601（含時區）")
    note.add_argument("--to", dest="end", required=True, help="ISO 8601（含時區）")
    note.add_argument("--reason", required=True)
    note.add_argument("--author", required=True)
    note.add_argument("--platform", required=True, choices=canary_policy.PLATFORMS)
    incident = canary_actions.add_parser("incident", help="人工事故開案、結案與目前狀態")
    incident_actions = incident.add_subparsers(dest="incident_action", required=True)
    opened = incident_actions.add_parser("open", help="人工開案（附加 incident_open）")
    opened.add_argument("--ledger-dir", required=True, type=Path)
    opened.add_argument("--kind", required=True, choices=canary_incidents.INCIDENT_KINDS)
    opened.add_argument("--platform", required=True, choices=canary_policy.PLATFORMS)
    opened.add_argument("--artifact-id", required=True)
    opened.add_argument("--summary", required=True)
    opened.add_argument("--evidence", action="append", default=[], metavar="REF")
    closed = incident_actions.add_parser("close", help="結案（附加 incident_close）")
    closed.add_argument("--ledger-dir", required=True, type=Path)
    closed.add_argument("--policy", required=True, type=Path)
    closed.add_argument("--id", dest="incident_id", required=True)
    closed.add_argument("--resolution", required=True, choices=canary_incidents.CLOSE_RESOLUTIONS)
    closed.add_argument("--issue-ref")
    closed.add_argument("--note", required=True)
    closed.add_argument("--closed-by", required=True)
    listed = incident_actions.add_parser("list", help="列出目前事故（自動偵測與人工開案合併）")
    listed.add_argument("--ledger", required=True, nargs="+", type=Path)
    listed.add_argument("--policy", required=True, type=Path)
    listed.add_argument("--platform", choices=canary_policy.PLATFORMS)
    report = canary_actions.add_parser("report", help="單一平台、單一 artifact 的 Canary gate 報告")
    report.add_argument("--policy", required=True, type=Path)
    report.add_argument("--addenda", required=True, type=Path)
    report.add_argument("--ledger", required=True, nargs="+", type=Path)
    report.add_argument("--platform", required=True, choices=canary_policy.PLATFORMS)
    report.add_argument("--artifact-id", required=True)
    report.add_argument("--bundle-tree-sha256", required=True)
    report.add_argument("--runtime-tree-sha256", required=True)
    report.add_argument("--carryover", type=Path, help="核准 carryover 的 JSON 陣列檔")
    report.add_argument("--md", action="store_true", help="stdout 改印 Markdown（JSON 仍寫入 --out）")
    report.add_argument("--out", type=Path, help="寫出 ingest 用的 JSON 報告")
    pack = canary_actions.add_parser("pack-collector", help="打包 Canary collector zipapp")
    pack.add_argument("--out", required=True, type=Path)

    drill = commands.add_parser("rollback-drill", help="實機回滾演練（P9-T13，破壞性）")
    drill_actions = drill.add_subparsers(dest="action", required=True)
    drill_run = drill_actions.add_parser("run", help="執行 D0～D8（會停止並重啟 daemon）")
    drill_run.add_argument("--cli", required=True, type=Path)
    drill_run.add_argument("--platform", required=True, choices=canary_policy.PLATFORMS)
    drill_run.add_argument("--boards", required=True, help="逗號分隔，例如 COM0,COM1")
    drill_run.add_argument("--expect-current", required=True)
    drill_run.add_argument("--expect-previous", required=True)
    drill_run.add_argument("--out", required=True, type=Path)
    drill_run.add_argument("--forward-argv", help="JSON 字串陣列；省略時由 D1 推得")
    drill_run.add_argument("--corrupt-from", type=Path)
    drill_run.add_argument("--start-fail-from", type=Path)
    drill_run.add_argument("--i-understand-this-restarts-the-daemon", action="store_true", required=True)
    corrupt = drill_actions.add_parser("make-corrupted", help="由受測 bundle 產生損毀 bundle（D7 來源）")
    corrupt.add_argument("--src", required=True, type=Path)
    corrupt.add_argument("--dst", required=True, type=Path)
    start_fail = drill_actions.add_parser("make-start-fail", help="由受測 bundle 產生啟動失敗 bundle（D8 來源）")
    start_fail.add_argument("--src", required=True, type=Path)
    start_fail.add_argument("--dst", required=True, type=Path)

    evidence = commands.add_parser("evidence", help="Stage 0～3 出口證據索引、target 與 ingest")
    evidence_actions = evidence.add_subparsers(dest="action", required=True)
    target = evidence_actions.add_parser("target", help="凍結 stage／platform 的受測 target")
    target.add_argument("--index", required=True, type=Path)
    target.add_argument("--stage", required=True, choices=evidence_index.STAGES)
    target.add_argument("--platform", required=True, choices=canary_policy.PLATFORMS)
    target.add_argument("--release-manifest", required=True, type=Path)
    target.add_argument("--contract-revision", required=True)
    target.add_argument("--tool-revision", required=True)
    target.add_argument("--case-list-sha256", required=True)
    target.add_argument("--set-by", required=True)
    target.add_argument("--bundle-dir", type=Path, help="Stage 3 必填：受測 bundle 目錄")
    env_id = evidence_actions.add_parser("env-id", help="由環境 JSON 計算 environment_id")
    env_id.add_argument("--env", required=True, type=Path)
    ingest = evidence_actions.add_parser("ingest", help="由產生工具報告推導結果並登錄（PASS 的唯一入口）")
    ingest.add_argument("--index", required=True, type=Path)
    ingest.add_argument("--kind", required=True)
    ingest.add_argument("--item", required=True)
    ingest.add_argument("--platform", required=True, choices=canary_policy.PLATFORMS)
    ingest.add_argument("--report", required=True, type=Path)
    ingest.add_argument("--evidence-root", required=True, type=Path)
    ingest.add_argument("--environment", required=True, type=Path)
    ingest.add_argument("--release-manifest", type=Path)
    ingest.add_argument("--reuse-of")
    add = evidence_actions.add_parser("add", help="登錄 manual-procedure 以外無法 PASS 的項目（僅接受完整 entry JSON）")
    add.add_argument("--index", required=True, type=Path)
    add.add_argument("--entry", required=True, type=Path)
    status = evidence_actions.add_parser("status", help="彙整指定 stage 的出口狀態")
    status.add_argument("--index", required=True, type=Path)
    status.add_argument("--stage", required=True, choices=evidence_index.STAGES)
    status.add_argument("--evidence-root", required=True, type=Path)
    report_cmd = evidence_actions.add_parser("report", help="輸出 Stage 0～3 Markdown 出口表")
    report_cmd.add_argument("--index", required=True, type=Path)
    report_cmd.add_argument("--evidence-root", required=True, type=Path)

    od09 = commands.add_parser("od09", help="OD-09 凍結結果唯讀核對（P9-T15）")
    od09_actions = od09.add_subparsers(dest="action", required=True)
    _add_od09_check_arguments(od09_actions.add_parser("check", help="核對門檻檔於 run 起點前是否已凍結（唯讀）"))

    commands.add_parser("retirement", help="Python daemon 退役切換（P9-T16）").add_argument(
        "rest", nargs=argparse.REMAINDER, help="交給 retirement 的子命令與參數"
    )
    return parser


def _plain(value: Any) -> Any:
    """把 dataclass、tuple、Path 轉成可 JSON 序列化的結構。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, PurePath):
        return str(value)
    return value


def _emit(payload: Any) -> None:
    print(jsonio.dumps_compact(_plain(payload)))


def _emit_error(code: str | None, detail: str) -> None:
    print(jsonio.dumps_compact({"ok": False, "reason_code": code, "detail": detail}))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError("HARNESS_EVIDENCE_INGEST_INVALID", f"無法讀取 JSON：{path.name}：{exc}") from exc


def _read_json_object(path: Path) -> dict:
    doc = _read_json(path)
    if not isinstance(doc, dict):
        raise HarnessError("HARNESS_EVIDENCE_INGEST_INVALID", f"{path.name} 頂層必須是 JSON 物件")
    return doc


def _owner_map(pairs: Sequence[tuple[str, str]]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for key, name in pairs:
        if key in owners:
            raise HarnessError("HARNESS_CANARY_POLICY_INVALID", f"evidence owner 重複指定：{key}")
        owners[key] = name
    return owners


def _load_cases() -> dict[str, dict]:
    """讀取 P2 `cases.yaml` 的 case_id 對照（XFAIL 核准來源）。"""
    import yaml  # noqa: PLC0415

    path = ACCEPTANCE_DIR / "cases.yaml"
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise HarnessError("HARNESS_CASE_SET_INCOMPLETE", f"無法讀取 cases.yaml：{exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("cases"), list):
        raise HarnessError("HARNESS_CASE_SET_INCOMPLETE", "cases.yaml 必須含 cases 陣列")
    return {row["case_id"]: row for row in doc["cases"] if isinstance(row, dict) and isinstance(row.get("case_id"), str)}


def _optional_l1_lock() -> dict | None:
    from tools.stage3 import evidence_index  # noqa: PLC0415

    try:
        return _read_json_object(evidence_index.L1_LOCK_PATH)
    except HarnessError:
        return None


def _entry_from_json(path: Path):
    from tools.stage3 import evidence_index  # noqa: PLC0415

    doc = _read_json_object(path)
    try:
        files = tuple(evidence_index.FrozenFile(**row) for row in doc.pop("files", []))
        xfail = tuple(doc.pop("xfail_cases", []))
        return evidence_index.EvidenceEntry(**doc, files=files, xfail_cases=xfail)
    except (TypeError, AttributeError) as exc:
        raise HarnessError("HARNESS_EVIDENCE_INGEST_INVALID", f"entry JSON 欄位不符：{exc}") from exc


def _ledger_records(ledger_dir: Path) -> list[dict]:
    from tools.stage3 import canary_incidents  # noqa: PLC0415

    path = Path(ledger_dir) / canary_incidents.LEDGER_FILE
    if not path.exists():
        return []
    records, malformed = jsonio.read_ndjson(path)
    if malformed:
        raise HarnessError("HARNESS_LEDGER_CORRUPT", f"{path.name} malformed lines {malformed[:5]}")
    return records


def _known_incidents(records: list[dict], policy: canary_policy.CanaryPolicy) -> dict:
    from tools.stage3 import canary_incidents  # noqa: PLC0415

    detected = canary_incidents.detect(
        [row for row in records if row.get("type") == "snapshot"],
        [row for row in records if row.get("type") == "wal_signal"],
        [row for row in records if row.get("type") == "note"],
        policy,
        [row for row in records if row.get("type") == "harvest_anomaly"],
    )
    return canary_incidents.current_incidents(records, detected)


def _load_carryovers(path: Path) -> list[dict]:
    doc = _read_json(path)
    if not isinstance(doc, list) or not all(isinstance(row, dict) for row in doc):
        raise HarnessError("HARNESS_CANARY_POLICY_INVALID", "carryover 檔必須是 JSON 物件陣列")
    return doc


def _interrupted_ab_report(out_dir: Path) -> tuple[str, str | None] | None:
    """中斷後讀回執行器寫出的 `ab-report.json`（P9-T6 步驟 9）。

    回傳 `(verdict, reason_code)`；讀不到、格式不符，或判定為 PASS（中斷的執行不得成為 PASS）時回 None，
    由呼叫端回退為 ABORTED。已記錄的產品 FAIL 必須保留，不得被中斷蓋成 INFRA（exit 3）。
    """
    try:
        doc = json.loads((Path(out_dir) / "ab-report.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    verdict = doc.get("verdict")
    if not isinstance(verdict, str) or verdict == "PASS" or verdict not in _AB_EXIT:
        return None
    reason = doc.get("reason_code")
    return verdict, reason if isinstance(reason, str) else None


def _ab_command(args: argparse.Namespace) -> int:
    if args.action == "verify-manifest":
        from tools.stage3 import ab_manifest  # noqa: PLC0415

        manifest = ab_manifest.load_manifest(args.manifest)
        violations = ab_manifest.verify_frozen(manifest, repo_root=args.repo_root)
        _emit({"ok": not violations, "violations": violations})
        return 0 if not violations else 1
    try:
        result = run_ab(args.manifest, out_dir=args.out, repo_root=args.repo_root)
    except KeyboardInterrupt:
        # 執行器於中斷前寫出 ABORTED 報告（或已有產品 FAIL 的報告）後重新拋出；結束碼依報告判定。
        recorded = _interrupted_ab_report(args.out)
        if recorded is None:
            _emit_error("HARNESS_ABORTED", "ab run 被中斷，結果不完整")
            return 3
        verdict, reason = recorded
        _emit({"verdict": verdict, "reason_code": reason, "interrupted": True,
               "report": str(Path(args.out) / "ab-report.json")})
        return _AB_EXIT[verdict]
    payload = _plain(result)
    _emit(payload)
    return _AB_EXIT.get(str(payload.get("verdict")), _AB_EXIT_UNKNOWN)


def _canary_policy_freeze(args: argparse.Namespace) -> int:
    owners = _owner_map(args.evidence_owner)
    result = canary_policy.freeze_policy(
        args.policy,
        decision_id=args.decision_id,
        frozen_by=args.frozen_by,
        evidence_owners=owners,
        addenda_path=args.addenda,
        now=jsonio.utc_now_iso(),
    )
    _emit({"ok": True, **_plain(result)})
    return 0


def _canary_collect(args: argparse.Namespace) -> int:
    from tools.stage3 import canary_collect  # noqa: PLC0415

    policy = canary_policy.load_policy(args.policy)
    if args.once:
        _emit(canary_collect.collect_once(args.cli, args.ledger_dir, policy, platform=args.platform))
        return 0
    stop = threading.Event()
    try:
        canary_collect.collect_loop(args.cli, args.ledger_dir, policy, platform=args.platform, stop=stop)
    except KeyboardInterrupt:
        stop.set()
    _emit({"ok": True, "stopped": True})
    return 0


def _canary_note(args: argparse.Namespace) -> int:
    from tools.stage3 import canary_collect, canary_incidents  # noqa: PLC0415

    record = canary_incidents.write_note(
        args.ledger_dir,
        kind=args.kind,
        start=args.start,
        end=args.end,
        reason=args.reason,
        author=args.author,
        host_id=canary_collect.host_id(),
        platform=args.platform,
    )
    _emit({"ok": True, "record": record})
    return 0


def _canary_incident(args: argparse.Namespace) -> int:
    from tools.stage3 import canary_collect, canary_incidents  # noqa: PLC0415

    if args.incident_action == "open":
        incident = canary_incidents.open_manual(
            args.ledger_dir,
            kind=args.kind,
            platform=args.platform,
            host_id=canary_collect.host_id(),
            artifact_id=args.artifact_id,
            summary=args.summary,
            evidence=args.evidence,
            now=jsonio.utc_now_iso(),
        )
        _emit({"ok": True, "incident": incident})
        return 0
    if args.incident_action == "close":
        policy = canary_policy.load_policy(args.policy)
        known = _known_incidents(_ledger_records(args.ledger_dir), policy)
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
        _emit({"ok": True, "record": record})
        return 0
    from tools.stage3 import canary_ledger  # noqa: PLC0415  (P9-T12)

    policy = canary_policy.load_policy(args.policy)
    records = canary_ledger.load_records(args.ledger)
    incidents = sorted(
        _known_incidents(records, policy).values(),
        key=lambda item: (item.opened_at, item.incident_id),
    )
    if args.platform is not None:
        incidents = [item for item in incidents if item.platform == args.platform]
    _emit({"ok": True, "incidents": incidents})
    return 0


def _canary_report(args: argparse.Namespace) -> int:
    from tools.stage3 import canary_ledger  # noqa: PLC0415  (P9-T12)

    policy = canary_policy.load_policy(args.policy, addenda_path=args.addenda)
    records = canary_ledger.load_records(args.ledger)
    carryovers = _load_carryovers(args.carryover) if args.carryover is not None else []
    gate = canary_ledger.gate(
        records,
        policy,
        platform=args.platform,
        artifact_id=args.artifact_id,
        bundle_tree_sha256=args.bundle_tree_sha256,
        runtime_tree_sha256=args.runtime_tree_sha256,
        carryovers=carryovers,
    )
    payload = _plain(gate)
    if args.out is not None:
        jsonio.atomic_write_text(args.out, jsonio.dumps_report(payload))
    if args.md:
        sys.stdout.write(canary_ledger.report_md([gate]))
    else:
        _emit(payload)
    return 0 if gate.result == "PASS" else 1


def _canary_command(args: argparse.Namespace) -> int:
    if args.action == "policy":
        return _canary_policy_freeze(args)
    if args.action == "collect":
        return _canary_collect(args)
    if args.action == "note":
        return _canary_note(args)
    if args.action == "incident":
        return _canary_incident(args)
    if args.action == "report":
        return _canary_report(args)
    if args.action == "pack-collector":
        from tools.stage3 import pack_collector  # noqa: PLC0415

        _emit({"ok": True, "path": pack_collector.pack(args.out)})
        return 0
    raise AssertionError(f"unknown canary action: {args.action}")


def _drill_command(args: argparse.Namespace) -> int:
    from tools.stage3 import rollback_drill  # noqa: PLC0415

    if args.action == "make-corrupted":
        _emit({"ok": True, "path": rollback_drill.make_corrupted_bundle(args.src, args.dst)})
        return 0
    if args.action == "make-start-fail":
        _emit({"ok": True, "path": rollback_drill.make_start_fail_bundle(args.src, args.dst)})
        return 0
    boards = tuple(part.strip() for part in args.boards.split(",") if part.strip())
    if not boards:
        raise ValueError("--boards 不得為空")
    forward = json.loads(args.forward_argv) if args.forward_argv is not None else []
    if not isinstance(forward, list) or not all(isinstance(value, str) for value in forward):
        raise ValueError("--forward-argv 必須是字串 JSON 陣列")
    config = rollback_drill.DrillConfig(
        cli=args.cli,
        platform=args.platform,
        boards=boards,
        expect_current=args.expect_current,
        expect_previous=args.expect_previous,
        out_dir=args.out,
        forward_argv=tuple(forward),
        corrupt_from=args.corrupt_from,
        start_fail_from=args.start_fail_from,
    )
    result = rollback_drill.run_drill(config)
    _emit(result)
    return 0 if result.verdict == "PASS" else 1


def _evidence_command(args: argparse.Namespace) -> int:
    from tools.stage3 import evidence_index, evidence_ingest  # noqa: PLC0415

    if args.action == "target":
        evidence_index.set_target(
            args.index,
            stage=args.stage,
            platform=args.platform,
            release_manifest=args.release_manifest,
            contract_revision=args.contract_revision,
            tool_revision=args.tool_revision,
            case_list_sha256=args.case_list_sha256,
            set_by=args.set_by,
            now=jsonio.utc_now_iso(),
            bundle_dir=args.bundle_dir,
        )
        _emit({"ok": True, "stage": args.stage, "platform": args.platform})
        return 0
    if args.action == "env-id":
        env_doc = _read_json_object(args.env)
        _emit({"ok": True, "environment_id": evidence_index.environment_id(env_doc)})
        return 0
    if args.action == "ingest":
        entry = evidence_ingest.ingest(
            args.index,
            kind=args.kind,
            item_id=args.item,
            platform=args.platform,
            report=args.report,
            evidence_root=args.evidence_root,
            catalog=evidence_index.load_exit_catalog(),
            cases=_load_cases(),
            lock=_read_json_object(ACCEPTANCE_DIR / "cases.lock.json"),
            environment=args.environment,
            release_manifest=args.release_manifest,
            reuse_of=args.reuse_of,
            now=jsonio.utc_now_iso(),
            l1_lock=_optional_l1_lock(),
        )
        _emit({"ok": True, "entry": entry})
        return 0 if entry.result == "PASS" else 1
    if args.action == "add":
        entry = _entry_from_json(args.entry)
        if entry.producer != "manual":
            # PASS／XFAIL 的唯一入口是 ingest；add 只接受人工程序紀錄（manual-procedure 項目，見 add_entry）。
            raise HarnessError(
                "HARNESS_EVIDENCE_INGEST_INVALID",
                "evidence add 只接受 producer 為 manual 的 manual-procedure 項目；工具產出的 PASS／XFAIL 一律走 evidence ingest",
            )
        evidence_index.add_entry(args.index, entry, catalog=evidence_index.load_exit_catalog())
        _emit({"ok": True, "entry_id": entry.entry_id})
        return 0
    index = evidence_index.load_index(args.index)
    catalog = evidence_index.load_exit_catalog()
    if args.action == "status":
        result = evidence_index.stage_status(
            index,
            args.stage,
            catalog=catalog,
            evidence_root=args.evidence_root,
            cases=_load_cases(),
            l1_lock=_optional_l1_lock(),
        )
        _emit(result)
        return 0 if result["result"] == "PASS" else 1
    if args.action == "report":
        text = evidence_index.render_report(
            index,
            catalog,
            evidence_root=args.evidence_root,
            cases=_load_cases(),
            l1_lock=_optional_l1_lock(),
        )
        sys.stdout.write(text)
        return 0
    raise AssertionError(f"unknown evidence action: {args.action}")


def _dispatch(args: argparse.Namespace, tokens: Sequence[str]) -> int:
    if args.command == "ab":
        return _ab_command(args)
    if args.command == "canary":
        return _canary_command(args)
    if args.command == "rollback-drill":
        return _drill_command(args)
    if args.command == "evidence":
        return _evidence_command(args)
    if args.command == "od09":
        from tools.stage3 import od09_consume  # noqa: PLC0415

        return od09_consume.check_main(list(tokens))
    raise AssertionError(f"unknown command: {args.command}")


def _run_guarded(call: Callable[[], int]) -> int:
    """統一錯誤出口：argparse（含 retirement 子解析器）、HarnessError、ValueError／OSError 與未預期例外。

    所有非判定的失敗都以 `{"ok":false,"reason_code":…}` JSON 輸出並以 exit 2 離開；未預期例外不得以 exit 1
    （FAIL／INCOMPLETE 判定）或未經整理的 traceback 離開。`--help` 的 SystemExit(0) 原樣保留。
    """
    try:
        return call()
    except SystemExit as exc:  # argparse 參數錯誤以 SystemExit 結束；於此轉為回傳值
        code = exc.code if isinstance(exc.code, int) else 2
        if code != 0:
            _emit_error("HARNESS_CLI_ERROR", "命令列參數錯誤，說明見 stderr")
        return code
    except HarnessError as exc:
        _emit_error(exc.code, exc.detail or exc.code)
        return 2
    except (ValueError, OSError) as exc:
        _emit_error("HARNESS_CLI_ERROR", str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001 — 最後防線：未預期例外不得以 exit 1（判定結果）離開
        traceback.print_exc(file=sys.stderr)
        _emit_error("HARNESS_CLI_ERROR", f"{type(exc).__name__}: {exc}")
        return 2


def _entry(tokens: list[str]) -> int:
    if tokens[:1] == ["retirement"]:
        # retirement 的完整 argv 原樣交給 P9-T16 的 `retirement.main`（release.yml 與 mkbundle.sh 既有呼叫方式不變）。
        return retirement_main(tokens)
    args = build_parser().parse_args(tokens)
    return _dispatch(args, tokens)


def main(argv: Sequence[str] | None = None) -> int:
    # 結構化輸出含中文且可能被重導向；非 UTF-8 的 stdout（例如 Windows 重導向）不得讓錯誤路徑本身崩潰。
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    tokens = list(sys.argv[1:] if argv is None else argv)
    return _run_guarded(lambda: _entry(tokens))


if __name__ == "__main__":
    raise SystemExit(main())
