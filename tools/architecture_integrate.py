#!/usr/bin/env python3
"""把一個已合入的功能頁整合進架構文件：事實檢查 → 以原版 Archify 驗證交付 → 套用加工 → 更新 toolchain。

用法（在含該功能頁三個檔案的 serialwrap checkout 上）：
  python3 tools/architecture_integrate.py 01-command-exec --archify-root <archify>/archify
  python3 tools/architecture_integrate.py --refresh-nav          # 依目前已整合頁面重新產生所有頁的導覽列
  python3 tools/architecture_integrate.py 01-command-exec --archify-root <archify>/archify --refresh-nav
  python3 tools/architecture_integrate.py overview --archify-root <archify>/archify   # 總覽重新交付後

<page-id> 模式只讀寫該頁的 HTML 與 toolchain.json 的該頁項目，不碰別頁；別頁導覽列上該頁仍是灰色，
直到執行 --refresh-nav（只替換各頁的加工區塊，原版交付不變，事後以 tools/architecture_review.py 驗證）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("serialwrap_architecture_review", REPO / "tools/architecture_review.py")
REVIEW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(REVIEW)
FEATURE = REVIEW._import("serialwrap_architecture_feature_check", REPO / "tools/architecture_feature_check.py")
OVERLAY = REVIEW.OVERLAY
ROOT = REPO / REVIEW.ARCH


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def integrate(page_id: str, engine: Path, toolchain: dict, pages: dict) -> list[str]:
    known = {row["id"] for row in REVIEW.page_entries(pages)}
    if page_id not in known:
        raise ValueError(f"{page_id} 不在 docs/architecture/pages.json（續頁要先列進 continuations）")
    files = REVIEW.page_files(page_id)
    for rel in files.values():
        if not (ROOT / rel).is_file():
            raise ValueError(f"缺少 docs/architecture/{rel}")
    # 1 事實與投影（總覽用 architecture_review.verify，功能頁用 architecture_feature_check）
    if page_id == "overview":
        REVIEW.verify(REPO)
    else:
        problems = FEATURE.check(ROOT / files["facts"])
        if problems:
            raise ValueError("事實檢查失敗：\n  " + "\n  ".join(problems))
    facts = REVIEW.load(ROOT / files["facts"])
    ir = REVIEW.load(ROOT / files["ir"])
    # 2 交付的 HTML 必須是原版 Archify 對這份 IR 的輸出（未加工、未手改）
    html = (ROOT / files["html"]).read_bytes()
    if b"SW-OVERLAY" in html:
        original = OVERLAY.remove(html)
    else:
        original = html
    REVIEW.check_engine(engine, toolchain)
    with tempfile.TemporaryDirectory(prefix="serialwrap-integrate-") as td:
        out = Path(td) / "page.html"
        REVIEW.deliver(engine, REPO, ir["diagram_type"], ROOT / files["ir"], out)
        if out.read_bytes() != original:
            raise ValueError(f"{files['html']} 不是原版 Archify 對 {files['ir']} 的逐位元組輸出（被手改或用了別的引擎）")
    # 3 套用加工（目前已整合的頁面＋本頁）
    available = set(toolchain["pages"]) | {page_id}
    final = OVERLAY.apply(original, REVIEW.overlay_manifest(pages, available), page_id)
    (ROOT / files["html"]).write_bytes(final)
    # 4 toolchain 該頁項目
    toolchain["pages"][page_id] = {
        "diagram_type": ir["diagram_type"],
        "source_revision": facts["repository"]["revision"],
        **files,
        "overlay_version": OVERLAY.VERSION,
        "sha256": {
            "facts": REVIEW.sha256((ROOT / files["facts"]).read_bytes()),
            "ir": REVIEW.sha256((ROOT / files["ir"]).read_bytes()),
            "delivery": REVIEW.sha256(original),
            "html": REVIEW.sha256(final),
        },
    }
    order = ["overview"] + [row["id"] for row in REVIEW.page_entries(pages) if row["id"] != "overview"]
    toolchain["pages"] = {k: toolchain["pages"][k] for k in order if k in toolchain["pages"]}
    return [f"docs/architecture/{files['html']}"]


def refresh_nav(toolchain: dict, pages: dict) -> list[str]:
    available = set(toolchain["pages"])
    changed = []
    for page_id, entry in toolchain["pages"].items():
        path = ROOT / entry["html"]
        current = path.read_bytes()
        original = OVERLAY.remove(current)
        if REVIEW.sha256(original) != entry["sha256"]["delivery"]:
            raise ValueError(f"{page_id}：移除加工後與 toolchain 記錄的原版交付不同，拒絕覆寫")
        final = OVERLAY.apply(original, REVIEW.overlay_manifest(pages, available), page_id)
        if final != current:
            path.write_bytes(final)
            changed.append(f"docs/architecture/{entry['html']}")
        entry["overlay_version"] = OVERLAY.VERSION
        entry["sha256"]["html"] = REVIEW.sha256(final)
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("page", nargs="?")
    parser.add_argument("--archify-root", type=Path)
    parser.add_argument("--refresh-nav", action="store_true")
    args = parser.parse_args(argv)
    if not args.page and not args.refresh_nav:
        parser.error("請指定 page id 或 --refresh-nav")
    if args.page and not args.archify_root:
        parser.error("整合功能頁需要 --archify-root（以原版 Archify 驗證交付）")
    try:
        toolchain = REVIEW.load(ROOT / "toolchain.json")
        pages = REVIEW.load_pages(REPO)
        changed = []
        if args.page:
            changed += integrate(args.page, args.archify_root.resolve(), toolchain, pages)
        if args.refresh_nav:
            changed += [c for c in refresh_nav(toolchain, pages) if c not in changed]
        _write_json(ROOT / "toolchain.json", toolchain)
        changed.append("docs/architecture/toolchain.json")
        if args.refresh_nav:
            REVIEW.verify_pages(REPO)
    except (OSError, ValueError) as exc:
        print(f"整合失敗：{exc}", file=sys.stderr)
        return 1
    print("已更新：\n  " + "\n  ".join(changed))
    if args.page and not args.refresh_nav:
        print("提醒：別頁導覽列上這頁仍是灰色；全部整合完後執行 --refresh-nav，再跑 tools/architecture_review.py 與 tools/architecture_browser.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
