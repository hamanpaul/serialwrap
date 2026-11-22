#!/usr/bin/env python3
"""驗證 serialwrap 架構文件（總覽＋功能頁）的來源證據、圖形投影、加工與逐位元組重建；不啟動 daemon。

- 總覽：facts.json 的每條 evidence 在釘選 revision 上成立，architecture.json 是它的投影。
- 功能頁：toolchain.json 記錄的每一頁以 tools/architecture_feature_check.py 驗事實與投影。
- 每一頁的 HTML 都是「Archify 原版交付＋tools/architecture_overlay.py 加工」：移除加工區塊即得原版交付
  （雜湊記在 toolchain），加工區塊必須是依 pages.json 與目前已整合頁面決定性產生的結果。
- --archify-root：以釘選的原生 Archify 重新交付每一頁，原版交付與加工後的 HTML 都必須逐位元組一致。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ARCH = Path("docs/architecture")
TOOLCHAIN_SCHEMA = 2
PAGE_ID_RE = re.compile(r"^\d{2}-[a-z0-9-]+$")
DIAGRAM_TYPES = {"architecture", "sequence", "workflow"}


def _import(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OVERLAY = _import("serialwrap_architecture_overlay", REPO / "tools/architecture_overlay.py")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重複 JSON key：{key}")
        result[key] = value
    return result


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _by_id(rows):
    result = {row["id"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("重複圖形身份")
    return result


# ---------------------------------------------------------------- 總覽：事實與投影

def verify(repo, facts=None, ir=None):
    repo = Path(repo).resolve()
    root = repo / ARCH
    facts = load(root / "facts.json") if facts is None else facts
    ir = load(root / "architecture.json") if ir is None else ir
    revision = facts["repository"]["revision"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("來源 revision 必須是完整 SHA")
    if facts["repository"]["url"] != "https://github.com/hamanpaul/serialwrap":
        raise ValueError("來源不是 serialwrap")
    if ir["diagram_type"] != "architecture" or ir["meta"].get("quality_profile") != "showcase":
        raise ValueError("必須保留原生架構圖與 showcase 驗收")
    for key in ("url", "revision"):
        if ir["meta"]["repository"][key] != facts["repository"][key]:
            raise ValueError("facts／IR 的來源不一致")
    blobs = {}
    anchors = [a for record in facts["components"] + facts["relations"] + facts["boundaries"] for a in record.get("evidence", [])]
    for record in facts["components"] + facts["relations"] + facts["boundaries"]:
        if not record.get("evidence"):
            raise ValueError("事實缺少證據")
    for anchor in anchors:
        path = anchor["path"]
        if Path(path).is_absolute() or ".." in Path(path).parts or path.startswith("docs/architecture/"):
            raise ValueError("來源不得越界或自我引用")
        if path not in blobs:
            blobs[path] = subprocess.check_output(["git", "show", f"{revision}:{path}"], cwd=repo).splitlines(keepends=True)
        lo, hi = anchor["line"], anchor["end_line"]
        if not 1 <= lo <= hi <= len(blobs[path]):
            raise ValueError("來源行範圍越界")
        actual = hashlib.sha256(b"".join(blobs[path][lo-1:hi])).hexdigest()
        if actual != anchor["excerpt_sha256"]:
            raise ValueError("來源片段 hash 不符")
    fn, nodes = _by_id(facts["components"]), _by_id(ir["components"])
    fr, edges = _by_id(facts["relations"]), _by_id(ir["connections"])
    if set(fn) != set(nodes) or set(fr) != set(edges):
        raise ValueError("元件／關係遺漏或捏造")
    for ident, record in fn.items():
        expected = {k: record[k] for k in ("id", "type", "label", "sublabel", "tag") if k in record}
        expected["sources"] = [{k: a[k] for k in ("path", "line", "end_line")} for a in record["evidence"][:3]]
        actual = {k: nodes[ident][k] for k in ("id", "type", "label", "sublabel", "tag", "sources") if k in nodes[ident]}
        if actual != expected:
            raise ValueError("元件語意漂移")
    for ident, record in fr.items():
        fields = ("id", "from", "to", "label", "variant")
        if {k: record[k] for k in fields if k in record} != {k: edges[ident][k] for k in fields if k in edges[ident]}:
            raise ValueError("關係語意漂移")
        if record["from"] not in fn or record["to"] not in fn:
            raise ValueError("不存在的關係端點")
    fields = ("kind", "label", "wraps")
    if [{k: b[k] for k in fields} for b in facts["boundaries"]] != [{k: b[k] for k in fields} for b in ir.get("boundaries", [])]:
        raise ValueError("邊界語意漂移")
    wrapped = [w for b in facts["boundaries"] for w in b["wraps"]]
    if len(wrapped) != len(set(wrapped)):
        raise ValueError("同一元件不得同時屬於兩個邊界")
    return {"ok": True, "source_revision": revision, "nodes": len(nodes), "relations": len(edges),
            "boundaries": len(facts["boundaries"])}


# ---------------------------------------------------------------- 頁面清單（pages.json）

def load_pages(repo=REPO, doc=None):
    doc = load(Path(repo) / ARCH / "pages.json") if doc is None else doc
    if doc.get("schema_version") != 1 or set(doc) != {"schema_version", "labels", "overview", "features"}:
        raise ValueError("pages.json：schema_version 必須為 1，且只有 labels／overview／features")
    overview = doc["overview"]
    if overview != {"id": "overview", "title": overview.get("title"), "nav": overview.get("nav"), "path": "architecture.html"} \
            or not all(isinstance(overview[k], str) and overview[k] for k in ("title", "nav")):
        raise ValueError("pages.json：overview 必須是 id=overview、path=architecture.html 並有 title／nav")
    seen = {"overview"}
    for index, feat in enumerate(doc["features"], 1):
        if set(feat) != {"id", "order", "title", "nav", "continuations"}:
            raise ValueError(f"pages.json：features[{index - 1}] 的鍵必須是 id／order／title／nav／continuations")
        if feat["order"] != index or not PAGE_ID_RE.fullmatch(feat["id"]) or feat["id"][:2] != f"{index:02d}":
            raise ValueError(f"pages.json：{feat['id']} 的序號必須連續且與 id 前兩碼相同")
        for part, cont in enumerate(feat["continuations"], 2):
            if cont != f"{feat['id']}-{part}":
                raise ValueError(f"pages.json：{feat['id']} 的續頁必須依序命名為 {feat['id']}-{part}")
        for ident in [feat["id"], *feat["continuations"]]:
            if ident in seen:
                raise ValueError(f"pages.json：重複的頁面 id {ident}")
            seen.add(ident)
        if not (isinstance(feat["title"], str) and feat["title"] and isinstance(feat["nav"], str) and feat["nav"]):
            raise ValueError(f"pages.json：{feat['id']} 缺 title 或 nav")
    return doc


def page_entries(doc):
    """依導覽列順序列出所有頁面：{id, title, short, path, parent?}（path 相對於 docs/architecture）。"""
    rows = [{"id": "overview", "title": doc["overview"]["title"], "short": doc["overview"]["nav"], "path": "architecture.html"}]
    for feat in doc["features"]:
        nn = f"{feat['order']:02d}"
        rows.append({"id": feat["id"], "title": f"{nn} {feat['title']}", "short": f"{nn} {feat['nav']}",
                     "path": f"features/{feat['id']}.html"})
        for part, cont in enumerate(feat["continuations"], 2):
            rows.append({"id": cont, "parent": feat["id"], "title": f"{nn} {feat['title']}（第 {part} 頁）",
                         "short": str(part), "path": f"features/{cont}.html"})
    return rows


def check_pages_readme(doc, readme_text: str) -> None:
    """features/README.md 的功能清單表格必須與 pages.json 的序號、id、標題逐列相同。"""
    rows = [(a, b, c.strip()) for a, b, c in re.findall(r"^\| (\d{2}) \| `([a-z0-9-]+)` \| ([^|]+?) \|", readme_text, re.M)]
    expected = [(f"{f['order']:02d}", f["id"], f["title"]) for f in doc["features"]]
    if rows != expected:
        raise ValueError(f"features/README.md 的功能清單與 pages.json 不一致：README {rows}，pages.json {expected}")


def overlay_manifest(doc, available):
    pages = []
    for row in page_entries(doc):
        page = {k: row[k] for k in ("id", "title", "short", "path")}
        if "parent" in row:
            page["parent"] = row["parent"]
        page["available"] = row["id"] in available
        pages.append(page)
    return {"schema": OVERLAY.SCHEMA, "labels": dict(doc["labels"]), "pages": pages}


def page_files(page_id):
    if page_id == "overview":
        return {"facts": "facts.json", "ir": "architecture.json", "html": "architecture.html"}
    return {"facts": f"features/{page_id}.facts.json", "ir": f"features/{page_id}.json", "html": f"features/{page_id}.html"}


# ---------------------------------------------------------------- toolchain 與各頁驗證

def verify_pages(repo=REPO, toolchain=None, pages=None, read=None, only=None):
    """核對 toolchain 記錄的每一頁：檔案雜湊、加工區塊、事實檢查；回傳每頁摘要。only 指定時只核對該頁（整合途中用）。"""
    repo = Path(repo).resolve()
    root = repo / ARCH
    read = read or (lambda rel: (root / rel).read_bytes())
    toolchain = load(root / "toolchain.json") if toolchain is None else toolchain
    pages = load_pages(repo, pages)
    check_pages_readme(pages, read("features/README.md").decode("utf-8"))
    if toolchain.get("schema_version") != TOOLCHAIN_SCHEMA:
        raise ValueError(f"toolchain.json 的 schema_version 必須為 {TOOLCHAIN_SCHEMA}")
    overlay = toolchain["overlay"]
    if overlay["tool"] != "tools/architecture_overlay.py" or overlay["version"] != OVERLAY.VERSION:
        raise ValueError("toolchain 記錄的加工工具或版本與 tools/architecture_overlay.py 不符")
    tool_bytes = (repo / overlay["tool"]).read_bytes()
    if sha256(tool_bytes) != overlay["sha256"]:
        raise ValueError("tools/architecture_overlay.py 與 toolchain 記錄的 skill 版 sha256 不同")
    known = {row["id"] for row in page_entries(pages)}
    recorded = toolchain["pages"]
    if "overview" not in recorded or set(recorded) - known:
        raise ValueError(f"toolchain 的頁面必須含 overview 且都在 pages.json：多 {sorted(set(recorded) - known)}")
    present = {p.name[: -len(".html")] for p in (root / "features").glob("*.html")}
    present |= {p.name[: -len(".facts.json")] for p in (root / "features").glob("*.facts.json")}
    if present - set(recorded):
        raise ValueError(f"features/ 有尚未整合（toolchain 沒有記錄）的頁面：{sorted(present - set(recorded))}")
    manifest_available = set(recorded)
    summary = {}
    feature_check = _import("serialwrap_architecture_feature_check", repo / "tools/architecture_feature_check.py")
    if only is not None and only not in recorded:
        raise ValueError(f"toolchain 沒有記錄 {only}")
    for page_id, entry in recorded.items():
        if only is not None and page_id != only:
            continue
        files = page_files(page_id)
        if {k: entry[k] for k in ("facts", "ir", "html")} != files:
            raise ValueError(f"{page_id}：toolchain 的檔案路徑必須是 {files}")
        if entry["diagram_type"] not in DIAGRAM_TYPES or entry["overlay_version"] != OVERLAY.VERSION:
            raise ValueError(f"{page_id}：diagram_type 或 overlay_version 不正確")
        if not re.fullmatch(r"[0-9a-f]{40}", entry["source_revision"]):
            raise ValueError(f"{page_id}：source_revision 必須是完整 SHA")
        data = {key: read(rel) for key, rel in files.items()}
        for key in ("facts", "ir", "html"):
            if sha256(data[key]) != entry["sha256"][key]:
                raise ValueError(f"{page_id}：{files[key]} 的 sha256 與 toolchain 不同")
        original = OVERLAY.remove(data["html"])
        if sha256(original) != entry["sha256"]["delivery"]:
            raise ValueError(f"{page_id}：移除加工後不是 toolchain 記錄的 Archify 原版交付")
        OVERLAY.verify(data["html"], overlay_manifest(pages, manifest_available), page_id,
                       original_sha256=entry["sha256"]["delivery"])
        ir = json.loads(data["ir"].decode("utf-8"))
        facts = json.loads(data["facts"].decode("utf-8"))
        if ir.get("diagram_type") != entry["diagram_type"] or facts["repository"]["revision"] != entry["source_revision"]:
            raise ValueError(f"{page_id}：IR 的 diagram_type 或 facts 的 revision 與 toolchain 不同")
        if page_id == "overview":
            result = verify(repo, facts, ir)
        else:
            problems = feature_check.check(root / files["facts"])
            if problems:
                raise ValueError(f"{page_id}：功能頁事實檢查失敗：" + "；".join(problems[:5]))
            result = {"ok": True, "source_revision": entry["source_revision"]}
        summary[page_id] = {**result, "delivery_sha256": entry["sha256"]["delivery"], "html_sha256": entry["sha256"]["html"]}
    return summary


def deliver(engine: Path, repo: Path, diagram_type: str, ir_path: Path, output: Path):
    """以原生 Archify 交付（離線、關閉更新檢查），回傳 receipt。"""
    env = {**os.environ, "ARCHIFY_UPDATE_CHECK_DISABLED": "1"}
    cp = subprocess.run(["node", str(engine / "bin/archify.mjs"), "deliver", diagram_type, str(ir_path), str(output),
                         "--repo-root", str(repo), "--quality", "showcase", "--json"],
                        capture_output=True, text=True, timeout=300, env=env)
    if cp.returncode:
        raise ValueError(f"Archify 交付失敗：{cp.stdout[-2000:]}{cp.stderr[-2000:]}")
    receipt = json.loads(cp.stdout)
    gate = receipt["validation"]
    if gate["checksPassed"] != 9 or gate["checkCount"] != 9 or gate["errors"] or gate["warnings"]:
        raise ValueError("原生 9/9 gate 未通過")
    if receipt["update"].get("reason") != "disabled" or receipt["update"].get("noticeRequired"):
        raise ValueError("重建時 Archify 更新檢查未關閉")
    return receipt


def check_engine(engine: Path, toolchain) -> None:
    pin = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=engine, text=True).strip()
    if pin != toolchain["archify_revision"]:
        raise ValueError("原生 Archify 版本不符")
    if load(engine / "skill-release.json").get("version") != toolchain["archify_version"]:
        raise ValueError("原生 Archify release 版本不符")


def rebuild(repo: Path, engine: Path, toolchain, pages, only=None):
    """每一頁重新交付；原版交付雜湊與「原版＋加工」都必須和已提交的檔案一致。"""
    root = repo / ARCH
    available = set(toolchain["pages"])
    receipts = {}
    with tempfile.TemporaryDirectory(prefix="serialwrap-architecture-") as td:
        for page_id, entry in toolchain["pages"].items():
            if only is not None and page_id != only:
                continue
            out = Path(td) / f"{page_id}.html"
            receipt = deliver(engine, repo, entry["diagram_type"], root / entry["ir"], out)
            delivered = out.read_bytes()
            if sha256(delivered) != entry["sha256"]["delivery"]:
                raise ValueError(f"{page_id}：Archify 原版交付與 toolchain 記錄的雜湊不同")
            final = OVERLAY.apply(delivered, overlay_manifest(pages, available), page_id)
            if final != (root / entry["html"]).read_bytes():
                raise ValueError(f"{page_id}：「原版交付＋加工」與已提交的 HTML 不是逐位元組相同")
            receipts[page_id] = receipt["validation"]
    return receipts


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--archify-root", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--page", help="只核對這一頁（整合途中、--refresh-nav 之前用；完整驗收請不帶此參數）")
    args = parser.parse_args()
    repo = args.repo_root.resolve()
    root = repo / ARCH
    toolchain = load(root / "toolchain.json")
    pages = load_pages(repo)
    result = {"ok": True, "pages": verify_pages(repo, toolchain, pages, only=args.page)}
    if args.page:
        result["partial"] = args.page
    elif toolchain["pages"]["overview"]["source_revision"] != result["pages"]["overview"]["source_revision"]:
        raise ValueError("toolchain 的來源 revision 與總覽 facts 不一致")
    # CI 重建用的引擎必須與 toolchain 釘的是同一個 commit，且必須關閉 Archify 的連網更新檢查。
    workflow = (repo / ".github/workflows/architecture-html.yml").read_text(encoding="utf-8")
    if f"ref: {toolchain['archify_revision']}" not in workflow or "ARCHIFY_UPDATE_CHECK_DISABLED: '1'" not in workflow:
        raise ValueError("CI workflow 的 Archify 釘選或更新檢查設定與 toolchain 不一致")
    if args.archify_root:
        engine = args.archify_root.resolve()
        check_engine(engine, toolchain)
        result["native_delivery"] = rebuild(repo, engine, toolchain, pages, only=args.page)
        result["exact_html"] = True
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1)
