"""
StoryMap Review — server
=========================
Flask app that serves the annotator page and captures a storymap with a
headless browser, reporting live progress to an in-app log.

Capture strategy (v4):
  1. Load the page, look less like an automated browser, then WAIT for the
     story to actually build — ArcGIS StoryMaps is a client-side app that
     fetches the story and renders sections after load, so the document starts
     at one screen tall and grows. We poll the document height (nudging with a
     wheel / End key) until it expands past one screen.
  2. DISCOVER the real scroll target by sending a real wheel event and seeing
     what moves (window or an inner element).
  3. Scroll that target precisely, screenshot each step clipped to its content
     area, and stitch the strips into one tall image.
Detailed diagnostics are logged so a failed capture is debuggable from the log.

Run locally:
    pip install -r requirements.txt
    playwright install chromium          # only if NOT using the Playwright Docker image
    python app.py
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

ALLOWED_HOST_SUFFIXES = ("arcgis.com",)
ITEM_ID_RE = re.compile(r"[0-9a-fA-F]{32}")
VIEWPORT = {"width": 1366, "height": 1200}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

JOBS = {}
JOBS_LOCK = threading.Lock()
CAPTURE_LOCK = threading.Lock()
RUNNING = {"jid": None}
JOB_TTL = 900

READY_JS = ("() => ({ docH: document.documentElement.scrollHeight, "
            "bodyH: document.body.scrollHeight, innerH: window.innerHeight })")

DISCOVER_JS = r"""
() => {
  const cands = [document.scrollingElement || document.documentElement];
  for (const el of document.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (/(auto|scroll|overlay)/.test(cs.overflowY) && el.scrollHeight - el.clientHeight > 40) {
      cands.push(el);
    }
  }
  window.__cands = cands;
  const label = el => {
    if (el === (document.scrollingElement || document.documentElement)) return 'document';
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    if (typeof el.className === 'string' && el.className.trim())
      s += '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.');
    return s;
  };
  const cdiag = cands.slice(0, 8).map((el, i) => ({ i, label: label(el).slice(0, 60),
                  sh: Math.round(el.scrollHeight), ch: Math.round(el.clientHeight) }));
  const ifr = [...document.querySelectorAll('iframe')].slice(0, 5).map(f => {
    const r = f.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height), src: (f.src || '').slice(0, 50) };
  });
  return {
    winY: window.scrollY, innerH: window.innerHeight,
    docH: document.documentElement.scrollHeight, bodyH: document.body.scrollHeight,
    frames: window.frames.length, iframes: ifr, cands: cdiag
  };
}
"""

READ_TOPS_JS = r"""
() => ({
  tops: window.__cands.map(el => el.scrollTop),
  winY: window.scrollY,
  docH: document.documentElement.scrollHeight,
  innerH: window.innerHeight
})
"""

SET_ACTIVE_JS = r"""
(idx) => {
  const el = idx >= 0 ? window.__cands[idx] : null;
  window.__active = el;
  if (el) { const r = el.getBoundingClientRect();
            return { left: r.left, top: r.top, width: r.width, height: r.height }; }
  return { left: 0, top: 0, width: 0, height: 0 };
}
"""

SCROLL_ACTIVE_JS = r"""
(y) => {
  const el = window.__active;
  if (el) { el.scrollTop = y;
            return { offset: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }; }
  window.scrollTo(0, y);
  return { offset: window.scrollY, scrollHeight: document.documentElement.scrollHeight, clientHeight: window.innerHeight };
}
"""

# Hide maps/videos we've already scrolled past so their WebGL canvases stop
# repainting — otherwise cumulative render load grows and screenshots stall.
PRUNE_JS = r"""
() => {
  let n = 0;
  for (const el of document.querySelectorAll('canvas, video, iframe')) {
    const r = el.getBoundingClientRect();
    if (r.bottom < -50 && el.style.visibility !== 'hidden') { el.style.visibility = 'hidden'; n++; }
  }
  return n;
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


def _wait_for_story(page, progress, vw, vh):
    """Poll until the document grows past one screen, nudging it to load."""
    progress("Waiting for the story to build its sections…", "info")
    s = {"docH": vh, "innerH": vh}
    for i in range(30):                       # up to ~45s
        s = page.evaluate(READY_JS)
        if i % 3 == 0:
            progress(f"  loading… document {int(s['docH'])}px tall")
        if s["docH"] > s["innerH"] * 1.5:
            progress(f"Story expanded to {int(s['docH'])}px — proceeding.", "info")
            return True
        page.mouse.move(vw / 2, vh / 2)
        page.mouse.wheel(0, int(vh))
        try:
            page.keyboard.press("End")
        except Exception:
            pass
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1500)
    progress(f"Story stayed at {int(s['docH'])}px after waiting (one screen). "
             f"Capturing what rendered — this story may block headless rendering.", "info")
    return False


