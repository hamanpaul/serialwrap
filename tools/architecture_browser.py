#!/usr/bin/env python3
"""以 file:// 驗收已提交的原生架構 HTML；截圖只作驗證證據。"""
import argparse
import hashlib
import json
from pathlib import Path
from playwright.sync_api import sync_playwright


# Archify 3.x 的 Reader 在圖下方附「節點索引」側欄（html[data-reader-rail="bottom"] 的 .reader-rail）；上游的
# visual-check 把它視為 below-the-fold 的閱讀材料，不計入一屏判定。這裡採同一定義，另外直接量主 SVG 是否完整落在第一屏、
# 側欄是否排在圖之後，並拒絕以 overflow 隱藏內容。
FIT_JS = """(() => {
  const root = document.documentElement;
  const svg = document.querySelector('.diagram-container > svg').getBoundingClientRect();
  const rail = document.querySelector('.reader-rail');
  const railBottom = root.getAttribute('data-reader-rail') === 'bottom' && rail && !rail.hidden
    && getComputedStyle(rail).display !== 'none';
  let railPx = 0, railTop = null;
  if (railBottom) {
    const style = getComputedStyle(rail), box = rail.getBoundingClientRect();
    railPx = box.height + (parseFloat(style.marginTop) || 0) + (parseFloat(style.marginBottom) || 0);
    railTop = box.top + scrollY;
  }
  return {width: innerWidth, height: innerHeight, scrollWidth: root.scrollWidth, scrollHeight: root.scrollHeight,
    readerRail: root.getAttribute('data-reader-rail'), railBelowFoldPx: Math.round(railPx), railTop: railTop,
    firstScreenHeight: Math.ceil(root.scrollHeight - railPx), svgTop: svg.top + scrollY, svgBottom: svg.bottom + scrollY,
    overflowY: [root, document.body].map(e => getComputedStyle(e).overflowY)};
})()"""


def review(html, output):
    html = Path(html).resolve()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    ir = json.loads(html.with_suffix(".json").read_text(encoding="utf-8"))
    nodes = {x["id"] for x in ir["components"]}
    edges = {x["id"] for x in ir["connections"]}
    errors, requests, measurements = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("request", lambda q: requests.append(q.url) if q.url.startswith(("https:", "http:")) else None)
        page.goto(html.as_uri(), wait_until="load")
        svg = page.locator(".diagram-container > svg")
        assert svg.count() == 1 and svg.is_visible()
        actual = svg.locator("g[data-node-id]")
        assert set(actual.evaluate_all("xs => xs.map(x=>x.dataset.nodeId)")) == nodes
        assert all(n.is_visible() for n in actual.all())
        paths = svg.locator("path[data-edge-id]").evaluate_all("xs=>xs.map(x=>({id:x.dataset.edgeId,d:x.getAttribute('d'),arrow:x.getAttribute('marker-end')}))")
        assert {x["id"] for x in paths} == edges
        assert all(x["d"] and x["arrow"] for x in paths)
        for w, h in [(1440,900),(1600,1000),(1920,1080),(2048,1320),(390,844)]:
            page.set_viewport_size({"width":w,"height":h})
            page.wait_for_timeout(300)
            m = page.evaluate(FIT_JS)
            assert m["scrollWidth"] <= w, m
            assert not {"hidden", "clip"}.intersection(m["overflowY"]), m
            assert m["railTop"] is None or m["railTop"] >= m["svgBottom"], m
            assert w < 1440 or (m["firstScreenHeight"] <= h and 0 <= m["svgTop"] and m["svgBottom"] <= h), m
            measurements.append(m)
            page.screenshot(path=str(output/f"diagram-{w}.png"), full_page=True)
        page.set_viewport_size({"width":1440,"height":900})
        for identity in ("service","arbiter","sessions","bridge","command-records","capture"):
            svg.locator(f'g[data-node-id="{identity}"]').click()
            assert identity == page.locator("#focus-id").inner_text().strip()
            source = page.locator("#focus-evidence-links a").first
            assert ir["meta"]["repository"]["revision"] in source.get_attribute("href")
            page.locator("#btn-reach-downstream").click()
            assert page.locator("#btn-reach-downstream").get_attribute("aria-pressed") == "true"
            page.locator("#btn-focus-clear").click()
        page.locator("#btn-node-finder").click()
        page.locator("#node-finder-input").fill("sessions")
        assert page.locator("#node-finder-results").is_visible()
        page.locator("#node-finder-close").click()
        before = page.locator("html").get_attribute("data-theme")
        page.locator("#btn-theme").click()
        assert before != page.locator("html").get_attribute("data-theme")
        page.screenshot(path=str(output/"diagram-dark.png"), full_page=True)
        assert not errors and not requests, (errors, requests)
        receipt = dict(ok=True, transport="file", file_navigation=True, html_sha256=hashlib.sha256(html.read_bytes()).hexdigest(), browser=browser.version, nodes=len(nodes), directional_svg_paths=len(edges), viewports=measurements, browser_errors=errors, http_requests=requests, runtime_e2e="not-run", user_acceptance="pending")
        browser.close()
    (output/"browser-review.json").write_text(json.dumps(receipt, indent=2)+"\n", encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(review(args.html, args.output), indent=2))
