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

# Install Python dependencies first (cached layer, only rebuilt when requirements change).
COPY music-extract/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install musictools as a real editable package (mirrors local dev setup).
COPY music-extract/pyproject.toml .
COPY music-extract/musictools ./musictools
RUN pip install --no-cache-dir -e .

# Playwright/Chromium: fallback downloader for geo-restricted videos (notube.im).
RUN python -m playwright install --with-deps chromium

# Application code.
COPY music-extract/app ./app
COPY music-extract/scripts ./scripts

WORKDIR /app/music-extract/app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
