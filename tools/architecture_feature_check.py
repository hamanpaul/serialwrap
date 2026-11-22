#!/usr/bin/env python3
"""功能頁（docs/architecture/features/<id>.facts.json）與其 Archify IR 的事實檢查。

只用標準庫與 git，不需要 Archify。檢查：
1. facts 結構（鍵集合、id 唯一、型別）。
2. 每一條 evidence 在 repository.revision 上逐位元組成立（path、行範圍、excerpt_sha256；不得引用 docs/architecture/）。
3. 每個 step 的 from／to 都是已宣告的 participant；participant 的 overview_component 必須是總覽 facts.json 的元件 id 或 null。
4. 規格上限與禁止事項（docs/architecture/features/README.md）：參與者最多 8 個、步驟最多 30 個、每個 phase 至少兩則
   訊息、sequence 的 variant 一律 default、不畫自畫箭頭（from == to）、IR 不設 meta.viewBox、頁面 id 必須列在
   docs/architecture/pages.json（續頁 <NN>-<slug>-2 要列進該功能的 continuations）。
   警告（不擋）：參與者 label 超過約 15 個寬度單位（CJK 算 2）；label／note／condition 出現「｜」但不是 Linux｜Windows 描述。
5. IR 是 facts 的投影：
   - sequence：participants 的 id／label 與 facts 相同；messages 依序對應 facts.steps（id、from、to、label 逐字相同）；
     segments 的 label 必須是 facts.steps 用到的 phase。
   - workflow／architecture：節點 id 集合等於 participants、邊 id 集合等於 steps（from／to／label 逐字相同）。
   - meta.repository.revision 等於 facts.repository.revision；meta.locale 為 zh-TW；meta.output 等於約定路徑。

用法：
  python3 tools/architecture_feature_check.py docs/architecture/features/01-command-exec.facts.json
  python3 tools/architecture_feature_check.py --all
退出碼：0 全部通過（警告只印出、不擋）；1 有錯誤（逐條印出）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
FEATURES = REPO / "docs" / "architecture" / "features"
OVERVIEW_FACTS = REPO / "docs" / "architecture" / "facts.json"
PAGES = REPO / "docs" / "architecture" / "pages.json"
MAX_PARTICIPANTS = 8
MAX_STEPS = 30
MAX_LABEL_UNITS = 15
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REV_RE = re.compile(r"^[0-9a-f]{40}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FILE_RE = re.compile(r"^(\d{2})-([a-z0-9-]+)\.facts\.json$")
BASES = {"observed", "inferred"}
TYPES = {"frontend", "backend", "database", "cloud", "security", "messagebus", "external"}
DIAGRAM_TYPES = {"sequence", "workflow", "architecture"}


class Problems(list):
    """錯誤清單；warnings 另存，不影響退出碼。"""

    def __init__(self):
        super().__init__()
        self.warnings: list[str] = []

    def add(self, where: str, msg: str) -> None:
        self.append(f"{where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"{where}: {msg}")


def _units(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _bar_without_platform(text) -> bool:
    return isinstance(text, str) and "｜" in text and "Linux" not in text and "Windows" not in text


def _page_ids() -> set[str]:
    try:
        doc = json.loads(PAGES.read_text(encoding="utf-8"))
        return {f["id"] for f in doc["features"]} | {c for f in doc["features"] for c in f["continuations"]}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def _keys(obj, required: set[str], optional: set[str], where: str, probs: Problems) -> bool:
    if not isinstance(obj, dict):
        probs.add(where, "必須是物件")
        return False
    missing = required - obj.keys()
    extra = obj.keys() - required - optional
    if missing:
        probs.add(where, f"缺少鍵 {sorted(missing)}")
    if extra:
        probs.add(where, f"不認得的鍵 {sorted(extra)}")
    return not missing


def _git_show(revision: str, path: str) -> bytes | None:
    cp = subprocess.run(["git", "-C", str(REPO), "show", f"{revision}:{path}"], capture_output=True)
    return cp.stdout if cp.returncode == 0 else None


def _check_evidence(anchors, revision: str, where: str, probs: Problems) -> None:
    if not isinstance(anchors, list) or not anchors:
        probs.add(where, "evidence 必須是非空陣列")
        return
    for i, a in enumerate(anchors):
        w = f"{where}.evidence[{i}]"
        if not _keys(a, {"path", "line", "end_line", "excerpt_sha256", "claim"}, set(), w, probs):
            continue
        path = a["path"]
        pure = PurePosixPath(path) if isinstance(path, str) else None
        if pure is None or pure.is_absolute() or ".." in pure.parts or "\\" in path:
            probs.add(w, "path 必須是 repo 相對路徑")
            continue
        if pure.parts[:2] == ("docs", "architecture"):
            probs.add(w, "不得引用 docs/architecture/（產出物不能為自己作證）")
            continue
        line, end = a["line"], a["end_line"]
        if not (isinstance(line, int) and not isinstance(line, bool) and line >= 1
                and isinstance(end, int) and not isinstance(end, bool) and end >= line):
            probs.add(w, "line／end_line 必須是正整數且 end_line >= line")
            continue
        if not (isinstance(a["excerpt_sha256"], str) and HASH_RE.fullmatch(a["excerpt_sha256"])):
            probs.add(w, "excerpt_sha256 必須是小寫 SHA-256")
            continue
        if not (isinstance(a["claim"], str) and a["claim"].strip()):
            probs.add(w, "claim 不得為空")
        blob = _git_show(revision, path)
        if blob is None:
            probs.add(w, f"{path} 在 {revision[:8]} 不存在")
            continue
        lines = blob.splitlines(keepends=True)
        if end > len(lines):
            probs.add(w, f"行 {line}-{end} 超出 {path}（{len(lines)} 行）")
            continue
        actual = hashlib.sha256(b"".join(lines[line - 1:end])).hexdigest()
        if actual != a["excerpt_sha256"]:
            probs.add(w, f"excerpt 雜湊不符（{path}:{line}-{end} 實際 {actual}）")


def _overview_ids() -> set[str]:
    try:
        return {c["id"] for c in json.loads(OVERVIEW_FACTS.read_text(encoding="utf-8"))["components"]}
    except (OSError, ValueError, KeyError):
        return set()


def check(facts_path: Path) -> Problems:
    probs = Problems()
    m = FILE_RE.match(facts_path.name)
    where = facts_path.name
    if not m:
        probs.add(where, "檔名必須是 <兩位數序號>-<slug>.facts.json")
        return probs
    stem = facts_path.name[: -len(".facts.json")]
    pages = _page_ids()
    if pages and stem not in pages:
        probs.add(where, "頁面 id 不在 docs/architecture/pages.json（續頁 <NN>-<slug>-2 要列進該功能的 continuations）")
    try:
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        probs.add(where, f"無法讀取：{exc}")
        return probs
    req = {"schema_version", "document_type", "feature", "repository", "diagram_type", "scope",
           "participants", "steps", "unknowns", "conflicts"}
    if not _keys(facts, req, set(), where, probs):
        return probs
    if facts["schema_version"] != 1 or facts["document_type"] != "feature-facts":
        probs.add(where, "schema_version 必須為 1、document_type 必須為 feature-facts")
    feat = facts["feature"]
    if _keys(feat, {"id", "order", "title"}, {"summary"}, f"{where}.feature", probs):
        if feat["id"] != stem:
            probs.add(f"{where}.feature.id", f"必須等於檔名主幹 {stem}")
        if feat["order"] != int(m.group(1)):
            probs.add(f"{where}.feature.order", f"必須等於檔名序號 {int(m.group(1))}")
    repo = facts["repository"]
    revision = ""
    if _keys(repo, {"url", "revision"}, set(), f"{where}.repository", probs):
        revision = repo["revision"]
        if not (isinstance(revision, str) and REV_RE.fullmatch(revision)):
            probs.add(f"{where}.repository.revision", "必須是 40 位小寫 commit SHA")
            revision = ""
    if facts["diagram_type"] not in DIAGRAM_TYPES:
        probs.add(f"{where}.diagram_type", f"必須是 {sorted(DIAGRAM_TYPES)} 之一")
    _keys(facts["scope"], {"includes", "excludes"}, set(), f"{where}.scope", probs)

    overview = _overview_ids()
    parts: dict[str, dict] = {}
    for i, p in enumerate(facts["participants"] if isinstance(facts["participants"], list) else []):
        w = f"{where}.participants[{i}]"
        if not _keys(p, {"id", "label", "type", "overview_component", "responsibility", "basis", "evidence"},
                     {"sublabel"}, w, probs):
            continue
        if not (isinstance(p["id"], str) and ID_RE.fullmatch(p["id"])) or p["id"] in parts:
            probs.add(w, f"id {p['id']!r} 不合法或重複")
        parts[p["id"]] = p
        if p["type"] not in TYPES:
            probs.add(w, f"type 必須是 {sorted(TYPES)} 之一")
        if isinstance(p["label"], str) and _units(p["label"]) > MAX_LABEL_UNITS:
            probs.warn(w, f"label 約 {_units(p['label'])} 個寬度單位，超過 {MAX_LABEL_UNITS}；細節請移到 sublabel")
        if p["overview_component"] is not None and overview and p["overview_component"] not in overview:
            probs.add(w, f"overview_component {p['overview_component']!r} 不在總覽 facts.json 的元件中")
        if p["basis"] not in BASES:
            probs.add(w, "basis 必須是 observed 或 inferred")
        if revision:
            _check_evidence(p["evidence"], revision, w, probs)
    if not parts:
        probs.add(f"{where}.participants", "不得為空")
    if len(parts) > MAX_PARTICIPANTS:
        probs.add(f"{where}.participants", f"{len(parts)} 個參與者，超過 {MAX_PARTICIPANTS}；合併儲存類參與者或拆成 <NN>-<slug>-2 續頁")

    steps: list[dict] = []
    seen: set[str] = set()
    for i, s in enumerate(facts["steps"] if isinstance(facts["steps"], list) else []):
        w = f"{where}.steps[{i}]"
        if not _keys(s, {"id", "from", "to", "label", "phase", "basis", "evidence"},
                     {"condition", "variant", "note"}, w, probs):
            continue
        if not (isinstance(s["id"], str) and ID_RE.fullmatch(s["id"])) or s["id"] in seen:
            probs.add(w, f"id {s['id']!r} 不合法或重複")
        seen.add(s["id"])
        for end in ("from", "to"):
            if s[end] not in parts:
                probs.add(w, f"{end}={s[end]!r} 不是已宣告的 participant")
        if s["from"] == s["to"]:
            probs.add(w, "不畫自畫箭頭（from == to）：改用真實的回覆箭頭，或併進相鄰步驟的 note")
        if facts["diagram_type"] == "sequence" and s.get("variant", "default") != "default":
            probs.add(w, "sequence 的 variant 一律用 default")
        for key in ("label", "note", "condition"):
            if _bar_without_platform(s.get(key)):
                probs.warn(w, f"{key} 有「｜」但不是 Linux｜Windows 描述；其他並列用「／」或「或」")
        if s["basis"] not in BASES:
            probs.add(w, "basis 必須是 observed 或 inferred")
        if s["basis"] == "inferred" and len(s["evidence"]) < 2:
            probs.add(w, "inferred 的 step 需要至少兩條獨立 evidence")
        if revision:
            _check_evidence(s["evidence"], revision, w, probs)
        steps.append(s)
    if not steps:
        probs.add(f"{where}.steps", "不得為空")
    if len(steps) > MAX_STEPS:
        probs.add(f"{where}.steps", f"{len(steps)} 個步驟，超過 {MAX_STEPS}；拆成 <NN>-<slug>-2 續頁")
    if facts["diagram_type"] == "sequence":
        counts: dict[str, int] = {}
        for s in steps:
            counts[s.get("phase")] = counts.get(s.get("phase"), 0) + 1
        for phase, n in counts.items():
            if n < 2:
                probs.add(f"{where}.steps", f"phase {phase!r} 只有 {n} 則訊息；每個 phase 至少兩則（否則 segment 標籤會重疊）")

    ir_path = facts_path.with_name(f"{stem}.json")
    if not ir_path.is_file():
        probs.add(where, f"缺少 IR {ir_path.name}")
        return probs
    try:
        ir = json.loads(ir_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        probs.add(ir_path.name, f"無法解析：{exc}")
        return probs
    w = ir_path.name
    if ir.get("diagram_type") != facts["diagram_type"]:
        probs.add(w, f"diagram_type 必須等於 facts 的 {facts['diagram_type']}")
    meta = ir.get("meta") or {}
    if (meta.get("repository") or {}).get("revision") != revision:
        probs.add(f"{w}.meta.repository.revision", "必須等於 facts.repository.revision")
    if meta.get("locale") != "zh-TW":
        probs.add(f"{w}.meta.locale", "必須是 zh-TW")
    if "viewBox" in meta:
        probs.add(f"{w}.meta.viewBox", "不設 viewBox（v3.2.0 會觸發 desktop-readability／viewport-height）")
    want_out = f"docs/architecture/features/{stem}.html"
    if meta.get("output") != want_out:
        probs.add(f"{w}.meta.output", f"必須是 {want_out}")
    if facts["diagram_type"] == "sequence":
        ir_parts = {p.get("id"): p for p in ir.get("participants", [])}
        if set(ir_parts) != set(parts):
            probs.add(w, f"participants 集合與 facts 不同：多 {sorted(set(ir_parts) - set(parts))}、少 {sorted(set(parts) - set(ir_parts))}")
        for pid, p in ir_parts.items():
            if pid in parts and p.get("label") != parts[pid]["label"]:
                probs.add(w, f"participant {pid} 的 label 與 facts 不同")
        msgs = ir.get("messages", [])
        if [x.get("id") for x in msgs] != [s["id"] for s in steps]:
            probs.add(w, "messages 的 id 與順序必須逐一對應 facts.steps")
        for msg, s in zip(msgs, steps):
            for k in ("from", "to", "label"):
                if msg.get(k) != s[k]:
                    probs.add(w, f"message {s['id']} 的 {k} 與 facts 不同")
            if msg.get("variant", "default") != "default":
                probs.add(w, f"message {s['id']} 的 variant 必須是 default")
            if _bar_without_platform(msg.get("note")):
                probs.warn(w, f"message {s['id']} 的 note 有「｜」但不是 Linux｜Windows 描述")
        phases = {s["phase"] for s in steps}
        for seg in ir.get("segments", []):
            if seg.get("label") not in phases:
                probs.add(w, f"segment {seg.get('label')!r} 不是 facts.steps 用到的 phase")
    else:
        nodes = ir.get("components") or ir.get("nodes") or []
        edges = ir.get("connections") or ir.get("edges") or []
        if {n.get("id") for n in nodes} != set(parts):
            probs.add(w, "節點 id 集合必須等於 facts.participants")
        by_id = {e.get("id"): e for e in edges}
        if set(by_id) != {s["id"] for s in steps}:
            probs.add(w, "邊 id 集合必須等於 facts.steps")
        for s in steps:
            e = by_id.get(s["id"]) or {}
            for k in ("from", "to", "label"):
                if e and e.get(k) != s[k]:
                    probs.add(w, f"邊 {s['id']} 的 {k} 與 facts 不同")
    return probs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("facts", nargs="*", type=Path)
    ap.add_argument("--all", action="store_true", help="檢查 docs/architecture/features/ 下全部 *.facts.json")
    args = ap.parse_args()
    targets = sorted(FEATURES.glob("*.facts.json")) if args.all else [p.resolve() for p in args.facts]
    if not targets:
        print("沒有要檢查的檔案", file=sys.stderr)
        return 0 if args.all else 1
    total = Problems()
    for t in targets:
        probs = check(t)
        print(f"{'OK ' if not probs else 'NG '} {t.name}" + ("" if not probs else f"（{len(probs)} 項）")
              + ("" if not probs.warnings else f"，警告 {len(probs.warnings)} 項"))
        for p in probs:
            print(f"   - {p}")
        for p in probs.warnings:
            print(f"   ! {p}")
        total.extend(probs)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
