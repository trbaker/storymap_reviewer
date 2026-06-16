# Official Playwright image: Chromium + all system libraries baked in,
# matching playwright==1.59.0 in requirements.txt. Pin these together.
FROM mcr.microsoft.com/playwright/python:v1.59.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Port works on both hosts:
#   • Hugging Face Spaces (free: 2 vCPU / 16 GB RAM): PORT is unset, so we bind
#     7860, which is declared as app_port in README.md.
#   • Render: injects PORT (e.g. 10000) at runtime, overriding the default.
EXPOSE 7860

# 1 worker keeps one Chromium at a time; threads serve status polls and the
# result download while the capture runs on a background thread.
CMD gunicorn --bind 0.0.0.0:${PORT:-7860} --workers 1 --threads 4 --timeout 300 app:app
