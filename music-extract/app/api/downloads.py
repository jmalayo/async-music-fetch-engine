import io
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from infrastructure.external import spotify_client
from infrastructure.persistence.database import get_db
from api.schemas import DownloadMusicManualRequest, DownloadPlaylistSpotifyRequest
from core.schemas import DownloadStatusResponse
from core import job_service
from infrastructure.external import downloader_service

router = APIRouter(prefix="/downloads", tags=["Downloader"])

@router.post("/download-urls-manual", response_model=DownloadStatusResponse, status_code=202)
def download_music_manual(body: DownloadMusicManualRequest, db: Session = Depends(get_db)):
    urls = [u.strip() for u in body.urls if u and u.strip()]

    if not urls:
        raise HTTPException(422, "urls no puede estar vacio")

    job_id, tracks, iteration_count = job_service.submit_job(db, "manual-urls", body.group_name, urls)

    downloader_service.enqueue_job(job_id, tracks, iteration_count=iteration_count)

    return job_service.get_status(db, job_id)

@router.post("/download-spotify-playlist", response_model=DownloadStatusResponse, status_code=202)
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

    job_id, tracks, iteration_count = job_service.submit_job(
        db, "playlist-urls", playlist["name"], urls, external_id=playlist.get("id"), meta_by_url=meta_by_url,
    )

    downloader_service.enqueue_job(job_id, tracks, iteration_count=iteration_count)

    return job_service.get_status(db, job_id)
