#!/usr/bin/env python3
"""驗證 serialwrap 的來源證據、圖形投影與原生 HTML；不啟動 daemon。"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重複 JSON key：{key}")
        result[key] = value
    return result


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique)


def _by_id(rows):
    result = {row["id"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("重複圖形身份")
    return result


def verify(repo, facts=None, ir=None):
    repo = Path(repo).resolve()
    root = repo / "docs/architecture"
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
    for record in facts["components"] + facts["relations"] + facts["boundaries"]:
        if not record.get("evidence"):
            raise ValueError("事實缺少證據")
        for anchor in record["evidence"]:
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
    return {"ok": True, "source_revision": revision, "nodes": len(nodes), "relations": len(edges)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--archify-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo = args.repo_root.resolve()
    result = verify(repo)
    root = repo / "docs/architecture"
    toolchain = load(root / "toolchain.json")
    for name in ("facts.json", "architecture.json", "architecture.html"):
        actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if actual != toolchain["sha256"][name]:
            raise ValueError(f"交付檔案 hash 漂移：{name}")
    if args.archify_root:
        engine = args.archify_root.resolve()
        pin = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=engine, text=True).strip()
        if pin != toolchain["archify_revision"]:
            raise ValueError("原生 Archify 版本不符")
        with tempfile.TemporaryDirectory(prefix="serialwrap-architecture-") as td:
            output = Path(td) / "architecture.html"
            cp = subprocess.run(["node", str(engine / "bin/archify.mjs"), "deliver", "architecture", str(root / "architecture.json"), str(output), "--repo-root", str(repo), "--quality", "showcase", "--json"], capture_output=True, text=True, check=True, timeout=120)
            receipt = json.loads(cp.stdout)
            gate = receipt["validation"]
            if gate["checksPassed"] != 9 or gate["checkCount"] != 9 or gate["errors"] or gate["warnings"]:
                raise ValueError("原生 9/9 gate 未通過")
            if output.read_bytes() != (root / "architecture.html").read_bytes():
                raise ValueError("HTML 並非此 IR 的逐位元組原生輸出")
            result["native_delivery"] = receipt
            result["exact_html"] = True
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
