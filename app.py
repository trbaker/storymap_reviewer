"""
StoryMap Review — server
=========================
One Flask app that:
  • serves the annotation web page (index.html), and
  • exposes POST /api/capture  -> renders a storymap with a headless browser,
    scrolls THROUGH IT so lazy media loads, and returns one tall stitched PNG.

Why the capture is non-trivial: ArcGIS StoryMaps does not scroll the document.
The story lives inside an inner scrolling container while the page body stays
one screen tall, so a naive `full_page` screenshot only captures the cover.
This module locates the real scroll container, scrolls it in steps, screenshots
each step, and stitches the strips into one long image.

Run locally:
    pip install -r requirements.txt
    playwright install chromium          # only if NOT using the Playwright Docker image
    python app.py

Deploy: see README.md (Docker -> Render).
"""

import io
import os
import re
import sys
from urllib.parse import urlparse

from flask import Flask, request, jsonify, make_response, send_from_directory
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from PIL import Image

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)

# --- Safety: only allow capturing ArcGIS StoryMaps hosts (prevents SSRF). ---
ALLOWED_HOST_SUFFIXES = ("arcgis.com",)

ITEM_ID_RE = re.compile(r"[0-9a-fA-F]{32}")

VIEWPORT = {"width": 1366, "height": 1200}

# Find the element that actually scrolls (largest scrollable overflow area),
# stash it on window.__sc, and report its geometry.
FIND_SCROLLER_JS = r"""
() => {
  const sh = el => el.scrollHeight - el.clientHeight;
  let best = document.scrollingElement || document.documentElement;
  let bestScore = sh(best);
  for (const el of document.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (/(auto|scroll|overlay)/.test(cs.overflowY) && sh(el) > 120 && sh(el) > bestScore) {
      best = el; bestScore = sh(el);
    }
  }
  window.__sc = best;
  const isDoc = best === document.scrollingElement || best === document.documentElement || best === document.body;
  const r = best.getBoundingClientRect();
  const vw = window.innerWidth, vh = window.innerHeight;
  const left   = isDoc ? 0  : Math.max(0, r.left);
  const top    = isDoc ? 0  : Math.max(0, r.top);
  const width  = isDoc ? vw : Math.min(r.width,  vw - left);
  const height = isDoc ? vh : Math.min(r.height, vh - top);
  return { isDoc, left, top, width, height,
           clientHeight: best.clientHeight, scrollHeight: best.scrollHeight };
}
"""

SET_SCROLL_JS = r"""
(y) => {
  const el = window.__sc;
  el.scrollTop = y;
  return { scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight };
}
"""


def extract_item_id(url: str):
    m = ITEM_ID_RE.search(url or "")
    if m:
        return m.group(0)
    m = re.search(r"stories/([^/?#]+)", url or "")
    return m.group(1) if m else None


def host_allowed(url: str) -> bool:
    try:
        netloc = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return False
    return any(netloc == h or netloc.endswith("." + h) for h in ALLOWED_HOST_SUFFIXES)


def _log(*a):
    print("[capture]", *a, file=sys.stderr, flush=True)


