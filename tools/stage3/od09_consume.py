"""OD-09 凍結結果的 Stage 3 消費端（P9-T15，唯讀）。

延後項（`resources`、`min_effective_load`、`comparison_uncertainty`）的唯一寫入者是 P2 Task 16
`realhw/acceptance/od09_freeze.py`。本模組只讀取其凍結輸出，不寫入 acceptance、候選開跑 ledger 或凍結報告：

- `derive_freeze_report`：把 P2 `freeze --out` 報告轉為 Task 14B `od09-freeze` kind 的 `Derived`；
- `earliest_candidate_start`：取 P2 候選開跑 ledger 與本索引 `candidate_longrun` entry 中最早的開跑時刻；
- `check_main`：`python3 -m tools.stage3 od09 check` 的唯讀委派，只呼叫 `od09_freeze.check_frozen_before`。

本模組只 import P2 的唯讀符號與 `check_frozen_before`，不得 import 寫入端。缺漏的輸入（ledger 不存在、
索引無 `entries`）一律拒收，不視為「無候選」（fail closed，與 P2 `freeze` 的 ledger 缺漏處理一致）。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from realhw.acceptance import od09_freeze
from tools.stage3 import jsonio
from tools.stage3.derived import Derived
from tools.stage3.reason_codes import HarnessError

# 索引與 ledger 皆無候選開跑時的替代時刻：只驗「已凍結且欄位無 null」（Task 14B 規格）。
NO_CANDIDATE_TS: str = "9999-12-31T00:00:00+00:00"
_CHECK_PREFIX: tuple[str, str] = ("od09", "check")


def _invalid(detail: str) -> HarnessError:
    return HarnessError("HARNESS_EVIDENCE_INGEST_INVALID", detail)


def _parse_started(row: Any, *, where: str) -> datetime:
    """解析一筆紀錄的 `started_at`；缺漏、非字串或 naive 時間一律視為無效（fail closed）。"""
    if not isinstance(row, Mapping):
        raise _invalid(f"{where} 不是 JSON 物件")
    started = row.get("started_at")
    if not isinstance(started, str):
        raise _invalid(f"{where} 缺少 started_at")
    try:
        return jsonio.parse_iso(started)
    except ValueError as exc:
        raise _invalid(f"{where} started_at 無法解析：{exc}") from exc


def _ledger_starts(ledger_path: Path) -> list[datetime]:
    """讀取 P2 候選開跑 ledger 每一行的 `started_at`。

    ledger 是受追蹤的固定檔（P2 `freeze` 亦視缺漏為錯誤），因此不存在、為目錄、權限不足或非 UTF-8 一律拒收，
    不得當成「無候選」而略過時序守門。只以 LF 切行：`ensure_ascii=False` 的 JSON 可能含 U+2028／U+2029／U+0085，
    `str.splitlines()` 會把它們誤切成合法紀錄的兩半。
    """
    path = Path(ledger_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _invalid(f"候選開跑 ledger 無法讀取（不存在、非一般檔或權限不足），無法核對時序：{path.name}：{exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid(f"候選開跑 ledger 無法以 UTF-8 讀取：{path.name}：{exc}") from exc
    starts: list[datetime] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise _invalid(f"候選開跑 ledger 第 {number} 行無法解析：{exc}") from exc
        starts.append(_parse_started(row, where=f"候選開跑 ledger 第 {number} 行"))
    return starts


def _index_candidate_starts(index: Mapping[str, Any]) -> list[datetime]:
    """取出索引中屬於候選長跑項目（與 P2 `CANDIDATE_LONGRUN_ITEMS` 同一集合）的 entry 之 `started_at`。"""
    if not isinstance(index, Mapping):
        raise _invalid("證據索引頂層必須是物件")
    entries = index.get("entries")
    if not isinstance(entries, list):
        raise _invalid("證據索引缺少 entries 陣列（不得視為無候選）")
    starts: list[datetime] = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            raise _invalid(f"證據索引第 {position} 筆 entry 不是物件")
        item_id = entry.get("item_id")
        if not isinstance(item_id, str) or item_id not in od09_freeze.CANDIDATE_LONGRUN_ITEMS:
            continue
        starts.append(_parse_started(entry, where=f"證據索引 entry {entry.get('entry_id', position)}"))
    return starts


def earliest_candidate_start(index: dict, *, ledger_path: Path = od09_freeze.LEDGER_PATH) -> str | None:
    """回傳 P2 候選開跑 ledger 與索引候選 entry 中最早的 `started_at`；皆無則 None。

    以時刻（而非字串）比較取最小值；回傳值保留來源的時區偏移（例如 `+08:00` 維持原樣），`Z` 已先轉為 `+00:00`。
    ledger 缺漏或無法讀取、不可解析的行、索引缺 `entries`、或候選 entry 的 `started_at` 無效 → `HARNESS_EVIDENCE_INGEST_INVALID`。
    """
    starts = _ledger_starts(Path(ledger_path)) + _index_candidate_starts(index)
    if not starts:
        return None
    return min(starts).isoformat()


def _load_report(report_path: Path) -> dict[str, Any]:
    """讀取 P2 `freeze --out` 報告並檢查 Task 15 會用到的必要鍵。"""
    try:
        doc = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _invalid(f"OD-09 凍結報告無法解析：{report_path.name}：{exc}") from exc
    if not isinstance(doc, dict):
        raise _invalid("OD-09 凍結報告頂層必須是物件")
    if not isinstance(doc.get("acceptance_sha256"), str):
        raise _invalid("OD-09 凍結報告缺少 acceptance_sha256")
    if not isinstance(doc.get("frozen_at"), str):
        raise _invalid("OD-09 凍結報告缺少 frozen_at")
    try:
        jsonio.parse_iso(doc["frozen_at"])
    except ValueError as exc:
        raise _invalid(f"OD-09 凍結報告 frozen_at 無法解析：{exc}") from exc
    return doc


def _load_live_frozen_acceptance(acceptance_path: Path) -> dict[str, Any] | None:
    """三個延後項於現行 acceptance 皆為 `status: frozen` 且帶 `frozen_at` 時回傳其內容；否則回傳 None。

    None 代表尚未全部凍結或門檻檔無法解析；此時 `check_frozen_before` 必得出違規（FAIL），不需再核對報告來源。
    """
    try:
        content = yaml.safe_load(Path(acceptance_path).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(content, dict) or not isinstance(content.get("thresholds"), Mapping):
        return None
    for key in od09_freeze.DEFERRED_KEYS:
        item = content["thresholds"].get(key)
        if (not isinstance(item, Mapping) or item.get("status") != "frozen"
                or not isinstance(item.get("frozen_at"), str)):
            return None
    return content


def _same_instant(report_value: str, live_value: Any) -> bool:
    """兩個 ISO 時刻是否指向同一瞬間（`Z` 與 `+00:00` 視為相同）；無法解析或 naive 一律不相等。"""
    if not isinstance(live_value, str):
        return False
    try:
        return jsonio.parse_iso(report_value) == jsonio.parse_iso(live_value)
    except ValueError:
        return False


def _check_report_binds_acceptance(report: Mapping[str, Any], acceptance_path: Path) -> None:
    """三個延後項皆已凍結時，凍結報告的來源欄位必須與現行 acceptance 一致（fail closed）。

    `acceptance_sha256` 只能證明「報告產生後 acceptance 未再被改動」，無法證明報告本身來自該次凍結：
    附上正確 sha 的偽造報告可任意填入 `frozen_at`，進而寫入證據 entry 的 `started_at`。因此另核對
    `revision`、`thresholds` 的鍵集合，以及報告 `frozen_at` 與每個延後項的凍結時刻。

    `thresholds[k]` 的數值不在此比對：本函式只負責報告來源；門檻數值是否完整交由 `check_frozen_before` 判定，
    其中「數值被改為 null 且 sha 已重算」的情境必須得出 FAIL（缺漏清單），不得在此被提前拒收為 INVALID。
    """
    content = _load_live_frozen_acceptance(acceptance_path)
    if content is None:
        return
    if report.get("revision") != content.get("revision"):
        raise _invalid("OD-09 凍結報告 revision 與現行 acceptance 不符")
    thresholds = report.get("thresholds")
    if not isinstance(thresholds, Mapping) or set(thresholds) != set(od09_freeze.DEFERRED_KEYS):
        raise _invalid("OD-09 凍結報告 thresholds 必須恰為三個延後項")
    for key in od09_freeze.DEFERRED_KEYS:
        if not _same_instant(report["frozen_at"], content["thresholds"][key].get("frozen_at")):
            raise _invalid(f"OD-09 凍結報告 frozen_at 與現行 acceptance 的 {key} 凍結時刻不符")


def derive_freeze_report(report_path: Path, *, acceptance_path: Path, index: dict,
                         ledger_path: Path = od09_freeze.LEDGER_PATH) -> Derived:
    """Task 14B `od09-freeze` kind 的唯一實作。

    - 報告 `acceptance_sha256` 不等於現行 acceptance 的 sha256（凍結後又被改動）→ `HARNESS_EVIDENCE_INGEST_INVALID`；
    - 三個延後項皆已凍結時，報告的 `revision`、`thresholds` 鍵集合與 `frozen_at` 必須與現行 acceptance 一致，否則同樣 INVALID；
    - `ts = earliest_candidate_start(...)`，無候選時為 `NO_CANDIDATE_TS`；
    - `od09_freeze.check_frozen_before(acceptance, ts)` 為空 → PASS，否則 FAIL；違規清單放入 `detail`，`note` 只記數量。
      Task 14B `ingest` 會把 `detail` 併入 entry note，因此清單只在那一處出現一次（避免 note 內重複）。
    """
    report_path = Path(report_path)
    acceptance_path = Path(acceptance_path)
    report = _load_report(report_path)
    try:
        actual_sha = jsonio.sha256_file(acceptance_path)
    except OSError as exc:
        raise _invalid(f"acceptance 無法讀取：{acceptance_path.name}：{exc}") from exc
    if report["acceptance_sha256"] != actual_sha:
        raise _invalid("acceptance 於凍結報告產生後被改動（acceptance_sha256 不符）")
    _check_report_binds_acceptance(report, acceptance_path)

    ts = earliest_candidate_start(index, ledger_path=ledger_path) or NO_CANDIDATE_TS
    violations = od09_freeze.check_frozen_before(acceptance_path, ts)

    scope = ("OD-09 凍結核對（無候選長跑開跑紀錄，僅驗證已凍結且欄位完整）" if ts == NO_CANDIDATE_TS
             else f"OD-09 凍結核對於 {ts}")
    verdict = f"：違規 {len(violations)} 項" if violations else "：無違規"
    return Derived(
        result="PASS" if not violations else "FAIL",
        artifact_id=None,
        contract_revision=None,
        tool_revision=None,
        case_list_sha256=None,
        workload_sha256=None,
        bundle_tree_sha256=None,
        runtime_tree_sha256=None,
        xfail_cases=(),
        started_at=report["frozen_at"],
        files=(report_path,),
        ledger_through=None,
        ledger_records=None,
        l1_set_sha256=None,
        l1_verdict=None,
        note=scope + verdict,
        detail=tuple(violations),
    )


def add_check_arguments(parser: argparse.ArgumentParser) -> None:
    """`od09 check` 的引數定義，供 `check_main` 自行解析使用。`tools.stage3` 主入口不共用此函式：`__main__._add_od09_check_arguments` 為同形的本地複本（避免 `build_parser()` 匯入本模組），由 `tests/test_stage3_cli.py::test_od09_check_arguments_match_consumer` 核對兩者一致。"""
    parser.add_argument("--acceptance", required=True, type=Path)
    parser.add_argument("--run-started-at", required=True)


def check_main(argv: Sequence[str]) -> int:
    """`od09 check --acceptance PATH --run-started-at ISO`：唯讀，只呼叫 `od09_freeze.check_frozen_before`。

    argv 可為只含選項的 token（`["--acceptance", ...]`），也可為含 `["od09", "check"]` 前綴的完整子命令
    （`tools.stage3` 主入口傳入時）；兩者等價。回傳 0 表示 run 起點前三個延後項皆已凍結且欄位完整；1 表示有違規。
    結果以一行 JSON 印到 stdout。`--run-started-at` 先正規化為 `+00:00` 形式再交給 P2（P2 於 Python 3.10 不接受 `Z`）。
    時間不是含時區的 ISO 8601 時，拋出 `ValueError`，由主入口轉為 `HARNESS_CLI_ERROR`（exit 2）；不得用 `parser.error`，
    否則 `SystemExit` 會穿出 `tools.stage3.main`，不符合 CLI JSON 契約。
    """
    tokens = list(argv)
    if tuple(tokens[:2]) == _CHECK_PREFIX:
        tokens = tokens[2:]
    parser = argparse.ArgumentParser(prog="python3 -m tools.stage3 od09 check")
    add_check_arguments(parser)
    args = parser.parse_args(tokens)
    try:
        started = jsonio.parse_iso(args.run_started_at).isoformat()
    except ValueError as exc:
        raise ValueError("--run-started-at 必須是含時區的 ISO 8601 時間") from exc
    try:
        violations = od09_freeze.check_frozen_before(args.acceptance, started)
    except OSError as exc:
        # 門檻檔不存在或不可讀時視同未凍結（fail closed），維持 0／1 的結果契約。
        violations = [f"acceptance unreadable: {exc}"]
    print(jsonio.dumps_compact({"result": "PASS" if not violations else "FAIL", "violations": violations}))
    return 0 if not violations else 1
