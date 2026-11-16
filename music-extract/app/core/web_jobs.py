"""Flujo de la web: vista previa → revisión → descarga, con intentos que se reinician y cancelación.

Estados de una canción:
    resolving → preview | rechazada → pending → in_progress → successful | incorrect | failed
                                              ↘ cancelling → cancelada         ↘ waiting_auth
Estados de un trabajo:
    resolving → preview → in_progress → completed | completed_with_errors | cancelled

Regla de intentos (plan del front §6.9): cada ronda de descarga suma 1 a `rondas_usadas`; bajar bien o
cambiar la URL lo deja en 0; cancelar y las cookies vencidas (`needs_auth`) no suman. Tope: settings.MAX_RETRIES_PER_TRACK.
"""

import json
from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy.orm import Session, object_session

from config.settings import settings
from core import job_service
from core.folders import DEFAULT_FOLDER
from core.job_service import _lock, _now, calculate_max_attempts
from infrastructure.persistence.models import Attempt, Busqueda, Job, Track, User

MIN_SCORE = 60
BITRATE_BPS = 192_000
DONE_STATUSES = ("successful", "incorrect", "failed", "skipped", "cancelada", "waiting_auth")
ACTIVE_STATUSES = ("pending", "in_progress", "cancelling")


class Conflict(Exception):
    """La acción no se puede hacer en el estado actual (la API responde 409)."""


class NotFound(Exception):
    pass


def origen_of(url: str) -> str:
    if "open.spotify.com" in url:
        return "spotify"
    if "tiktok.com" in url:
        return "tiktok"
    return "youtube"


