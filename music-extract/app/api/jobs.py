"""Endpoints del flujo de la web (plan del front §13): vista previa, decisiones, progreso, cancelar y reintentar.

Los endpoints anteriores (`/downloads/*`, `/stages/*`) siguen igual: este flujo no los reemplaza.
"""

import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import auth, web_jobs
from infrastructure.external import downloader_service, spotify_client
from infrastructure.persistence.database import get_db
from infrastructure.persistence.models import User
from api import files

router = APIRouter(prefix="/jobs", tags=["Web"])
# Canciones de todos los trabajos (sección Historial, pestaña «Fallidas»).
tracks_router = APIRouter(prefix="/tracks", tags=["Web"])

_SPOTIFY_TRACK_RE = re.compile(r"https?://open\.spotify\.com/(?:intl-[a-zA-Z-]+/)?track/[A-Za-z0-9]+")
_YOUTUBE_RE = re.compile(r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/)[\w-]{11}")
# Los mismos formatos que reconoce yt-dlp: tiktok.com/@usuario/video/ID, /embed/…, vm. y vt.tiktok.com, tiktok.com/t/…
_TIKTOK_RE = re.compile(r"https?://(?:(?:www\.)?tiktokv?\.com/(?:embed|(?:share|@[\w.-]+)/video)/\d+|(?:vm|vt)\.tiktok\.com/\w+|(?:www\.)?tiktok\.com/t/\w+)")


class TracklistOptions(BaseModel):
    cortar: bool = False
    guardar_completo: bool = True


class CreateJobRequest(BaseModel):
    # "enlaces": la lista compartida de la web, con enlaces de Spotify (canciones o playlists), YouTube y TikTok
    # mezclados en `urls`, más nombres de playlists de Spotify en `playlists`.
    # "spotify" y "youtube": un solo origen, como antes.
    origen: Literal["enlaces", "spotify", "youtube"]
    # Spotify: enlace de playlist, nombre de playlist o enlaces de canciones. YouTube: se usa `urls`.
    entrada: str = ""
    urls: list[str] = Field(default_factory=list)
    playlists: list[str] = Field(default_factory=list)
    carpeta: str | None = None
    tracklist: TracklistOptions | None = None
    modo: Literal["preview"] = "preview"


class UpdateTrackRequest(BaseModel):
    accion: Literal["mantener", "quitar", "cambiar_url", "tracklist"]
    url: str | None = None
    cortar: bool | None = None
    guardar_completo: bool | None = None
    # Índices de los temas del tracklist que no se cortan (la sublista de la vista previa).
    excluidos: list[int] | None = None


class UpdateJobRequest(BaseModel):
    carpeta: str | None = None


def _handle(fn):
    try:
        return fn()
    except web_jobs.NotFound as e:
        raise HTTPException(404, str(e))
    except web_jobs.Conflict as e:
        raise HTTPException(409, str(e))


def _spotify_input(entrada: str, db: Session) -> tuple[list[str], str | None, str | None, dict]:
    """Devuelve (urls, nombre_playlist, id_playlist, metadatos_por_url) según lo que pegó el usuario."""
    track_urls = _SPOTIFY_TRACK_RE.findall(entrada)
    if track_urls and not spotify_client.extract_playlist_id(entrada):
        # Canciones sueltas: se leen con las credenciales de la app al resolver, sin cuenta conectada.
        return list(dict.fromkeys(track_urls)), None, None, {}

    try:
        playlist, tracks = spotify_client.search_and_get_tracks(entrada, db)
    except spotify_client.InsufficientSpotifyAuth as e:
        raise HTTPException(403, {"error": str(e)})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Error consultando la API de Spotify: {e}")

    if not playlist:
        raise HTTPException(404, {"error": "Playlist no encontrada", "playlist_name": entrada})

    meta_by_url = {
        t["spotify_url"]: {
            "name": t.get("name", ""),
            "artists": [a for a in (t.get("artists") or "").split("; ") if a],
            "duration_s": (t.get("duration_ms") or 0) / 1000,
        }
        for t in tracks if t.get("spotify_url")
    }
    if not meta_by_url:
        raise HTTPException(422, f"La playlist «{playlist['name']}» no tiene canciones que podamos leer")

    return list(meta_by_url), playlist["name"], playlist.get("id"), meta_by_url


def _clean_url(url: str) -> str:
    """Quita parámetros de seguimiento (?si=…, ?is_from_webapp=…) salvo el ?v= de YouTube."""
    url = url.strip()
    if _YOUTUBE_RE.search(url):
        return url
    return url.split("?", 1)[0].split("#", 1)[0]


