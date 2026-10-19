"""cases.lock.json 的唯一寫入者（P2-T12，§11.3「不得在測試失敗後刪除案例、縮小分母」）。

用法：
  python3 tests/acceptance/regen_lock.py --check      # 驗證鎖檔、案例屬性快照與統計檔與 cases.yaml 一致（exit 0＝一致）
  python3 tests/acceptance/regen_lock.py --add-only   # 重算；既有 ID 從任何集合消失、被延後切片，或分母屬性被改動即 exit 1 且不寫檔，否則寫回

集合定義（字典序排序的 case_id）：
  stage0   ＝ stage0_required 為 true
  cli_1A   ＝ cli_stage == "1A"
  cli_1B   ＝ cli_stage ∈ {"1A", "1B"}
  slice_X  ＝ necessity == "required" 且 daemon_slice ≤ X（X ∈ 2.1～2.4；daemon_slice 為 null 者不入切片集合）

案例屬性快照（case_attrs，P2-T12 審查修正）：鎖定每個案例的 suite、layer、platforms、expected、necessity、
daemon_slice、cli_stage、stage0_required。只鎖 ID 集合時，把 platforms 縮成 [linux]、layer 改成 L1.5，
或把 expected 改成 XFAIL 都會無聲縮小分母；因此既有案例的任一快照欄位改動即拒絕（與刪除、延後同級，
須擁有者裁決並在 PR 說明）。新增案例不受此限。

既有 ID 若改到較晚切片，會從較早切片集合消失，因此同樣被拒絕。
統計檔 cases.counts.json 僅作進度資訊（§11.3「承接率只作進度資訊」），與鎖檔同目錄寫入。
所有輸出一律 UTF-8、LF 行尾（Windows 非 UTF-8 locale 亦然）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import yaml

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_CASES = HERE / "cases.yaml"
DEFAULT_LOCK = HERE / "cases.lock.json"
COUNTS_NAME = "cases.counts.json"
SLICES = ("2.1", "2.2", "2.3", "2.4")
ATTR_KEYS = ("suite", "layer", "platforms", "expected", "necessity", "daemon_slice", "cli_stage", "stage0_required")


def _configure_stdio() -> None:
    """Windows 非 UTF-8 locale（如 cp1252）下輸出中文訊息會擲 UnicodeEncodeError（P2-T12 審查修正）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", newline="\n")


def _slice_tuple(s: str) -> tuple[int, ...]:
    return tuple(int(p) for p in s.split("."))


def compute_sets(cases_data: dict[str, Any]) -> dict[str, list[str]]:
    """由 cases.yaml 內容計算全部鎖定集合（回傳排序後的 case_id 清單）。"""
    cases = cases_data.get("cases") or []
    sets: dict[str, list[str]] = {
        "stage0": [],
        "cli_1A": [],
        "cli_1B": [],
    }
    for s in SLICES:
        sets[f"slice_{s}"] = []
    for c in cases:
        cid = c["case_id"]
        if c.get("stage0_required") is True:
            sets["stage0"].append(cid)
        if c.get("cli_stage") == "1A":
            sets["cli_1A"].append(cid)
        if c.get("cli_stage") in ("1A", "1B"):
            sets["cli_1B"].append(cid)
        dsl = c.get("daemon_slice")
        if c.get("necessity") == "required" and dsl is not None:
            for s in SLICES:
                if _slice_tuple(dsl) <= _slice_tuple(s):
                    sets[f"slice_{s}"].append(cid)
    return {name: sorted(ids) for name, ids in sets.items()}


