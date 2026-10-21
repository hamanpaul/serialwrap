#!/usr/bin/env python3
"""凍結切片必要案例分母與觀測窗口（P5-T0b，§11.3、§12.2、OD-02、OD-13）。

讀取 P2 案例鎖 `tests/acceptance/cases.lock.json` 的 `sets["slice_<X>"]` 作為分母，寫出斷路器凍結檔
`tests/acceptance/breaker/slice_<X>.json`（DISK_STABLE：排序鍵、緊湊分隔符）。
凍結檔一經寫出即不可覆寫（分母不得事後改）；已存在時以 exit 1 拒絕。
寫入採「暫存檔 → os.link」發佈：目標已存在或寫入中途失敗時都不會留下半寫的凍結檔。

分母指紋 `denominator_sha256` 為 required 案例的 `case_attrs`（依凍結時原序）canonical JSON 的 sha256。
lock 為 add-only，後續切片只會新增案例，既有案例屬性不得改動，因此指紋在 P6～P8 重算 lock 後仍成立；
`cases_lock_sha256` 僅作為 provenance 記錄。

Stage 2 起算日（OD-02 的 24 週上限起點）全案只有一個：Slice 2.1 的 `stage2_started_at`。
2.1 以開工日（`--date`）為起算日；2.2～2.4 必須沿用 `breaker/slice_2.1.json` 的值（缺檔、不一致或晚於開工日 → exit 2）。

用法：
  python3 scripts/rust/freeze_slice_cases.py --slice 2.1 --date YYYY-MM-DD [--out <路徑>]
  （`--frozen-at` 為 `--date` 的別名，供 09 計畫沿用。日期只接受 YYYY-MM-DD。）

退出碼：0 寫入成功；1 凍結檔已存在（拒絕覆寫）；2 輸入錯誤（日期格式、版本不一致、起算日缺漏或衝突等）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
ACC = ROOT / "tests" / "acceptance"
BREAKER_DIR = ACC / "breaker"
WINDOW_WEEKS = 3
SLICES = ("2.1", "2.2", "2.3", "2.4")
ORIGIN_SLICE = "2.1"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(value: str) -> str:
    """驗證並正規化為 YYYY-MM-DD；拒絕 20261021、2026-W43-3 等 fromisoformat 變體。"""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError(f"日期必須為 YYYY-MM-DD：{value!r}")
    return dt.date.fromisoformat(value).isoformat()


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def denominator_sha256(lock: dict, required: list[str]) -> str:
    """分母屬性指紋：required 每個案例的 case_attrs（依凍結原序）之 canonical JSON sha256。"""
    canon = json.dumps([[cid, lock["case_attrs"][cid]] for cid in required],
                       ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def resolve_stage2_started_at(slice_id: str, date: str, requested: str | None,
                              breaker_dir: pathlib.Path) -> str:
    """決定 Stage 2 起算日：2.1 以開工日為預設；其餘切片必須沿用 2.1 凍結檔的值。"""
    if slice_id == ORIGIN_SLICE:
        started = requested or date
        if started > date:
            raise ValueError(f"Stage 2 起算日 {started} 晚於開工日 {date}")
        return started
    origin_path = breaker_dir / f"slice_{ORIGIN_SLICE}.json"
    if not origin_path.exists():
        raise ValueError(f"缺少 Slice {ORIGIN_SLICE} 凍結檔 {origin_path}，無法取得 Stage 2 起算日（OD-02 單一起點）")
    origin = json.loads(origin_path.read_text(encoding="utf-8"))["stage2_started_at"]
    if requested is not None and requested != origin:
        raise ValueError(f"Slice {slice_id} 的 Stage 2 起算日必須沿用 Slice {ORIGIN_SLICE} 的 {origin}，不得改為 {requested}")
    if origin > date:
        raise ValueError(f"Stage 2 起算日 {origin} 晚於開工日 {date}")
    return origin


def build_freeze(*, slice_id: str, date: str, stage2_started_at: str,
                 lock_path: pathlib.Path, yaml_path: pathlib.Path) -> dict:
    """依鎖檔與 cases.yaml 組出凍結內容；鎖檔與 cases.yaml 版本不一致時拒絕（ValueError）。"""
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    cases_doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    revision = cases_doc["revision"]
    if lock["cases_revision"] != revision:
        raise ValueError(f"cases.lock.json（{lock['cases_revision']}）與 cases.yaml（{revision}）版本不一致")
    set_name = f"slice_{slice_id}"
    required = list(lock["sets"][set_name])
    return {
        "schema_version": 1,
        "slice": slice_id,
        "frozen_at": date,
        "window": {"start": date, "weeks": WINDOW_WEEKS},
        "cases_revision": revision,
        "cases_lock_sha256": _sha256(lock_path),
        "cases_yaml_sha256": _sha256(yaml_path),
        "denominator_set": set_name,
        "required_case_ids": required,
        "denominator": len(required),
        "denominator_sha256": denominator_sha256(lock, required),
        "stage2_started_at": stage2_started_at,
    }


def write_once(path: pathlib.Path, doc: dict) -> None:
    """以暫存檔寫入後用 os.link 發佈到 path：目標已存在即 FileExistsError（不覆寫）；
    寫入中途失敗時暫存檔會被移除，不會留下半寫的凍結檔。"""
    text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="凍結 Slice 2.x 必要案例分母與觀測窗口（拒絕覆寫）")
    parser.add_argument("--slice", required=True, choices=SLICES, help="切片編號，例如 2.1")
    parser.add_argument("--date", "--frozen-at", dest="date", required=True,
                        help="開工日 YYYY-MM-DD（frozen_at 與窗口起點；亦可用 --frozen-at）")
    parser.add_argument("--stage2-started-at", default=None,
                        help="Stage 2 起算日（2.1 預設為開工日；2.2～2.4 必須等於 Slice 2.1 凍結檔的值，可省略）")
    parser.add_argument("--out", type=pathlib.Path, default=None,
                        help="輸出路徑（預設 tests/acceptance/breaker/slice_<X>.json）")
    args = parser.parse_args(argv)

    try:
        date = _parse_date(args.date)
        requested = _parse_date(args.stage2_started_at) if args.stage2_started_at else None
        started = resolve_stage2_started_at(args.slice, date, requested, BREAKER_DIR)
    except (ValueError, OSError, KeyError) as exc:
        print(f"[freeze] 輸入錯誤：{exc}", file=sys.stderr)
        return 2

    out = args.out or BREAKER_DIR / f"slice_{args.slice}.json"
    try:
        doc = build_freeze(slice_id=args.slice, date=date, stage2_started_at=started,
                           lock_path=ACC / "cases.lock.json", yaml_path=ACC / "cases.yaml")
    except (ValueError, OSError, KeyError) as exc:
        print(f"[freeze] 輸入錯誤：{exc}", file=sys.stderr)
        return 2
    try:
        write_once(out, doc)
    except FileExistsError:
        print(f"[freeze] 凍結檔已存在，拒絕覆寫（分母不得事後改）：{out}", file=sys.stderr)
        return 1
    print(f"[freeze] 已寫入 {out}（denominator={doc['denominator']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
