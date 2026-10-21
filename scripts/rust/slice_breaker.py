#!/usr/bin/env python3
"""承接率斷路器判定器（P5-T0b，§12.2「校準與斷路器」、OD-02、OD-13）。

介面與 09-slice24-transfer-flash.md 斷路器段相同（本檔為首個建立者，09／07／08 沿用、不重建；分母由
`freeze_slice_cases.py` 凍結）：
- `acceptance_rate(lock, results, *, set_name, required_case_ids=None)`：有效 PASS 的必要案例數 ÷ 凍結分母。
  同一案例的全部 verdict 都必須為 `PASS` 才計入；SKIP、FAIL、EVIDENCE_MISSING、INSUFFICIENT_EVIDENCE 一律不計
  （§11.3 不得以 SKIP 取得全綠；證據不足永不為 PASS）。呼叫端應傳入凍結檔的 `required_case_ids`，
  分母不得隨後續 add-only 擴充的 lock 改變。
- `evaluate(freeze, snapshots, *, today, stage2_estimate_weeks)`：觀測窗口起點固定於切片開始（freeze.window），
  窗口 `weeks` 週後才判定。窗口內快照的 rate 由第一筆到最後一筆的累計增加 < 10 個百分點即 tripped
  （恰為 10 pp 不觸發，以 9 位小數捨入比較）。窗口已結束但有效快照不足 2 筆 → insufficient_evidence。
  od02_resubmit：Stage 2 起算至今已耗週數 + 剩餘預估週數 > 24。快照的 rate 必須在 0～1（分數，非百分比）。
- `main(argv)` 退出碼：
  - 0：未觸發、證據足夠、未超過 OD-02 上限。
  - 1：tripped 或 od02_resubmit → 停止擴大部署、退守已驗收的 Stage 1 artifact、提請擁有者審查／重提 OD-02。
  - 2：輸入錯誤（凍結檔／快照檔不存在或格式錯誤、rate 不在 0～1、日期非 YYYY-MM-DD）。
  - 3：窗口已結束但證據不足（insufficient_evidence）；不得視為「未觸發」而放行。

快照格式：`{"date": "YYYY-MM-DD", "rate": 0.0~1.0, "results_sha256": "..."}`（每週一筆，ndjson）。
結果格式：`{"case_id": "...", "verdict": "PASS|SKIP|EVIDENCE_MISSING|..."}`。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

TRIP_DELTA_PP = 10.0
OD02_LIMIT_WEEKS = 24.0
EXIT_OK, EXIT_STOP, EXIT_INPUT, EXIT_INSUFFICIENT = 0, 1, 2, 3
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(value: object) -> dt.date:
    """只接受 YYYY-MM-DD（Python 3.11+ 的 fromisoformat 會接受 20261021 等變體，3.10 無法讀回）。"""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError(f"日期必須為 YYYY-MM-DD：{value!r}")
    return dt.date.fromisoformat(value)


def _checked_rate(snapshot: dict) -> float:
    rate = snapshot["rate"]
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise ValueError(f"快照 {snapshot.get('date')} 的 rate 不是數值：{rate!r}")
    if not 0.0 <= float(rate) <= 1.0:
        raise ValueError(f"快照 {snapshot.get('date')} 的 rate={rate} 超出 0～1（須為分數，不是百分比）")
    return float(rate)


def acceptance_rate(lock: dict, results: list[dict], *, set_name: str,
                    required_case_ids: list[str] | None = None) -> float:
    """有效 PASS 的必要案例數 ÷ 凍結分母。

    分母取 `required_case_ids`（凍結檔的 required_case_ids，呼叫端應傳入）；未提供時退回
    lock["sets"][set_name]，僅供沒有凍結檔的情境使用。同一案例的所有 verdict 皆為 `PASS` 才計入。
    """
    members = list(required_case_ids) if required_case_ids is not None else list(lock["sets"][set_name])
    if not members:
        return 0.0
    verdicts: dict[str, list[object]] = {}
    for result in results:
        verdicts.setdefault(result["case_id"], []).append(result.get("verdict"))
    passed = {cid for cid, vs in verdicts.items() if vs and all(v == "PASS" for v in vs)}
    return len(passed & set(members)) / len(members)


def evaluate(freeze: dict, snapshots: list[dict], *, today: str, stage2_estimate_weeks: float) -> dict:
    """依凍結窗口與每週快照判定斷路器；回傳 tripped／insufficient_evidence／reason／window_delta_pp／od02_resubmit。

    所有快照（含窗口外）都會驗證日期與 rate 範圍；違反即 ValueError。
    """
    today_d = _parse_date(today)
    start = _parse_date(freeze["window"]["start"])
    end = start + dt.timedelta(weeks=freeze["window"]["weeks"])
    window_complete = today_d >= end
    parsed = [(_parse_date(s["date"]), _checked_rate(s)) for s in snapshots]
    rows = sorted(
        ((d, rate) for d, rate in parsed if start <= d <= min(today_d, end)),
        key=lambda row: row[0],
    )
    delta_pp = (rows[-1][1] - rows[0][1]) * 100.0 if len(rows) >= 2 else 0.0
    insufficient = window_complete and len(rows) < 2
    tripped = window_complete and len(rows) >= 2 and round(delta_pp, 9) < TRIP_DELTA_PP
    reason = None
    if tripped:
        reason = f"觀測窗口 {start.isoformat()}～{end.isoformat()} 承接率累計增加 {delta_pp:.2f} pp，低於 {TRIP_DELTA_PP:g} pp"
    elif insufficient:
        reason = (f"觀測窗口 {start.isoformat()}～{end.isoformat()} 已結束，但有效快照僅 {len(rows)} 筆"
                  "（至少需要 2 筆）")
    elapsed_weeks = (today_d - _parse_date(freeze["stage2_started_at"])).days / 7.0
    od02 = elapsed_weeks + stage2_estimate_weeks > OD02_LIMIT_WEEKS
    return {
        "tripped": tripped,
        "insufficient_evidence": insufficient,
        "reason": reason,
        "window_delta_pp": round(delta_pp, 6),
        "od02_resubmit": od02,
    }


def _read_ndjson(path: pathlib.Path) -> list[dict]:
    """讀取 ndjson；檔案不存在即丟出 OSError，不得視為零筆快照（路徑錯誤不能被當成「未觸發」）。"""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="承接率斷路器判定（§12.2、OD-02、OD-13）")
    parser.add_argument("--freeze", type=pathlib.Path, required=True, help="斷路器凍結檔（slice_2.x.json）")
    parser.add_argument("--snapshots", type=pathlib.Path, required=True, help="每週快照 ndjson")
    parser.add_argument("--today", default=None, help="判定日 YYYY-MM-DD（預設今日 UTC）")
    parser.add_argument("--stage2-estimate-weeks", type=float, default=0.0,
                        help="Stage 2 剩餘預估週數（與已耗週數相加判定 24 週上限；未提供時以 0 計）")
    args = parser.parse_args(argv)

    try:
        freeze = json.loads(args.freeze.read_text(encoding="utf-8"))
        snapshots = _read_ndjson(args.snapshots)
        today = args.today or dt.datetime.now(dt.timezone.utc).date().isoformat()
        out = evaluate(freeze, snapshots, today=today, stage2_estimate_weeks=args.stage2_estimate_weeks)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[breaker] 輸入錯誤：{exc}", file=sys.stderr)
        return EXIT_INPUT
    print(json.dumps(out, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    if out["tripped"] or out["od02_resubmit"]:
        print("[breaker] 停止擴大部署、退守已驗收的 Stage 1 artifact，並提請擁有者審查／重提 OD-02。"
              + (f" 原因：{out['reason']}" if out["reason"] else "")
              + (" 原因：Stage 2 總日曆預估超過 24 週。" if out["od02_resubmit"] else ""),
              file=sys.stderr)
        return EXIT_STOP
    if out["insufficient_evidence"]:
        print(f"[breaker] 證據不足，不得視為未觸發；請補齊每週快照後重新判定。原因：{out['reason']}",
              file=sys.stderr)
        return EXIT_INSUFFICIENT
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