def compute_attrs(cases_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """每個案例的分母相關屬性快照（JSON 可序列化，platforms 排序）。"""
    out: dict[str, dict[str, Any]] = {}
    for c in cases_data.get("cases") or []:
        attrs = {k: c.get(k) for k in ATTR_KEYS}
        attrs["platforms"] = sorted(attrs["platforms"] or [])
        out[c["case_id"]] = attrs
    return dict(sorted(out.items()))


def compute_counts(cases_data: dict[str, Any]) -> dict[str, Any]:
    """統計（僅資訊）。鍵值計算方式與 tests/test_acceptance_cases.py::test_counts_report 一致。"""
    by_suite: dict[str, int] = {}
    by_layer: dict[str, int] = {}
    by_slice: dict[str, int] = {}
    cases = cases_data.get("cases") or []
    for c in cases:
        s = c.get("suite", "unknown")
        by_suite[s] = by_suite.get(s, 0) + 1
        layer = c.get("layer", "unknown")
        by_layer[layer] = by_layer.get(layer, 0) + 1
        dsl = str(c.get("daemon_slice", "unknown"))
        by_slice[dsl] = by_slice.get(dsl, 0) + 1
    return {
        "schema_version": 1,
        "cases_revision": cases_data.get("revision"),
        "total": len(cases),
        "by_suite": dict(sorted(by_suite.items())),
        "by_layer": dict(sorted(by_layer.items())),
        "by_slice": dict(sorted(by_slice.items())),
    }


def _load_yaml(path: pathlib.Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def _write(path: pathlib.Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _empty_lock(revision: str | None) -> dict[str, Any]:
    names = ["stage0", "cli_1A", "cli_1B"] + [f"slice_{s}" for s in SLICES]
    return {"schema_version": 1, "cases_revision": revision, "sets": {n: [] for n in names}}


def cmd_check(cases_path: pathlib.Path, lock_path: pathlib.Path) -> int:
    cases_data = _load_yaml(cases_path)
    if not lock_path.exists():
        print(f"[regen_lock] 鎖檔不存在：{lock_path}（請以 --add-only 產生）", file=sys.stderr)
        return 1
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if lock.get("cases_revision") != cases_data.get("revision"):
        problems.append(
            f"cases_revision 不一致：lock={lock.get('cases_revision')!r} yaml={cases_data.get('revision')!r}"
        )
    current = compute_sets(cases_data)
    locked_sets = lock.get("sets", {})
    for name in sorted(set(current) | set(locked_sets)):
        want = current.get(name)
        have = locked_sets.get(name)
        if want != have:
            missing = sorted(set(have or []) - set(want or []))
            extra = sorted(set(want or []) - set(have or []))
            problems.append(f"集合 {name} 與 cases.yaml 不一致：鎖檔獨有 {missing}，yaml 新增 {extra}")
    if "case_attrs" not in lock:
        problems.append("鎖檔缺少 case_attrs 快照（舊格式；請以 --add-only 重產）")
    else:
        want_attrs = compute_attrs(cases_data)
        have_attrs = lock["case_attrs"]
        for cid in sorted(set(have_attrs) | set(want_attrs)):
            if have_attrs.get(cid) != want_attrs.get(cid):
                problems.append(f"案例屬性與 cases.yaml 不一致：{cid}")
    counts_path = lock_path.parent / COUNTS_NAME
    want_counts = compute_counts(cases_data)
    if not counts_path.exists():
        problems.append(f"統計檔不存在：{counts_path}")
    else:
        have_counts = json.loads(counts_path.read_text(encoding="utf-8"))
        if have_counts != want_counts:
            problems.append("統計檔 cases.counts.json 與 cases.yaml 不一致")
    if problems:
        for p in problems:
            print(f"[regen_lock] FAIL {p}", file=sys.stderr)
        print("[regen_lock] 請以 --add-only 重算（僅新增允許；刪除、延後或改動分母屬性須擁有者裁決）", file=sys.stderr)
        return 1
    print("[regen_lock] OK：鎖檔、案例屬性快照與統計檔與 cases.yaml 一致")
    return 0


def cmd_add_only(cases_path: pathlib.Path, lock_path: pathlib.Path) -> int:
    cases_data = _load_yaml(cases_path)
    new_sets = compute_sets(cases_data)
    new_attrs = compute_attrs(cases_data)
    if lock_path.exists():
        old = json.loads(lock_path.read_text(encoding="utf-8"))
    else:
        old = _empty_lock(None)
    removed: list[str] = []
    for name, old_ids in (old.get("sets") or {}).items():
        gone = sorted(set(old_ids) - set(new_sets.get(name, [])))
        for cid in gone:
            removed.append(f"{cid}（集合 {name}）")
    changed: list[str] = []
    old_attrs = old.get("case_attrs")
    if old_attrs is not None:
        for cid, old_a in sorted(old_attrs.items()):
            if cid not in new_attrs:
                removed.append(f"{cid}（案例已不存在）")
                continue
            if new_attrs[cid] != old_a:
                diff = {k: (old_a.get(k), new_attrs[cid].get(k)) for k in ATTR_KEYS if old_a.get(k) != new_attrs[cid].get(k)}
                changed.append(f"{cid}（分母屬性變更：{diff}）")
    if removed or changed:
        print("[regen_lock] REFUSED：既有 case 從集合消失、被延後切片或分母屬性被改動，鎖檔未變更：", file=sys.stderr)
        for line in removed + changed:
            print(f"  - {line}", file=sys.stderr)
        print("[regen_lock] 移除、延後或改動分母屬性須另有擁有者裁決並在 PR 說明（§11.3）。", file=sys.stderr)
        return 1
    added = {
        name: sorted(set(new_sets[name]) - set(old.get("sets", {}).get(name, [])))
        for name in new_sets
    }
    lock = {
        "schema_version": 1,
        "cases_revision": cases_data.get("revision"),
        "sets": new_sets,
        "case_attrs": new_attrs,
    }
    _write(lock_path, _dump_json(lock))
    counts_path = lock_path.parent / COUNTS_NAME
    _write(counts_path, _dump_json(compute_counts(cases_data)))
    total_added = sum(len(v) for v in added.values())
    print(f"[regen_lock] wrote {lock_path.name} 與 {COUNTS_NAME}（新增集合條目 {total_added}）")
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="重算／驗證 Stage 0 案例凍結鎖（P2-T12）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="驗證鎖檔與統計檔一致")
    mode.add_argument("--add-only", action="store_true", help="重算並寫回；既有 ID 消失或分母屬性改動即拒絕")
    parser.add_argument("--cases", type=pathlib.Path, default=DEFAULT_CASES, help="cases.yaml 路徑")
    parser.add_argument("--lock", type=pathlib.Path, default=DEFAULT_LOCK, help="cases.lock.json 路徑")
    args = parser.parse_args(argv)
    if args.check:
        return cmd_check(args.cases, args.lock)
    return cmd_add_only(args.cases, args.lock)


if __name__ == "__main__":
    sys.exit(main())