def _mixed_input(body: "CreateJobRequest", db: Session) -> tuple[list[str], str | None, str | None, dict, str]:
    """Expande la lista compartida: cada playlist de Spotify se convierte en sus canciones.

    Devuelve (urls, nombre_playlist, id_playlist, metadatos_por_url, origen_del_trabajo).
    """
    urls: list[str] = []
    meta_by_url: dict = {}
    playlists: list[dict] = []
    invalid = []

    def add_playlist(entrada: str):
        found_urls, found_name, found_id, found_meta = _spotify_input(entrada, db)
        playlists.append({"name": found_name, "id": found_id})
        urls.extend(found_urls)
        meta_by_url.update(found_meta)

    for raw in body.urls:
        if not raw or not raw.strip():
            continue
        url = _clean_url(raw)
        if spotify_client.extract_playlist_id(url):
            add_playlist(url)
        elif _SPOTIFY_TRACK_RE.match(url) or _YOUTUBE_RE.search(url) or _TIKTOK_RE.match(url):
            urls.append(url)
        else:
            invalid.append(raw.strip())

    if invalid:
        raise HTTPException(422, {"error": "Hay enlaces que no son de Spotify, YouTube ni TikTok", "urls": invalid})

    for name in body.playlists:
        if name and name.strip():
            add_playlist(name.strip())

    urls = list(dict.fromkeys(urls))
    if not urls:
        raise HTTPException(422, "Agrega al menos un enlace.")

    # El nombre de la playlist pone la carpeta por defecto solo si la lista es exactamente esa playlist.
    only_playlist = len(playlists) == 1 and len(urls) == len([u for u in urls if u in meta_by_url])
    name = playlists[0]["name"] if only_playlist else None
    playlist_id = playlists[0]["id"] if only_playlist else None

    origins = {web_jobs.origen_of(u) for u in urls}
    origen = origins.pop() if len(origins) == 1 else "mixto"
    return urls, name, playlist_id, meta_by_url, origen