def capture_storymap(url: str, scale: int = 1):
    """Return (png_bytes, meta_dict)."""
    scale = 2 if int(scale) == 2 else 1
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=scale)
        page = context.new_page()
        try:
            page.goto(url, wait_until="load", timeout=90000)
        except PWTimeout:
            pass
        page.wait_for_timeout(4000)  # let the cover / app shell render

        info = page.evaluate(FIND_SCROLLER_JS)
        _log("scroller:", info)

        # Fast path: the document itself scrolls -> one full-page screenshot.
        if info["isDoc"] and info["scrollHeight"] > info["clientHeight"] + 50:
            _settle_scroll(page)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(1000)
            png = page.screenshot(full_page=True, type="png")
            meta = {"mode": "fullpage", "strips": 1, "scroll_height": info["scrollHeight"]}
            context.close(); browser.close()
            return png, meta

        # Strip path: scroll the inner container, capture & stitch.
        clip = {
            "x": int(info["left"]), "y": int(info["top"]),
            "width": int(info["width"]), "height": int(info["height"]),
        }
        step = max(200, clip["height"])           # advance content by one captured strip
        strips = []                                # (content_top_css, png_bytes)
        y = 0
        last_top = -1
        stall = 0
        final_sh = info["scrollHeight"]

        for _ in range(500):
            m = page.evaluate(SET_SCROLL_JS, y)
            page.wait_for_timeout(850)             # let media in view render
            try:
                page.wait_for_load_state("networkidle", timeout=2500)
            except PWTimeout:
                pass
            strips.append((m["scrollTop"], page.screenshot(type="png", clip=clip)))
            final_sh = m["scrollHeight"]

            if m["scrollTop"] + m["clientHeight"] >= m["scrollHeight"] - 2:
                break
            if m["scrollTop"] == last_top:
                stall += 1
                if stall >= 2:
                    break
            else:
                stall = 0
            last_top = m["scrollTop"]
            y = m["scrollTop"] + step

        png = _stitch(strips, final_sh, scale)
        meta = {"mode": "strips", "strips": len(strips), "scroll_height": final_sh}
        _log("done:", meta)
        context.close(); browser.close()
        return png, meta


def _settle_scroll(page):
    """Step a normally-scrolling document to the bottom so lazy content loads."""
    page.evaluate(
        """async () => {
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            let locked = -1;
            for (let i = 0; i < 600; i++) {
                window.scrollBy(0, Math.floor(window.innerHeight * 0.85));
                await sleep(400);
                const h = document.body.scrollHeight;
                const atBottom = window.scrollY + window.innerHeight >= h - 4;
                if (atBottom && h === locked) break;
                locked = atBottom ? h : locked;
            }
        }"""
    )


def _stitch(strips, final_scroll_height_css, scale):
    """Paste each strip at its absolute content position. Overlaps overwrite
    identically, so no seam math is needed. Memory stays bounded to one decoded
    strip plus the output canvas."""
    if not strips:
        return b""
    first = Image.open(io.BytesIO(strips[0][1])).convert("RGB")
    width, strip_h = first.width, first.height
    out_h = max(int(round(final_scroll_height_css * scale)),
                int(round(strips[-1][0] * scale)) + strip_h)
    out = Image.new("RGB", (width, out_h), "white")
    out.paste(first, (0, 0))
    first.close()
    for top, data in strips[1:]:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        out.paste(im, (0, int(round(top * scale))))   # off-canvas overflow auto-clips
        im.close()
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


# --- Routes ----------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/health")
def health():
    return "ok", 200


@app.post("/api/capture")
def api_capture():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    scale = data.get("scale", 1)

    if not url:
        return jsonify(error="No URL provided."), 400
    if not url.lower().startswith(("http://", "https://")):
        return jsonify(error="URL must start with http:// or https://"), 400
    if not host_allowed(url):
        allowed = ", ".join(ALLOWED_HOST_SUFFIXES)
        return jsonify(error=f"Only these hosts are allowed: {allowed}. "
                             f"Edit ALLOWED_HOST_SUFFIXES in app.py to add more."), 400

    item_id = extract_item_id(url) or "storymap"
    try:
        png, meta = capture_storymap(url, scale)
    except Exception as e:  # noqa: BLE001 - surface a readable message to the UI
        _log("ERROR:", repr(e))
        return jsonify(error=f"Capture failed: {e}"), 500

    if not png:
        return jsonify(error="Capture produced no image (could not find scrollable content)."), 500

    resp = make_response(png)
    resp.headers["Content-Type"] = "image/png"
    resp.headers["X-Item-Id"] = item_id
    resp.headers["X-Capture-Mode"] = meta["mode"]
    resp.headers["X-Capture-Strips"] = str(meta["strips"])
    resp.headers["Cache-Control"] = "no-store"
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
