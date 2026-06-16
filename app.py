"""
StoryMap Review — server
=========================
One Flask app that:
  • serves the annotation web page (index.html), and
  • captures a storymap with a headless browser and reports live progress.

Capture is a *job*: POST /api/capture starts it on a background thread and
returns a job id; the page polls GET /api/capture/status/<id> for a growing
log, then GET /api/capture/result/<id> for the finished PNG. This lets the
page show an on-screen log of every step.

Why capture is non-trivial: ArcGIS StoryMaps does not scroll the document.
The story lives inside an inner scrolling container while the page body stays
one screen tall, so a naive full_page screenshot only grabs the cover. We find
the real scroll container, scroll it in steps so lazy media loads, screenshot
each step, and stitch the strips into one long image.

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
import time
import uuid
import threading
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

# --- Job store -------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()
CAPTURE_LOCK = threading.Lock()       # one capture at a time (memory safety)
RUNNING = {"jid": None}
JOB_TTL = 900                          # seconds to keep a finished job around

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


def _stamp():
    return time.strftime("%H:%M:%S")


def _job_log(job, msg, level="srv"):
    job["log"].append({"t": _stamp(), "msg": msg, "level": level})
    print("[capture]", msg, file=sys.stderr, flush=True)


# --- Capture ----------------------------------------------------------------
def capture_storymap(url, scale, progress):
    """progress(msg, level) is called as work proceeds.
    Returns (png_bytes, meta_dict)."""
    scale = 2 if int(scale) == 2 else 1
    progress("Launching headless browser…", "info")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=scale)
        page = context.new_page()
        progress(f"Loading page (scale {scale}x)…", "info")
        try:
            page.goto(url, wait_until="load", timeout=90000)
        except PWTimeout:
            progress("Load event timed out — continuing with what rendered.", "info")
        page.wait_for_timeout(4000)

        info = page.evaluate(FIND_SCROLLER_JS)

        if info["isDoc"] and info["scrollHeight"] > info["clientHeight"] + 50:
            progress("Document scrolls normally — capturing full page.", "info")
            _settle_scroll(page)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(1000)
            png = page.screenshot(full_page=True, type="png")
            w, h = Image.open(io.BytesIO(png)).size
            meta = {"mode": "fullpage", "strips": 1, "width": w, "height": h}
            context.close(); browser.close()
            return png, meta

        progress(f"Found inner scroll container · content ≈{int(info['scrollHeight'])}px tall.", "info")
        clip = {"x": int(info["left"]), "y": int(info["top"]),
                "width": int(info["width"]), "height": int(info["height"])}
        step = max(200, clip["height"])
        strips = []
        y = 0
        last_top = -1
        stall = 0
        final_sh = info["scrollHeight"]

        for _ in range(500):
            m = page.evaluate(SET_SCROLL_JS, y)
            page.wait_for_timeout(850)
            try:
                page.wait_for_load_state("networkidle", timeout=2500)
            except PWTimeout:
                pass
            strips.append((m["scrollTop"], page.screenshot(type="png", clip=clip)))
            final_sh = m["scrollHeight"]
            pct = min(100, int(100 * (m["scrollTop"] + m["clientHeight"]) / max(1, m["scrollHeight"])))
            progress(f"Captured section {len(strips)} · {int(m['scrollTop'])}px / {int(m['scrollHeight'])}px ({pct}%)")

            if m["scrollTop"] + m["clientHeight"] >= m["scrollHeight"] - 2:
                break
            if m["scrollTop"] == last_top:
                stall += 1
                if stall >= 2:
                    progress("Scrolling stalled — stopping.", "info")
                    break
            else:
                stall = 0
            last_top = m["scrollTop"]
            y = m["scrollTop"] + step

        progress(f"Stitching {len(strips)} sections…", "info")
        png, (w, h) = _stitch(strips, final_sh, scale)
        progress(f"Encoded image {w}×{h}px.", "info")
        meta = {"mode": "strips", "strips": len(strips), "width": w, "height": h}
        context.close(); browser.close()
        return png, meta


def _settle_scroll(page):
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
    if not strips:
        return b"", (0, 0)
    first = Image.open(io.BytesIO(strips[0][1])).convert("RGB")
    width, strip_h = first.width, first.height
    out_h = max(int(round(final_scroll_height_css * scale)),
                int(round(strips[-1][0] * scale)) + strip_h)
    out = Image.new("RGB", (width, out_h), "white")
    out.paste(first, (0, 0))
    first.close()
    for top, data in strips[1:]:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        out.paste(im, (0, int(round(top * scale))))
        im.close()
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue(), (out.width, out.height)


# --- Job runner -------------------------------------------------------------
def _sweep_jobs():
    now = time.time()
    with JOBS_LOCK:
        for jid in [j for j, v in JOBS.items()
                    if v["status"] in ("done", "error") and now - v["created"] > JOB_TTL]:
            JOBS.pop(jid, None)


def _run_job(jid, url, scale):
    job = JOBS[jid]
    try:
        with CAPTURE_LOCK:
            png, meta = capture_storymap(url, scale, lambda m, level="srv": _job_log(job, m, level))
        if not png:
            job["status"] = "error"
            job["error"] = "No image produced (no scrollable content found)."
            _job_log(job, "Error: no image produced.", "error")
        else:
            job["image"] = png
            job["meta"] = meta
            job["status"] = "done"
            _job_log(job, f"Done · {meta['mode']} · {meta['strips']} section(s) · "
                          f"{meta['width']}×{meta['height']}px.", "success")
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)
        _job_log(job, f"Error: {e}", "error")
    finally:
        with JOBS_LOCK:
            if RUNNING["jid"] == jid:
                RUNNING["jid"] = None


# --- Routes -----------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/health")
def health():
    return "ok", 200


@app.post("/api/capture")
def api_capture_start():
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

    _sweep_jobs()
    with JOBS_LOCK:
        if RUNNING["jid"] and JOBS.get(RUNNING["jid"], {}).get("status") == "running":
            return jsonify(error="A capture is already running. Please wait for it to finish."), 429
        jid = uuid.uuid4().hex
        item_id = extract_item_id(url) or "storymap"
        JOBS[jid] = {"status": "running", "log": [], "image": None, "meta": {},
                     "error": None, "item_id": item_id, "created": time.time()}
        RUNNING["jid"] = jid

    _job_log(JOBS[jid], f"Queued capture · item {item_id}.", "info")
    threading.Thread(target=_run_job, args=(jid, url, scale), daemon=True).start()
    return jsonify(job_id=jid, item_id=item_id)


@app.get("/api/capture/status/<jid>")
def api_capture_status(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify(error="Unknown or expired job."), 404
    return jsonify(status=job["status"], log=job["log"], meta=job["meta"],
                   item_id=job["item_id"], error=job["error"])


@app.get("/api/capture/result/<jid>")
def api_capture_result(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify(error="Unknown or expired job."), 404
    if job["status"] != "done" or not job["image"]:
        return jsonify(error="Result not ready."), 409
    png = job["image"]
    resp = make_response(png)
    resp.headers["Content-Type"] = "image/png"
    resp.headers["X-Item-Id"] = job["item_id"]
    resp.headers["Cache-Control"] = "no-store"
    JOBS.pop(jid, None)        # free the image now that it's delivered
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
