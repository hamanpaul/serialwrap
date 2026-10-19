"""Stage 出口證據索引校驗器（§15.12，P2-T12）。

用法：
  python3 tests/acceptance/check_evidence_index.py --stage 0 [--root DIR] [--index PATH] [--cases PATH]

讀取索引與承接清單（預設皆為 <root>/tests/acceptance/ 下的 stage<STAGE>_evidence_index.yaml 與 cases.yaml），
schema 取自本檔同目錄的 evidence_index.schema.json。--root 預設為 repo 根，同時是 artifact 相對路徑的基準；
--index／--cases 可分別指定索引與清單的位置（例如索引放在暫存目錄、證據放在另一目錄），未指定者仍取 <root>/tests/acceptance/。

檢查規則：
  schema     索引符合 evidence_index.schema.json；單案例列（case_ids 非空）必填 platform 與 evidence。
  stage      索引的 stage 與 --stage 一致。
  ids        item id 不重複；artifact_path 不重複、不得為絕對路徑或含 ..；case_ids 皆為 cases.yaml 已知案例。
  file       status 為 present 的項目：檔案存在且 sha256 與實際相符（只驗完整性，不讀取 artifact 內容）。
  evidence   單案例列的 evidence 必為該案例 cases.yaml evidence 清單之一，artifact_path 須對應該檔名（目錄以 / 結尾者比對路徑段）。
  cover      （stage 0）每個 stage0_required 案例，對其每個適用平台（linux；windows 限 windows_status native／ported）
             與 evidence 清單中每一項，都必須有至少一列覆蓋（§11.6、§15.12：逐檔登錄，不得只靠單一報告）。
  required   （stage 0）§4 必要項目 ID 集合 STAGE0_REQUIRED_ITEM_IDS 必須全部存在於索引（不論狀態）。

退出碼：
  0  索引完整、所有 present 項檔案完整性相符，且無 missing／blocked_owner 項目。
     注意：exit 0 不是 verdict PASS；checker 不讀取 artifact 內容，FAIL 或 INSUFFICIENT 的報告只要 sha256 相符仍會 exit 0。
  1  FAIL：schema 不合、stage 不符、重複 id／路徑、路徑不安全、未知案例、evidence 不屬該案例、
     檔名不對應、present 項檔案缺失或 sha256 不符、覆蓋缺口、必要項目缺列
  2  用法或輸入錯誤（索引／案例檔不存在、stage 參數不合法；--index／--cases 指定的路徑不存在亦同）
  5  BLOCKED：無 FAIL，但存在 missing 或 blocked_owner 項目（不得視為通過）

BLOCKED 與 FAIL 同時存在時回 1（FAIL 優先）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import Any

import yaml

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parents[1]
SCHEMA_PATH = HERE / "evidence_index.schema.json"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_BLOCKED = 5

STAGE_RE = re.compile(r"^[0-9][a-z]?$")

# §4 必要項目（plan 03 Task 12／Task 16、§4 B 項與 §4.1 gate）。缺列即 FAIL，避免以刪列方式縮小出口清單。
STAGE0_REQUIRED_ITEM_IDS = frozenset({
    "S0-BRIDGE-SELFTEST",
    "S0-BRIDGE-SELFTEST-WIN",
    "S0-BRIDGE-APPROVED-REVISION",
    "S0-L1-BC",
    "S0-L1-WIN-PYTEST",
    "S0-L3-BASELINE-LINUX-48H",
    "S0-L3-BASELINE-WINDOWS-48H",
    "S0-OD09-DEFERRED-FROZEN",
    "S0-B01-WIN-COM-ENDPOINT",
    "S0-B02-PEER-CLOSE",
    "S0-B03-WIN-FUNCTEST-BRIDGE",
    "S0-B04-LINUX-BENCH",
    "S0-B05-WIN-BENCH",
    "S0-B06-DESTRUCTIVE-AUTH",
    "S0-B07-F4-BENCH",
    "S0-B10-LONGRUN-RESTART-OFF",
    "S0-B12-WIN-TASK-72H",
    "S0-B13-WIN-COM-CI-RUNNER",
    "S0-B14-CROSS-OS-BENCH-MUTEX",
    "S0-B15-BENCH-NOT-SUBSTITUTED",
    "S0-BLOCKED-P2-1-MCU-MANUAL-FLASH-LOG",
    "S0-BLOCKED-P2-1-MCU-MANUAL-PROCEDURE-CHECKLIST",
    "S0-BLOCKED-P2-1-MCU-MANUAL-DAEMON-STATUS-BEFORE",
    "S0-BLOCKED-P2-1-MCU-MANUAL-DAEMON-STATUS-AFTER",
})
REQUIRED_ITEM_IDS_BY_STAGE: dict[str, frozenset[str]] = {"0": STAGE0_REQUIRED_ITEM_IDS}


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_relpath(rel: str) -> bool:
    p = pathlib.PurePosixPath(rel)
    return not p.is_absolute() and ".." not in p.parts and not rel.startswith("~")


def _path_matches_evidence(rel: str, evidence: str) -> bool:
    """artifact_path 是否對應 evidence 名稱：檔名相等；目錄（以 / 結尾）則需為路徑段。"""
    parts = pathlib.PurePosixPath(rel).parts
    if evidence.endswith("/"):
        return evidence[:-1] in parts[:-1]
    return bool(parts) and parts[-1] == evidence


def _required_platforms(case: dict[str, Any]) -> list[str]:
    """案例在 Stage 出口必須具備證據的平台（windows_status gap 者不要求 Windows 列，缺口記於 cases.yaml）。"""
    plats = set(case.get("platforms") or [])
    out: list[str] = []
    if "linux" in plats:
        out.append("linux")
    if "windows" in plats and case.get("windows_status") in ("native", "ported"):
        out.append("windows")
    return out


def check(stage: str, root: pathlib.Path, index_path: pathlib.Path | None = None,
          cases_path: pathlib.Path | None = None) -> int:
    acc = root / "tests" / "acceptance"
    index_path = index_path or acc / f"stage{stage}_evidence_index.yaml"
    cases_path = cases_path or acc / "cases.yaml"
    for p in (index_path, cases_path):
        if not p.is_file():
            print(f"[evidence-index] 輸入不存在：{p}", file=sys.stderr)
            return EXIT_USAGE

    index = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    cases_data = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}
    cases_by_id = {c["case_id"]: c for c in cases_data.get("cases", [])}

    fails: list[str] = []
    blocked: list[str] = []

    import jsonschema  # 測試側依賴（pyproject 測試組）

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(index), key=lambda e: list(e.path)):
        loc = "/".join(str(x) for x in err.path) or "<root>"
        fails.append(f"schema_invalid {loc}: {err.message}")

    if str(index.get("stage")) != stage:
        fails.append(f"stage_mismatch index.stage={index.get('stage')!r} 與參數 --stage {stage} 不符")

    items = index.get("items") or []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    referenced: set[str] = set()
    covered: set[tuple[str, str | None, str]] = set()

    for item in items:
        iid = item.get("id", "<no-id>")
        if iid in seen_ids:
            fails.append(f"duplicate_item_id {iid}")
        seen_ids.add(iid)

        rel = item.get("artifact_path")
        if rel:
            if rel in seen_paths:
                fails.append(f"duplicate_artifact_path {iid}: {rel}")
            seen_paths.add(rel)

        case_ids = item.get("case_ids") or []
        for cid in case_ids:
            referenced.add(cid)
            if cid not in cases_by_id:
                fails.append(f"unknown_case_id {iid} -> {cid}")
        if len(case_ids) == 1 and case_ids[0] in cases_by_id and rel:
            evidence = item.get("evidence")
            case_evidence = cases_by_id[case_ids[0]].get("evidence") or []
            if evidence not in case_evidence:
                fails.append(f"evidence_not_in_case {iid} -> {evidence!r}（該案例 evidence：{case_evidence}）")
            elif not _path_matches_evidence(rel, evidence):
                fails.append(f"artifact_name_mismatch {iid}: {rel} 不對應 evidence {evidence}")
            else:
                covered.add((case_ids[0], item.get("platform"), evidence))

        status = item.get("status")
        if status == "present":
            if not rel or not _safe_relpath(rel):
                fails.append(f"bad_artifact_path {iid}: {rel!r}")
                continue
            target = root / rel
            if not target.is_file():
                fails.append(f"artifact_missing {iid}: {rel}")
                continue
            digest = _sha256(target)
            if digest != item.get("sha256"):
                fails.append(f"sha256_mismatch {iid}: 索引 {item.get('sha256')} 實際 {digest}")
        elif status in ("missing", "blocked_owner"):
            blocked.append(f"{status} {iid}")
        elif rel and not _safe_relpath(rel):
            fails.append(f"bad_artifact_path {iid}: {rel!r}")

    if stage == "0":
        for cid, case in sorted(cases_by_id.items()):
            if case.get("stage0_required") is not True:
                continue
            if cid not in referenced:
                fails.append(f"uncovered_stage0_case {cid}")
            for plat in _required_platforms(case):
                for ev in case.get("evidence") or []:
                    if (cid, plat, ev) not in covered:
                        fails.append(f"uncovered_evidence {cid} {plat} {ev}")
    for req in sorted(REQUIRED_ITEM_IDS_BY_STAGE.get(stage, frozenset()) - seen_ids):
        fails.append(f"missing_required_item {req}")

    for line in blocked:
        print(f"[evidence-index] BLOCKED {line}")
    for line in fails:
        print(f"[evidence-index] FAIL {line}", file=sys.stderr)

    print(f"[evidence-index] stage={stage} items={len(items)} blocked_or_missing={len(blocked)} fail={len(fails)}")
    if fails:
        return EXIT_FAIL
    if blocked:
        print("[evidence-index] BLOCKED：仍有 missing／blocked_owner 項目，Stage 出口不得視為通過", file=sys.stderr)
        return EXIT_BLOCKED
    return EXIT_OK


def _configure_stdio() -> None:
    """Windows 非 UTF-8 locale 下輸出中文訊息會擲 UnicodeEncodeError（P2-T12 審查修正）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Stage 出口證據索引校驗（§15.12）")
    parser.add_argument("--stage", required=True, help="stage 代號，例如 0、1a、1b、2、3")
    parser.add_argument("--root", type=pathlib.Path, default=DEFAULT_ROOT, help="證據根目錄（預設為 repo 根）")
    parser.add_argument("--index", type=pathlib.Path, default=None, help="索引檔路徑（預設 <root>/tests/acceptance/）")
    parser.add_argument("--cases", type=pathlib.Path, default=None, help="cases.yaml 路徑（預設 <root>/tests/acceptance/）")
    args = parser.parse_args(argv)
    if not STAGE_RE.match(args.stage):
        print(f"[evidence-index] 不合法的 stage：{args.stage!r}", file=sys.stderr)
        return EXIT_USAGE
    return check(args.stage, args.root, index_path=args.index, cases_path=args.cases)


if __name__ == "__main__":
    sys.exit(main())
