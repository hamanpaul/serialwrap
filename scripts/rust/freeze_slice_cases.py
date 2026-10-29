#!/usr/bin/env python3
"""凍結切片必要案例分母與觀測窗口（P5-T0b，§11.3、§12.2、OD-02、OD-13）。

讀取 P2 案例鎖 `tests/acceptance/cases.lock.json` 的 `sets["slice_<X>"]` 作為分母，寫出斷路器凍結檔
`tests/acceptance/breaker/slice_<X>.json`（DISK_STABLE：排序鍵、緊湊分隔符）。
分母先與 `cases.yaml` 以 P2 鎖檔工具 `regen_lock.compute_sets` 重算比對；鎖檔與 cases.yaml 不一致（鎖檔遭手改或過期）即拒絕。
凍結檔一經寫出即不可覆寫（分母不得事後改）；已存在時以 exit 1 拒絕。
寫入採「暫存檔 → os.link」發佈：目標已存在或寫入中途失敗時都不會留下半寫的凍結檔。

分母指紋 `denominator_sha256` 為 required 案例的 `case_attrs`（依凍結時原序）canonical JSON 的 sha256。
lock 為 add-only，後續切片只會新增案例，既有案例屬性不得改動，因此指紋在 P6～P8 重算 lock 後仍成立；
`cases_lock_sha256` 僅作為 provenance 記錄。
`case_fingerprints`（parity-amendments-2026-10-26 #183）為 {case_id: 該必要案例完整 YAML 映射之 canonical JSON sha256}；
revision 前進後整檔指紋 `cases_yaml_sha256` 不再可比，改由此逐案驗證必要案例內容未被改動。

Stage 2 起算日（OD-02 的 24 週上限起點）全案只有一個：Slice 2.1 的 `stage2_started_at`。
2.1 以開工日（`--date`）為起算日；2.2～2.4 必須沿用 `breaker/slice_2.1.json` 的值（缺檔、不一致或晚於開工日 → exit 2）。

用法：
  python3 scripts/rust/freeze_slice_cases.py --slice 2.1 --date YYYY-MM-DD [--out <路徑>]
  （`--frozen-at` 為 `--date` 的別名，供 09 計畫沿用。日期只接受 YYYY-MM-DD。）
  python3 scripts/rust/freeze_slice_cases.py --amend --slice 2.1 --date YYYY-MM-DD --defer <case_id>:<to>:<OD-NN> [...]
  （OD-34 起：擁有者逐名核准的延後切片。凍結檔本體不改寫，另於 `slice_<X>.amendments.json` 追加一筆修訂紀錄：
   引用的 OD、被延後的案例與目標切片、修訂當下的 lock／cases.yaml provenance 與有效分母。延後須已先以
   `regen_lock.py --add-only --allow-defer` 寫入鎖；有效分母必須恰等於鎖的 `slice_<X>` 集合，未核准的移除或新增一律拒絕。
   觀測窗口、frozen_at 與 Stage 2 起算日沿用凍結檔，不得重設。讀取方一律經 `load_effective_freeze()`。）

退出碼：0 寫入成功；1 凍結檔已存在（拒絕覆寫）；2 輸入或寫入錯誤（日期格式、版本不一致、鎖檔與 cases.yaml 不一致、
起算日缺漏或衝突、目錄不可寫、父路徑不是目錄等）。
目錄不可寫等 OS 錯誤不得以 1 結束，否則與「已存在」無法區分。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
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


_OD_RE = re.compile(r"^OD-\d+$")


class AlreadyFrozen(Exception):
    """凍結檔已存在（os.link 目標衝突）。與目錄不可寫等 OS 錯誤分開：前者退出碼 1，後者退出碼 2。"""


def _force_utf8_stdio() -> None:
    """固定 stdout／stderr 為 UTF-8、LF：Windows 預設 code page（如 cp1252）無法編碼繁中輸出時，
    寫入後的成功訊息會以 exit 1 結束，與「已存在」混淆。僅於 __main__ 呼叫，避免改寫行程內（pytest）的串流。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", newline="\n")


