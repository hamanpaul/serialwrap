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
  l1_set     （P2-T13，R5-F3）項目帶 `l1_set: {set_id, platforms}` 且 status 為 present 時，artifact_path 為目錄：
             每個平台須有 `<platform>/resource-report.json`（l1-results.v1）與同目錄 `junit.xml`，缺檔為 BLOCKED；
             sha256 為目錄內各平台兩檔摘要的映射之 sha256（`l1_artifact_digest`），不符為 FAIL；
             報告以 tests/acceptance/l1_results.schema.json（jsonschema）驗證，不符為 BLOCKED（INVALID）；
             逐平台以 tests/acceptance/l1_judge.py 的 judge() 判定（selector 取自 l1_required.lock.json）：
             PASS 才算通過；FAIL → 1；INSUFFICIENT／INVALID → BLOCKED（5）並列出 reasons。
             `l1_set` 與 l1_required.lock.json 綁定（P2-T13 審查修正）：set_id 必須存在於鎖檔，platforms 必須與鎖檔
             該 set 的平台集合完全相同（少一個平台即 FAIL）；L1_SET_ITEMS 所列項目（S0-L1-BC、S0-L1-WIN-PYTEST）
             缺 l1_set 即 FAIL，不得退回只核對 sha256。

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
import importlib.util
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET
from typing import Any

import yaml

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parents[1]
SCHEMA_PATH = HERE / "evidence_index.schema.json"
L1_JUDGE_PATH = HERE / "l1_judge.py"
DEFAULT_L1_LOCK = HERE / "l1_required.lock.json"
L1_REPORT_NAME = "resource-report.json"
L1_JUNIT_NAME = "junit.xml"
L1_RESULTS_SCHEMA_PATH = HERE / "l1_results.schema.json"
# Stage 0 的 L1 項目必須帶 l1_set，且 set_id 固定（P2-T13 審查修正；不得以移除 l1_set 退回 sha256-only 判定）。
L1_SET_ITEMS = {"S0-L1-BC": "s0_bc", "S0-L1-WIN-PYTEST": "s0_win_pytest"}

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