def _loads(text: str | None, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


# --- Creación y vista previa ---------------------------------------------------------------------

def create_preview_job(
    db: Session,
    *,
    origen: str,
    urls: list[str],
    playlist_name: str | None,
    playlist_id: str | None,
    carpeta: str | None,
    tracklist: dict | None,
    meta_by_url: dict[str, dict] | None = None,
    user_id: int | None = None,
) -> tuple[str, list[tuple[int, str, dict | None]]]:
    """Crea el trabajo en estado `resolving`. Devuelve las canciones que hay que resolver.

    Una URL que ya se bajó bien antes queda como «Ya descargada» y no se resuelve ni se baja otra vez.
    """
    meta_by_url = meta_by_url or {}
    job_id = job_service.new_job_id()
    now = _now()
    cortar = bool((tracklist or {}).get("cortar"))
    guardar = True  # con tracklist, el set completo siempre se guarda (el usuario lo baja si quiere)

    with _lock:
        db.add(Job(
            job_id=job_id,
            type="playlist-urls" if playlist_name and origen == "spotify" else "manual-urls",
            playlist_name=playlist_name,
            playlist_id=playlist_id,
            amount=len(urls),
            status="resolving",
            origen=origen,
            carpeta=(carpeta or "").strip() or playlist_name or DEFAULT_FOLDER,
            user_id=user_id,
            created_at=now,
            updated_at=now,
        ))

        prior_by_url = _previously_downloaded(db, urls, user_id, {"cortar": cortar, "guardar_completo": guardar},
                                             carpeta=(carpeta or "").strip() or playlist_name or DEFAULT_FOLDER)
        to_resolve: list[tuple[int, str, dict | None]] = []

        for url in urls:
            meta = meta_by_url.get(url)
            prior = prior_by_url.get(url)
            track = Track(
                job_id=job_id,
                url=url,
                status="successful" if prior else "resolving",
                ya_descargada=bool(prior),
                max_attempts=calculate_max_attempts(url),
                elapsed_s=0.0,
                title=(meta or {}).get("name") or (prior.title if prior else None),
                artist=((meta or {}).get("artists") or [None])[0] or (prior.artist if prior else None),
                duration_s=(meta or {}).get("duration_s"),
                # Ya descargada: se copia cómo se bajó, así la biblioteca muestra si está entera o por partes.
                # «pedido»: lo que se pidió ahora, por si al cambiar de carpeta hay que bajarla de verdad.
                tracklist=(json.dumps({**_loads(prior.tracklist, {}), "pedido": {"cortar": cortar, "guardar_completo": guardar}})
                           if prior else json.dumps({"cortar": cortar, "guardar_completo": guardar}))
                if origen_of(url) == "youtube" else None,
                preview=prior.preview if prior else None,
                created_at=now,
                updated_at=now,
            )
            db.add(track)
            db.flush()

            if prior:
                db.add(Attempt(
                    track_id=track.track_id, provider=None, url_pick=url, title_pick=prior.title,
                    status_attempt="successful",
                    message=f"[SKIP] ya descargado segun la base de datos (job {prior.job_id}, track {prior.track_id})",
                    timestamp_attempt=now,
                ))
            else:
                to_resolve.append((track.track_id, url, meta))

        db.commit()

    with _lock:
        refresh_preview_status(db, job_id)

    return job_id, to_resolve


MIN_TEMA_SCORE = 60  # por debajo, el tema no se baja de su video: se corta del set (es exacto)


def tema_files(track: Track) -> dict[int, Path]:
    """Los MP3 de los temas que bajó esta canción: índice del tema en el tracklist → ruta en disco."""
    archivos = (_loads(track.tracklist, {}) or {}).get("archivos") or {}
    return {int(k): settings.MUSIC_DOWNLOADED_ROOT / v for k, v in archivos.items()}


def saved_modes(track: Track) -> dict:
    """Cómo quedó guardado un video: entero (el set completo) y/o por partes (sus temas)."""
    options = _loads(track.tracklist, {}) or {}
    chapters = _loads(track.preview, {}).get("chapters") or []
    cut = bool(options.get("archivos")) or (bool(options.get("cortar")) and bool(chapters))
    return {"entero": not cut or options.get("guardar_completo", True) is not False, "partes": cut}


def covers_request(prior: Track, tracklist: dict | None) -> bool:
    """Para un pedido sin tracklist: sirve cualquier descarga que tenga el video entero."""
    return origen_of(prior.url) != "youtube" or saved_modes(prior)["entero"]


def _file_exists(track: Track) -> bool:
    """El archivo está en disco o en la nube (R2)."""
    if not track.archivo:
        return False
    from core import nube

    if nube.in_cloud(track):
        return True
    path = Path(track.archivo)
    return (path if path.is_absolute() else settings.MUSIC_DOWNLOADED_ROOT / path).exists()


def track_extension(track: Track) -> str | None:
    """Extensión del archivo guardado, sin punto y en minúsculas («mp3», «opus», «m4a»…)."""
    return Path(track.archivo).suffix.lstrip(".").lower() or None if track.archivo else None


def _same_format(track: Track, formato: str) -> bool:
    """Una copia en MP3 sirve para un pedido en MP3; cualquier otra extensión, para un pedido en original."""
    return (track_extension(track) == "mp3") == (formato == "mp3")


def _previously_downloaded(db: Session, urls: list[str], user_id: int | None, tracklist: dict | None = None,
                           carpeta: str | None = None, tracklist_by_url: dict[str, dict] | None = None,
                           formato: str = "mp3") -> dict[str, Track]:
    """URLs que el mismo usuario ya tiene en su biblioteca, en la misma carpeta, en el mismo formato (MP3 u original)
    y de la misma forma en que las pide ahora (entero o por partes). Sin cuenta no hay biblioteca, así que nunca.

    Solo cuenta una descarga con su archivo en disco: ni las copias «ya descargada» (no tienen archivo propio) ni
    las borradas de la biblioteca ni las que perdieron el archivo. Si no, una canción borrada no se podría volver a
    bajar nunca. `tracklist_by_url` pisa `tracklist` para cada URL (cada canción con sus propias opciones).
    """
    if user_id is None or not urls:
        return {}
    rows = (
        db.query(Track, Job)
        .join(Job, Job.job_id == Track.job_id)
        .filter(Job.user_id == user_id, Track.url.in_(urls), Track.status.in_(["successful", "incorrect"]),
                Track.eliminada.is_(False), Track.ya_descargada.is_(False), Track.archivo.isnot(None))
        .order_by(Track.updated_at.desc())
        .all()
    )
    slug = job_service.slugify(carpeta) if carpeta else None
    by_url: dict[str, list[Track]] = {}
    for track, job in rows:
        if slug and job_service.slugify(job.carpeta or DEFAULT_FOLDER) != slug:
            continue
        if not _same_format(track, formato):
            continue
        by_url.setdefault(track.url, []).append(track)

    found: dict[str, Track] = {}
    for url, priors in by_url.items():
        options = (tracklist_by_url or {}).get(url, tracklist) or {}
        chapters = next((c for p in priors if (c := _loads(p.preview, {}).get("chapters"))), [])
        full = next((p for p in priors if covers_request(p, None) and _file_exists(p)), None)
        if full is None:
            continue
        # Con tracklist: está descargada solo si ya están todos sus temas (pueden venir de varias descargas).
        if origen_of(url) == "youtube" and options.get("cortar") and chapters:
            if not set(range(len(chapters))) <= _temas_on_disk(priors):
                continue
        found[url] = full
    return found


def _temas_on_disk(priors: list[Track]) -> set[int]:
    from core import nube

    return {i for p in priors for i, path in tema_files(p).items() if nube.in_cloud(p) or path.exists()}


def downloaded_temas(db: Session, track: Track) -> set[int]:
    """Temas de este video que el usuario ya tiene bajados en la carpeta del trabajo (en cualquier descarga)."""
    job = track.job
    if job is None or job.user_id is None or origen_of(track.url) != "youtube":
        return set()
    slug = job_service.slugify(job.carpeta or DEFAULT_FOLDER)
    rows = (
        db.query(Track, Job)
        .join(Job, Job.job_id == Track.job_id)
        .filter(Job.user_id == job.user_id, Track.url == track.url, Track.track_id != track.track_id,
                Track.status.in_(["successful", "incorrect"]), Track.eliminada.is_(False), Track.ya_descargada.is_(False))
        .all()
    )
    return _temas_on_disk([t for t, j in rows if job_service.slugify(j.carpeta or DEFAULT_FOLDER) == slug])


# --- Búsquedas guardadas (tabla busquedas) ---------------------------------------------------------------------

def cached_resolution(db: Session, url: str) -> dict | None:
    """Lo que ya se encontró para este enlace. No vence: «Volver a buscar» lo reemplaza."""
    row = db.get(Busqueda, url)
    return _loads(row.datos, None) if row else None


def store_resolution(db: Session, url: str, data: dict) -> None:
    """Guarda lo encontrado para no volver a buscarlo. Lo que no se pudo leer no se guarda (puede ser pasajero:
    un bloqueo, un corte): la próxima vez se intenta de nuevo."""
    readable = data.get("disponible") and (data.get("origen") != "spotify" or data.get("candidatos"))
    if not readable:
        return
    with _lock:
        row = db.get(Busqueda, url) or Busqueda(clave=url)
        row.datos = json.dumps(data)
        row.updated_at = _now()
        db.merge(row)
        db.commit()


def save_resolution(db: Session, track_id: int, data: dict) -> None:
    """Guarda lo que encontró `musictools.download.core.resolve` para una canción."""
    with _lock:
        track = db.get(Track, track_id)
        if track is None or track.status != "resolving":
            return

        track.title = data.get("title") or track.title
        track.artist = data.get("artist") or track.artist
        track.duration_s = data.get("duration_s") or track.duration_s
        track.motivo = data.get("motivo")
        candidatos = list(data.get("candidatos") or [])
        sugerencias = []
        # «Volver a buscar»: el elegido de antes sigue elegido; los 2 mejores distintos quedan como sugerencias
        # para que el usuario decida si reemplazarlo.
        old = _loads(track.preview, {})
        previous = old.get("elegido_antes")
        if previous:
            current = next((c for c in candidatos if c.get("url") == previous.get("url")), previous)
            candidatos = [current] + [c for c in candidatos if c.get("url") != previous.get("url")]
            sugerencias = candidatos[1:3]
        track.preview = json.dumps({
            "candidatos": candidatos,
            "disponible": bool(data.get("disponible")) or bool(candidatos),
            "chapters": data.get("chapters") or [],
            "thumbnail": data.get("thumbnail"),
            "sugerencias": sugerencias,
        })
        track.status = "preview"
        track.updated_at = _now()
        db.commit()
        refresh_preview_status(db, track.job_id)


def save_resolution_error(db: Session, track_id: int, message: str) -> None:
    save_resolution(db, track_id, {"disponible": False, "motivo": "error", "candidatos": [], "chapters": []})
    with _lock:
        track = db.get(Track, track_id)
        if track is not None:
            track.motivo = "error"
            db.add(Attempt(track_id=track_id, provider=None, status_attempt="failed", message=f"[preview] {message}"[:2000], timestamp_attempt=_now()))
            db.commit()


def refresh_preview_status(db: Session, job_id: str) -> None:
    """Estado de un trabajo sin confirmar: «resolving» mientras alguna canción se está buscando, si no «preview».
    En los dos sentidos: volver a buscar una canción o activar su tracklist lo pasa otra vez a «resolving»."""
    job = db.get(Job, job_id)
    if job is None or job.status not in ("resolving", "preview"):
        return
    resolving = db.query(Track).filter(Track.job_id == job_id, Track.status == "resolving").count()
    status = "resolving" if resolving else "preview"
    if job.status != status:
        job.status = status
        job.updated_at = _now()
        db.commit()


# --- Lectura -------------------------------------------------------------------------------------

def _get_job(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFound(f"No existe el job {job_id}")
    return job


def check_owner(db: Session, job_id: str, user: User | None) -> Job:
    """El trabajo, si es de este usuario (o es una descarga sin cuenta). Si es de otro, se responde como si no existiera."""
    job = _get_job(db, job_id)
    if job.user_id is not None and (user is None or user.id != job.user_id):
        raise NotFound(f"No existe el job {job_id}")
    return job


def used_bytes(db: Session, user_id: int) -> int:
    total = (
        db.query(Track.bytes)
        .join(Job, Job.job_id == Track.job_id)
        .filter(Job.user_id == user_id, Track.status.in_(["successful", "incorrect"]), Track.bytes.isnot(None), Track.eliminada.is_(False))
        .all()
    )
    return sum(row[0] for row in total)


def _get_track(db: Session, job_id: str, track_id: int) -> Track:
    track = db.get(Track, track_id)
    if track is None or track.job_id != job_id:
        raise NotFound("No existe la canción.")
    return track


def _mejor(preview: dict) -> dict | None:
    candidates = preview.get("candidatos") or []
    if not candidates:
        return None
    c = candidates[0]
    return {
        "title_pick": c.get("title"),
        "channel_pick": c.get("channel"),
        "source_pick": "ytmusic" if c.get("source") == "ytmusic" else "youtube",
        "duration_s_pick": c.get("duration"),
        "score_pick": c.get("score"),
        "url_pick": c.get("url"),
        "thumbnail_pick": c.get("thumbnail"),
    }


def _tema_video(chapter: dict) -> dict | None:
    """El video que se encontró para un tema (el mejor candidato con score suficiente, o el que eligió el usuario);
    sin él, se corta del set."""
    best = next(iter(chapter.get("candidatos") or []), None)
    if not best or (not chapter.get("elegido") and (best.get("score") or 0) < MIN_TEMA_SCORE):
        return None
    return {k: best.get(k) for k in ("url", "title", "channel", "thumbnail", "score", "duration")}


def _at_time(url: str, seconds: float | None) -> str:
    return f"{url}{'&' if '?' in url else '?'}t={int(seconds or 0)}s"


def _downloaded_url(track: Track) -> str | None:
    """El video que se bajó de verdad (el último intento que funcionó): para Spotify puede no ser el primer candidato."""
    ok = next((a for a in reversed(track.attempts)
               if a.status_attempt == "successful" and a.url_pick and "open.spotify.com" not in a.url_pick), None)
    return ok.url_pick if ok else None


def _tracklist_payload(track: Track, tracklist: dict | None, chapters: list[dict], excluded: set[int]) -> dict:
    options = tracklist or {}
    from core import nube

    in_cloud = nube.in_cloud(track)
    files = {i: p for i, p in tema_files(track).items() if in_cloud or p.exists()}
    fuentes = options.get("fuentes") or {}
    urls = options.get("urls") or {}
    ya: set[int] = set()
    if track.status in ("preview", "rechazada", "resolving") and options.get("cortar"):
        db = object_session(track)
        ya = downloaded_temas(db, track) if db is not None else set()
    temas = []
    for i, c in enumerate(chapters):
        fin = c.get("fin_s") if c.get("fin_s") is not None else (chapters[i + 1].get("inicio_s") if i + 1 < len(chapters) else track.duration_s)
        temas.append({
            "inicio_s": c.get("inicio_s"), "fin_s": fin, "titulo": c.get("titulo"),
            "artista": c.get("artista"), "cancion": c.get("cancion"),
            "incluido": i not in excluded,
            # Encontrado en YouTube como canción completa (None: se corta del set).
            "video": _tema_video(c),
            "buscado": "candidatos" in c,
            # El mejor score encontrado aunque no alcance (la sublista lo muestra como dudoso).
            "mejor_score": (next(iter(c.get("candidatos") or []), {}) or {}).get("score"),
            "elegido": bool(c.get("elegido")),
            "buscando": bool(c.get("buscando")),
            # Tras «Volver a buscar» este tema: otras coincidencias para reemplazar (✓ / ✕).
            "sugerencias": [
                {k: x.get(k) for k in ("url", "title", "channel", "duration", "thumbnail", "score", "source")}
                for x in c.get("sugerencias") or []
            ] if track.status in ("preview", "rechazada") else [],
            # Ya está en la carpeta (de otra descarga): no se vuelve a bajar.
            "ya_descargado": i in ya,
            # En la biblioteca: si este tema se guardó y de dónde salió («video» o «corte»).
            "descargado": i in files,
            "fuente": fuentes.get(str(i)),
            # Lo que se bajó de verdad: el video de la canción, o el set en el minuto del tema si se cortó.
            "url_descargada": urls.get(str(i)) or (_at_time(track.url, c.get("inicio_s")) if fuentes.get(str(i)) == "corte" else None),
        })
    return {
        "disponible": bool(chapters),
        "cortar": bool(options.get("cortar")) and bool(chapters),
        "guardar_completo": True,
        "ya_descargados": len(ya),
        "temas": temas,
    }


def track_payload(track: Track, progreso: dict | None = None) -> dict:
    preview = _loads(track.preview, {})
    tracklist = _loads(track.tracklist, None)
    chapters = preview.get("chapters") or []
    excluded = set((tracklist or {}).get("excluidos") or [])
    origen = origen_of(track.url)
    last = track.attempts[-1] if track.attempts else None

    return {
        "track_id": track.track_id,
        "job_id": track.job_id,
        "url": track.url,
        "origen": origen,
        "title": track.title,
        "artist": track.artist,
        "duration_s": track.duration_s,
        # Imagen del video que se va a bajar: la del video (YouTube, TikTok) o la del mejor candidato (Spotify).
        "thumbnail": preview.get("thumbnail") or ((preview.get("candidatos") or [{}])[0].get("thumbnail") if origen == "spotify" else None),
        "bytes_estimados": round(track.duration_s * BITRATE_BPS / 8) if track.duration_s else None,
        "status": track.status,
        "ya_descargada": bool(track.ya_descargada),
        # Si el video se pudo leer en la vista previa (None mientras se busca). Un video de YouTube o TikTok que
        # no se pudo leer pasa a «Revisa estas primero» con su motivo.
        "disponible": None if track.status == "resolving" else bool(preview.get("disponible", True)),
        "mejor": _mejor(preview) if origen == "spotify" else None,
        # Tras «Volver a buscar»: otros videos con buen score para reemplazar al elegido (el usuario acepta o rechaza).
        "sugerencias": [
            {k: c.get(k) for k in ("url", "title", "channel", "duration", "thumbnail", "score", "source")}
            for c in preview.get("sugerencias") or []
        ] if track.status in ("preview", "rechazada") else [],
        "candidatos": len(preview.get("candidatos") or []) if origen == "spotify" else None,
        "tracklist": None if origen == "spotify" else _tracklist_payload(track, tracklist, chapters, excluded),
        "rondas_usadas": track.rondas_usadas or 0,
        "max_rondas": settings.MAX_RETRIES_PER_TRACK,
        "progreso": progreso if track.status in ("in_progress", "cancelling") else None,
        "motivo": track.motivo,
        "message": last.message if last else None,
        "ultimo_proveedor": last.provider if last else None,
        "elapsed_s": round(track.elapsed_s or 0, 1),
        "updated_at": track.updated_at,
        "archivo_disponible": bool(track.archivo),
        "bytes": track.bytes,
        "extension": track_extension(track),
        "descargas": track.descargas or 0,
        "recuperada": bool(track.recuperada),
        "url_descargada": _downloaded_url(track) if track.status in ("successful", "incorrect") else None,
        "eliminada": bool(track.eliminada),
        # Cómo está guardado (para bajarlo al equipo entero o por partes).
        "guardado": saved_modes(track) if origen == "youtube" else None,
    }


def preview_payload(db: Session, job_id: str) -> dict:
    job = _get_job(db, job_id)
    tracks = db.query(Track).filter(Track.job_id == job_id).order_by(Track.track_id).all()
    return {
        "job_id": job.job_id,
        "origen": job.origen,
        "status": job.status,
        "playlist_name": job.playlist_name,
        "carpeta": job.carpeta,
        "formato": job_format(db, job),
        "total": len(tracks),
        "resueltas": sum(1 for t in tracks if t.status != "resolving"),
        "tracks": [track_payload(t) for t in tracks],
    }


def tracks_payload(db: Session, job_id: str, progress_of) -> list[dict]:
    _get_job(db, job_id)
    tracks = (
        db.query(Track)
        .filter(Track.job_id == job_id, Track.status.notin_(["resolving", "preview"]))
        .order_by(Track.track_id)
        .all()
    )
    return [track_payload(t, progress_of(t.track_id)) for t in tracks]


def summary(db: Session, job: Job) -> dict:
    tracks = db.query(Track).filter(Track.job_id == job.job_id).all()
    count = lambda status: sum(1 for t in tracks if t.status == status)  # noqa: E731
    in_job = [t for t in tracks if t.status not in ("rechazada", "preview", "resolving")]
    pending = sum(1 for t in tracks if t.status in ACTIVE_STATUSES)

    finished = sorted(
        (t for t in tracks if t.status in ("successful", "incorrect", "failed") and (t.elapsed_s or 0) > 0),
        key=lambda t: t.updated_at, reverse=True,
    )[:20]
    average = sum(t.elapsed_s for t in finished) / len(finished) if len(finished) >= 3 else None

    if job.elapsed_time_s is not None:
        elapsed = job.elapsed_time_s
    else:
        elapsed = round((datetime.now(timezone.utc) - datetime.fromisoformat(job.created_at)).total_seconds(), 1)

    return {
        "job_id": job.job_id,
        "type": job.type,
        "origen": job.origen,
        "playlist_name": job.playlist_name,
        "carpeta": job.carpeta,
        "status": job.status,
        "total": len(in_job),
        "successful": count("successful"),
        "incorrect": count("incorrect"),
        "failed": count("failed"),
        "skipped": count("skipped"),
        "waiting_auth": count("waiting_auth"),
        "cancelada": count("cancelada"),
        "rechazada": count("rechazada"),
        "pending": pending,
        # Con settings.PROCESSING_THREADS descargas en paralelo, el tiempo por canción efectivo es el promedio dividido por settings.PROCESSING_THREADS.
        "average_time_per_track_s": round(average / settings.PROCESSING_THREADS, 1) if average else None,
        "estimated_remaining_time_s": round(average * pending / settings.PROCESSING_THREADS, 1) if average else None,
        # Descargas que corren a la vez (settings.PROCESSING_THREADS en config/settings.py).
        "en_paralelo": settings.PROCESSING_THREADS,
        # De dónde vienen sus canciones (sin repetir): spotify, youtube, tiktok.
        "origenes": sorted({origen_of(t.url) for t in tracks}),
        # Las primeras 3 canciones que se enviaron a buscar, para reconocer el trabajo de un vistazo.
        "primeras": [
            {"track_id": t.track_id, "titulo": t.title, "artista": t.artist, "url": t.url, "origen": origen_of(t.url), "status": t.status}
            for t in sorted(tracks, key=lambda t: t.track_id)[:3]
        ],
        "elapsed_time_s": elapsed,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


FAILED_STATUSES = ("failed", "skipped", "waiting_auth")


def failed_tracks_payload(db: Session, progress_of, user_id: int) -> list[dict]:
    """Canciones que no bajaron, de todos los trabajos, de la más reciente a la más vieja.

    Cada una lleva su trabajo (id, nombre, carpeta) para reintentarla o cambiarle la URL desde el historial.
    """
    rows = (
        db.query(Track, Job)
        .join(Job, Job.job_id == Track.job_id)
        .filter(Job.user_id == user_id, Track.status.in_(FAILED_STATUSES))
        .order_by(Track.updated_at.desc())
        .all()
    )
    result = []
    for track, job in rows:
        payload = track_payload(track, progress_of(track.track_id))
        payload["trabajo"] = _job_ref(db, job)
        result.append(payload)
    return result


def job_summary(db: Session, job_id: str) -> dict:
    return summary(db, _get_job(db, job_id))


def _downloading(db: Session, job: Job, progress_of, limit: int | None = None) -> list[dict]:
    """Canciones bajando ahora mismo en un trabajo, con su progreso (y sus temas, si es un tracklist)."""
    query = (db.query(Track).filter(Track.job_id == job.job_id, Track.status.in_(["in_progress", "cancelling"]))
             .order_by(Track.track_id))
    tracks = query.limit(limit).all() if limit else query.all()
    return [track_payload(t, progress_of(t.track_id)) for t in tracks]


def list_summaries(db: Session, user_id: int, progress_of=None) -> list[dict]:
    jobs = (
        db.query(Job)
        .filter(Job.user_id == user_id, Job.status.notin_(["resolving", "preview"]))
        .order_by(Job.updated_at.desc())
        .all()
    )
    return [
        {**summary(db, j), "descargando": _downloading(db, j, progress_of, limit=3)}
        if progress_of and j.status == "in_progress" else summary(db, j)
        for j in jobs
    ]


# --- Decisiones del usuario ----------------------------------------------------------------------

def set_included(db: Session, job_id: str, track_id: int, included: bool) -> Track:
    with _lock:
        job = _get_job(db, job_id)
        track = _get_track(db, job_id, track_id)
        if job.status not in ("resolving", "preview"):
            raise Conflict("El trabajo ya no está en vista previa.")
        if track.status not in ("preview", "rechazada"):
            raise Conflict("Esta canción no se puede quitar ni mantener.")
        track.status = "preview" if included else "rechazada"
        track.updated_at = _now()
        db.commit()
        return track


def _requested_options(track: Track) -> dict:
    options = _loads(track.tracklist, {}) or {}
    return options.get("pedido") or options


def research_track(db: Session, job_id: str, track_id: int) -> tuple[Track, dict | None]:
    """Vuelve a buscar una sola canción de la vista previa (por ejemplo, si salió con score bajo), sin rehacer el
    trabajo. Queda «buscando» y el resolvedor la actualiza. Devuelve la canción y sus metadatos conocidos (Spotify)."""
    with _lock:
        job = _get_job(db, job_id)
        if job.status not in ("resolving", "preview"):
            raise Conflict("La canción ya no se puede volver a buscar: el trabajo ya se confirmó.")
        track = _get_track(db, job_id, track_id)
        if track.ya_descargada or track.status not in ("preview", "rechazada"):
            raise Conflict("Esta canción no se puede volver a buscar ahora.")
        known_meta = None
        if origen_of(track.url) == "spotify" and track.title:
            known_meta = {"name": track.title, "artists": [track.artist] if track.artist else [], "duration_s": track.duration_s or 0}
        # Se recuerda el elegido actual: al volver, sigue elegido y lo nuevo se ofrece como sugerencia.
        preview = _loads(track.preview, {})
        best = next(iter(preview.get("candidatos") or []), None)
        if best:
            preview["elegido_antes"] = best
            track.preview = json.dumps(preview)
        track.status = "resolving"
        track.updated_at = _now()
        db.commit()
        refresh_preview_status(db, job_id)
        return track, known_meta


def _tema_duration(track: Track, chapters: list[dict], index: int) -> float | None:
    chapter = chapters[index]
    fin = chapter.get("fin_s")
    if fin is None:
        fin = chapters[index + 1].get("inicio_s") if index + 1 < len(chapters) else track.duration_s
    return (fin - (chapter.get("inicio_s") or 0)) if fin else None


def research_tema(db: Session, job_id: str, track_id: int, index: int) -> dict:
    """Vuelve a buscar un solo tema del tracklist (como «Volver a buscar» una canción). La canción queda «buscando»
    mientras tanto; el tema elegido sigue elegido y lo nuevo se ofrece como sugerencia. Devuelve qué buscar."""
    with _lock:
        job = _get_job(db, job_id)
        if job.status not in ("resolving", "preview"):
            raise Conflict("El tema ya no se puede volver a buscar: el trabajo ya se confirmó.")
        track = _get_track(db, job_id, track_id)
        if track.ya_descargada or track.status not in ("preview", "rechazada"):
            raise Conflict("Este tema no se puede volver a buscar ahora.")
        preview = _loads(track.preview, {})
        chapters = preview.get("chapters") or []
        if not 0 <= index < len(chapters):
            raise NotFound("No existe ese tema.")
        chapter = chapters[index]
        video = _tema_video(chapter)
        chapter["elegido_antes"] = chapter["candidatos"][0] if video else None
        chapter["buscando"] = True
        chapter.pop("sugerencias", None)
        preview["estado_antes_tema"] = track.status
        track.preview = json.dumps(preview)
        track.status = "resolving"
        track.updated_at = _now()
        db.commit()
        refresh_preview_status(db, job_id)
        return {"artista": chapter.get("artista") or "", "cancion": chapter.get("cancion") or chapter.get("titulo") or "",
                "duracion": _tema_duration(track, chapters, index)}


def save_tema_research(db: Session, track_id: int, index: int, found: list[dict]) -> None:
    """Guarda la búsqueda nueva de un tema y devuelve la canción a la vista previa."""
    with _lock:
        track = db.get(Track, track_id)
        if track is None:
            return
        preview = _loads(track.preview, {})
        chapters = preview.get("chapters") or []
        if 0 <= index < len(chapters):
            chapter = chapters[index]
            before = chapter.pop("elegido_antes", None)
            chapter.pop("buscando", None)
            if before:
                others = [c for c in found if c.get("url") != before.get("url")]
                chapter["candidatos"] = [before] + others
                chapter["sugerencias"] = others[:3]
            else:
                chapter["candidatos"] = found
                chapter.pop("elegido", None)
                good = bool(found) and (found[0].get("score") or 0) >= MIN_TEMA_SCORE
                # Si nada alcanza, las mejores se ofrecen igual para que el usuario elija.
                chapter["sugerencias"] = found[1:4] if good else found[:3]
        track.status = preview.pop("estado_antes_tema", None) or "preview"
        track.preview = json.dumps(preview)
        track.updated_at = _now()
        db.commit()
        refresh_preview_status(db, track.job_id)


def choose_tema_suggestion(db: Session, job_id: str, track_id: int, index: int, url: str | None, accept: bool) -> Track:
    """Sugerencia de un tema: aceptarla la pone como su video (aunque su score sea bajo); rechazarla la quita."""
    with _lock:
        track = _get_track(db, job_id, track_id)
        if track.status not in ("preview", "rechazada"):
            raise Conflict("Este tema ya no se puede cambiar.")
        preview = _loads(track.preview, {})
        chapters = preview.get("chapters") or []
        if not 0 <= index < len(chapters):
            raise NotFound("No existe ese tema.")
        chapter = chapters[index]
        sugerencias = chapter.get("sugerencias") or []
        chosen = next((c for c in sugerencias if c.get("url") == url), None)
        if chosen is None:
            raise NotFound("Esa sugerencia ya no está.")
        if accept:
            chapter["candidatos"] = [chosen] + [c for c in chapter.get("candidatos") or [] if c.get("url") != url]
            chapter["elegido"] = True
            chapter["sugerencias"] = []
        else:
            chapter["sugerencias"] = [c for c in sugerencias if c.get("url") != url]
        track.preview = json.dumps(preview)
        track.updated_at = _now()
        db.commit()
        return track


def choose_suggestion(db: Session, job_id: str, track_id: int, url: str | None, accept: bool) -> Track:
    """Sugerencia de reemplazo (tras «Volver a buscar»): aceptarla la pone como el video elegido (y se descartan las
    demás); rechazarla la quita de las sugerencias."""
    with _lock:
        track = _get_track(db, job_id, track_id)
        if track.status not in ("preview", "rechazada"):
            raise Conflict("Esta canción ya no se puede cambiar.")
        preview = _loads(track.preview, {})
        sugerencias = preview.get("sugerencias") or []
        chosen = next((c for c in sugerencias if c.get("url") == url), None)
        if chosen is None:
            raise NotFound("Esa sugerencia ya no está.")
        if accept:
            candidatos = preview.get("candidatos") or []
            preview["candidatos"] = [chosen] + [c for c in candidatos if c.get("url") != url]
            preview["sugerencias"] = []
            track.motivo = None
        else:
            preview["sugerencias"] = [c for c in sugerencias if c.get("url") != url]
        track.preview = json.dumps(preview)
        track.updated_at = _now()
        db.commit()
        return track


FORMATOS = ("mp3", "original")


def is_individual(db: Session, job: Job) -> bool:
    """Trabajo «I»: una sola canción, sin playlist (la misma regla que la letra P/L/I de la web)."""
    return not job.playlist_name and db.query(Track).filter(Track.job_id == job.job_id).count() <= 1


def job_format(db: Session, job: Job | None) -> str:
    """Formato con que se bajan las canciones del trabajo. Una canción individual siempre va en MP3."""
    if job is None or is_individual(db, job):
        return "mp3"
    return job.formato or "mp3"


def set_format(db: Session, job_id: str, formato: str) -> list[tuple[int, str, dict | None]]:
    """MP3 (convertido) u original (Opus o AAC sin convertir), mientras el trabajo está en vista previa. Las
    canciones individuales («I») se bajan siempre en MP3. «Ya descargada» depende del formato: se vuelve a calcular
    (tener una canción en MP3 no impide bajarla también en original). Devuelve las canciones que hay que resolver."""
    if formato not in FORMATOS:
        raise Conflict("Formato no válido: elige «mp3» u «original».")
    with _lock:
        job = _get_job(db, job_id)
        if job.status not in ("resolving", "preview"):
            raise Conflict("El formato ya no se puede cambiar: el trabajo ya se confirmó.")
        if formato != "mp3" and is_individual(db, job):
            raise Conflict("Una canción individual se baja siempre en MP3.")
        job.formato = formato
        job.updated_at = _now()
        to_resolve = _recompute_already_downloaded(db, job)
        db.commit()
    refresh_preview_status(db, job_id)
    return to_resolve


def set_folder(db: Session, job_id: str, carpeta: str | None) -> list[tuple[int, str, dict | None]]:
    """Cambia la carpeta de destino mientras el trabajo está en vista previa. Vacía vuelve a la de por defecto.
    «Ya descargada» depende de la carpeta: se vuelve a calcular. Devuelve las canciones que hay que resolver."""
    with _lock:
        job = _get_job(db, job_id)
        if job.status not in ("resolving", "preview"):
            raise Conflict("La carpeta ya no se puede cambiar: el trabajo ya se confirmó.")
        job.carpeta = (carpeta or "").strip() or job.playlist_name or DEFAULT_FOLDER
        job.updated_at = _now()
        to_resolve = _recompute_already_downloaded(db, job)
        db.commit()
    refresh_preview_status(db, job_id)
    return to_resolve


def _recompute_already_downloaded(db: Session, job: Job) -> list[tuple[int, str, dict | None]]:
    """Tras cambiar la carpeta o el formato: lo que ya estaba bajado pero no coincide pasa a la vista previa para
    bajarse; lo que sí coincide pasa a «ya descargada». Devuelve las copias sin vista previa guardada (a resolver)."""
    to_resolve: list[tuple[int, str, dict | None]] = []
    tracks = db.query(Track).filter(Track.job_id == job.job_id).all()
    candidates = [t for t in tracks if t.ya_descargada or t.status == "preview"]
    priors = _previously_downloaded(
        db, [t.url for t in candidates], job.user_id, carpeta=job.carpeta,
        tracklist_by_url={t.url: _requested_options(t) for t in candidates}, formato=job_format(db, job),
    )
    for track in candidates:
        prior = priors.get(track.url)
        if prior and not track.ya_descargada:
            pedido = {k: v for k, v in (_loads(track.tracklist, {}) or {}).items() if k != "pedido"}
            track.status, track.ya_descargada = "successful", True
            track.tracklist = json.dumps({**_loads(prior.tracklist, {}), "pedido": pedido}) if track.tracklist else None
        elif not prior and track.ya_descargada:
            track.ya_descargada = False
            if track.tracklist:
                track.tracklist = json.dumps(_requested_options(track))
            if track.preview:
                track.status = "preview"
            else:
                track.status = "resolving"
                to_resolve.append((track.track_id, track.url, None))
        else:
            continue
        track.updated_at = _now()
    return to_resolve


def set_tracklist(db: Session, job_id: str, track_id: int, cortar: bool | None, guardar_completo: bool | None,
                  excluidos: list[int] | None = None) -> Track:
    with _lock:
        track = _get_track(db, job_id, track_id)
        if track.status not in ("preview", "rechazada", "pending", "failed", "cancelada"):
            raise Conflict("El tracklist ya no se puede cambiar: la canción está en curso o terminó.")
        options = _loads(track.tracklist, {"cortar": False, "guardar_completo": True})
        if cortar is not None:
            options["cortar"] = bool(cortar)
        options["guardar_completo"] = True
        chapters = _loads(track.preview, {}).get("chapters") or []
        # Al activar el tracklist en la vista previa, sus temas todavía no se buscaron: se vuelve a resolver.
        if options.get("cortar") and chapters and not all("candidatos" in c for c in chapters) and track.status == "preview":
            track.status = "resolving"
        if excluidos is not None:
            total = len(_loads(track.preview, {}).get("chapters") or [])
            excluded = sorted({i for i in excluidos if 0 <= i < total})
            if total and len(excluded) == total:
                raise Conflict("Deja al menos un tema del tracklist.")
            options["excluidos"] = excluded
        track.tracklist = json.dumps(options)
        track.updated_at = _now()
        db.commit()
        return track


def change_url(db: Session, job_id: str, track_id: int, url: str, resolved: dict) -> tuple[Track, bool]:
    """Reemplaza la URL de una canción («Cambiar URL» / «Buscar otra URL»).

    Es otra búsqueda: los intentos vuelven a 0. `resolved` es lo que devolvió `core.resolve`
    (YouTube) o `core.score_url_for` envuelto en {"candidatos": [...]} (Spotify).
    Devuelve (canción, hay_que_encolar).
    """
    with _lock:
        job = _get_job(db, job_id)
        track = _get_track(db, job_id, track_id)
        if track.status in ("in_progress", "cancelling"):
            raise Conflict("La canción se está descargando: cancélala antes de cambiar la URL.")

        preview = _loads(track.preview, {})
        preview["url_anterior"] = preview.get("url_anterior") or []
        if origen_of(track.url) != "spotify":
            preview["url_anterior"].append(track.url)
            track.url = url
            track.title = resolved.get("title") or track.title
            track.artist = resolved.get("artist") or track.artist
            track.duration_s = resolved.get("duration_s") or track.duration_s
            preview["chapters"] = resolved.get("chapters") or []
            preview["thumbnail"] = resolved.get("thumbnail")
        else:
            preview["url_anterior"].append((preview.get("candidatos") or [{}])[0].get("url"))
        preview["candidatos"] = resolved.get("candidatos") or []
        preview["disponible"] = bool(resolved.get("disponible", True))
        track.preview = json.dumps(preview)
        track.rondas_usadas = 0
        track.motivo = None
        track.updated_at = _now()

        enqueue = job.status not in ("resolving", "preview")
        track.status = "pending" if enqueue else "preview"
        if enqueue:
            job.status = "in_progress"
            job.elapsed_time_s = None
            job.updated_at = _now()
        db.commit()
        return track, enqueue


def confirm(db: Session, job_id: str) -> list[tuple[int, str, dict | None]]:
    with _lock:
        job = _get_job(db, job_id)
        if job.status != "preview":
            raise Conflict("El trabajo no está en vista previa." if job.status != "resolving" else "Todavía se están buscando canciones.")
        if job.user_id is not None:
            user = db.get(User, job.user_id)
            if used_bytes(db, job.user_id) >= user.quota_bytes:
                raise Conflict(f"Llegaste a tu límite de almacenamiento ({user.quota_bytes / 1024 ** 3:g} GB). Libera espacio en Biblioteca.")
            # El nombre de la carpeta tal como lo escribió el usuario (para Biblioteca y el selector): se guarda aquí,
            # en la misma sesión, y no en los hilos de descarga (no compiten por escribir en SQLite).
            from core import folders

            nombre = job.carpeta or DEFAULT_FOLDER
            folders.remember_name(folders.user_root(user.username) / job_service.slugify(nombre), nombre, db)

        to_enqueue = []
        for track in db.query(Track).filter(Track.job_id == job_id, Track.status == "preview").order_by(Track.track_id).all():
            preview = _loads(track.preview, {})
            # Una canción de Spotify sin candidatos no tiene nada que bajar. Un video de YouTube que no se
            # pudo leer se intenta igual: el motor tiene respaldos (rotación de clientes, notube).
            if origen_of(track.url) == "spotify" and not preview.get("candidatos"):
                track.status = "rechazada"
                continue
            track.status = "pending"
            to_enqueue.append((track.track_id, track.url, None))

        now = _now()
        job.status = "in_progress" if to_enqueue else "completed"
        job.created_at = now
        job.updated_at = now
        job.amount = len(to_enqueue)
        db.commit()
        return to_enqueue


# --- Durante la descarga -------------------------------------------------------------------------

def selected_chapters(chapters: list[dict], excluidos: list[int]) -> list[dict]:
    """Los temas a cortar, sin los excluidos. Cada uno con su `fin_s` fijo: al quitar uno, el anterior no se alarga."""
    excluded = set(excluidos)
    selected = []
    for i, chapter in enumerate(chapters):
        if i in excluded:
            continue
        end = chapter.get("fin_s")
        if end is None and i + 1 < len(chapters):
            end = chapters[i + 1].get("inicio_s")
        selected.append({**chapter, "fin_s": end})
    return selected


def _plan_temas(db: Session, track: Track, chapters: list[dict], tracklist: dict) -> list[dict]:
    """Los temas a bajar: los incluidos que todavía no están en la carpeta. Cada uno con su índice en el tracklist,
    su tramo del set (para cortarlo si no hay video) y los candidatos buenos de su búsqueda."""
    excluded = set(tracklist.get("excluidos") or [])
    already = downloaded_temas(db, track)
    plan = []
    for i, chapter in enumerate(selected_chapters(chapters, [])):
        if i in excluded or i in already:
            continue
        plan.append({
            "index": i,
            "titulo": chapter.get("titulo") or f"Tema {i + 1}",
            "artista": chapter.get("artista") or "",
            "cancion": chapter.get("cancion") or chapter.get("titulo") or "",
            "inicio_s": chapter.get("inicio_s") or 0,
            "fin_s": chapter.get("fin_s"),
            "candidatos": _tema_candidates(chapter),
        })
    return plan


def _tema_candidates(chapter: dict) -> list[dict]:
    """Los candidatos que se intentan al bajar un tema: los de score suficiente, y primero el que eligió el usuario
    (aunque su score sea bajo)."""
    candidatos = chapter.get("candidatos") or []
    good = [c for c in candidatos if (c.get("score") or 0) >= MIN_TEMA_SCORE]
    if chapter.get("elegido") and candidatos:
        return [candidatos[0]] + [c for c in good if c.get("url") != candidatos[0].get("url")]
    return good


def wants_temas(db: Session, track_id: int) -> bool:
    track = db.get(Track, track_id)
    return track is not None and origen_of(track.url) == "youtube" and bool((_loads(track.tracklist, {}) or {}).get("cortar"))


def download_plan(db: Session, track_id: int) -> dict:
    """Lo que el worker necesita para bajar la canción: candidatos revisados, capítulos y tracklist."""
    track = db.get(Track, track_id)
    preview = _loads(track.preview, {})
    tracklist = _loads(track.tracklist, {})
    known_meta = None
    if origen_of(track.url) == "spotify" and track.title:
        known_meta = {"name": track.title, "artists": [track.artist] if track.artist else [], "duration_s": track.duration_s or 0}
    job = db.get(Job, track.job_id)
    return {
        "formato": job_format(db, job),
        "candidates": preview.get("candidatos") or None,
        "temas": _plan_temas(db, track, preview.get("chapters") or [], tracklist) if tracklist.get("cortar") else [],
        "cortar": bool(tracklist.get("cortar")),
        "known_meta": known_meta,
        "title": track.title,
    }


def start_round(db: Session, track_id: int) -> bool:
    """Marca la canción en curso y suma una ronda. False si ya no hay que bajarla (cancelada, rechazada…)."""
    with _lock:
        track = db.get(Track, track_id)
        if track is None or track.status != "pending":
            return False
        # Seguridad: si el trabajo se canceló (por ejemplo mientras se reanudaban sus canciones), no arranca.
        job = db.get(Job, track.job_id)
        if job is not None and job.status == "cancelled":
            track.status = "cancelada"
            track.updated_at = _now()
            db.commit()
            return False
        track.status = "in_progress"
        track.rondas_usadas = (track.rondas_usadas or 0) + 1
        track.motivo = None
        track.updated_at = _now()
        db.commit()
        return True


def _motivo(intentos: list[dict], message: str) -> str | None:
    if "Ningun candidato" in (message or "") or "Sin candidatos" in (message or ""):
        return "no_candidates"
    kinds = [it.get("tipo_error") for it in intentos if it.get("tipo_error")]
    for kind in ("needs_auth", "premium_only", "geo_restricted", "client_blocked", "stalled"):
        if kind in kinds:
            return kind
    if "stalled" in (message or ""):
        return "stalled"
    return kinds[-1] if kinds else "error"


def finish_round(db: Session, track_id: int, result: dict, elapsed_s: float, files: dict | None = None) -> None:
    """Guarda el resultado de una ronda y aplica la regla de intentos."""
    estado = result["estado"]
    intentos = result.get("intentos") or []
    motivo = None if estado in ("successful", "incorrect") else _motivo(intentos, result.get("mensaje"))

    final = estado
    if estado == "failed" and motivo == "needs_auth":
        final = "waiting_auth"

    def apply_rules(track: Track) -> None:
        if estado in ("successful", "incorrect"):
            # Recuperada: bajó después de fallar (más de una ronda) o con una URL cambiada por el usuario.
            preview = _loads(track.preview, {})
            track.recuperada = (track.rondas_usadas or 0) > 1 or bool(preview.get("url_anterior"))
            if files:
                track.archivo = files.get("archivo")
                track.bytes = files.get("bytes")
                if files.get("temas") is not None:
                    options = _loads(track.tracklist, {}) or {}
                    options["archivos"] = {str(k): v for k, v in files["temas"].items()}
                    options["fuentes"] = {str(k): v for k, v in (files.get("fuentes") or {}).items()}
                    options["urls"] = {str(k): v for k, v in (files.get("urls") or {}).items()}
                    track.tracklist = json.dumps(options)
            track.rondas_usadas = 0
        elif final == "waiting_auth":
            track.rondas_usadas = max(0, (track.rondas_usadas or 1) - 1)
        track.motivo = motivo
        if result.get("mensaje") and not intentos:
            db.add(Attempt(track_id=track_id, provider=None, status_attempt="failed", message=result["mensaje"][:2000], timestamp_attempt=_now()))

    job_service.record_result(
        db, track_id, 0, final, intentos, result.get("titulo"), result.get("artista"), elapsed_s, before_commit=apply_rules,
    )
    if files and estado in ("successful", "incorrect"):
        from core import nube

        nube.track_ready(track_id)


def mark_cancelled(db: Session, track_id: int, elapsed_s: float) -> None:
    """Cancelada por el usuario: no gasta intento."""
    with _lock:
        track = db.get(Track, track_id)
        track.status = "cancelada"
        track.rondas_usadas = max(0, (track.rondas_usadas or 1) - 1)
        track.elapsed_s = (track.elapsed_s or 0) + elapsed_s
        track.updated_at = _now()
        db.add(Attempt(track_id=track_id, provider=None, status_attempt="failed", message="[CANCEL] cancelada por el usuario", timestamp_attempt=_now()))
        job_service._update_job_status(db, track.job_id)
        db.commit()


def request_cancel(db: Session, job_id: str, track_id: int) -> Track:
    """pending → cancelada al momento; in_progress → cancelling (el worker corta y pasa a cancelada)."""
    with _lock:
        track = _get_track(db, job_id, track_id)
        if track.status == "pending":
            track.status = "cancelada"
        elif track.status == "in_progress":
            track.status = "cancelling"
        else:
            raise Conflict("La canción ya terminó.")
        track.updated_at = _now()
        job_service._update_job_status(db, job_id)
        db.commit()
        return track


def request_cancel_job(db: Session, job_id: str) -> list[int]:
    """Cancela lo que falta del trabajo. Devuelve los ids en curso, para que el worker los corte."""
    with _lock:
        job = _get_job(db, job_id)
        running = []
        for track in db.query(Track).filter(Track.job_id == job_id, Track.status.in_(["pending", "in_progress"])).all():
            if track.status == "pending":
                track.status = "cancelada"
            else:
                track.status = "cancelling"
                running.append(track.track_id)
            track.updated_at = _now()
        job.status = "cancelled"
        job.updated_at = _now()
        job_service._update_job_status(db, job_id)
        db.commit()
        return running


def _reactivate_job(job: Job) -> None:
    job.status = "in_progress"
    job.elapsed_time_s = None
    job.updated_at = _now()


def retry_track(db: Session, job_id: str, track_id: int) -> Track:
    with _lock:
        job = _get_job(db, job_id)
        track = _get_track(db, job_id, track_id)
        if track.status not in ("failed", "skipped", "waiting_auth"):
            raise Conflict("Solo se reintentan canciones que fallaron.")
        if (track.rondas_usadas or 0) >= settings.MAX_RETRIES_PER_TRACK:
            raise Conflict("La canción ya no tiene intentos: cambia la URL.")
        track.status = "pending"
        track.updated_at = _now()
        _reactivate_job(job)
        db.commit()
        return track


def requeue_track(db: Session, job_id: str, track_id: int) -> Track:
    with _lock:
        job = _get_job(db, job_id)
        track = _get_track(db, job_id, track_id)
        if track.status != "cancelada":
            raise Conflict("Solo se vuelven a encolar canciones canceladas.")
        track.status = "pending"
        track.updated_at = _now()
        _reactivate_job(job)
        db.commit()
        return track


def resume_interrupted(db: Session) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """Al arrancar la API, lo que quedó a medias por un reinicio vuelve a la cola sin gastar intento.

    Devuelve (para_descargar, para_resolver), cada uno como [(job_id, track_id, url)].
    """
    with _lock:
        to_download = []
        for track in db.query(Track).filter(Track.status.in_(["in_progress", "cancelling", "pending"])).all():
            if track.status == "cancelling":
                track.status = "cancelada"
                track.rondas_usadas = max(0, (track.rondas_usadas or 1) - 1)
                continue
            if track.status == "in_progress":
                track.rondas_usadas = max(0, (track.rondas_usadas or 1) - 1)
                track.status = "pending"
            to_download.append((track.job_id, track.track_id, track.url))

        to_resolve = [(t.job_id, t.track_id, t.url) for t in db.query(Track).filter(Track.status == "resolving").all()]
        db.commit()
        return to_download, to_resolve


# --- Tablero de Historial ------------------------------------------------------------------------

LISTED_STATUSES = ("successful", "incorrect", "failed", "skipped", "waiting_auth", "rechazada", "cancelada")


def _user_jobs(db: Session, user_id: int):
    return db.query(Job).filter(Job.user_id == user_id)


def queue_payload(db: Session, user_id: int, progress_of, limit: int | None = None) -> tuple[list[dict], int]:
    """Canciones en espera del usuario, en el orden en que se van a bajar (posición 1 = la próxima)."""
    query = (
        db.query(Track, Job)
        .join(Job, Job.job_id == Track.job_id)
        .filter(Job.user_id == user_id, Track.status == "pending")
        .order_by(Job.created_at, Track.track_id)
    )
    total = query.count()
    rows = query.limit(limit).all() if limit else query.all()
    items = []
    for position, (track, job) in enumerate(rows, start=1):
        payload = track_payload(track, progress_of(track.track_id))
        payload["posicion"] = position
        payload["trabajo"] = {"job_id": job.job_id, "nombre": job.playlist_name or job.carpeta, "carpeta": job.carpeta}
        items.append(payload)
    return items, total


def unconfirmed_payload(db: Session, user_id: int) -> list[dict]:
    """Búsquedas que el usuario mandó y nunca confirmó (siguen en vista previa)."""
    jobs = _user_jobs(db, user_id).filter(Job.status.in_(["resolving", "preview"])).order_by(Job.created_at.desc()).all()
    result = []
    for job in jobs:
        tracks = db.query(Track).filter(Track.job_id == job.job_id).all()
        result.append({
            "job_id": job.job_id,
            "nombre": job.playlist_name or job.carpeta,
            "playlist_name": job.playlist_name,  # para la letra del tipo (P playlist, L lista, I individual)
            "carpeta": job.carpeta,
            "origen": job.origen,
            "status": job.status,
            "total": len(tracks),
            "resueltas": sum(1 for t in tracks if t.status != "resolving"),
            "created_at": job.created_at,
        })
    return result


def discard_job(db: Session, job_id: str) -> None:
    """Descarta una búsqueda sin confirmar: se borra con sus canciones (no tiene archivos)."""
    with _lock:
        job = _get_job(db, job_id)
        if job.status not in ("resolving", "preview"):
            raise Conflict("Solo se descartan búsquedas que todavía no se confirmaron.")
        db.delete(job)
        db.commit()


def _job_ref(db: Session, job: Job) -> dict:
    """El trabajo de una canción, para agruparlas en los paneles: nombre, carpeta, letra del tipo (playlist y total)
    y fecha de creación."""
    total = db.query(Track).filter(Track.job_id == job.job_id).count()
    return {"job_id": job.job_id, "nombre": job.playlist_name or job.carpeta, "carpeta": job.carpeta, "origen": job.origen,
            "playlist_name": job.playlist_name, "total": total, "created_at": job.created_at}


def omitted_tracks(db: Session, user_id: int, progress_of, limit: int | None = None) -> tuple[list[dict], int]:
    """Omitidas: canciones que se pidieron pero no se bajaron por decisión o porque no hacía falta. Son las que el
    usuario quitó en la vista previa («rechazada») y las que ya estaban en la carpeta («ya descargada»).
    Solo de trabajos confirmados, de la más reciente a la más vieja."""
    from sqlalchemy import or_

    query = (
        db.query(Track, Job)
        .join(Job, Job.job_id == Track.job_id)
        .filter(Job.user_id == user_id, Job.status.notin_(["resolving", "preview"]),
                or_(Track.status == "rechazada", Track.ya_descargada.is_(True)))
        .order_by(Track.updated_at.desc())
    )
    total = query.count()
    rows = query.limit(limit).all() if limit else query.all()
    items = []
    for track, job in rows:
        payload = track_payload(track, progress_of(track.track_id))
        payload["motivo_omitida"] = "ya_descargada" if track.ya_descargada else "quitada"
        payload["trabajo"] = _job_ref(db, job)
        items.append(payload)
    return items, total


def tracks_with_status(db: Session, user_id: int, statuses: tuple, progress_of, limit: int | None = None) -> tuple[list[dict], int]:
    """Canciones del usuario en ciertos estados (fallidas, rechazadas), de la más reciente a la más vieja,
    solo de trabajos ya confirmados."""
    query = (
        db.query(Track, Job)
        .join(Job, Job.job_id == Track.job_id)
        .filter(Job.user_id == user_id, Job.status.notin_(["resolving", "preview"]), Track.status.in_(statuses))
        .order_by(Track.updated_at.desc())
    )
    total = query.count()
    rows = query.limit(limit).all() if limit else query.all()
    items = []
    for track, job in rows:
        payload = track_payload(track, progress_of(track.track_id))
        payload["trabajo"] = _job_ref(db, job)
        items.append(payload)
    return items, total


def resume_job(db: Session, job_id: str) -> list[tuple[int, str]]:
    """«Reanudar todas»: vuelve a encolar, en una sola operación, las omitidas (quitadas en la vista previa) y las
    canceladas de un trabajo confirmado. Al ser una sola transacción, una cancelación posterior las alcanza a todas
    (no quedan pedidos sueltos llegando después). Devuelve las que hay que encolar."""
    with _lock:
        job = _get_job(db, job_id)
        if job.status in ("resolving", "preview"):
            raise Conflict("El trabajo todavía no se confirmó.")
        resumed: list[tuple[int, str]] = []
        for track in db.query(Track).filter(Track.job_id == job_id, Track.status.in_(["rechazada", "cancelada"])).all():
            if origen_of(track.url) == "spotify" and not _loads(track.preview, {}).get("candidatos"):
                continue  # sin candidatos no hay qué bajar: se recupera cambiando la URL
            if track.status == "rechazada":
                track.rondas_usadas = 0
            track.status = "pending"
            track.updated_at = _now()
            resumed.append((track.track_id, track.url))
        if resumed:
            _reactivate_job(job)
        db.commit()
        return resumed


def recover_track(db: Session, job_id: str, track_id: int) -> tuple[Track, bool]:
    """«Recuperar» una rechazada. En vista previa vuelve a la lista; ya confirmado el trabajo, se encola.
    Devuelve (canción, hay_que_encolar)."""
    with _lock:
        job = _get_job(db, job_id)
        track = _get_track(db, job_id, track_id)
        if track.status != "rechazada":
            raise Conflict("Solo se recuperan canciones rechazadas.")
        if job.status in ("resolving", "preview"):
            track.status = "preview"
            db.commit()
            return track, False
        preview = _loads(track.preview, {})
        if origen_of(track.url) == "spotify" and not preview.get("candidatos"):
            raise Conflict("Esta canción no tiene candidatos: cambia la URL para recuperarla.")
        track.status = "pending"
        track.rondas_usadas = 0
        track.updated_at = _now()
        _reactivate_job(job)
        db.commit()
        return track, True


RECENT_MAX = 100


def recent_payload(db: Session, user_id: int, progress_of, page: int = 1, limit: int = 8, categoria: str | None = None) -> dict:
    """Canciones recientes de todas las carpetas del usuario, paginadas (tabla principal del tablero)."""
    query = (
        db.query(Track, Job)
        .join(Job, Job.job_id == Track.job_id)
        .filter(Job.user_id == user_id, Job.status.notin_(["resolving", "preview"]), Track.status.in_(LISTED_STATUSES))
    )
    if categoria == "descargada":
        query = query.filter(Track.status == "successful", Track.recuperada.is_(False))
    elif categoria == "recuperada":
        query = query.filter(Track.status.in_(["successful", "incorrect"]), Track.recuperada.is_(True))
    elif categoria == "dudosa":
        query = query.filter(Track.status == "incorrect")
    elif categoria == "fallida":
        query = query.filter(Track.status.in_(FAILED_STATUSES))
    elif categoria == "rechazada":
        query = query.filter(Track.status == "rechazada")

    # Solo las RECENT_MAX más recientes (5 páginas de 20): las más viejas siguen en la base, pero no se listan.
    total = min(query.count(), RECENT_MAX)
    limit = max(1, min(limit, 50))
    pages = max(1, -(-total // limit))
    page = max(1, min(page, pages))
    offset = (page - 1) * limit
    rows = query.order_by(Track.updated_at.desc()).offset(offset).limit(max(0, min(limit, total - offset))).all()

    items = []
    for track, job in rows:
        payload = track_payload(track, progress_of(track.track_id))
        payload["trabajo"] = {"job_id": job.job_id, "nombre": job.playlist_name or job.carpeta, "carpeta": job.carpeta,
                              "carpeta_slug": job_service.slugify(job.carpeta or DEFAULT_FOLDER)}
        payload["tipo_descarga"] = download_kind(track, job)
        items.append(payload)
    return {"items": items, "total": total, "page": page, "pages": pages, "limit": limit}


def download_kind(track: Track, job: Job) -> str:
    """Cómo se pidió la canción: «tracklist» (un set cortado por canciones), «lista» (parte de una playlist o de
    varios enlaces) o «suelta» (un solo enlace)."""
    if (_loads(track.tracklist, {}) or {}).get("cortar") and (_loads(track.preview, {}).get("chapters")):
        return "tracklist"
    return "lista" if job.playlist_name or (job.amount or 0) > 1 else "suelta"


def redownload(db: Session, track_id: int, user_id: int) -> tuple[str, int, str]:
    """Vuelve a encolar una canción pasada en su misma carpeta, aunque ya se haya bajado: crea un trabajo nuevo de una
    canción, ya confirmado, que baja el mismo candidato revisado. Devuelve (job_id, track_id, url)."""
    with _lock:
        row = (
            db.query(Track, Job)
            .join(Job, Job.job_id == Track.job_id)
            .filter(Track.track_id == track_id, Job.user_id == user_id)
            .first()
        )
        if row is None:
            raise NotFound("No existe la canción.")
        old, job = row
        now = _now()
        new_job = Job(
            job_id=job_service.new_job_id(),
            type="manual-urls",
            playlist_name=None,
            amount=1,
            status="in_progress",
            origen=origen_of(old.url),
            carpeta=job.carpeta,
            user_id=user_id,
            created_at=now,
            updated_at=now,
        )
        db.add(new_job)
        db.flush()
        new_track = Track(
            job_id=new_job.job_id,
            url=old.url,
            status="pending",
            max_attempts=calculate_max_attempts(old.url),
            elapsed_s=0.0,
            title=old.title,
            artist=old.artist,
            duration_s=old.duration_s,
            preview=old.preview,
            tracklist=old.tracklist,
            created_at=now,
            updated_at=now,
        )
        db.add(new_track)
        db.commit()
        return new_job.job_id, new_track.track_id, new_track.url


PROBLEMS_ON_BOARD = 10


def dashboard(db: Session, user: User, progress_of, n: int = 4) -> dict:
    """Todo lo del tablero en un pedido: contadores, almacenamiento y las primeras filas de cada bloque."""
    active_jobs = _user_jobs(db, user.id).filter(Job.status == "in_progress").order_by(Job.created_at).all()
    en_curso = []
    for job in active_jobs[:n]:
        en_curso.append({**summary(db, job), "descargando": _downloading(db, job, progress_of, limit=3)})

    cola, en_cola = queue_payload(db, user.id, progress_of, limit=n)
    sin_confirmar = unconfirmed_payload(db, user.id)
    # Fallidas y omitidas: hasta 10 en el tablero (el resto, con «Ver más»).
    fallidas, total_fallidas = tracks_with_status(db, user.id, FAILED_STATUSES, progress_of, limit=PROBLEMS_ON_BOARD)
    rechazadas, total_rechazadas = tracks_with_status(db, user.id, ("rechazada",), progress_of, limit=n)
    omitidas, total_omitidas = omitted_tracks(db, user.id, progress_of, limit=PROBLEMS_ON_BOARD)

    return {
        "contadores": {
            "en_curso": len(active_jobs),
            "en_cola": en_cola,
            "sin_confirmar": len(sin_confirmar),
            "fallidas": total_fallidas,
            "rechazadas": total_rechazadas,
            "omitidas": total_omitidas,
        },
        "almacenamiento": {"usado_bytes": used_bytes(db, user.id), "cuota_bytes": user.quota_bytes},
        "en_curso": en_curso,
        "cola": cola,
        "sin_confirmar": sin_confirmar,
        "fallidas": fallidas,
        "rechazadas": rechazadas,
        "omitidas": omitidas,
    }
