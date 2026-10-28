"""cases.lock.json 的唯一寫入者（P2-T12，§11.3「不得在測試失敗後刪除案例、縮小分母」）。

用法：
  python3 tests/acceptance/regen_lock.py --check      # 驗證案例鎖與 L1 鎖結構；兩者缺一即 exit 1（P2-T13 審查修正）
  python3 tests/acceptance/regen_lock.py --check --cases-only
                                                      # 只驗證案例鎖（供 func-test 執行器；不檢查 L1 鎖）
  python3 tests/acceptance/regen_lock.py --check --base <rev> [--allow-removal <set_id:platform:nodeid>…]
                                                      # 另以 <rev> 的 L1 鎖為基準：基準已有的 (set, platform, nodeid) 消失或 selector 改變即 exit 1
  python3 tests/acceptance/regen_lock.py --add-only   # 重算；既有 ID 從任何集合消失、被延後切片，或分母屬性被改動即 exit 1 且不寫檔，否則寫回
  python3 tests/acceptance/regen_lock.py --add-only --allow-defer <case_id>:<from>:<to>:<OD-NN> [...]
                                                      # 擁有者裁決逐名核准的延後切片（OD-34 起）：只允許該案例 daemon_slice 由 from 改為較晚的 to，
                                                      # 其餘屬性不得改；核准須與實際延後集合完全相符（多列、少列、OD 編號不合法皆拒絕且不寫檔）
  python3 tests/acceptance/regen_lock.py --l1-add-only --set <id> --platform <linux|windows> --collect <檔>
          (--selector <json檔> | --argv-json <json> [--paths-json <json>] [--markexpr <str>])
                                                      # L1 必要集合只增（P2-T13，R5-F3）；既有 nodeid 消失即 exit 1 且不寫檔；
                                                      # 經核准替換者須逐筆列 --allow-removal set_id:platform:nodeid，三元組須全部屬於本次 set／平台，
                                                      # 且清單必須與該集合消失集合完全相符；新增 set／平台時必須給 selector（檔案或行內 JSON 皆可）
                                                      # OD-24：collect JSON 的 ignored 名單（cargo_l1_results.py --list-only）新增時排除；
                                                      # 鎖中既有 ignored 列仍屬 collected、不視為消失，只能逐名 --allow-removal 移除
  核准移除格式（收緊，2026-10-23）：`--allow-removal` 僅接受三元組 `set_id:platform:nodeid`（platform ∈ linux|windows）。
  裸 nodeid 不再被接受（舊版只比對 nodeid，Windows 之核准會放行 Linux 必要集合中同名 nodeid 之刪除；sol d82d146）。

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
import re
import subprocess
import sys
from typing import Any

import yaml

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_CASES = HERE / "cases.yaml"
DEFAULT_LOCK = HERE / "cases.lock.json"
DEFAULT_L1_LOCK = HERE / "l1_required.lock.json"
L1_PLATFORMS = ("linux", "windows")
SET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_:.-]*$")
COUNTS_NAME = "cases.counts.json"
SLICES = ("2.1", "2.2", "2.3", "2.4")
ATTR_KEYS = ("suite", "layer", "platforms", "expected", "necessity", "daemon_slice", "cli_stage", "stage0_required")
# 核准移除之粒度為 (set_id, platform, nodeid) 三元組：同名 nodeid 在不同集合／平台的核准互不相通。
Approval = tuple[str, str, str]


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
    if not cases_path.exists():
        print(f"[regen_lock] FAIL 案例清單不存在：{cases_path}", file=sys.stderr)
        return 1
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


# 延後切片核准：(case_id, from_slice, to_slice, OD 編號)；OD 編號須為擁有者裁決 `OD-<數字>`。
DeferApproval = tuple[str, str, str, str]
_OD_RE = re.compile(r"^OD-\d+$")


def parse_defer_approval(token: str) -> DeferApproval:
    parts = token.split(":")
    if len(parts) != 4 or not all(parts):
        raise ValueError(f"--allow-defer 須為 case_id:from:to:OD-NN：{token!r}")
    cid, src, dst, od = parts
    if src not in SLICES or dst not in SLICES or _slice_key(dst) <= _slice_key(src):
        raise ValueError(f"--allow-defer 的切片須為 {SLICES} 且 to 晚於 from：{token!r}")
    if not _OD_RE.match(od):
        raise ValueError(f"--allow-defer 須引用擁有者裁決 OD-NN：{token!r}")
    return cid, src, dst, od


def _slice_key(slice_id: str) -> tuple[int, ...]:
    return tuple(int(x) for x in slice_id.split("."))


def cmd_add_only(cases_path: pathlib.Path, lock_path: pathlib.Path,
                 defers: set[DeferApproval] | None = None) -> int:
    defers = set(defers or ())
    cases_data = _load_yaml(cases_path)
    new_sets = compute_sets(cases_data)
    new_attrs = compute_attrs(cases_data)
    if lock_path.exists():
        old = json.loads(lock_path.read_text(encoding="utf-8"))
    else:
        old = _empty_lock(None)
    old_attrs = old.get("case_attrs")
    # 核准的延後：該案例只有 daemon_slice 由 from 改為 to；其從較早切片集合消失屬核准範圍。
    approved: dict[str, DeferApproval] = {}
    unused: list[str] = []
    for item in sorted(defers):
        cid, src, dst, od = item
        old_a = (old_attrs or {}).get(cid)
        new_a = new_attrs.get(cid)
        if (old_a is None or new_a is None or old_a.get("daemon_slice") != src or new_a.get("daemon_slice") != dst
                or {k: v for k, v in old_a.items() if k != "daemon_slice"}
                != {k: v for k, v in new_a.items() if k != "daemon_slice"}):
            unused.append(f"{cid}:{src}:{dst}:{od}（與實際變更不符或未使用）")
            continue
        approved[cid] = item
    removed: list[str] = []
    for name, old_ids in (old.get("sets") or {}).items():
        gone = sorted(set(old_ids) - set(new_sets.get(name, [])))
        for cid in gone:
            if cid in approved and name.startswith("slice_") and _slice_key(name[len("slice_"):]) < _slice_key(approved[cid][2]):
                continue
            removed.append(f"{cid}（集合 {name}）")
    changed: list[str] = []
    if old_attrs is not None:
        for cid, old_a in sorted(old_attrs.items()):
            if cid not in new_attrs:
                removed.append(f"{cid}（案例已不存在）")
                continue
            if new_attrs[cid] != old_a and cid not in approved:
                diff = {k: (old_a.get(k), new_attrs[cid].get(k)) for k in ATTR_KEYS if old_a.get(k) != new_attrs[cid].get(k)}
                changed.append(f"{cid}（分母屬性變更：{diff}）")
    if unused:
        print("[regen_lock] REFUSED：--allow-defer 核准與實際延後不符，鎖檔未變更：", file=sys.stderr)
        for line in unused:
            print(f"  - {line}", file=sys.stderr)
        return 1
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
    for cid, (_c, src, dst, od) in sorted(approved.items()):
        print(f"[regen_lock] 核准延後切片：{cid} {src}→{dst}（{od}）")
    return 0


def _load_l1(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump_l1(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _spec_shape_problems(where: str, spec: Any) -> list[str]:
    if not isinstance(spec, dict) or set(spec) != {"selector", "nodeids"}:
        return [f"{where}：需為 {{selector, nodeids}} 物件"]
    problems: list[str] = []
    sel = spec["selector"]
    if (
        not isinstance(sel, dict)
        or set(sel) != {"argv", "paths", "markexpr"}
        or not isinstance(sel["argv"], list) or not all(isinstance(x, str) for x in sel["argv"])
        or not isinstance(sel["paths"], list) or not all(isinstance(x, str) for x in sel["paths"])
        or not isinstance(sel["markexpr"], str)
    ):
        problems.append(f"{where}：selector 需含 argv／paths（字串陣列）與 markexpr（字串）")
    nids = spec["nodeids"]
    if not isinstance(nids, list) or not nids or not all(isinstance(x, str) and x for x in nids):
        problems.append(f"{where}：nodeids 必須為非空字串陣列")
    elif nids != sorted(nids):
        problems.append(f"{where}：nodeids 未依字典序排序")
    elif len(set(nids)) != len(nids):
        problems.append(f"{where}：nodeids 有重複")
    return problems


def l1_shape_problems(lock: Any) -> list[str]:
    """L1 鎖結構檢查（與 l1_required.schema.json 同義；schema 無法表達排序與重複，於此補上）。"""
    if not isinstance(lock, dict) or lock.get("schema_version") != 1 or not isinstance(lock.get("sets"), dict):
        return ["L1 鎖結構錯誤：需 schema_version=1 與 sets 物件"]
    problems: list[str] = []
    for set_id, plats in lock["sets"].items():
        if not SET_ID_RE.match(set_id):
            problems.append(f"set 名稱不合法：{set_id!r}")
        if not isinstance(plats, dict) or not plats:
            problems.append(f"{set_id}：至少需一個平台")
            continue
        for plat, spec in plats.items():
            if plat not in L1_PLATFORMS:
                problems.append(f"{set_id}：未知平台 {plat!r}")
            problems.extend(_spec_shape_problems(f"{set_id}.{plat}", spec))
    return problems


def _git_base_lock(lock_path: pathlib.Path, rev: str) -> tuple[dict[str, Any] | None, str | None]:
    """讀取 <rev> 的 L1 鎖。回傳 (鎖或 None, 錯誤訊息)；rev 本身不存在為錯誤，該 rev 無此檔則回 None（首次引入）。"""
    cwd = str(lock_path.parent)
    verify = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if verify.returncode != 0:
        return None, f"基準 rev 不存在或不是 commit：{rev}"
    exists = subprocess.run(
        ["git", "-C", cwd, "cat-file", "-e", f"{rev}:./{lock_path.name}"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if exists.returncode != 0:
        return None, None
    shown = subprocess.run(
        ["git", "-C", cwd, "show", f"{rev}:./{lock_path.name}"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if shown.returncode != 0:
        return None, f"無法讀取基準 {rev} 的鎖檔：{shown.stderr.strip()}"
    try:
        return json.loads(shown.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"基準 {rev} 的鎖檔不是合法 JSON：{exc}"


def parse_removal_approval(token: str) -> Approval:
    """解析 `set_id:platform:nodeid` 三元組；不合法即拋 ValueError（不得靜默忽略）。

    以 maxsplit=2 切分，故 nodeid 內的 `::` 不影響切分；set_id 不含 `:`（由 SET_ID_RE 與切分共同保證）。
    """
    parts = token.split(":", 2)
    if len(parts) != 3 or not SET_ID_RE.match(parts[0]) or ":" in parts[0] or parts[1] not in L1_PLATFORMS or not parts[2]:
        raise ValueError(f"--allow-removal 須為 set_id:platform:nodeid 三元組（platform ∈ linux|windows）：{token!r}")
    return parts[0], parts[1], parts[2]


def cmd_check_l1(lock_path: pathlib.Path, base: str | None, approvals: set[Approval]) -> int:
    if not lock_path.exists():
        print(f"[regen_lock] FAIL L1 鎖檔不存在：{lock_path}", file=sys.stderr)
        return 1
    try:
        lock = _load_l1(lock_path)
    except json.JSONDecodeError as exc:
        print(f"[regen_lock] FAIL L1 鎖檔不是合法 JSON：{exc}", file=sys.stderr)
        return 1
    rc = 0
    for problem in l1_shape_problems(lock):
        print(f"[regen_lock] FAIL {problem}", file=sys.stderr)
        rc = 1
    if base is not None:
        base_lock, err = _git_base_lock(lock_path, base)
        if err is not None:
            print(f"[regen_lock] FAIL {err}", file=sys.stderr)
            return 1
        if base_lock is None:
            print(f"[regen_lock] 基準 {base} 尚無 L1 鎖檔（首次引入），略過基準比對")
        else:
            rc |= _report_base_diff(base_lock, lock, approvals)
    if rc == 0:
        print("[regen_lock] OK：L1 必要集合鎖結構" + ("與基準比對一致" if base is not None else "正確"))
    return rc


def _report_base_diff(base: dict[str, Any], head: dict[str, Any], approvals: set[Approval]) -> int:
    """基準已有的 (set, platform, nodeid) 於 HEAD 消失者，須有同一三元組之核准；selector 改變一律拒絕。"""
    rc = 0
    head_sets = head.get("sets", {})
    for set_id, plats in sorted(base.get("sets", {}).items()):
        for plat, spec in sorted(plats.items()):
            head_spec = head_sets.get(set_id, {}).get(plat)
            if head_spec is None:
                gone = list(spec["nodeids"])
                changed = False
            else:
                gone = sorted(set(spec["nodeids"]) - set(head_spec["nodeids"]))
                changed = head_spec["selector"] != spec["selector"]
            for nid in gone:
                if (set_id, plat, nid) in approvals:
                    print(f"[regen_lock] 基準已有、HEAD 已消失（已以 --allow-removal 允許）：{set_id}.{plat} {nid}")
                else:
                    print(f"[regen_lock] FAIL 基準已有、HEAD 已消失（須擁有者裁決並以 --allow-removal {set_id}:{plat}:{nid} 列名）：{set_id}.{plat} {nid}")
                    rc = 1
            if changed:
                print(f"[regen_lock] FAIL selector 改變（基準 {spec['selector']}）：{set_id}.{plat}")
                rc = 1
    return rc


def _read_collect(path: pathlib.Path) -> tuple[list[str], set[str]]:
    """讀 collect 輸出，回傳 (collected, ignored)。

    JSON 形式 `{"collected": [...], "ignored": [...]}`（`scripts/rust/cargo_l1_results.py --list-only`）
    的 ignored 必須 ⊆ collected（OD-24）；文字形式（pytest --collect-only）沒有 ignored 名單。
    """
    raw = path.read_text(encoding="utf-8")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict) and isinstance(value.get("collected"), list):
        if not all(isinstance(item, str) for item in value["collected"]):
            raise ValueError(f"collect JSON 的 collected 必須全為字串：{path}")
        ignored = value.get("ignored", [])
        if not isinstance(ignored, list) or not all(isinstance(item, str) for item in ignored):
            raise ValueError(f"collect JSON 的 ignored 必須為字串陣列：{path}")
        stray = sorted(set(ignored) - set(value["collected"]))
        if stray:
            raise ValueError(f"collect JSON 的 ignored 必須屬於 collected：{stray}")
        return list(value["collected"]), set(ignored)

    ids: list[str] = []
    for line in raw.splitlines():
        text = line.strip()
        if "::" in text:
            ids.append(text)
    return ids, set()


def _selector_from_args(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str | None]:
    """由 CLI 組出 selector：`--selector <檔>` 或行內 `--argv-json` 等（兩者互斥）。回傳 (selector, 錯誤訊息)。"""
    inline = args.argv_json is not None or args.paths_json is not None or args.markexpr is not None
    if args.selector is not None and inline:
        return None, "--selector 與 --argv-json／--paths-json／--markexpr 互斥"
    if args.selector is not None:
        return json.loads(args.selector.read_text(encoding="utf-8")), None
    if not inline:
        return None, None
    if args.argv_json is None:
        return None, "行內 selector 需 --argv-json（產生命令的完整參數陣列）"
    try:
        argv = json.loads(args.argv_json)
        paths = json.loads(args.paths_json) if args.paths_json is not None else []
    except json.JSONDecodeError as exc:
        return None, f"selector JSON 不合法：{exc}"
    selector = {"argv": argv, "paths": paths, "markexpr": args.markexpr or ""}
    if _spec_shape_problems("selector", {"selector": selector, "nodeids": ["x::y"]}):
        return None, "selector 結構不合法：argv／paths 需為字串陣列"
    return selector, None


def cmd_l1_add_only(lock_path: pathlib.Path, set_id: str, platform: str, collect: pathlib.Path,
                    selector: dict[str, Any] | None, approvals: set[Approval]) -> int:
    if platform not in L1_PLATFORMS or not SET_ID_RE.match(set_id or ""):
        print("[regen_lock] 用法錯誤：--set 與 --platform（linux|windows）必填", file=sys.stderr)
        return 2
    foreign = sorted(a for a in approvals if (a[0], a[1]) != (set_id, platform))
    if foreign:
        print(f"[regen_lock] REFUSED --allow-removal 含非本次集合／平台之三元組（本次為 {set_id}.{platform}），鎖檔未變更：", file=sys.stderr)
        for s, p, nid in foreign:
            print(f"  - {s}:{p}:{nid}", file=sys.stderr)
        return 1
    try:
        collected, ignored = _read_collect(collect)
    except ValueError as exc:
        print(f"[regen_lock] REFUSED {exc}", file=sys.stderr)
        return 1
    if not collected:
        print(f"[regen_lock] REFUSED 收集輸出沒有任何 nodeid：{collect}", file=sys.stderr)
        return 1
    dupes = sorted({nid for nid in collected if collected.count(nid) > 1})
    if dupes:
        print(f"[regen_lock] REFUSED 收集輸出有重複 nodeid：{dupes}", file=sys.stderr)
        return 1
    lock = _load_l1(lock_path) if lock_path.exists() else {"schema_version": 1, "sets": {}}
    spec = lock.get("sets", {}).get(set_id, {}).get(platform)
    if spec is None:
        if selector is None:
            print(f"[regen_lock] 用法錯誤：新增 {set_id}.{platform} 需以 --selector 或 --argv-json 指定產生命令", file=sys.stderr)
            return 2
        if approvals:
            print(f"[regen_lock] REFUSED 新增集合 {set_id}.{platform} 無既有 nodeid 可移除，--allow-removal 須為空，鎖檔未變更", file=sys.stderr)
            return 1
        # OD-24：#[ignore] 列不由官方 selector 執行，新增時一律排除
        new_spec = {"selector": selector, "nodeids": sorted(set(collected) - ignored)}
        added_count = len(new_spec["nodeids"])
    else:
        if selector is not None and selector != spec["selector"]:
            print(f"[regen_lock] REFUSED selector 與鎖檔不同：{set_id}.{platform}", file=sys.stderr)
            return 1
        allowed = {nid for _s, _p, nid in approvals}
        # 既有 ignored 列仍在 collected 內，不視為消失（plan 04 Task 17）；OD-24 起可逐名核准移除
        removed = sorted((set(spec["nodeids"]) - set(collected)) | (allowed & set(spec["nodeids"]) & ignored))
        unapproved = sorted(set(removed) - allowed)
        unused_approval = sorted(allowed - set(removed))
        if unapproved or unused_approval:
            print("[regen_lock] REFUSED：nodeid 移除核准須與實際消失集合完全相符，鎖檔未變更：", file=sys.stderr)
            for nid in unapproved:
                print(f"  - 未列名 {set_id}.{platform} {nid}", file=sys.stderr)
            for nid in unused_approval:
                print(f"  - 未消失 {set_id}.{platform} {nid}", file=sys.stderr)
            print("[regen_lock] 每筆移除都須對應已核准的案例／測試替換並於交付說明列明。", file=sys.stderr)
            return 1
        added = sorted(set(collected) - set(spec["nodeids"]) - ignored)
        if not added and not removed:
            print(f"[regen_lock] {set_id}.{platform} 無新增 nodeid，鎖檔未變更")
            return 0
        new_spec = {"selector": spec["selector"], "nodeids": sorted(set(added) | (set(spec["nodeids"]) - set(removed)))}
        added_count = len(added)
    lock["schema_version"] = 1
    lock.setdefault("sets", {}).setdefault(set_id, {})[platform] = new_spec
    problems = l1_shape_problems(lock)
    if problems:
        for problem in problems:
            print(f"[regen_lock] REFUSED {problem}", file=sys.stderr)
        return 1
    _write(lock_path, _dump_l1(lock))
    removed_count = len(removed) if spec is not None else 0
    print(f"[regen_lock] wrote {lock_path.name}：{set_id}.{platform} 新增 {added_count} 筆、核准移除 {removed_count} 筆 nodeid")
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="重算／驗證 Stage 0 案例凍結鎖（P2-T12）與 L1 必要集合鎖（P2-T13）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="驗證案例鎖（若存在）與 L1 鎖結構")
    mode.add_argument("--add-only", action="store_true", help="重算並寫回；既有 ID 消失或分母屬性改動即拒絕")
    parser.add_argument("--allow-defer", action="append", default=[], metavar="CASE:FROM:TO:OD-NN",
                        help="--add-only：擁有者裁決逐名核准的延後切片（只改 daemon_slice；核准須與實際延後完全相符）")
    mode.add_argument("--l1-add-only", action="store_true", help="L1 必要集合只增登記；核准替換可逐名搭配 --allow-removal")
    parser.add_argument("--cases", type=pathlib.Path, default=DEFAULT_CASES, help="cases.yaml 路徑")
    parser.add_argument("--lock", type=pathlib.Path, default=DEFAULT_LOCK, help="cases.lock.json 路徑")
    parser.add_argument("--lock-file", type=pathlib.Path, default=DEFAULT_L1_LOCK, help="l1_required.lock.json 路徑")
    parser.add_argument("--base", default=None, help="--check 時的基準 rev（以其 L1 鎖比對只增）")
    parser.add_argument("--allow-removal", action="extend", nargs="+", default=[], metavar="SET:PLATFORM:NODEID",
                        help="經核准替換而允許移除的三元組 set_id:platform:nodeid（僅接受三元組）；須逐筆列出且於交付說明對應新測試")
    parser.add_argument("--set", dest="set_id", default=None, help="--l1-add-only：集合 ID")
    parser.add_argument("--platform", default=None, help="--l1-add-only：linux 或 windows")
    parser.add_argument("--collect", type=pathlib.Path, default=None, help="--l1-add-only：collect-only 輸出檔")
    parser.add_argument("--selector", type=pathlib.Path, default=None,
                        help="--l1-add-only：新增 set／平台時的 selector JSON 檔（{argv, paths, markexpr}）")
    parser.add_argument("--argv-json", default=None,
                        help="--l1-add-only：行內 selector.argv（JSON 字串陣列；產生該平台報告的完整命令）")
    parser.add_argument("--paths-json", default=None, help="--l1-add-only：行內 selector.paths（JSON 字串陣列）")
    parser.add_argument("--markexpr", default=None, help="--l1-add-only：行內 selector.markexpr")
    parser.add_argument("--cases-only", action="store_true",
                        help="--check：只驗證案例鎖（func-test 執行器用；不檢查 L1 鎖）")
    args = parser.parse_args(argv)
    try:
        approvals = {parse_removal_approval(token) for token in args.allow_removal}
        defers = {parse_defer_approval(token) for token in args.allow_defer}
    except ValueError as exc:
        print(f"[regen_lock] 用法錯誤：{exc}", file=sys.stderr)
        return 2
    if args.l1_add_only:
        if args.collect is None:
            print("[regen_lock] 用法錯誤：--l1-add-only 需 --collect", file=sys.stderr)
            return 2
        selector, err = _selector_from_args(args)
        if err is not None:
            print(f"[regen_lock] 用法錯誤：{err}", file=sys.stderr)
            return 2
        return cmd_l1_add_only(args.lock_file, args.set_id or "", args.platform or "", args.collect, selector,
                               approvals)
    if args.check:
        rc = cmd_check(args.cases, args.lock)
        if args.cases_only:
            return 1 if rc else 0
        # L1 鎖缺檔即失敗（P2-T13 審查修正）：刪除 l1_required.lock.json 不得讓 --check 通過。
        rc |= cmd_check_l1(args.lock_file, args.base, approvals)
        return 1 if rc else 0
    if defers and not args.add_only:
        print("[regen_lock] 用法錯誤：--allow-defer 只用於 --add-only", file=sys.stderr)
        return 2
    return cmd_add_only(args.cases, args.lock, defers)


if __name__ == "__main__":
    sys.exit(main())
