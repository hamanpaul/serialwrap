#!/usr/bin/env python3
"""Add a versioned, removable viewer overlay to an original Archify delivery.

The overlay is the only HTML change this skill allows after delivery. It adds
(a) wheel zoom that calls the public ``Archify.view`` API and (b) a navigation
bar between pages of the same diagram set. Everything lives in one marked block
right after ``<body>``; removing that block restores the original delivery byte
for byte. The output is deterministic and idempotent.

  apply   --html ORIGINAL --pages PAGES.json --page ID --output FINAL [--receipt R.json]
  remove  --html FINAL --output ORIGINAL
  verify  --html FINAL --pages PAGES.json --page ID [--original ORIGINAL | --original-sha256 HEX]

PAGES.json (viewer-overlay-pages/v1)::

  {"schema": "viewer-overlay-pages/v1",
   "labels": {"nav": "...", "wheel": "...", "alt_wheel": "...", "missing": "..."},   (optional)
   "pages": [{"id": "overview", "title": "Overview", "short": "Overview",
              "path": "architecture.html", "available": true},
             {"id": "01-x", "title": "01 X", "short": "01", "path": "features/01-x.html",
              "available": false},
             {"id": "01-x-2", "parent": "01-x", "title": "01 X (2)", "short": "2",
              "path": "features/01-x-2.html", "available": false}]}

Paths are POSIX paths relative to one common root; links are made relative to
the current page. Pages that are not available render as disabled text, never
as links. A ``parent`` page is a continuation shown as a small part link after
its parent item instead of a separate item.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import posixpath
import re
import sys
from pathlib import Path

VERSION = 2  # 2：導覽列定位改為工具列帶→空白帶→一般區塊的明確規則
SCHEMA = 'viewer-overlay-pages/v1'
START = '<!-- SW-OVERLAY:START'
END = '<!-- SW-OVERLAY:END -->\n'
ANCHOR = '<body>\n'
ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')
MARK_RE = re.compile(r'^<!-- SW-OVERLAY:START viewer-overlay/(\d+) page=([a-z0-9-]+) original-sha256=([0-9a-f]{64}) -->\n')
DEFAULT_LABELS = {
    'nav': '功能導覽',
    'wheel': '滾輪縮放',
    'alt_wheel': 'Alt＋滾輪縮放',
    'missing': '尚未提供',
}
# 加工區塊不得引入任何網路或外部資源。
FORBIDDEN = re.compile(r'https?:|<link|@import|url\(|fetch\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon|import\(|\bsrc\s*=|'
                       r'href\s*=\s*"(?:[a-z][a-z0-9+.-]*:|//)', re.I)

STYLE = """<style id="sw-overlay-style">
#sw-overlay-nav{box-sizing:border-box;display:flex;align-items:center;gap:6px;margin:8px 16px 0;font-family:inherit;font-size:10.5px;font-weight:500;line-height:1.2;color:var(--toolbar-text,var(--text,#e2e8f0));z-index:5}
#sw-overlay-nav.is-docked{position:absolute;margin:0}
#sw-overlay-nav .sw-ov-hint{flex:none;padding:2px 6px;border:1px dashed var(--toolbar-border,#334155);border-radius:999px;color:var(--text-muted,#94a3b8);white-space:nowrap}
#sw-overlay-nav .sw-ov-list{display:flex;flex-wrap:wrap;align-content:center;gap:3px;margin:0;padding:0;list-style:none;min-width:0}
#sw-overlay-nav .sw-ov-list>li{display:flex;align-items:center;gap:2px}
#sw-overlay-nav .sw-ov-item,#sw-overlay-nav .sw-ov-part{display:inline-block;padding:2px 5px;border:1px solid var(--toolbar-border,#334155);border-radius:999px;background:var(--toolbar-bg,rgba(15,23,42,.8));color:inherit;text-decoration:none;white-space:nowrap}
#sw-overlay-nav .sw-ov-part{padding:2px 4px}
#sw-overlay-nav a.sw-ov-item:hover,#sw-overlay-nav a.sw-ov-part:hover{background:var(--toolbar-hover,rgba(15,23,42,.95));border-color:var(--arrow-emphasis,#34d399)}
#sw-overlay-nav a:focus-visible{outline:2px solid var(--arrow-emphasis,#34d399);outline-offset:1px}
#sw-overlay-nav .is-current{border-color:var(--arrow-emphasis,#34d399);color:var(--arrow-emphasis,#34d399);font-weight:700}
#sw-overlay-nav .is-missing{opacity:.45;border-style:dashed;cursor:not-allowed}
html[data-present="true"] #sw-overlay-nav,html[data-embed="true"] #sw-overlay-nav{display:none}
@media print{#sw-overlay-nav{display:none}}
</style>
"""

# 只用 Archify.view 的公開方法（state／centerAt／zoomIn／zoomOut）；不讀寫其他內部狀態，不連網。
SCRIPT = """<script id="sw-overlay-script">
(function () {
  'use strict';
  var nav = document.getElementById('sw-overlay-nav');
  var hint = document.getElementById('sw-overlay-hint');
  var STEP = 40, COOLDOWN = 110, IDLE = 220;
  var PANELS = '.diagram-nav, .focus-chip, .node-finder, .diagram-guide, .overview-map, .route-probe, .semantic-lens';
  var acc = 0, lastAt = 0, coolUntil = 0;
  function view() { return window.Archify && window.Archify.view; }
  function diagram() { return document.querySelector('.diagram-container > svg'); }
  // 判斷依據：主 SVG 的版面高度（不含 CSS 縮放）是否超過視窗高度。
  function tall() { var svg = diagram(); return !!svg && svg.clientHeight > window.innerHeight; }
  function syncHint() {
    if (!hint) return;
    var t = tall();
    hint.textContent = t ? hint.getAttribute('data-alt-text') : hint.getAttribute('data-wheel-text');
    hint.setAttribute('data-wheel-mode', t ? 'alt' : 'wheel');
  }
  function overDiagram(target) {
    var box = document.querySelector('.diagram-container');
    if (!box || !target || !box.contains(target)) return false;
    return !(target.closest && target.closest(PANELS));
  }
  // 以游標為錨點：算出縮放後應置中的邏輯座標，交給公開的 centerAt。centerAt 把該點放到 SVG 中心
  // （圖比視窗高時改為捲到視窗垂直中心），故反推中心點，使游標下的內容在縮放後仍留在游標下。
  function anchored(v, svg, next, e) {
    var vb = svg.viewBox && svg.viewBox.baseVal;
    var w = svg.clientWidth || 1, h = svg.clientHeight || 1;
    if (!vb || vb.width <= 0 || vb.height <= 0) return false;
    var rect = svg.getBoundingClientRect();
    var shown = rect.width / w;
    if (!(shown > 0)) return false;
    var t = readTranslate(svg);
    var baseLeft = rect.left - t[0], baseTop = rect.top - t[1];
    var ux = (e.clientX - rect.left) / shown, uy = (e.clientY - rect.top) / shown;
    var cx = ux + (w / 2 - (e.clientX - baseLeft)) / next;
    var cy = h > window.innerHeight ? uy - (e.clientY - window.innerHeight / 2) / next
                                    : uy + (h / 2 - (e.clientY - baseTop)) / next;
    var k = Math.min(w / vb.width, h / vb.height);
    var offX = (w - vb.width * k) / 2, offY = (h - vb.height * k) / 2;
    return v.centerAt(vb.x + (cx - offX) / k, vb.y + (cy - offY) / k, { scale: next, instant: true }) !== false;
  }
  function readTranslate(svg) {
    var m = (getComputedStyle(svg).transform || '').match(/^matrix\\(([^)]+)\\)$/);
    if (!m) return [0, 0];
    var p = m[1].split(',').map(Number);
    return p.length === 6 && p.every(isFinite) ? [p[4], p[5]] : [0, 0];
  }
  function step(dir, e) {
    var v = view();
    if (!v) return;
    var svg = diagram();
    var st = typeof v.state === 'function' ? v.state() : null;
    if (st && svg && typeof v.centerAt === 'function' && window.innerWidth > 720) {
      var cur = Math.round(st.scale * 4) / 4;
      var next = Math.max(0.25, Math.min(3, cur + 0.25 * dir));
      if (next === cur) return;
      if (cur >= 1 && next >= 1 && anchored(v, svg, next, e)) return;
    }
    if (dir > 0) v.zoomIn(); else v.zoomOut();
  }
  function onWheel(e) {
    if (e.ctrlKey || e.metaKey) return;
    if (!overDiagram(e.target)) return;
    var v = view();
    if (!v || typeof v.zoomIn !== 'function' || typeof v.zoomOut !== 'function') return;
    if (tall() && !e.altKey) return;
    e.preventDefault();
    var dy = e.deltaY;
    if (e.deltaMode === 1) dy *= 20; else if (e.deltaMode === 2) dy *= window.innerHeight;
    if (!dy) return;
    var now = Date.now();
    if (now - lastAt > IDLE || (acc !== 0 && (acc > 0) !== (dy > 0))) acc = 0;
    lastAt = now;
    if (now < coolUntil) return;
    acc += dy;
    if (Math.abs(acc) < STEP) return;
    var dir = acc < 0 ? 1 : -1;
    acc = 0;
    coolUntil = now + COOLDOWN;
    step(dir, e);
  }
  function textRight(el) {
    var range = document.createRange();
    range.selectNodeContents(el);
    return range.getBoundingClientRect().right;
  }
  // 定位規則（可測）：
  // 1. 視窗寬 >= 1200 且標題文字右緣到工具列左緣（各留 20px）至少 160px 時嘗試嵌入，寬度就是這段空間。
  // 2. 先試工具列帶（與工具列同高同位置）；導覽列換行後的高度放得下就嵌入（與先前版本相同的位置）。
  // 3. 放不下時改試空白帶：以工具列垂直中心為中心，向上可到頁頂 2px、向下可到圖容器上緣 6px（取兩側較小者）；
  //    放得下（例如窄容器版面的三列）就嵌入。
  // 4. 仍放不下才退回圖上方的一般區塊，並下移到工具列下方，不蓋標題、工具列或圖。
  // 結果寫在 data-placement（docked｜block）、data-band（最後嘗試的嵌入帶高度）、data-need（嵌入時內容高度）、
  // data-overflow（嵌入時水平溢出的 px）。退回一般區塊的原因因此可判定：沒有嵌入帶（data-band 缺）、高度放不下或水平溢出。
  function place() {
    if (!nav) return;
    nav.style.left = nav.style.top = nav.style.width = nav.style.height = nav.style.marginTop = '';
    nav.setAttribute('data-placement', 'block');
    nav.removeAttribute('data-band');
    nav.removeAttribute('data-need');
    nav.removeAttribute('data-overflow');
    // 先讓導覽列脫離文件流（絕對定位）再量標題、工具列與圖容器，量到的是未加工時的原始版面。
    nav.classList.add('is-docked');
    var title = document.querySelector('.container h1') || document.querySelector('h1');
    var bar = document.querySelector('.toolbar');
    var barBox = bar ? bar.getBoundingClientRect() : null;
    var bands = [];
    var left = 0, right = 0;
    if (title && barBox && window.innerWidth >= 1200) {
      left = textRight(title) + 20;
      right = barBox.left - 20;
      bands.push([barBox.top, barBox.height]);
      var center = (barBox.top + barBox.bottom) / 2;
      var box = document.querySelector('.diagram-container');
      var below = box ? box.getBoundingClientRect().top - 6 - center : 0;
      var half = Math.min(center - 2, below);
      if (2 * half > barBox.height) bands.push([center - half, 2 * half]);
    }
    if (right - left >= 160) {
      var list = nav.querySelector('.sw-ov-list');
      for (var i = 0; i < bands.length; i++) {
        nav.style.left = (left + window.scrollX) + 'px';
        nav.style.top = (bands[i][0] + window.scrollY) + 'px';
        nav.style.width = (right - left) + 'px';
        nav.style.height = bands[i][1] + 'px';
        var need = list ? list.scrollHeight : 0;
        nav.setAttribute('data-band', String(Math.round(bands[i][1])));
        var overflow = Math.max(0, Math.ceil(nav.scrollWidth - (right - left)));
        nav.setAttribute('data-need', String(need));
        nav.setAttribute('data-overflow', String(overflow));
        if (overflow <= 1 && need <= bands[i][1] + 1) {
          nav.setAttribute('data-placement', 'docked');
          return;
        }
      }
    }
    nav.classList.remove('is-docked');
    nav.style.left = nav.style.top = nav.style.width = nav.style.height = '';
    if (barBox) {
      var gap = barBox.bottom + window.scrollY + 6 - (nav.getBoundingClientRect().top + window.scrollY);
      if (gap > 0) nav.style.marginTop = (8 + gap) + 'px';
    }
  }
  function sync() { place(); syncHint(); }
  // Archify 的 Reader 在 resize 後的下一個畫格才重排標題與工具列，故延到其後再量。
  var pending = 0;
  function schedule() {
    if (pending) cancelAnimationFrame(pending);
    pending = requestAnimationFrame(function () { pending = requestAnimationFrame(function () { pending = 0; sync(); }); });
    setTimeout(sync, 160);
    setTimeout(sync, 700);
  }
  document.addEventListener('wheel', onWheel, { passive: false });
  window.addEventListener('resize', schedule);
  window.addEventListener('load', schedule);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(schedule);
  if (typeof ResizeObserver === 'function') {
    var watch = new ResizeObserver(schedule);
    document.addEventListener('DOMContentLoaded', function () {
      var bar = document.querySelector('.toolbar');
      if (bar) { watch.observe(bar); bar.addEventListener('transitionend', schedule); }
      // Reader 會在 resize 後重算容器寬度（標題跟著移動），觀察容器尺寸以便重新定位。
      ['h1', '.container', '.diagram-container'].forEach(function (sel) {
        var el = document.querySelector(sel);
        if (el) watch.observe(el);
      });
    });
  }
  document.addEventListener('DOMContentLoaded', sync);
})();
</script>
"""


class OverlayError(ValueError):
    pass


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_path(path, where: str) -> str:
    # 先確認型別：非字串值交給 normpath 會丟 TypeError，繞過 CLI 的 JSON 錯誤回報。
    if not isinstance(path, str):
        raise OverlayError(f'{where}: path must be a string')
    pure = posixpath.normpath(path)
    if (not path.endswith('.html') or path.startswith('/') or '\\' in path
            or pure != path or pure.startswith('..') or '//' in path or ':' in path):
        raise OverlayError(f'{where}: path must be a relative POSIX .html path')
    return path


def load_pages(data: dict) -> dict:
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        raise OverlayError(f'pages: schema must be {SCHEMA}')
    labels = dict(DEFAULT_LABELS)
    extra = data.get('labels', {})
    if not isinstance(extra, dict) or set(extra) - set(DEFAULT_LABELS):
        raise OverlayError('pages.labels: unknown keys')
    for key, value in extra.items():
        if not isinstance(value, str) or not value.strip():
            raise OverlayError(f'pages.labels.{key}: must be a non-empty string')
        labels[key] = value
    pages = data.get('pages')
    if not isinstance(pages, list) or not pages:
        raise OverlayError('pages.pages: must be a non-empty array')
    seen, out = set(), []
    for i, page in enumerate(pages):
        where = f'pages.pages[{i}]'
        if not isinstance(page, dict) or set(page) - {'id', 'title', 'short', 'path', 'available', 'parent'} \
                or not {'id', 'title', 'short', 'path', 'available'} <= set(page):
            raise OverlayError(f'{where}: keys must be id/title/short/path/available[/parent]')
        if not isinstance(page['id'], str) or not ID_RE.fullmatch(page['id']) or page['id'] in seen:
            raise OverlayError(f'{where}.id: invalid or duplicate')
        for key in ('title', 'short'):
            if not isinstance(page[key], str) or not page[key].strip():
                raise OverlayError(f'{where}.{key}: must be a non-empty string')
        if not isinstance(page['available'], bool):
            raise OverlayError(f'{where}.available: must be boolean')
        _check_path(page['path'], where)
        if 'parent' in page and (not isinstance(page['parent'], str) or page['parent'] not in seen):
            raise OverlayError(f'{where}.parent: must name an earlier top-level page')
        seen.add(page['id'])
        out.append(dict(page))
    by_id = {p['id']: p for p in out}
    for p in out:
        if 'parent' in p and 'parent' in by_id[p['parent']]:
            raise OverlayError(f"pages: {p['id']} parent must be a top-level page")
    return {'labels': labels, 'pages': out}


def _href(current: str, target: str) -> str:
    return posixpath.relpath(target, posixpath.dirname(current) or '.')


def render_block(original_sha256: str, manifest: dict, page_id: str) -> str:
    pages = manifest['pages']
    labels = manifest['labels']
    by_id = {p['id']: p for p in pages}
    if page_id not in by_id:
        raise OverlayError(f'page {page_id!r} is not in the pages manifest')
    current = by_id[page_id]
    if not current['available']:
        raise OverlayError(f'page {page_id!r} must be available to carry the overlay')
    esc = lambda s: html.escape(s, quote=True)

    def item(page, cls):
        title = page['title'] if page['available'] else f"{page['title']}（{labels['missing']}）"
        if page['id'] == page_id:
            return f'<span class="{cls} is-current" aria-current="page" title="{esc(title)}">{esc(page["short"])}</span>'
        if page['available']:
            extra = ' is-current' if cls == 'sw-ov-item' and current.get('parent') == page['id'] else ''
            return f'<a class="{cls}{extra}" href="{esc(_href(current["path"], page["path"]))}" title="{esc(title)}">{esc(page["short"])}</a>'
        return f'<span class="{cls} is-missing" aria-disabled="true" title="{esc(title)}">{esc(page["short"])}</span>'

    rows = []
    for page in pages:
        if 'parent' in page:
            continue
        parts = [p for p in pages if p.get('parent') == page['id']]
        rows.append('<li>' + item(page, 'sw-ov-item') + ''.join(item(p, 'sw-ov-part') for p in parts) + '</li>')
    nav = (f'<nav id="sw-overlay-nav" aria-label="{esc(labels["nav"])}" data-sw-overlay="{VERSION}">'
           f'<span class="sw-ov-hint" id="sw-overlay-hint" data-wheel-text="{esc(labels["wheel"])}" '
           f'data-alt-text="{esc(labels["alt_wheel"])}" data-wheel-mode="wheel">{esc(labels["wheel"])}</span>'
           '<ol class="sw-ov-list">' + ''.join(rows) + '</ol></nav>\n')
    return (f'{START} viewer-overlay/{VERSION} page={page_id} original-sha256={original_sha256} -->\n'
            + STYLE + nav + SCRIPT + END)


def split(document: bytes) -> tuple[bytes, dict | None]:
    """Return (original bytes, marker info) and validate any existing overlay block."""
    text = document.decode('utf-8')
    starts, ends = text.count(START), text.count(END.rstrip('\n'))
    if starts == 0 and ends == 0:
        if 'SW-OVERLAY' in text:
            raise OverlayError('stray SW-OVERLAY text outside a well-formed block')
        return document, None
    if starts != 1 or ends != 1:
        raise OverlayError('exactly one SW-OVERLAY block is allowed')
    a = text.index(START)
    b = text.index(END, a) + len(END) if END in text[a:] else -1
    if b < 0:
        raise OverlayError('SW-OVERLAY block end marker is malformed')
    if not text[:a].endswith(ANCHOR) or text.count(ANCHOR) != 1:
        raise OverlayError('SW-OVERLAY block must follow the single <body> line')
    match = MARK_RE.match(text[a:])
    if not match:
        raise OverlayError('SW-OVERLAY start marker is malformed')
    original = (text[:a] + text[b:]).encode('utf-8')
    if 'SW-OVERLAY' in original.decode('utf-8'):
        raise OverlayError('stray SW-OVERLAY text outside the block')
    info = {'version': int(match.group(1)), 'page': match.group(2), 'original_sha256': match.group(3)}
    if _sha(original) != info['original_sha256']:
        raise OverlayError('content outside the overlay block differs from the recorded original delivery')
    return original, info


def _check_original(original: bytes) -> None:
    text = original.decode('utf-8')
    if text.count(ANCHOR) != 1:
        raise OverlayError('original must contain exactly one "<body>\\n" line')
    if '<meta name="generator" content="archify ' not in text or 'class="diagram-container"' not in text:
        raise OverlayError('input is not an Archify delivery')
    if 'Archify.view' not in text:
        raise OverlayError('Archify.view API is missing from the delivery')


def apply(document: bytes, pages: dict, page_id: str) -> bytes:
    original, _ = split(document)
    _check_original(original)
    manifest = load_pages(pages)
    block = render_block(_sha(original), manifest, page_id)
    hit = FORBIDDEN.search(block)
    if hit:
        raise OverlayError(f'overlay block would contain a network or external reference: {hit.group(0)}')
    text = original.decode('utf-8')
    i = text.index(ANCHOR) + len(ANCHOR)
    return (text[:i] + block + text[i:]).encode('utf-8')


def remove(document: bytes) -> bytes:
    return split(document)[0]


def verify(final: bytes, pages: dict, page_id: str, original: bytes | None = None,
           original_sha256: str | None = None) -> dict:
    stripped, info = split(final)
    if info is None:
        raise OverlayError('no SW-OVERLAY block')
    if info['version'] != VERSION:
        raise OverlayError(f"overlay version {info['version']} != tool version {VERSION}")
    if info['page'] != page_id:
        raise OverlayError(f"overlay page {info['page']!r} != {page_id!r}")
    if original is not None and stripped != original:
        raise OverlayError('removing the overlay does not restore the original delivery')
    if original_sha256 is not None and info['original_sha256'] != original_sha256:
        raise OverlayError('original delivery sha256 differs from the recorded value')
    if apply(stripped, pages, page_id) != final:
        raise OverlayError('overlay block is not the deterministic rendering for this manifest')
    return {'ok': True, 'overlay_version': VERSION, 'page': page_id,
            'original_sha256': info['original_sha256'], 'final_sha256': _sha(final)}


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)
    a = sub.add_parser('apply')
    a.add_argument('--html', type=Path, required=True)
    a.add_argument('--pages', type=Path, required=True)
    a.add_argument('--page', required=True)
    a.add_argument('--output', type=Path, required=True)
    a.add_argument('--receipt', type=Path)
    r = sub.add_parser('remove')
    r.add_argument('--html', type=Path, required=True)
    r.add_argument('--output', type=Path, required=True)
    v = sub.add_parser('verify')
    v.add_argument('--html', type=Path, required=True)
    v.add_argument('--pages', type=Path, required=True)
    v.add_argument('--page', required=True)
    group = v.add_mutually_exclusive_group()
    group.add_argument('--original', type=Path)
    group.add_argument('--original-sha256')
    args = parser.parse_args(argv)
    try:
        if args.command == 'apply':
            source = args.html.read_bytes()
            final = apply(source, _read_json(args.pages), args.page)
            args.output.write_bytes(final)
            receipt = verify(final, _read_json(args.pages), args.page)
            if args.receipt:
                args.receipt.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
            print(json.dumps(receipt, ensure_ascii=False))
        elif args.command == 'remove':
            args.output.write_bytes(remove(args.html.read_bytes()))
            print(json.dumps({'ok': True, 'original_sha256': _sha(args.output.read_bytes())}))
        else:
            original = args.original.read_bytes() if args.original else None
            print(json.dumps(verify(args.html.read_bytes(), _read_json(args.pages), args.page,
                                    original=original, original_sha256=args.original_sha256), ensure_ascii=False))
        return 0
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