@router.post("", status_code=202)
def create_job(body: CreateJobRequest, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    """Crea el trabajo en modo vista previa: busca y puntúa sin descargar nada. Sin límite de canciones.

    Sin cuenta se permiten YouTube y TikTok (los archivos son temporales y se bajan directo al equipo);
    Spotify necesita iniciar sesión.
    """
    uses_spotify = body.origen == "spotify" or bool(body.playlists) or any("open.spotify.com" in (u or "") for u in body.urls)
    if user is None and uses_spotify:
        raise HTTPException(401, "Inicia sesión para descargar desde Spotify. Sin cuenta puedes usar YouTube y TikTok.")
    if user is None:
        downloader_service.purge_anonymous()
    origen = body.origen
    if body.origen == "enlaces":
        urls, playlist_name, playlist_id, meta_by_url, origen = _mixed_input(body, db)
    elif body.origen == "spotify":
        if not body.entrada.strip():
            raise HTTPException(422, "Pega un enlace o el nombre de una playlist.")
        urls, playlist_name, playlist_id, meta_by_url = _spotify_input(body.entrada.strip(), db)
    else:
        urls = list(dict.fromkeys(u.strip() for u in body.urls if u and u.strip()))
        if not urls:
            raise HTTPException(422, "urls no puede estar vacio")
        invalid = [u for u in urls if not _YOUTUBE_RE.search(u)]
        if invalid:
            raise HTTPException(422, {"error": "Hay enlaces que no son videos de YouTube", "urls": invalid})
        playlist_name, playlist_id, meta_by_url = None, None, {}

    job_id, to_resolve = web_jobs.create_preview_job(
        db,
        origen=origen,
        urls=urls,
        playlist_name=playlist_name,
        playlist_id=playlist_id,
        carpeta=body.carpeta,
        tracklist=body.tracklist.model_dump() if body.tracklist else None,
        meta_by_url=meta_by_url,
        user_id=user.id if user else None,
    )
    downloader_service.enqueue_resolve(to_resolve)

    preview = web_jobs.preview_payload(db, job_id)
    return {"job_id": job_id, "status": preview["status"], "total": preview["total"], "resueltas": preview["resueltas"]}


@tracks_router.get("/failed")
def failed_tracks(db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    """Todas las canciones del usuario que no bajaron (fallidas, omitidas, esperando cookies), con su trabajo."""
    return web_jobs.failed_tracks_payload(db, downloader_service.progress_of, user.id)


@tracks_router.get("/queue")
def queue(db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    """Canciones en espera, con su posición en la cola del usuario."""
    items, total = web_jobs.queue_payload(db, user.id, downloader_service.progress_of)
    return {"items": items, "total": total}


@tracks_router.get("/rejected")
def rejected(db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    """Canciones que el usuario quitó (o que no tenían candidatos) en trabajos ya confirmados."""
    items, total = web_jobs.tracks_with_status(db, user.id, ("rechazada",), downloader_service.progress_of)
    return {"items": items, "total": total}


@tracks_router.get("/recent")
def recent(page: int = 1, limit: int = 8, categoria: str | None = None, db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    """Recientes de todas las carpetas, paginadas. `categoria`: descargada, recuperada, dudosa, fallida o rechazada."""
    return web_jobs.recent_payload(db, user.id, downloader_service.progress_of, page, limit, categoria)


@tracks_router.post("/{track_id}/redownload", status_code=202)
def redownload(track_id: int, db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    """Vuelve a encolar una canción pasada en su misma carpeta, aunque ya se haya bajado."""
    def run():
        job_id, new_track_id, url = web_jobs.redownload(db, track_id, user.id)
        downloader_service.enqueue_track(job_id, new_track_id, url)
        return {"job_id": job_id, "track_id": new_track_id}

    return _handle(run)


@tracks_router.get("/{track_id}/file")
def track_file(track_id: int, modo: Literal["entero", "partes"] | None = None, tema: int | None = None,
               db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    """Baja al equipo el archivo de una canción (o un ZIP con sus temas, si se cortó por tracklist).

    `modo` en un mix guardado entero y por partes: «entero» (el MP3) o «partes» (ZIP con los temas).
    `tema`: un solo tema del tracklist (su índice en la lista completa), como MP3.
    """
    from infrastructure.persistence.models import Track

    track = db.get(Track, track_id)
    if track is None:
        raise HTTPException(404, "No existe la canción.")
    _handle(lambda: web_jobs.check_owner(db, track.job_id, user))
    if tema is not None:
        from fastapi.responses import FileResponse

        path = files.tema_file(track, tema)
        track.descargas = (track.descargas or 0) + 1
        db.commit()
        return FileResponse(path, media_type="audio/mpeg", filename=path.name)
    return files.serve_tracks(db, [track], name=track.title or "cancion", modo=modo)


@router.get("")
def list_jobs(db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    """Trabajos ya confirmados del usuario, del más reciente al más viejo, con sus contadores."""
    return web_jobs.list_summaries(db, user.id, downloader_service.progress_of)


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    """Tablero de Historial: contadores, almacenamiento y las primeras filas de cada bloque."""
    return web_jobs.dashboard(db, user, downloader_service.progress_of)


@router.delete("/{job_id}", status_code=204)
def discard(job_id: str, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    """Descarta una búsqueda que nunca se confirmó."""
    def run():
        web_jobs.check_owner(db, job_id, user)
        web_jobs.discard_job(db, job_id)

    _handle(run)


@router.post("/{job_id}/tracks/{track_id}/recover")
def recover(job_id: str, track_id: int, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    """Recupera una canción rechazada: vuelve a la vista previa o, si el trabajo ya se confirmó, a la cola."""
    def run():
        web_jobs.check_owner(db, job_id, user)
        track, enqueue = web_jobs.recover_track(db, job_id, track_id)
        if enqueue:
            downloader_service.enqueue_track(job_id, track_id, track.url)
        db.refresh(track)
        return web_jobs.track_payload(track)

    return _handle(run)


@router.get("/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    return _handle(lambda: (web_jobs.check_owner(db, job_id, user), web_jobs.job_summary(db, job_id))[1])


@router.get("/{job_id}/preview")
def get_preview(job_id: str, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    return _handle(lambda: (web_jobs.check_owner(db, job_id, user), web_jobs.preview_payload(db, job_id))[1])


@router.patch("/{job_id}")
def update_job(job_id: str, body: UpdateJobRequest, db: Session = Depends(get_db), user: User = Depends(auth.require_user)):
    """Cambia la carpeta de destino desde la vista previa (sin cuenta los archivos se bajan directo al equipo)."""
    def run():
        web_jobs.check_owner(db, job_id, user)
        downloader_service.enqueue_resolve(web_jobs.set_folder(db, job_id, body.carpeta))
        return web_jobs.preview_payload(db, job_id)

    return _handle(run)


@router.patch("/{job_id}/tracks/{track_id}")
def update_track(job_id: str, track_id: int, body: UpdateTrackRequest, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    def run():
        web_jobs.check_owner(db, job_id, user)
        if body.accion in ("mantener", "quitar"):
            track = web_jobs.set_included(db, job_id, track_id, body.accion == "mantener")
        elif body.accion == "tracklist":
            track = web_jobs.set_tracklist(db, job_id, track_id, body.cortar, body.guardar_completo, body.excluidos)
            if track.status == "resolving":
                # Se activó el tracklist: hay que buscar sus temas como canciones completas.
                web_jobs.refresh_preview_status(db, job_id)
                downloader_service.enqueue_resolve([(track.track_id, track.url, None)])
        else:
            if not body.url or not (_YOUTUBE_RE.search(body.url) or _TIKTOK_RE.match(body.url.strip())):
                raise HTTPException(422, "Pega un enlace de video de YouTube o TikTok.")
            current = web_jobs.track_payload(web_jobs._get_track(db, job_id, track_id))
            resolved = downloader_service.resolve_new_url(body.url.strip(), current)
            track, enqueue = web_jobs.change_url(db, job_id, track_id, body.url.strip(), resolved)
            if enqueue:
                downloader_service.enqueue_track(job_id, track.track_id, track.url)
        db.refresh(track)
        return web_jobs.track_payload(track, downloader_service.progress_of(track.track_id))

    return _handle(run)


@router.post("/{job_id}/confirm")
def confirm(job_id: str, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    def run():
        web_jobs.check_owner(db, job_id, user)
        tracks = web_jobs.confirm(db, job_id)
        downloader_service.enqueue_job(job_id, tracks)
        return web_jobs.job_summary(db, job_id)

    return _handle(run)


@router.get("/{job_id}/tracks")
def list_tracks(job_id: str, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    """Canciones del trabajo con su estado, intentos y, si están en curso, su % por fase."""
    return _handle(lambda: (web_jobs.check_owner(db, job_id, user), web_jobs.tracks_payload(db, job_id, downloader_service.progress_of))[1])


@router.get("/{job_id}/tracks/{track_id}")
def track_detail(job_id: str, track_id: int, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    def run():
        web_jobs.check_owner(db, job_id, user)
        track = web_jobs._get_track(db, job_id, track_id)
        payload = web_jobs.track_payload(track, downloader_service.progress_of(track_id))
        payload["attempts"] = [
            {
                "attempt_id": a.attempt_id, "provider": a.provider, "url_pick": a.url_pick, "title_pick": a.title_pick,
                "channel_pick": a.channel_pick, "score_pick": a.score_pick, "status_attempt": a.status_attempt,
                "message": a.message, "timestamp_attempt": a.timestamp_attempt,
            }
            for a in track.attempts
        ]
        return payload

    return _handle(run)


@router.post("/{job_id}/tracks/{track_id}/cancel", status_code=202)
def cancel_track(job_id: str, track_id: int, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    def run():
        web_jobs.check_owner(db, job_id, user)
        track = web_jobs.request_cancel(db, job_id, track_id)
        if track.status == "cancelling":
            downloader_service.cancel_running(track_id)
        return {"track_id": track_id, "status": track.status}

    return _handle(run)


@router.post("/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    def run():
        web_jobs.check_owner(db, job_id, user)
        for track_id in web_jobs.request_cancel_job(db, job_id):
            downloader_service.cancel_running(track_id)
        return web_jobs.job_summary(db, job_id)

    return _handle(run)


@router.post("/{job_id}/tracks/{track_id}/retry")
def retry_track(job_id: str, track_id: int, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    def run():
        web_jobs.check_owner(db, job_id, user)
        track = web_jobs.retry_track(db, job_id, track_id)
        downloader_service.enqueue_track(job_id, track_id, track.url)
        return web_jobs.track_payload(track)

    return _handle(run)


@router.post("/{job_id}/tracks/{track_id}/requeue")
def requeue_track(job_id: str, track_id: int, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    def run():
        web_jobs.check_owner(db, job_id, user)
        track = web_jobs.requeue_track(db, job_id, track_id)
        downloader_service.enqueue_track(job_id, track_id, track.url)
        return web_jobs.track_payload(track)

    return _handle(run)


@router.get("/{job_id}/zip")
def job_zip(job_id: str, db: Session = Depends(get_db), user: User | None = Depends(auth.current_user)):
    """Todo lo que bajó un trabajo, en un ZIP (para la descarga sin cuenta y para llevarse un trabajo entero)."""
    from infrastructure.persistence.models import Track

    job = _handle(lambda: web_jobs.check_owner(db, job_id, user))
    tracks = db.query(Track).filter(Track.job_id == job_id, Track.archivo.isnot(None)).all()
    if not tracks:
        raise HTTPException(404, "Este trabajo todavía no tiene archivos para bajar.")
    return files.serve_tracks(db, tracks, name=job.playlist_name or job.carpeta or "owlo", force_zip=True)
