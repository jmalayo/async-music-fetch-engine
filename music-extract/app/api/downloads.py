import io
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from infrastructure.external import spotify_client
from infrastructure.persistence.database import get_db
from api.schemas import (
    DownloadMusicManualRequest,
    DownloadMusicManualResponse,
    DownloadPlaylistSpotifyRequest,
    DownloadPlaylistSpotifyResponse,
    RetryResponse,
)
from core import job_service
from infrastructure.external import downloader_service

router = APIRouter(prefix="/downloads", tags=["Descargar música"])

@router.post("/download-music-manual", response_model=DownloadMusicManualResponse, status_code=202)
def download_music_manual(body: DownloadMusicManualRequest, db: Session = Depends(get_db)):
    urls = [u.strip() for u in body.urls if u and u.strip()]

    if not urls:
        raise HTTPException(422, "urls no puede estar vacio")

    job_id, tracks = job_service.create_job(db, "manual-urls", body.group_name, urls)

    downloader_service.enqueue_job(job_id, tracks)

    return DownloadMusicManualResponse(job_id=job_id, type="manual-urls", group_name=body.group_name, total=len(urls))

@router.post("/download-playlist-spotify", response_model=DownloadPlaylistSpotifyResponse, status_code=202)
def download_playlist_spotify(body: DownloadPlaylistSpotifyRequest, db: Session = Depends(get_db)):
    try:
        playlist, spotify_tracks = spotify_client.search_and_get_tracks(body.playlist_name, db)

    except spotify_client.InsufficientSpotifyAuth as e:
        raise HTTPException(403, {"error": str(e), "see": "music-extract/definitions/FASTAPI_AUTENTICACION_PENDIENTE.md"})

    except Exception as e:
        raise HTTPException(502, f"Error consultando la API de Spotify: {e}")

    if not playlist:
        raise HTTPException(404, {"error": "Playlist no encontrada", "playlist_name": body.playlist_name})

    urls = [t["spotify_url"] for t in spotify_tracks if t.get("spotify_url")]

    if not urls:
        raise HTTPException(422, f"La playlist «{playlist['name']}» no tiene tracks con spotify_url")

    meta_by_url = {
        t["spotify_url"]: {
            "name": t.get("name", ""),
            "artists": [a for a in (t.get("artists") or "").split("; ") if a],
            "duration_s": (t.get("duration_ms") or 0) / 1000,
        }
        for t in spotify_tracks if t.get("spotify_url")
    }

    existing_job_id = job_service.find_existing_playlist_job(db, playlist.get("id"), playlist["name"])

    if existing_job_id:
        job_id = existing_job_id
        tracks, iteration_count = job_service.resume_or_retry_playlist_job(db, job_id, playlist.get("id"), urls, meta_by_url)
    else:
        job_id, tracks = job_service.create_job(db, "playlist-urls", playlist["name"], urls, playlist_id=playlist.get("id"), meta_by_url=meta_by_url)
        iteration_count = 0

    downloader_service.enqueue_job(job_id, tracks, iteration_count=iteration_count)

    return DownloadPlaylistSpotifyResponse(
        job_id=job_id,
        type="playlist-urls",
        playlist_name=playlist["name"],
        total=len(urls),
    )

@router.post("/{job_id}/retry", response_model=RetryResponse, status_code=202)
def retry(job_id: str, db: Session = Depends(get_db)):
    
    job = job_service.get_job(db, job_id)

    if not job:
        raise HTTPException(404, f"No existe el job {job_id}")

    iteration_count, candidates = job_service.retry(db, job_id)

    if iteration_count is None:
        return RetryResponse(job_id=job_id, requeued=0)

    downloader_service.enqueue_job(job_id, candidates, iteration_count=iteration_count)

    return RetryResponse(job_id=job_id, iteration_count=iteration_count, requeued=len(candidates))
