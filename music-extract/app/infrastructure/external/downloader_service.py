import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlalchemy.orm import Session

from musictools.download.core import SpotifyCredentials, default_js_runtime, download

from config.settings import (
    BLOCKED_CACHE_PATH,
    BITRATE,
    COOKIES_FILE,
    FORMAT,
    MUSIC_DOWNLOADED_ROOT,
    PROXY,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    THREADS,
)
from infrastructure.persistence.database import SessionLocal
from core import job_service

_executor = ThreadPoolExecutor(max_workers=THREADS)
_js_runtime = default_js_runtime()
_spotify_creds = SpotifyCredentials(client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET)

def resolve_output_dir(db: Session, job_id: str) -> Path:

    job = job_service.get_job(db, job_id)

    if job.type == "manual-urls":
        out_dir = MUSIC_DOWNLOADED_ROOT / (job_service.slugify(job.playlist_name) if job.playlist_name else "any-musics")

    elif job.type == "playlist-urls":
        out_dir = MUSIC_DOWNLOADED_ROOT / job_service.slugify(job.playlist_name)

    else:
        raise ValueError(f"Tipo de job no soportado: {job.type}")

    out_dir.mkdir(parents=True, exist_ok=True)

    return out_dir


def _process_track(job_id: str, track_id: int, url: str, iteration_count: int, known_meta: dict | None = None) -> None:

    with SessionLocal() as db:
        job_service.mark_in_progress(db, track_id)
        out_dir = resolve_output_dir(db, job_id)

    t0 = time.monotonic()
    result = download(
        url,
        out_dir=out_dir,
        cookies_file=COOKIES_FILE,
        cache_path=BLOCKED_CACHE_PATH,
        js_runtime=_js_runtime,
        fmt=FORMAT,
        bitrate=BITRATE,
        spotify_creds=_spotify_creds,
        proxy=PROXY,
        known_meta=known_meta,
    )
    elapsed_s = time.monotonic() - t0

    with SessionLocal() as db:
        job_service.record_result(
            db,
            track_id,
            iteration_count,
            result["estado"],
            result["intentos"],
            result["titulo"],
            result["artista"],
            elapsed_s,
        )

def enqueue_track(job_id: str, track_id: int, url: str, iteration_count: int = 0, known_meta: dict | None = None) -> None:
    _executor.submit(_process_track, job_id, track_id, url, iteration_count, known_meta)

def enqueue_job(job_id: str, tracks: list[tuple[int, str, dict | None]], iteration_count: int = 0) -> None:
    for track_id, url, known_meta in tracks:
        enqueue_track(job_id, track_id, url, iteration_count, known_meta)
