#!/usr/bin/env python3
"""以 file:// 驗收已提交的架構 HTML（總覽＋已整合的功能頁）；截圖只作驗證證據。

每一頁：真實 SVG 節點與方向箭頭和 IR 一致、預設可見、無瀏覽器錯誤與網路請求、不水平溢出。
總覽：主 SVG 完整落在四個 desktop viewport 的第一屏（Archify 3.x 圖下方的節點索引側欄不計，與上游 visual-check
相同），並驗 focus／reach／finder／theme。功能頁（直向閱讀）：第一屏看得到標題、全部參與者表頭與第一個 phase。
加工：滾輪（一屏頁：指標在圖上縮放且以游標為錨點、圖外捲動頁面、Ctrl＋滾輪不攔截、觸控板小 delta 不會一下縮到底；
長頁：一般滾輪捲動頁面、Alt＋滾輪縮放）；導覽列每個連結的目標檔存在、灰色項目不可點、目前頁唯一，並實際以 file:// 點一次連結。
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("serialwrap_architecture_review", REPO / "tools/architecture_review.py")
REVIEW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(REVIEW)

DESKTOP = [(1440, 900), (1600, 1000), (1920, 1080), (2048, 1320)]
PANELS = ".diagram-nav, .focus-chip, .node-finder, .diagram-guide, .overview-map, .route-probe, .semantic-lens"

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

# 導覽列位置與內容：不得蓋到標題文字、工具列或圖。
NAV_JS = """(() => {
  const box = e => { const r = e.getBoundingClientRect(); return {left: r.left, top: r.top + scrollY, right: r.right, bottom: r.bottom + scrollY}; };
  const nav = document.getElementById('sw-overlay-nav');
  const title = document.querySelector('h1'); const range = document.createRange(); range.selectNodeContents(title);
  const t = range.getBoundingClientRect();
  return {nav: box(nav), docked: nav.classList.contains('is-docked'), title: {left: t.left, top: t.top + scrollY, right: t.right, bottom: t.bottom + scrollY},
    toolbar: box(document.querySelector('.toolbar')), container: box(document.querySelector('.diagram-container')),
    links: [...nav.querySelectorAll('a')].map(a => ({href: a.href, raw: a.getAttribute('href')})),
    disabled: [...nav.querySelectorAll('[aria-disabled="true"]')].map(e => ({tag: e.tagName, href: e.getAttribute('href'), cursor: getComputedStyle(e).cursor})),
    current: [...nav.querySelectorAll('[aria-current="page"]')].map(e => e.textContent),
    items: nav.querySelectorAll('.sw-ov-item').length,
    hint: document.getElementById('sw-overlay-hint').textContent, mode: document.getElementById('sw-overlay-hint').dataset.wheelMode,
    tall: document.querySelector('.diagram-container > svg').clientHeight > innerHeight};
})()"""


def _overlap(a, b):
    return a["left"] < b["right"] and b["left"] < a["right"] and a["top"] < b["bottom"] and b["top"] < a["bottom"]


def _ir_sets(ir):
    kind = ir["diagram_type"]
    if kind == "sequence":
        return {x["id"] for x in ir["participants"]}, {x["id"] for x in ir["messages"]}
    if kind == "workflow":
        return {x["id"] for x in ir["nodes"]}, {x["id"] for x in ir["edges"]}
    return {x["id"] for x in ir["components"]}, {x["id"] for x in ir["connections"]}


SETTLE_JS = """(() => { const r = e => { const b = e.getBoundingClientRect(); return [b.left, b.top, b.right, b.bottom].map(Math.round).join(','); };
    const n = document.getElementById('sw-overlay-nav'), t = document.querySelector('h1'), bar = document.querySelector('.toolbar');
    const s = document.querySelector('.diagram-container > svg');
    return [n ? r(n) : '', r(t), r(bar), r(s), n && n.classList.contains('is-docked')].join('|'); })()"""


def _settle(page):
    """等版面穩定：Reader 在載入與 resize 後的幾個畫格內才重排容器、標題與工具列，加工的導覽列隨後重新定位。"""
    page.wait_for_timeout(300)
    last = None
    for _ in range(30):
        now = page.evaluate(SETTLE_JS)
        if now == last:
            return
        last = now
        page.wait_for_timeout(150)


def _wait_scale(page, previous, timeout=1500):
    for _ in range(timeout // 50):
        page.wait_for_timeout(50)
        scale = page.evaluate("Archify.view.state().scale")
        if scale != previous:
            page.wait_for_timeout(350)
            return page.evaluate("Archify.view.state().scale")
    return page.evaluate("Archify.view.state().scale")


def _center(box):
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _check_wheel(page, nodes):
    """依目前視窗判斷一屏頁或長頁，驗證滾輪行為；回傳量測值。"""
    page.evaluate("Archify.view.reset(); scrollTo(0, 0)")
    page.wait_for_timeout(300)
    tall = page.evaluate("document.querySelector('.diagram-container > svg').clientHeight > innerHeight")
    target = next(n for n in sorted(nodes) if page.locator(f'.diagram-container > svg g[data-node-id="{n}"]').bounding_box()["y"] > 0)
    x, y = _center(page.locator(f'.diagram-container > svg g[data-node-id="{target}"]').bounding_box())
    assert page.evaluate(f"!document.elementFromPoint({x}, {y}).closest({json.dumps(PANELS)})")
    page.mouse.move(x, y)
    result = {"tall": tall, "node": target}
    if not tall:
        s0 = page.evaluate("Archify.view.state().scale")
        page.mouse.wheel(0, -100)
        s1 = _wait_scale(page, s0)
        x1, y1 = _center(page.locator(f'.diagram-container > svg g[data-node-id="{target}"]').bounding_box())
        assert s1 > s0, ("滾輪沒有放大", s0, s1)
        assert page.evaluate("scrollY") == 0, "滾輪在圖上時頁面不應捲動"
        drift = max(abs(x1 - x), abs(y1 - y))
        assert drift <= 3, ("游標下的節點在縮放後偏移", drift)
        page.mouse.wheel(0, 100)
        s2 = _wait_scale(page, s1)
        assert s2 < s1, ("反向滾輪沒有縮小", s1, s2)
        result.update(zoom=[s0, s1, s2], anchor_drift_px=round(drift, 2))
        # 觸控板：60 次 −4px 的小 delta 不能一下縮到底（上限 300%）
        page.evaluate("Archify.view.reset()"); page.wait_for_timeout(300)
        for _ in range(60):
            page.mouse.wheel(0, -4); page.wait_for_timeout(8)
        page.wait_for_timeout(400)
        trackpad = page.evaluate("Archify.view.state().scale")
        assert 1 < trackpad <= 2.5, ("觸控板小 delta 的縮放不合理", trackpad)
        result["trackpad_60x4"] = trackpad
    else:
        s0, y0 = page.evaluate("Archify.view.state().scale"), page.evaluate("scrollY")
        page.mouse.wheel(0, 300); page.wait_for_timeout(400)
        assert page.evaluate("Archify.view.state().scale") == s0, "長頁一般滾輪不應縮放"
        assert page.evaluate("scrollY") > y0, "長頁一般滾輪應捲動頁面"
        page.evaluate("scrollTo(0, 0)"); page.wait_for_timeout(200)
        x, y = _center(page.locator(f'.diagram-container > svg g[data-node-id="{target}"]').bounding_box())
        page.mouse.move(x, y)
        page.keyboard.down("Alt"); page.mouse.wheel(0, -100); page.keyboard.up("Alt")
        s1 = _wait_scale(page, s0)
        assert s1 > s0, ("長頁 Alt＋滾輪沒有放大", s0, s1)
        x1, y1 = _center(page.locator(f'.diagram-container > svg g[data-node-id="{target}"]').bounding_box())
        result.update(alt_zoom=[s0, s1], anchor_drift_px=round(max(abs(x1 - x), abs(y1 - y)), 2))
    page.evaluate("Archify.view.reset()"); page.wait_for_timeout(300)
    # Ctrl＋滾輪留給瀏覽器：事件不被攔截、Archify 不縮放
    prevented, scale = page.evaluate("""([x, y]) => { const t = document.elementFromPoint(x, y);
        const e = new WheelEvent('wheel', {deltaY: -100, ctrlKey: true, clientX: x, clientY: y, bubbles: true, cancelable: true});
        t.dispatchEvent(e); return [e.defaultPrevented, Archify.view.state().scale]; }""", [x, y])
    assert not prevented and scale == 1, ("Ctrl＋滾輪被攔截", prevented, scale)
    # 指標不在圖上：照常捲動頁面
    page.evaluate("scrollTo(0, 0)"); page.wait_for_timeout(200)
    page.mouse.move(8, 300)
    page.mouse.wheel(0, 300); page.wait_for_timeout(400)
    assert page.evaluate("scrollY") > 0 and page.evaluate("Archify.view.state().scale") == 1, "圖外的滾輪應捲動頁面"
    page.evaluate("scrollTo(0, 0)"); page.wait_for_timeout(200)
    result["ctrl_wheel_prevented"] = prevented
    return result


def _check_nav(page, html: Path, expected_items: int):
    nav = page.evaluate(NAV_JS)
    assert nav["items"] == expected_items, ("導覽列項目數", nav["items"], expected_items)
    assert len(nav["current"]) == 1, ("目前頁必須唯一", nav["current"])
    for link in nav["links"]:
        target = Path(unquote(urlparse(link["href"]).path))
        assert link["raw"] and not link["raw"].startswith(("/", "http", "file:")), ("導覽列連結必須是相對路徑", link)
        assert target.is_file(), ("導覽列連結指向不存在的檔案", link)
    for item in nav["disabled"]:
        assert item["tag"] != "A" and item["href"] is None and item["cursor"] == "not-allowed", ("灰色項目必須不可點", item)
    return nav


def review_page(browser, html: Path, output: Path, one_screen: bool, expected_items: int, ir):
    nodes, edges = _ir_sets(ir)
    errors, requests, measurements = [], [], []
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda q: requests.append(q.url) if q.url.startswith(("https:", "http:")) else None)
    page.goto(html.as_uri(), wait_until="load")
    _settle(page)
    svg = page.locator(".diagram-container > svg")
    assert svg.count() == 1 and svg.is_visible()
    actual = svg.locator("g[data-node-id]")
    assert set(actual.evaluate_all("xs => xs.map(x=>x.dataset.nodeId)")) == nodes
    assert all(n.is_visible() for n in actual.all())
    # architecture 的箭頭是 path[data-edge-id]；sequence 是 g[data-edge-id] 內的 path。每條關係都要有一條帶 marker-end 的真實路徑。
    rows = svg.locator("[data-edge-id]").evaluate_all("""xs => xs.map(x => { const p = x.matches('path[marker-end]') ? x : x.querySelector('path[marker-end]');
        return {id: x.dataset.edgeId, d: p && p.getAttribute('d'), arrow: p && p.getAttribute('marker-end')}; })""")
    assert {x["id"] for x in rows} == edges
    directional = {x["id"] for x in rows if x["d"] and x["arrow"]}
    assert directional == edges, sorted(edges - directional)
    stem = html.stem
    for w, h in DESKTOP + [(390, 844)]:
        page.set_viewport_size({"width": w, "height": h})
        _settle(page)
        m = page.evaluate(FIT_JS)
        assert m["scrollWidth"] <= w, m
        assert not {"hidden", "clip"}.intersection(m["overflowY"]), m
        assert m["railTop"] is None or m["railTop"] >= m["svgBottom"], m
        if w >= 1440:
            nav = page.evaluate(NAV_JS)
            assert nav["docked"], ("desktop 導覽列應嵌在標題與工具列之間", w, nav["nav"])
            for other in ("title", "toolbar", "container"):
                assert not _overlap(nav["nav"], nav[other]), ("導覽列蓋到", other, w, nav["nav"], nav[other])
            m["nav"] = nav["nav"]
            if one_screen:
                assert m["firstScreenHeight"] <= h and 0 <= m["svgTop"] and m["svgBottom"] <= h, m
            else:
                first = page.evaluate("""(() => ({title: document.querySelector('h1').getBoundingClientRect().bottom,
                    heads: Math.max(...[...document.querySelectorAll('.diagram-container > svg g[data-node-id]')].map(e => e.getBoundingClientRect().bottom)),
                    phase: (document.querySelector('.diagram-container > svg [data-graph-role="segment-label"][data-segment-id="0"]') || {getBoundingClientRect: () => ({top: 0})}).getBoundingClientRect().top}))()""")
                assert first["title"] <= h and first["heads"] <= h and first["phase"] <= h, ("第一屏必須看得到標題、參與者表頭與第一個 phase", w, first)
                m["first_screen"] = first
        measurements.append(m)
        page.screenshot(path=str(output / f"{stem}-{w}.png"), full_page=one_screen)
    page.set_viewport_size({"width": 1440, "height": 900})
    _settle(page)
    nav = _check_nav(page, html, expected_items)
    wheel = _check_wheel(page, nodes)
    if one_screen:
        for identity in ("service", "arbiter", "sessions", "bridge", "command-records", "capture", "pal-serial", "pal-rpc"):
            svg.locator(f'g[data-node-id="{identity}"]').click()
            assert identity == page.locator("#focus-id").inner_text().strip()
            source = page.locator("#focus-evidence-links a").first
            assert ir["meta"]["repository"]["revision"] in source.get_attribute("href")
            reach = page.locator("#btn-reach-downstream")
            if reach.is_enabled():  # 平台層節點沒有下游，Archify 會停用此按鈕
                reach.click()
                assert reach.get_attribute("aria-pressed") == "true"
            else:
                assert identity.startswith("pal-"), identity
            page.locator("#btn-focus-clear").click()
        page.locator("#btn-node-finder").click()
        page.locator("#node-finder-input").fill("sessions")
        assert page.locator("#node-finder-results").is_visible()
        page.locator("#node-finder-close").click()
    before = page.locator("html").get_attribute("data-theme")
    page.locator("#btn-theme").click()
    assert before != page.locator("html").get_attribute("data-theme")
    page.screenshot(path=str(output / f"{stem}-dark.png"), full_page=one_screen)
    navigated = None
    if nav["links"]:
        link = page.locator("#sw-overlay-nav a").first
        expected = Path(unquote(urlparse(nav["links"][0]["href"]).path))
        link.click()
        page.wait_for_load_state("load")
        navigated = unquote(urlparse(page.url).path)
        assert Path(navigated) == expected, ("點導覽列連結後沒有到達目標頁", navigated, expected)
    assert not errors and not requests, (errors, requests)
    page.close()
    return {"html": str(html.relative_to(REPO)) if html.is_relative_to(REPO) else str(html),
            "html_sha256": hashlib.sha256(html.read_bytes()).hexdigest(), "one_screen": one_screen,
            "nodes": len(nodes), "directional_svg_paths": len(edges), "viewports": measurements,
            "nav": {"items": nav["items"], "links": [x["raw"] for x in nav["links"]], "disabled": len(nav["disabled"]),
                    "current": nav["current"], "hint": nav["hint"]},
            "wheel": wheel, "file_navigation_click": navigated, "browser_errors": errors, "http_requests": requests}


def review(html, output, all_pages=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    root = REPO / REVIEW.ARCH
    toolchain = REVIEW.load(root / "toolchain.json")
    pages = REVIEW.load_pages(REPO)
    expected_items = sum(1 for row in REVIEW.page_entries(pages) if "parent" not in row)
    targets = []
    if all_pages:
        targets = [(page_id, root / entry["html"], root / entry["ir"]) for page_id, entry in toolchain["pages"].items()]
    else:
        html = Path(html).resolve()
        match = [pid for pid, entry in toolchain["pages"].items() if (root / entry["html"]).resolve() == html]
        page_id = match[0] if match else None
        targets = [(page_id, html, html.with_suffix(".json"))]
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for page_id, html_path, ir_path in targets:
            ir = json.loads(Path(ir_path).read_text(encoding="utf-8"))
            results.append({"page": page_id, **review_page(browser, Path(html_path).resolve(), output, page_id == "overview",
                                                           expected_items, ir)})
        version = browser.version
        browser.close()
    receipt = dict(ok=True, transport="file", file_navigation=True, browser=version, pages=results,
                   runtime_e2e="not-run", user_acceptance="pending")
    (output / "browser-review.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--html", type=Path)
    group.add_argument("--all", action="store_true", help="驗收 toolchain.json 記錄的所有頁面")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(review(args.html, args.output, args.all), indent=2, ensure_ascii=False))
