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

## Run locally

```bash
pip install -r requirements.txt
playwright install chromium      # only needed outside the Docker image
python app.py                    # http://localhost:10000
```

## Notes & knobs

- **Allowed hosts (security):** `app.py` only captures `*.arcgis.com` URLs, to
  stop the service being used to screenshot arbitrary/internal URLs (SSRF). Edit
  `ALLOWED_HOST_SUFFIXES` to add domains.
- **Very tall stories:** browsers cap canvas height (~32k px). If a capture is
  enormous, leave **Hi-res off** (the default) — Hi-res doubles pixel height. If
  an export still fails, the app tells you to retry without Hi-res.
- **Capture time:** ~20–60s for a long story. `gunicorn --timeout 300` covers it.
- **Concurrency:** the Dockerfile runs 1 worker (one capture at a time) to stay
  within Starter RAM. On a bigger instance, raise `--workers`.
- **Blank `View live` panel:** some storymaps block embedding; use *Open in new
  tab*. That panel is reference-only and isn't part of the export.
