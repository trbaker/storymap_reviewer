"""
StoryMap Review — server
=========================
One Flask app that:
  • serves the annotation web page (index.html), and
  • exposes POST /api/capture  -> renders a storymap with a headless browser,
    scrolls it so lazy media loads, and streams back a full-page PNG.

The browser capture is the part that can't happen in the user's browser
(cross-origin pages can't be read client-side), so it runs here server-side
where the network is open.

Run locally:
    pip install -r requirements.txt
    playwright install chromium          # only if NOT using the Playwright Docker image
    python app.py

Deploy: see README.md (Docker -> Render).
"""

import os
import re
from urllib.parse import urlparse

from flask import Flask, request, jsonify, make_response, send_from_directory
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)

# --- Safety: only allow capturing ArcGIS StoryMaps hosts. ------------------
# This prevents the service from being abused to screenshot arbitrary URLs
# (SSRF). Add hosts here if you need to capture stories on other domains.
ALLOWED_HOST_SUFFIXES = ("arcgis.com",)

ITEM_ID_RE = re.compile(r"[0-9a-fA-F]{32}")


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


def capture_storymap(url: str, scale: int = 1) -> bytes:
    """Render the storymap, scroll through it to trigger lazy loading,
    then return a full-page PNG as bytes."""
    scale = 2 if int(scale) == 2 else 1
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 1000},
            device_scale_factor=scale,
        )
        page = context.new_page()
        try:
            # networkidle can hang on map-heavy stories; "load" + waits is safer.
            page.goto(url, wait_until="load", timeout=90000)
        except PWTimeout:
            pass  # proceed with whatever has loaded
        page.wait_for_timeout(3000)

        _auto_scroll(page)
        page.wait_for_timeout(5000)            # let map tiles / embeds settle
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1500)

        png = page.screenshot(full_page=True, type="png")
        context.close()
        browser.close()
        return png


def _auto_scroll(page):
    """Scroll down in steps, pausing so media loads. Stops only once the page
    has reached the bottom AND stopped growing (storymaps add height lazily)."""
    page.evaluate(
        """async () => {
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            let locked = -1;
            for (let i = 0; i < 600; i++) {
                window.scrollBy(0, Math.floor(window.innerHeight * 0.8));
                await sleep(450);
                const h = document.body.scrollHeight;
                const atBottom = window.scrollY + window.innerHeight >= h - 4;
                if (atBottom && h === locked) break;
                locked = atBottom ? h : locked;
            }
        }"""
    )


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
        png = capture_storymap(url, scale)
    except Exception as e:  # noqa: BLE001 - surface a readable message to the UI
        return jsonify(error=f"Capture failed: {e}"), 500

    resp = make_response(png)
    resp.headers["Content-Type"] = "image/png"
    resp.headers["X-Item-Id"] = item_id
    resp.headers["Cache-Control"] = "no-store"
    return resp


if __name__ == "__main__":
    # Local dev only. In production the Dockerfile runs gunicorn (see README).
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
