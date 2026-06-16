---
title: StoryMap Review
emoji: 🗺️
colorFrom: red
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# StoryMap Review

A teacher tool for grading ArcGIS StoryMaps. Paste a student's storymap URL,
the server renders and scrolls through it in a headless browser to produce one
tall image, you mark it up in the browser, and export the annotated image —
named with the storymap's item id.

## Files

| File | What it is |
|------|------------|
| `app.py` | The whole backend: Flask server + Playwright capture. |
| `index.html` | The annotation web app (served at `/`). |
| `requirements.txt` | Python deps (pinned to the Playwright image version). |
| `Dockerfile` | Based on the official Playwright image — browsers preinstalled. |
| `render.yaml` | Optional Render Blueprint for one-step deploy. |

## How it works

The live storymap is on another domain, so the browser can't read its pixels to
screenshot it (cross-origin). The capture therefore runs **server-side** in
`/api/capture`, where the network is open: Playwright loads the page, scrolls
top-to-bottom so lazy images and maps render, then returns a full-page PNG. The
front-end draws that onto a canvas, you annotate, and the export merges your
marks with the image.

## Deploy to Render

1. Put these files in a Git repo (GitHub/GitLab) at the repo root and push.
2. In the Render dashboard: **New → Web Service**, connect the repo.
3. Render detects the `Dockerfile` and selects the **Docker** runtime. (Or it
   reads `render.yaml` if you use **New → Blueprint**.)
4. Pick an instance. **Starter (512MB)** works but is tight — if captures fail
   with out-of-memory, move to **Standard**.
5. Create the service. First build pulls the Playwright image (~1–2 GB), so it
   takes a few minutes. When it's live, open the `*.onrender.com` URL.

No build/start commands to set — the Dockerfile's `CMD` runs gunicorn bound to
`0.0.0.0:$PORT`, which is what Render expects.

## Deploy to Hugging Face Spaces (free, 16 GB RAM)

The free CPU tier gives 2 vCPU and 16 GB RAM with Docker support — plenty for
headless Chromium, and no credit card. The YAML block at the top of this README
configures the Space (`sdk: docker`, `app_port: 7860`).

1. Create a Hugging Face account, then **New → Space**. Give it a name, pick
   **Docker** as the SDK, and **Blank** as the template. Leave hardware on the
   free **CPU basic** tier.
2. Push these files to the Space's git repo (it works like any git remote):
   ```bash
   git remote add space https://huggingface.co/spaces/<your-username>/<space-name>
   git push space main
   ```
   (Or use the Space's web "Files" tab to upload them.)
3. The Space builds the Dockerfile and starts. The app is at
   `https://<your-username>-<space-name>.hf.space`.

Notes specific to Spaces:
- **Visibility:** a public Space means anyone with the URL can run captures.
  The `*.arcgis.com` allow-list keeps that low-risk (they could only screenshot
  public storymaps). Set the Space to **Private** in its settings if you want it
  to yourself.
- The Space **sleeps** after inactivity and cold-starts on the next visit
  (~30–60s), which is fine for occasional grading.
- Disk is ephemeral (resets on restart) — not a problem; captures live in memory.

## Run locally

```bash
pip install -r requirements.txt
playwright install chromium      # only needed outside the Docker image
python app.py                    # http://localhost:10000
```

## How the capture handles StoryMaps' scrolling

ArcGIS StoryMaps doesn't scroll the page — the story sits in an inner scrolling
container while the document body stays one screen tall. A plain `full_page`
screenshot therefore only grabs the cover. So `app.py` finds the real scroll
container (largest scrollable element), scrolls *it* in viewport-sized steps so
each section's lazy images and maps load, screenshots each step, and stitches
the strips into one tall PNG with Pillow. Normal pages that scroll the document
still take the simple one-shot `full_page` path automatically.

## In-app status log

Capture runs as a **job** so progress is visible without leaving the page:

- `POST /api/capture` → starts the capture on a background thread, returns
  `{job_id, item_id}` (or `429` if a capture is already running).
- `GET /api/capture/status/<job_id>` → `{status, log[], meta, error}`. The page
  polls this ~once a second and streams each new log line into the on-screen
  **Capture & export log** window (toggle with the **Log** button in the header).
- `GET /api/capture/result/<job_id>` → the finished PNG (then the job is freed).

The same log window also records the client-side export (PNG/JPG generation,
file size, the saved filename). Every server log line is still printed to stderr
too, so it also shows in Render's **Logs** tab.

## Notes & knobs

- **Allowed hosts (security):** `app.py` only captures `*.arcgis.com` URLs, to
  stop the service being used to screenshot arbitrary/internal URLs (SSRF). Edit
  `ALLOWED_HOST_SUFFIXES` to add domains.
- **Slow maps / blank tiles:** raise the per-strip wait `page.wait_for_timeout(850)`
  in `app.py` to give map tiles and embeds longer to render before each shot.
- **Very tall stories:** browsers cap canvas height (~32k px) on the *front-end*
  export. Leave **Hi-res off** (the default) — Hi-res doubles pixel height. If an
  export fails, the app tells you to retry without Hi-res.
- **Capture time:** scales with strip count (~1–2s each); a long story may take a
  minute or two. `gunicorn --timeout 300` covers it.
- **Concurrency:** the Dockerfile runs 1 worker (one capture at a time) to stay
  within Starter RAM. On a bigger instance, raise `--workers`.
- **Blank `View live` panel:** some storymaps block embedding; use *Open in new
  tab*. That panel is reference-only and isn't part of the export.
