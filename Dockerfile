# Official Playwright image: Chromium + all system libraries baked in,
# matching playwright==1.59.0 in requirements.txt. Pin these together.
FROM mcr.microsoft.com/playwright/python:v1.59.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render injects PORT (default 10000) and expects the app on 0.0.0.0.
ENV PORT=10000
EXPOSE 10000

# Shell form so ${PORT} expands. One worker keeps memory low (each capture
# launches a Chromium ~300-500MB); raise --workers on a larger instance.
# --timeout 300 because a long-story capture can take ~a minute.
CMD gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 1 --timeout 300 app:app
