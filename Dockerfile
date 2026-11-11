FROM python:3.11-slim

# ffmpeg  -> audio normalization (loudnorm, two-pass LUFS/peak)
# nodejs  -> JS runtime yt-dlp needs to solve YouTube's player challenges
# curl/ca-certificates -> required by the Playwright browser installer below
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        nodejs \
        npm \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/music-extract

# Layers go from what changes least to what changes most, so a code change only rebuilds the last ones.

# Python dependencies (rebuilt only when requirements.txt changes).
COPY music-extract/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright/Chromium: fallback downloader for geo-restricted videos (notube.im). It only depends on the
# playwright package installed above, so it goes before the code: editing musictools no longer reinstalls it.
RUN python -m playwright install --with-deps chromium

# Install musictools as a real editable package (mirrors local dev setup).
COPY music-extract/pyproject.toml .
COPY music-extract/musictools ./musictools
RUN pip install --no-cache-dir -e .

# Application code.
COPY music-extract/app ./app
COPY music-extract/scripts ./scripts

WORKDIR /app/music-extract/app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