def capture_storymap(url, scale, progress):
    scale = 2 if int(scale) == 2 else 1
    vw, vh = VIEWPORT["width"], VIEWPORT["height"]
    progress("Launching headless browser…", "info")
    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--enable-unsafe-swiftshader",   # allow WebGL maps to render in software
            "--hide-scrollbars", "--mute-audio",
        ])
        context = browser.new_context(viewport=VIEWPORT, device_scale_factor=scale,
                                      user_agent=UA, locale="en-US")
        # look less like an automated browser (some apps degrade for bots)
        context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = context.new_page()

        progress(f"Loading page (scale {scale}x)…", "info")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
        except PWTimeout:
            progress("Initial load timed out — continuing.", "info")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PWTimeout:
            pass

        _wait_for_story(page, progress, vw, vh)
        page.wait_for_timeout(1500)

        # --- Diagnostics ---
        d = page.evaluate(DISCOVER_JS)
        progress(f"Page: window doc {d['docH']}px, body {d['bodyH']}px, "
                 f"iframes {d['frames']}, scroll-candidates {len(d['cands'])}.", "info")
        for c in d["cands"]:
            progress(f"  cand[{c['i']}] {c['label']} — sh {c['sh']} / ch {c['ch']}")
        for f in d["iframes"]:
            if f["w"] > 300 and f["h"] > 300:
                progress(f"  iframe {f['w']}×{f['h']} src={f['src']}")

        # --- Discover which element actually scrolls on a real wheel ---
        page.mouse.move(vw / 2, vh / 2)
        before = page.evaluate(READ_TOPS_JS)
        page.mouse.wheel(0, int(vh * 0.85))
        page.wait_for_timeout(900)
        after = page.evaluate(READ_TOPS_JS)

        win_d = after["winY"] - before["winY"]
        best_i, best_d = -1, 0
        for idx in range(len(after["tops"])):
            delta = after["tops"][idx] - before["tops"][idx]
            if delta > best_d:
                best_d, best_i = delta, idx
        progress(f"Wheel test: window moved {int(win_d)}px, "
                 f"best element cand[{best_i}] moved {int(best_d)}px.", "info")

        if best_i > 0 and best_d >= win_d and best_d > 2:
            active_idx = best_i
            progress(f"Active scroller: cand[{best_i}].", "info")
        elif win_d > 2 or best_i == 0 or d["docH"] > vh + 50:
            active_idx = -1
            progress("Active scroller: window/document.", "info")
        else:
            active_idx = -1
            progress("Nothing scrolled on a wheel event — falling back to window. "
                     "If the result is one section, send me these diagnostic lines.", "info")

        rect = page.evaluate(SET_ACTIVE_JS, active_idx)
        if active_idx == -1:
            clip = {"x": 0, "y": 0, "width": vw, "height": vh}
        else:
            left = max(0, int(rect["left"])); top = max(0, int(rect["top"]))
            width = min(vw - left, int(rect["width"])); height = min(vh - top, int(rect["height"]))
            clip = {"x": left, "y": top, "width": width, "height": height} \
                if width > 0 and height > 0 else {"x": 0, "y": 0, "width": vw, "height": vh}

        # --- Back to top, then capture & stitch ---
        page.evaluate(SCROLL_ACTIVE_JS, 0)
        page.wait_for_timeout(900)
        step = max(200, clip["height"])
        strips = []
        last_off, stall, final_sh, y, fails = -1, 0, 0, 0, 0

        for _ in range(500):
            m = page.evaluate(SCROLL_ACTIVE_JS, y)
            page.wait_for_timeout(1500)        # let media in view paint
            pruned = page.evaluate(PRUNE_JS)    # drop already-passed maps/videos
            if pruned:
                progress(f"  freed {pruned} off-screen map/media element(s).")
            page.wait_for_timeout(200)
            final_sh = m["scrollHeight"]
            shot = _shot(page, clip)
            if shot is None:
                fails += 1
                progress(f"Section {len(strips) + 1}: screenshot timed out — skipping.", "info")
                if fails >= 3:
                    progress("Screenshots keep timing out (heavy maps under software "
                             "rendering). Stopping with what we have.", "info")
                    break
            else:
                fails = 0
                strips.append((m["offset"], shot))
                pct = min(100, int(100 * (m["offset"] + m["clientHeight"]) / max(1, m["scrollHeight"])))
                progress(f"Captured section {len(strips)} · {int(m['offset'])}px / "
                         f"{int(m['scrollHeight'])}px ({pct}%)")

            if m["offset"] + m["clientHeight"] >= m["scrollHeight"] - 2:
                break
            if m["offset"] == last_off:
                stall += 1
                if stall >= 2:
                    progress("Scrolling stalled — stopping.", "info")
                    break
            else:
                stall = 0
            last_off = m["offset"]
            y = m["offset"] + step

        progress(f"Stitching {len(strips)} section(s)…", "info")
        png, (w, h) = _stitch(strips, final_sh, scale)
        progress(f"Encoded image {w}×{h}px.", "info")
        meta = {"mode": "strips" if len(strips) > 1 else "single",
                "strips": len(strips), "width": w, "height": h}
        context.close(); browser.close()
        return png, meta


def _shot(page, clip):
    """One screenshot, animations frozen, generous timeout. Returns bytes or
    None on timeout (caller skips that strip rather than aborting)."""
    try:
        return page.screenshot(type="png", clip=clip, timeout=35000, animations="disabled")
    except PWTimeout:
        return None


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
            job["status"] = "error"; job["error"] = "No image produced."
            _job_log(job, "Error: no image produced.", "error")
        else:
            job["image"] = png; job["meta"] = meta; job["status"] = "done"
            _job_log(job, f"Done · {meta['strips']} section(s) · {meta['width']}×{meta['height']}px.", "success")
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"; job["error"] = str(e)
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
        return jsonify(error="Only *.arcgis.com URLs are allowed. "
                             "Edit ALLOWED_HOST_SUFFIXES in app.py to add more."), 400

    _sweep_jobs()
    with JOBS_LOCK:
        if RUNNING["jid"] and JOBS.get(RUNNING["jid"], {}).get("status") == "running":
            return jsonify(error="A capture is already running. Please wait."), 429
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
    JOBS.pop(jid, None)
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