def _load_regen_lock():
    """以檔案路徑載入 P2 鎖檔工具 regen_lock.py，重用其 compute_sets（集合定義單一來源，不複製規則）。"""
    spec = importlib.util.spec_from_file_location("regen_lock", ACC / "regen_lock.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def case_fingerprint(case: dict) -> str:
    """單一案例完整映射（cases.yaml 的一個元素）之 canonical JSON sha256。"""
    canon = json.dumps(case, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def case_fingerprints(cases_doc: dict, required: list[str]) -> dict[str, str]:
    by_id = {c["case_id"]: c for c in cases_doc.get("cases", [])}
    return {cid: case_fingerprint(by_id[cid]) for cid in required}


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
    """依鎖檔與 cases.yaml 組出凍結內容。

    鎖檔與 cases.yaml 版本不一致、或鎖檔的集合與 cases.yaml 重算結果不一致時拒絕（ValueError）。
    write-once 的凍結檔會永久固定分母，因此不能只比對版本字串，必須比對實際集合內容。
    """
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    cases_doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    revision = cases_doc["revision"]
    if lock["cases_revision"] != revision:
        raise ValueError(f"cases.lock.json（{lock['cases_revision']}）與 cases.yaml（{revision}）版本不一致")
    set_name = f"slice_{slice_id}"
    recomputed = _load_regen_lock().compute_sets(cases_doc)[set_name]
    if list(lock["sets"][set_name]) != recomputed:
        raise ValueError(f"cases.lock.json 的 {set_name} 與 cases.yaml 重算結果不一致（鎖檔可能遭手改或過期）")
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
        "case_fingerprints": case_fingerprints(cases_doc, required),
        "stage2_started_at": stage2_started_at,
    }


def write_once(path: pathlib.Path, doc: dict) -> None:
    """以暫存檔寫入後用 os.link 發佈到 path：目標已存在即 AlreadyFrozen（不覆寫）；
    父路徑不可用或不可寫等 OS 錯誤維持原例外（OSError），由呼叫端以退出碼 2 處理；
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
        try:
            os.link(tmp, path)
        except FileExistsError as exc:
            raise AlreadyFrozen(str(path)) from exc
    finally:
        tmp.unlink(missing_ok=True)


def amendments_path(base_path: pathlib.Path) -> pathlib.Path:
    return base_path.with_name(f"{base_path.stem}.amendments.json")


def _canon_sha(obj: object) -> str:
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_effective_freeze(base_path: pathlib.Path) -> dict:
    """讀凍結檔並套用修訂紀錄（若有）得出有效凍結內容；修訂鏈不一致（本體雜湊、前一筆雜湊、分母計算、OD 編號）
    即 ValueError——不得以竄改的修訂檔縮小分母。"""
    base_bytes = base_path.read_bytes()
    base = json.loads(base_bytes.decode("utf-8"))
    eff = dict(base)
    eff["amendments"] = []
    apath = amendments_path(base_path)
    if not apath.exists():
        return eff
    entries = json.loads(apath.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{apath}：修訂檔須為非空 JSON 陣列")
    base_sha = hashlib.sha256(base_bytes).hexdigest()
    prev_sha = None
    required = list(base["required_case_ids"])
    for i, entry in enumerate(entries, start=1):
        if entry.get("amendment") != i or entry.get("base_freeze_sha256") != base_sha:
            raise ValueError(f"{apath} 第 {i} 筆：編號或凍結檔本體雜湊不符")
        if entry.get("prev_amendment_sha256") != prev_sha:
            raise ValueError(f"{apath} 第 {i} 筆：前一筆修訂雜湊不符（修訂鏈遭改動）")
        removed = entry.get("removed") or []
        if not removed or any(not _OD_RE.match(str(r.get("decision", ""))) for r in removed):
            raise ValueError(f"{apath} 第 {i} 筆：須逐名列出延後案例並引用 OD-NN")
        gone = {r["case_id"] for r in removed}
        if not gone <= set(required):
            raise ValueError(f"{apath} 第 {i} 筆：延後案例不在前一版有效分母內")
        required = [c for c in required if c not in gone]
        if entry.get("required_case_ids") != required or entry.get("denominator") != len(required):
            raise ValueError(f"{apath} 第 {i} 筆：有效分母與「前一版扣除核准延後」不符")
        prev_sha = _canon_sha(entry)
    last = entries[-1]
    for key in ("required_case_ids", "denominator", "denominator_sha256", "cases_revision",
                "cases_lock_sha256", "cases_yaml_sha256"):
        eff[key] = last[key]
    eff["amendments"] = entries
    return eff


def _parse_defer(token: str) -> dict:
    parts = token.split(":")
    if len(parts) != 3 or not all(parts) or parts[1] not in SLICES or not _OD_RE.match(parts[2]):
        raise ValueError(f"--defer 須為 case_id:<{'|'.join(SLICES)}>:OD-NN：{token!r}")
    return {"case_id": parts[0], "to_slice": parts[1], "decision": parts[2]}


def amend_freeze(*, base_path: pathlib.Path, slice_id: str, date: str, defers: list[dict],
                 lock_path: pathlib.Path, yaml_path: pathlib.Path) -> dict:
    """追加一筆延後修訂（見模組說明）；回傳新的修訂紀錄。輸入不一致即 ValueError，不寫檔。"""
    if not base_path.exists():
        raise ValueError(f"凍結檔不存在：{base_path}")
    eff = load_effective_freeze(base_path)
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    cases_doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    revision = cases_doc["revision"]
    if lock["cases_revision"] != revision:
        raise ValueError(f"cases.lock.json（{lock['cases_revision']}）與 cases.yaml（{revision}）版本不一致")
    set_name = f"slice_{slice_id}"
    recomputed = _load_regen_lock().compute_sets(cases_doc)[set_name]
    if list(lock["sets"][set_name]) != recomputed:
        raise ValueError(f"cases.lock.json 的 {set_name} 與 cases.yaml 重算結果不一致（鎖檔可能遭手改或過期）")
    if not defers:
        raise ValueError("--amend 須以 --defer 逐名列出核准延後的案例")
    lock_set = list(lock["sets"][set_name])
    gone = [c for c in eff["required_case_ids"] if c not in lock_set]
    want = sorted(d["case_id"] for d in defers)
    if sorted(gone) != want or len(set(want)) != len(want):
        raise ValueError(f"有效分母相對鎖的消失集合 {sorted(gone)} 與 --defer 核准 {want} 不符")
    for d in defers:
        attr = lock["case_attrs"].get(d["case_id"], {}).get("daemon_slice")
        if attr != d["to_slice"]:
            raise ValueError(f"{d['case_id']}：鎖的 daemon_slice={attr!r} 與 --defer 目標 {d['to_slice']} 不符")
    required = [c for c in eff["required_case_ids"] if c not in set(want)]
    if required != lock_set:
        raise ValueError(f"有效分母與鎖的 {set_name} 不符（未核准的新增或順序變動）")
    prev = eff["amendments"]
    entry = {
        "amendment": len(prev) + 1,
        "date": date,
        "decisions": sorted({d["decision"] for d in defers}),
        "removed": sorted(defers, key=lambda d: d["case_id"]),
        "base_freeze_sha256": _sha256(base_path),
        "prev_amendment_sha256": _canon_sha(prev[-1]) if prev else None,
        "cases_revision": revision,
        "cases_lock_sha256": _sha256(lock_path),
        "cases_yaml_sha256": _sha256(yaml_path),
        "required_case_ids": required,
        "denominator": len(required),
        "denominator_sha256": denominator_sha256(lock, required),
    }
    apath = amendments_path(base_path)
    text = json.dumps(prev + [entry], ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=apath.parent, prefix=f".{apath.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, apath)
    finally:
        pathlib.Path(tmp_name).unlink(missing_ok=True)
    load_effective_freeze(base_path)  # 寫後自驗
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="凍結 Slice 2.x 必要案例分母與觀測窗口（拒絕覆寫）")
    parser.add_argument("--slice", required=True, choices=SLICES, help="切片編號，例如 2.1")
    parser.add_argument("--date", "--frozen-at", dest="date", required=True,
                        help="開工日 YYYY-MM-DD（frozen_at 與窗口起點；亦可用 --frozen-at）")
    parser.add_argument("--stage2-started-at", default=None,
                        help="Stage 2 起算日（2.1 預設為開工日；2.2～2.4 必須等於 Slice 2.1 凍結檔的值，可省略）")
    parser.add_argument("--out", type=pathlib.Path, default=None,
                        help="輸出路徑（預設 tests/acceptance/breaker/slice_<X>.json）")
    parser.add_argument("--amend", action="store_true",
                        help="不改寫凍結檔，追加擁有者逐名核准的延後修訂（須搭配 --defer）")
    parser.add_argument("--defer", action="append", default=[], metavar="CASE:TO:OD-NN",
                        help="--amend：核准延後的案例、目標切片與擁有者裁決編號")
    parser.add_argument("--lock", type=pathlib.Path, default=ACC / "cases.lock.json", help="cases.lock.json 路徑")
    parser.add_argument("--cases", type=pathlib.Path, default=ACC / "cases.yaml", help="cases.yaml 路徑")
    args = parser.parse_args(argv)
    if args.amend:
        try:
            date = _parse_date(args.date)
            defers = [_parse_defer(t) for t in args.defer]
            base = args.out or BREAKER_DIR / f"slice_{args.slice}.json"
            entry = amend_freeze(base_path=base, slice_id=args.slice, date=date, defers=defers,
                                 lock_path=args.lock, yaml_path=args.cases)
        except (ValueError, OSError, KeyError) as exc:
            print(f"[freeze] 修訂拒絕：{exc}", file=sys.stderr)
            return 2
        print(f"[freeze] 已追加修訂 #{entry['amendment']}（{', '.join(entry['decisions'])}；denominator={entry['denominator']}）")
        return 0
    if args.defer:
        print("[freeze] 輸入錯誤：--defer 只用於 --amend", file=sys.stderr)
        return 2

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
                           lock_path=args.lock, yaml_path=args.cases)
    except (ValueError, OSError, KeyError) as exc:
        print(f"[freeze] 輸入錯誤：{exc}", file=sys.stderr)
        return 2
    try:
        write_once(out, doc)
    except AlreadyFrozen:
        print(f"[freeze] 凍結檔已存在，拒絕覆寫（分母不得事後改）：{out}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"[freeze] 寫入失敗（非覆寫衝突）：{out}：{exc}", file=sys.stderr)
        return 2
    print(f"[freeze] 已寫入 {out}（denominator={doc['denominator']}）")
    return 0


if __name__ == "__main__":
    _force_utf8_stdio()
    sys.exit(main())