def _load_l1_judge() -> Any:
    """l1_judge 為全計畫唯一的 L1 判定實作，依路徑載入（不得在此另寫判定）。"""
    spec = importlib.util.spec_from_file_location("sw_l1_judge", L1_JUDGE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {L1_JUDGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def l1_artifact_digest(base: pathlib.Path, platforms: list[str]) -> str:
    """l1_set 項目的 sha256：各平台 `resource-report.json` 與 `junit.xml` 摘要映射（排序 JSON）之 sha256。"""
    table = {
        plat: {name: _sha256(base / plat / name) for name in (L1_REPORT_NAME, L1_JUNIT_NAME)}
        for plat in sorted(platforms)
    }
    return hashlib.sha256(json.dumps(table, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _junit_counts(path: pathlib.Path) -> dict[str, int]:
    """junit.xml 的 testcase 層計數（支援 testsuites 包裝）。

    不讀 testsuite 的 tests／failures 屬性：pytest 9 的 subtests 會計入屬性（tests=6 對應 3 個 testcase），
    但不產生對應的 testcase 元素，屬性計數會與 l1-results 列數永遠不符（P2-T13 審查修正）。
    failures／errors／skipped 依 testcase 子元素（failure／error／skipped）計數。
    """
    root = ET.parse(path).getroot()
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for case in root.iter("testcase"):
        totals["tests"] += 1
        children = {child.tag for child in case}
        if "failure" in children:
            totals["failures"] += 1
        if "error" in children:
            totals["errors"] += 1
        if "skipped" in children:
            totals["skipped"] += 1
    return totals


def _check_l1_pin(iid: str, item: dict[str, Any], lock_path: pathlib.Path, fails: list[str]) -> dict[str, Any] | None:
    """l1_set 與 L1 鎖綁定：set_id 存在於鎖檔、platforms 與鎖檔該 set 的平台集合完全相同、L1_SET_ITEMS 必帶。"""
    expected = L1_SET_ITEMS.get(iid)
    l1 = item.get("l1_set")
    if not l1:
        if expected is not None:
            fails.append(f"l1_set_missing {iid}: 須帶 l1_set {{set_id: {expected}}}")
        return None
    set_id = l1.get("set_id")
    if expected is not None and set_id != expected:
        fails.append(f"l1_set_mismatch {iid}: set_id 應為 {expected}，實際 {set_id!r}")
    if not lock_path.is_file():
        fails.append(f"l1_lock_missing {iid}: {lock_path}")
        return None
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock_plats = set((lock.get("sets") or {}).get(set_id, {}))
    if not lock_plats:
        fails.append(f"l1_set_unknown {iid}: 鎖檔無 set {set_id!r}")
        return None
    if set(l1.get("platforms") or []) != lock_plats:
        fails.append(f"l1_platforms_mismatch {iid}: 索引 {sorted(l1.get('platforms') or [])}，鎖檔 {sorted(lock_plats)}")
        return None
    return lock


def _check_l1_item(iid: str, item: dict[str, Any], root: pathlib.Path, lock: dict[str, Any],
                   fails: list[str], blocked: list[str]) -> None:
    import jsonschema  # 測試側依賴（與 check() 相同）

    l1 = item["l1_set"]
    rel = item.get("artifact_path")
    if not rel or not _safe_relpath(rel):
        fails.append(f"bad_artifact_path {iid}: {rel!r}")
        return
    base = root / rel
    plats = list(l1["platforms"])
    missing = [f"{p}/{name}" for p in plats for name in (L1_REPORT_NAME, L1_JUNIT_NAME)
               if not (base / p / name).is_file()]
    if missing:
        blocked.append(f"l1_missing {iid}: 缺 {', '.join(missing)}（L1 證據不足）")
        return
    digest = l1_artifact_digest(base, plats)
    if digest != item.get("sha256"):
        fails.append(f"sha256_mismatch {iid}: 索引 {item.get('sha256')} 實際 {digest}")
    judge_mod = _load_l1_judge()
    results_schema = jsonschema.Draft202012Validator(json.loads(L1_RESULTS_SCHEMA_PATH.read_text(encoding="utf-8")))
    set_id = l1["set_id"]
    for plat in plats:
        try:
            report = json.loads((base / plat / L1_REPORT_NAME).read_text(encoding="utf-8"))
            junit = _junit_counts(base / plat / L1_JUNIT_NAME)
        except (OSError, ValueError, ET.ParseError) as exc:
            blocked.append(f"l1_invalid {iid} {plat}: 報告無法解析（{exc}）")
            continue
        schema_errors = sorted(results_schema.iter_errors(report), key=lambda e: list(e.path))
        if schema_errors:
            loc = "/".join(str(x) for x in schema_errors[0].path) or "<root>"
            blocked.append(f"l1_invalid {iid} {plat}: schema {loc}: {schema_errors[0].message}")
            continue
        try:
            required = judge_mod.required_for(lock, set_id, plat)
            selector = lock["sets"][set_id][plat]["selector"]
        except KeyError:
            blocked.append(f"l1_insufficient {iid} {plat}: no required set（{set_id}）")
            continue
        verdict = judge_mod.judge(report, required, platform=plat, junit=junit, selector=selector)
        if verdict["verdict"] == "PASS":
            continue
        line = f"l1_{verdict['verdict'].lower()} {iid} {plat}: " + "; ".join(verdict["reasons"])
        if verdict["verdict"] == "FAIL":
            fails.append(line)
        else:
            blocked.append(line)


def _safe_relpath(rel: str) -> bool:
    p = pathlib.PurePosixPath(rel)
    return not p.is_absolute() and ".." not in p.parts and not rel.startswith("~")


def _present_problems(item: dict[str, Any], root: pathlib.Path) -> list[str]:
    """status 為 present 的單檔項目之完整性（路徑安全、檔案存在、sha256 相符）；空清單即通過。"""
    iid = item.get("id", "<no-id>")
    rel = item.get("artifact_path")
    if not rel or not _safe_relpath(rel):
        return [f"bad_artifact_path {iid}: {rel!r}"]
    target = root / rel
    if not target.is_file():
        return [f"artifact_missing {iid}: {rel}"]
    digest = _sha256(target)
    if digest != item.get("sha256"):
        return [f"sha256_mismatch {iid}: 索引 {item.get('sha256')} 實際 {digest}"]
    return []


def verify_item(index_path: pathlib.Path, item_id: str, *, root: pathlib.Path) -> tuple[dict[str, Any] | None, list[str]]:
    """回傳索引中 `item_id` 的項目與違規清單（P2-T16）。

    違規：項目不存在（`item_missing`）、status 非 present（`not_present`）、檔案缺失、路徑不安全、sha256 不符。
    只適用單檔 present 項目；不讀取 artifact 內容，不做 CLI 的 stage 級檢查。
    """
    index = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    item = next((x for x in index.get("items") or [] if x.get("id") == item_id), None)
    if item is None:
        return None, [f"item_missing {item_id}"]
    if item.get("status") != "present":
        return item, [f"not_present {item_id}: status={item.get('status')}"]
    return item, _present_problems(item, root)


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
          cases_path: pathlib.Path | None = None, l1_lock_path: pathlib.Path | None = None) -> int:
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
        lock = _check_l1_pin(iid, item, l1_lock_path or DEFAULT_L1_LOCK, fails) if item.get("l1_set") or iid in L1_SET_ITEMS else None
        if status == "present" and item.get("l1_set"):
            if lock is not None:
                _check_l1_item(iid, item, root, lock, fails, blocked)
            continue
        if status == "present":
            fails.extend(_present_problems(item, root))
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
    parser.add_argument("--stage", default=None, help="stage 代號，例如 0、1a、1b、2、3（--l1-digest 時不需要）")
    parser.add_argument("--root", type=pathlib.Path, default=DEFAULT_ROOT, help="證據根目錄（預設為 repo 根）")
    parser.add_argument("--index", "--index-file", dest="index", type=pathlib.Path, default=None,
                        help="索引檔路徑（預設 <root>/tests/acceptance/）")
    parser.add_argument("--cases", type=pathlib.Path, default=None, help="cases.yaml 路徑（預設 <root>/tests/acceptance/）")
    parser.add_argument("--l1-lock", type=pathlib.Path, default=None,
                        help="l1_required.lock.json 路徑（預設為本檔同目錄；l1_set 判定以此為唯一分母來源）")
    parser.add_argument("--l1-digest", type=pathlib.Path, default=None,
                        help="只印出目錄 DIR 的 l1_artifact_digest（需搭配 --l1-platforms）後結束")
    parser.add_argument("--l1-platforms", default="linux,windows", help="--l1-digest 使用的平台清單（逗號分隔）")
    args = parser.parse_args(argv)
    if args.l1_digest is not None:
        platforms = [x for x in args.l1_platforms.split(",") if x]
        try:
            print(l1_artifact_digest(args.l1_digest, platforms))
        except OSError as exc:
            print(f"[evidence-index] 無法計算 l1_artifact_digest（缺檔）：{exc}", file=sys.stderr)
            return EXIT_USAGE
        return EXIT_OK
    if args.stage is None:
        print("[evidence-index] 需要 --stage", file=sys.stderr)
        return EXIT_USAGE
    if not STAGE_RE.match(args.stage):
        print(f"[evidence-index] 不合法的 stage：{args.stage!r}", file=sys.stderr)
        return EXIT_USAGE
    l1_lock = args.l1_lock or DEFAULT_L1_LOCK
    return check(args.stage, args.root, index_path=args.index, cases_path=args.cases, l1_lock_path=l1_lock)


if __name__ == "__main__":
    sys.exit(main())
