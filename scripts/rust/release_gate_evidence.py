#!/usr/bin/env python3
"""RC 封裝前重驗 Slice 2.1、2.2、2.4 的結論與同一 HEAD 證據。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TREE_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SUMMARY_LABELS = ("missing IDs", "failed IDs", "A/B mismatch", "identity errors", "incomplete runs")


class GateEvidenceError(ValueError):
    """一個或多個 Slice gate 報告未達 RC 封裝門檻。"""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateEvidenceError(f"{label} 報告無法讀取：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GateEvidenceError(f"{label} 報告根節點必須是 object：{path}")
    return value


def _require_empty(report: Mapping[str, Any], key: str, expected_type: type, label: str) -> None:
    value = report.get(key)
    if not isinstance(value, expected_type) or value:
        raise GateEvidenceError(f"Slice 2.4 {label}.{key} 必須是空的 {expected_type.__name__}：{value!r}")


def _check_identity_problems(value: Any, label: str) -> None:
    if not isinstance(value, Mapping):
        raise GateEvidenceError(f"{label} identity_problems 必須是含 missing/mismatch 的 object：{value!r}")
    for key in ("missing", "mismatch"):
        problems = value.get(key)
        if not isinstance(problems, list) or problems:
            raise GateEvidenceError(f"{label} identity_problems.{key} 必須為空陣列：{problems!r}")


def _check_slice21(run_dir: Path, head: str) -> None:
    report = _read_json(run_dir / "report.json", "Slice 2.1")
    if report.get("verdict") != "READY_FOR_OWNER_EVIDENCE":
        raise GateEvidenceError(f"Slice 2.1 結論不是 READY_FOR_OWNER_EVIDENCE：{report.get('verdict')!r}")
    if report.get("dry_run") is not False:
        raise GateEvidenceError("Slice 2.1 報告必須是正式執行（dry_run=false）")
    if report.get("git_revision") != head:
        raise GateEvidenceError(f"Slice 2.1 git_revision={report.get('git_revision')!r}，預期 {head}")
    _check_identity_problems(report.get("identity_problems"), "Slice 2.1")


def _check_slice22(report_path: Path, head: str) -> None:
    report = _read_json(report_path, "Slice 2.2")
    if report.get("result") != "PASS":
        raise GateEvidenceError(f"Slice 2.2 結論不是 PASS：{report.get('result')!r}")
    if report.get("dry_run") is not False:
        raise GateEvidenceError("Slice 2.2 報告必須是正式執行（dry_run=false）")
    identity = report.get("identity")
    if not isinstance(identity, Mapping) or identity.get("git_revision") != head:
        actual = identity.get("git_revision") if isinstance(identity, Mapping) else None
        raise GateEvidenceError(f"Slice 2.2 identity.git_revision={actual!r}，預期 {head}")
    _check_identity_problems(report.get("identity_problems"), "Slice 2.2")


def _check_slice24(run_dir: Path, repo: Path, head: str) -> None:
    json_report_path = run_dir / "slice24.json"
    md_report_path = run_dir / "slice24.md"
    report = _read_json(json_report_path, "Slice 2.4")

    recorded_run = report.get("run_dir")
    if not isinstance(recorded_run, str) or not recorded_run:
        raise GateEvidenceError("Slice 2.4 報告缺少 run_dir")
    recorded_path = Path(recorded_run)
    if not recorded_path.is_absolute():
        recorded_path = repo / recorded_path
    if recorded_path.resolve() != run_dir.resolve():
        raise GateEvidenceError(f"Slice 2.4 run_dir={recorded_run!r} 與輸入報告目錄不符")

    # Slice 2.4 沒有單一 verdict 欄位；正式 driver 的結論是所有 hard-error 集合皆空，且 driver 命令全數成功。
    _require_empty(report, "missing", dict, "JSON")
    _require_empty(report, "failed", list, "JSON")
    _require_empty(report, "ab_mismatch", list, "JSON")
    _require_empty(report, "identity_errors", list, "JSON")
    _require_empty(report, "incomplete_runs", dict, "JSON")

    try:
        markdown = md_report_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateEvidenceError(f"Slice 2.4 Markdown 報告無法讀取：{md_report_path}: {exc}") from exc
    if "## 判定彙總" not in markdown:
        raise GateEvidenceError("Slice 2.4 Markdown 缺少判定彙總")
    for label in _SUMMARY_LABELS:
        matches = re.findall(rf"^- {re.escape(label)}：([0-9]+)$", markdown, re.MULTILINE)
        if len(matches) != 1 or matches[0] != "0":
            raise GateEvidenceError(f"Slice 2.4 Markdown {label} 必須恰為 0：{matches!r}")

    driver_path = run_dir / "driver.tsv"
    try:
        lines = driver_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GateEvidenceError(f"Slice 2.4 driver.tsv 無法讀取：{driver_path}: {exc}") from exc
    rows: dict[str, int] = {}
    for line in lines:
        if not line:
            continue
        name, separator, code_text = line.partition("\t")
        if not separator or not name or not code_text.isdigit() or name in rows:
            raise GateEvidenceError(f"Slice 2.4 driver.tsv 列格式錯誤或重複：{line!r}")
        rows[name] = int(code_text)
    if not rows or any(code != 0 for code in rows.values()):
        raise GateEvidenceError(f"Slice 2.4 driver.tsv 有未通過命令：{rows!r}")
    required_steps = {"linux-candidate-source", "contract-coverage"}
    if not required_steps.issubset(rows):
        raise GateEvidenceError(f"Slice 2.4 driver.tsv 缺少必要命令：{sorted(required_steps - set(rows))}")

    candidate_record = _read_json(run_dir / "artifacts/linux/candidate-bundle.json", "Slice 2.4 candidate bundle")
    if candidate_record.get("schema_version") != "slice24-candidate-bundle.v1" or candidate_record.get("platform") != "linux":
        raise GateEvidenceError("Slice 2.4 candidate-bundle.json schema/platform 不符")
    if not isinstance(candidate_record.get("bundle_tree_sha256"), str) or not _TREE_SHA_RE.fullmatch(candidate_record["bundle_tree_sha256"]):
        raise GateEvidenceError("Slice 2.4 candidate-bundle.json 缺少有效 bundle_tree_sha256")
    files = candidate_record.get("files")
    if not isinstance(files, list) or not files:
        raise GateEvidenceError("Slice 2.4 candidate-bundle.json 缺少檔案身分清單")

    source_log = run_dir / "logs/linux-candidate-source.log"
    try:
        source_text = source_log.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateEvidenceError(f"Slice 2.4 候選來源日誌無法讀取：{source_log}: {exc}") from exc
    if f"候選 bundle git_revision={head}" not in source_text:
        raise GateEvidenceError(f"Slice 2.4 候選 bundle 未證明綁定正式 tag HEAD {head}")


def validate_release_gate_evidence(
    *, repo: Path, head: str, slice21_run: Path, slice22_report: Path, slice24_run: Path,
) -> None:
    """只接受同一完整 HEAD 上的 2.1／2.2／2.4 結論與候選身分證據。"""
    if _SHA_RE.fullmatch(head) is None:
        raise GateEvidenceError(f"HEAD 必須是完整 40 位小寫 Git SHA：{head!r}")
    repo = repo.resolve(strict=True)
    _check_slice21(slice21_run.resolve(strict=True), head)
    _check_slice22(slice22_report.resolve(strict=True), head)
    _check_slice24(slice24_run.resolve(strict=True), repo, head)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="核對 RC 封裝所需的 gate 報告與 HEAD")
    check.add_argument("--repo", type=Path, required=True)
    check.add_argument("--head", required=True, help="RC tag 的完整 40 位 Git SHA")
    check.add_argument("--slice21-run", type=Path, required=True, help="Slice 2.1 run 目錄，含 report.json")
    check.add_argument("--slice22-report", type=Path, required=True, help="Slice 2.2 s22-gate.json")
    check.add_argument("--slice24-run", type=Path, required=True, help="Slice 2.4 run 目錄，含 slice24.json／slice24.md")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_release_gate_evidence(
            repo=args.repo, head=args.head, slice21_run=args.slice21_run,
            slice22_report=args.slice22_report, slice24_run=args.slice24_run,
        )
    except (OSError, GateEvidenceError) as exc:
        print(f"release_gate_evidence: 拒絕封裝 RC：{exc}", file=sys.stderr)
        return 1
    print(f"release_gate_evidence: PASS Slice 2.1／2.2／2.4 與 HEAD {args.head} 一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
