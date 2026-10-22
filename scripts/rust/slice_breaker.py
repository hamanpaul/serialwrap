#!/usr/bin/env python3
"""承接率斷路器判定器（P5-T0b，§12.2「校準與斷路器」、OD-02、OD-13）。

介面與 09-slice24-transfer-flash.md 斷路器段相同（本檔為首個建立者，09／07／08 沿用、不重建；分母由
`freeze_slice_cases.py` 凍結）：
- `acceptance_rate(lock, results, *, set_name, required_case_ids=None)`：有效 PASS 的必要案例數 ÷ 凍結分母。
  同一案例的全部 verdict 都必須為 `PASS` 才計入；SKIP、FAIL、EVIDENCE_MISSING、INSUFFICIENT_EVIDENCE 一律不計
  （§11.3 不得以 SKIP 取得全綠；證據不足永不為 PASS）。呼叫端應傳入凍結檔的 `required_case_ids`，
  分母不得隨後續 add-only 擴充的 lock 改變。
- `evaluate(freeze, snapshots, *, today, stage2_estimate_weeks)`：觀測窗口起訖固定於切片開始（freeze.window，
  §12.2「觀測窗口起訖於切片開始時固定」），窗口 `weeks` 週後才判定。窗口內快照的 rate 由第一筆到最後一筆的累計增加
  < 10 個百分點即 tripped（恰為 10 pp 不觸發，以 9 位小數捨入比較）。窗口已結束時，若有效快照不足 2 筆，
  或快照未涵蓋窗口首尾（首筆晚於窗口起點 7 日以上，或末筆早於窗口終點 7 日以上），即 insufficient_evidence；
  證據不足不得放行。od02_resubmit：Stage 2 起算至今已耗週數 + 剩餘預估週數 > 24；剩餘預估須為有限且 >= 0 的數值
  （NaN 與負數會使上限無聲失效，ValueError）。快照的 rate 必須在 0～1（分數，非百分比）。
- `main(argv)` 退出碼：
  - 0：未觸發、證據足夠、未超過 OD-02 上限。
  - 1：tripped 或 od02_resubmit → 停止擴大部署、退守已驗收的 Stage 1 artifact、提請擁有者審查／重提 OD-02。
  - 2：輸入錯誤（凍結檔／快照檔不存在或格式錯誤、rate 不在 0～1、日期非 YYYY-MM-DD、快照分母與凍結分母不一致）。
  - 3：窗口已結束但證據不足（insufficient_evidence）；不得視為「未觸發」而放行。

快照格式：`{"date": "YYYY-MM-DD", "rate": 0.0~1.0, "results_sha256": "..."}`（每週一筆，ndjson）。
選填 `passed`、`denominator`：若附上，`denominator` 須等於凍結分母，且 `passed` 不得超出分母、`rate` 須等於
`passed ÷ denominator`（不符即 exit 2）。`results_sha256` 目前不強制，缺欄位的舊格式快照仍可判定。
結果格式：`{"case_id": "...", "verdict": "PASS|SKIP|EVIDENCE_MISSING|..."}`。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import re
import sys

TRIP_DELTA_PP = 10.0
OD02_LIMIT_WEEKS = 24.0
SNAPSHOT_SLACK_DAYS = 7
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


def _checked_estimate(value: object) -> float:
    """Stage 2 剩餘預估週數須為有限且 >= 0 的數值；NaN 會使 OD-02 比較恆為假，負數會抵銷已耗週數。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"stage2_estimate_weeks 必須為數值：{value!r}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"stage2_estimate_weeks 超出可表示範圍：{value!r}") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"stage2_estimate_weeks 必須為有限且 >= 0 的數值：{value!r}")
    return number


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
    """依凍結窗口與每週快照判定斷路器；回傳 tripped／insufficient_evidence／reason／window_delta_pp／
    elapsed_weeks／stage2_estimate_weeks／od02_resubmit。

    所有快照（含窗口外）都會驗證日期與 rate 範圍；違反即 ValueError。
    """
    estimate = _checked_estimate(stage2_estimate_weeks)
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
    slack = dt.timedelta(days=SNAPSHOT_SLACK_DAYS)
    gap = bool(rows) and (rows[0][0] > start + slack or rows[-1][0] < end - slack)
    tripped = window_complete and len(rows) >= 2 and round(delta_pp, 9) < TRIP_DELTA_PP
    insufficient = window_complete and not tripped and (len(rows) < 2 or gap)
    reason = None
    if tripped:
        reason = f"觀測窗口 {start.isoformat()}～{end.isoformat()} 承接率累計增加 {delta_pp:.2f} pp，低於 {TRIP_DELTA_PP:g} pp"
    elif insufficient and len(rows) < 2:
        reason = (f"觀測窗口 {start.isoformat()}～{end.isoformat()} 已結束，但有效快照僅 {len(rows)} 筆"
                  "（至少需要 2 筆）")
    elif insufficient:
        reason = (f"觀測窗口 {start.isoformat()}～{end.isoformat()} 已結束，但快照未涵蓋窗口首尾"
                  f"（首筆 {rows[0][0].isoformat()}、末筆 {rows[-1][0].isoformat()}，容許 {SNAPSHOT_SLACK_DAYS} 日）")
    elapsed_weeks = (today_d - _parse_date(freeze["stage2_started_at"])).days / 7.0
    od02 = elapsed_weeks + estimate > OD02_LIMIT_WEEKS
    return {
        "tripped": tripped,
        "insufficient_evidence": insufficient,
        "reason": reason,
        "window_delta_pp": round(delta_pp, 6),
        "elapsed_weeks": round(elapsed_weeks, 6),
        "stage2_estimate_weeks": estimate,
        "od02_resubmit": od02,
    }


def _read_ndjson(path: pathlib.Path) -> list[dict]:
    """讀取 ndjson；檔案不存在即丟出 OSError，不得視為零筆快照（路徑錯誤不能被當成「未觸發」）。"""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _check_snapshot_denominators(snapshots: list[dict], freeze: dict) -> None:
    """快照若附上 denominator（選填），必須等於凍結分母；若再附上 passed，rate 須等於 passed ÷ denominator。

    用於防止以縮小的分母或手填的 rate 通過判定；未附欄位的舊格式快照不受影響。
    """
    frozen = freeze.get("denominator")
    for snap in snapshots:
        if "denominator" not in snap:
            continue
        denom = snap["denominator"]
        if isinstance(denom, bool) or not isinstance(denom, int) or denom <= 0 or denom != frozen:
            raise ValueError(f"快照 {snap.get('date')} 的 denominator={denom!r} 與凍結分母 {frozen!r} 不一致")
        if "passed" not in snap:
            continue
        passed = snap["passed"]
        if isinstance(passed, bool) or not isinstance(passed, int) or not 0 <= passed <= denom:
            raise ValueError(f"快照 {snap.get('date')} 的 passed={passed!r} 超出 0～{denom}")
        if abs(float(snap["rate"]) - passed / denom) >= 1e-9:
            raise ValueError(f"快照 {snap.get('date')} 的 rate={snap['rate']} 與 passed÷denominator={passed / denom:.6f} 不符")


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
        _check_snapshot_denominators(snapshots, freeze)
        today = args.today or dt.datetime.now(dt.timezone.utc).date().isoformat()
        out = evaluate(freeze, snapshots, today=today, stage2_estimate_weeks=args.stage2_estimate_weeks)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
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


def _force_utf8_stdio() -> None:
    """固定 stdout／stderr 為 UTF-8、LF：Windows 預設 code page（如 cp1252）無法編碼繁中輸出時會以 exit 1 結束，
    與本腳本的 1（tripped）、3（證據不足）混淆。僅於 __main__ 呼叫，避免改寫行程內（pytest）的串流。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", newline="\n")


if __name__ == "__main__":
    _force_utf8_stdio()
    sys.exit(main())
