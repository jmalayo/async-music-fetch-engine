import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy.orm import Session

from musictools.download import audio, progress, ytdlp_lib
from musictools.download.core import (
    SpotifyCredentials, configure, default_js_runtime, download, download_song, query_variants, resolve, score_url_for, search_song,
    split_tema_title,
)
from musictools.download.progress import Reporter, TrackCancelled

from config.settings import settings
from infrastructure.persistence.database import SessionLocal
from infrastructure.persistence.models import Track, User
from core import folders, job_service, web_jobs

# Dos pools: uno para descargar (settings.PROCESSING_THREADS en paralelo) y otro para la vista previa, así buscar canciones
# nuevas no espera a que terminen las descargas en curso.
_executor = ThreadPoolExecutor(max_workers=settings.PROCESSING_THREADS, thread_name_prefix="download")
_resolver = ThreadPoolExecutor(max_workers=settings.PROCESSING_THREADS, thread_name_prefix="resolve")
_js_runtime = default_js_runtime()
configure(
    top_n=settings.SPOTIFY_YOUTUBE_MATCH_LIMIT,
    youtube_clients=settings.YOUTUBE_CLIENTS,
    socket_timeout_s=settings.SOCKET_TIMEOUT_S,
    stall_timeout_s=settings.FFMPEG_STALL_TIMEOUT_S,
    notube_max_audio_duration_s=settings.NOTUBE_MAX_AUDIO_DURATION_S,
)
ytdlp_lib.BROWSER_COOKIES = settings.YTDLP_COOKIES_FROM_BROWSER or None
_spotify_creds = SpotifyCredentials(client_id=settings.SPOTIFY_CLIENT_ID, client_secret=settings.SPOTIFY_CLIENT_SECRET)

# Progreso en memoria de cada canción en curso: track_id → Reporter. No se escribe en la base.
_reporters: dict[int, Reporter] = {}
_reporters_lock = threading.Lock()


def resolve_output_dir(db: Session, job_id: str) -> Path:

    job = job_service.get_job(db, job_id)

    # Flujo de la web: con cuenta, la carpeta del usuario; sin cuenta, una carpeta temporal por trabajo.
    if job.origen is not None:
        if job.user_id is None:
            out_dir = folders.anon_root() / job_id
        else:
            user = db.get(User, job.user_id)
            out_dir = folders.user_root(user.username) / job_service.slugify(job.carpeta or folders.DEFAULT_FOLDER)
            out_dir.mkdir(parents=True, exist_ok=True)

    # Endpoints anteriores (/downloads): como siempre.
    elif job.type == "manual-urls":
        out_dir = settings.MUSIC_DOWNLOADED_ROOT / (job_service.slugify(job.playlist_name) if job.playlist_name else "any-musics")

    elif job.type == "playlist-urls":
        out_dir = settings.MUSIC_DOWNLOADED_ROOT / job_service.slugify(job.playlist_name)

    else:
        raise ValueError(f"Tipo de job no soportado: {job.type}")

    out_dir.mkdir(parents=True, exist_ok=True)

    return out_dir


def purge_anonymous() -> None:
    """Borra las descargas sin cuenta que quedaron sin entregar (se llama al arrancar, sin descargas en curso)."""
    shutil.rmtree(folders.anon_root(), ignore_errors=True)


# --- Progreso y cancelación -----------------------------------------------------------------------

def progress_of(track_id: int) -> dict | None:
    with _reporters_lock:
        reporter = _reporters.get(track_id)
    return reporter.snapshot() if reporter else None


def cancel_running(track_id: int) -> None:
    with _reporters_lock:
        reporter = _reporters.get(track_id)
    if reporter:
        reporter.cancel()


# --- Vista previa ---------------------------------------------------------------------------------

def _resolve_track(track_id: int, url: str, known_meta: dict | None, force: bool = False) -> None:
    """Busca una canción para la vista previa. Si el enlace ya se buscó hace poco (tabla busquedas), usa lo guardado
    sin consultar YouTube ni Spotify; `force` (botón «Volver a buscar») ignora lo guardado y lo reemplaza."""
    with SessionLocal() as db:
        cached = None if force else web_jobs.cached_resolution(db, url)
        track = db.get(Track, track_id)
        with_account = bool(track and track.job and track.job.user_id is not None)

    data = cached
    if data is None:
        try:
            with ytdlp_lib.browser_cookies_allowed(with_account):
                data = resolve(
                    url,
                    cookies_file=settings.COOKIES_FILE,
                    js_runtime=_js_runtime,
                    spotify_creds=_spotify_creds,
                    proxy=settings.PROXY,
                    known_meta=known_meta,
                )
        except Exception as e:  # noqa: BLE001 — cualquier error de una canción no debe frenar la vista previa
            with SessionLocal() as db:
                web_jobs.save_resolution_error(db, track_id, f"{type(e).__name__}: {e}")
            return

    # «Volver a buscar» una canción de Spotify: además de la búsqueda normal se prueban otras frases, y se juntan
    # todos los candidatos ordenados por score (de ahí salen las sugerencias de reemplazo).
    if force and data.get("origen") == "spotify" and known_meta:
        data = {**data, "candidatos": _wider_candidates(known_meta, data.get("candidatos") or [])}

    with SessionLocal() as db:
        temas = bool(data.get("chapters")) and web_jobs.wants_temas(db, track_id)
    # Los temas del tracklist se buscan solo si faltan (lo guardado puede venir de una vista previa sin tracklist).
    # También si se buscaron con un puntaje anterior (sin `busqueda` actual): se vuelven a buscar con el de ahora.
    searched_temas = temas and not all("candidatos" in c and c.get("busqueda") == TEMA_SEARCH_VERSION for c in data["chapters"])
    if searched_temas:
        data = {**data, "chapters": _search_temas(data["chapters"], data.get("duration_s"))}

    with SessionLocal() as db:
        if cached is None or searched_temas:
            web_jobs.store_resolution(db, url, data)
        web_jobs.save_resolution(db, track_id, data)


def _wider_candidates(meta: dict, found: list[dict], top_n: int = 8) -> list[dict]:
    artist = (meta.get("artists") or [""])[0] or ""
    title = meta.get("name") or ""
    by_url = {c.get("url"): c for c in found if c.get("url")}
    for query in query_variants(artist, title):
        try:
            more = search_song(artist, title, meta.get("duration_s"), cookies_file=settings.COOKIES_FILE, js_runtime=_js_runtime,
                               proxy=settings.PROXY, top_n=top_n, query=query)
        except Exception:  # noqa: BLE001 — una frase que falla no frena las demás
            more = []
        for c in more:
            if c.get("url") and c["url"] not in by_url:
                by_url[c["url"]] = c
    return sorted(by_url.values(), key=lambda c: c.get("score") or 0, reverse=True)[:top_n]


# Sube cuando cambia cómo se puntúan los temas: lo guardado con otra versión se vuelve a buscar.
TEMA_SEARCH_VERSION = 2


def _search_temas(chapters: list[dict], total_s: float | None) -> list[dict]:
    """Busca cada tema del tracklist como canción completa en YouTube (como con Spotify), varios a la vez."""
    def one(i: int, chapter: dict) -> dict:
        artista, cancion = split_tema_title(chapter.get("titulo") or "")
        fin = chapter.get("fin_s")
        if fin is None:
            fin = chapters[i + 1].get("inicio_s") if i + 1 < len(chapters) else total_s
        duracion = (fin - (chapter.get("inicio_s") or 0)) if fin else None
        try:
            candidatos = search_song(artista, cancion, duracion, cookies_file=settings.COOKIES_FILE, js_runtime=_js_runtime, proxy=settings.PROXY, tema=True)
        except Exception:  # noqa: BLE001 — sin búsqueda, el tema se corta del set
            candidatos = []
        found = {**chapter, "artista": artista, "cancion": cancion, "candidatos": candidatos, "busqueda": TEMA_SEARCH_VERSION}
        found.pop("elegido", None)
        # Score bajo: las 2 mejores coincidencias quedan como sugerencias desde esta misma búsqueda (✓ / ✕ / escuchar).
        best = (candidatos[0].get("score") or 0) if candidatos else None
        found["sugerencias"] = candidatos[:2] if best is not None and best < web_jobs.MIN_TEMA_SCORE else []
        return found

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="tema") as pool:
        return list(pool.map(lambda pair: one(*pair), enumerate(chapters)))


def _research_tema(track_id: int, index: int, meta: dict) -> None:
    """Busca otra vez un tema con varias frases (como «Volver a buscar» una canción de Spotify) y guarda los mejores."""
    found: dict[str, dict] = {}
    try:
        with SessionLocal() as db:
            track = db.get(Track, track_id)
            with_account = bool(track and track.job and track.job.user_id is not None)
        with ytdlp_lib.browser_cookies_allowed(with_account):
            for query in query_variants(meta["artista"], meta["cancion"])[:4]:
                for c in search_song(meta["artista"], meta["cancion"], meta["duracion"], cookies_file=settings.COOKIES_FILE,
                                     js_runtime=_js_runtime, proxy=settings.PROXY, top_n=8, query=query, tema=True):
                    if c["url"] not in found or (c.get("score") or 0) > (found[c["url"]].get("score") or 0):
                        found[c["url"]] = c
    except Exception:  # noqa: BLE001 — sin resultados, el tema queda como estaba
        pass
    ranked = sorted(found.values(), key=lambda c: c.get("score") or 0, reverse=True)[:8]
    with SessionLocal() as db:
        web_jobs.save_tema_research(db, track_id, index, ranked)


def enqueue_research_tema(track_id: int, index: int, meta: dict) -> None:
    _resolver.submit(_research_tema, track_id, index, meta)


def enqueue_resolve(tracks: list[tuple[int, str, dict | None]], force: bool = False) -> None:
    for track_id, url, known_meta in tracks:
        _resolver.submit(_resolve_track, track_id, url, known_meta, force)


def resolve_new_url(url: str, track: dict) -> dict:
    """Para «Cambiar URL»: lee la URL nueva y, si la canción es de Spotify, la puntúa contra sus metadatos."""
    if track["origen"] == "spotify":
        candidate = score_url_for(
            url, artist=track.get("artist") or "", title=track.get("title") or "", duration_s=track.get("duration_s"),
            cookies_file=settings.COOKIES_FILE, js_runtime=_js_runtime, proxy=settings.PROXY,
        )
        return {"candidatos": [candidate] if candidate else [], "disponible": candidate is not None}

    return resolve(url, cookies_file=settings.COOKIES_FILE, js_runtime=_js_runtime, proxy=settings.PROXY)


# --- Descarga -------------------------------------------------------------------------------------

def _move_results(work_dir: Path, out_dir: Path) -> dict:
    """Mueve lo terminado (MP3 y carpeta de temas) a la carpeta definitiva.

    Devuelve lo que se registra en la biblioteca: el archivo principal (el MP3 completo o, si solo quedaron los
    temas del tracklist, su carpeta), con ruta relativa a settings.MUSIC_DOWNLOADED_ROOT, y el tamaño total.
    """
    moved: list[Path] = []
    for item in work_dir.iterdir():
        if item.name.startswith("."):
            continue
        if item.is_dir() or item.suffix.lower() in audio.AUDIO_EXTS:
            target = out_dir / item.name
            if item.is_dir():
                # Se combinan: otra descarga del mismo set pudo dejar otros temas en esa carpeta.
                target.mkdir(exist_ok=True)
                for file in item.iterdir():
                    dest = target / file.name
                    if dest.exists():
                        dest.unlink()
                    shutil.move(str(file), str(dest))
            else:
                if target.exists():
                    target.unlink()
                shutil.move(str(item), str(target))
            moved.append(target)

    if not moved:
        return {"archivo": None, "bytes": None}

    size = lambda p: sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size  # noqa: E731
    main = next((p for p in moved if p.is_file()), moved[0])
    try:
        relative = str(main.relative_to(settings.MUSIC_DOWNLOADED_ROOT))
    except ValueError:
        relative = str(main)
    return {"archivo": relative, "bytes": sum(size(p) for p in moved)}


def _download_temas(work_dir: Path, plan: dict, reporter: Reporter) -> dict | None:
    """Baja cada tema del tracklist como canción completa (su propio video) y, si no hay uno bueno o falla, lo corta
    del set. El set completo queda guardado. Devuelve los archivos de cada tema y de dónde salió cada uno."""
    mp3s = sorted(p for p in work_dir.iterdir() if p.is_file() and p.suffix.lower() in audio.AUDIO_EXTS)
    if not mp3s or not plan["temas"]:
        return None
    full = mp3s[0]
    folder = work_dir / audio.safe_name(plan.get("title") or full.stem)
    folder.mkdir(exist_ok=True)

    temas = plan["temas"]
    estados = ["pendiente"] * len(temas)
    names: dict[int, str] = {}
    fuentes: dict[int, str] = {}
    urls: dict[int, str] = {}
    for k, tema in enumerate(temas):
        progress.check_cancelled()
        estados[k] = "bajando"
        reporter.set_context(etapa="temas", tema_actual=k, temas=len(temas), temas_estado=list(estados))
        reporter.update(fase="descargando", porcentaje=0.0, bytes=0, bytes_total=None, velocidad_bps=None, eta_s=None)
        index = tema["index"]
        stem = f"{index + 1:02d} - {audio.safe_name(tema['titulo'])}"
        target = folder / f"{stem}{full.suffix.lower()}"
        duracion = (tema["fin_s"] - tema["inicio_s"]) if tema.get("fin_s") is not None else None

        ok = False
        if tema["candidatos"]:
            tmp = work_dir / f".tema-{index}"
            tmp.mkdir(exist_ok=True)
            result = download_song(
                artist=tema["artista"], title=tema["cancion"], duration_s=duracion, candidates=tema["candidatos"], out_dir=tmp,
                cookies_file=settings.COOKIES_FILE, cache_path=settings.YOUTUBE_BLOCKED_VIDEOS_PATH, js_runtime=_js_runtime, fmt=plan.get("formato") or "mp3", bitrate=settings.MP3_ENCODING_BITRATE, proxy=settings.PROXY,
            )
            got = sorted(p for p in tmp.iterdir() if p.is_file() and p.suffix.lower() in audio.AUDIO_EXTS)
            if result["estado"] == "successful" and got:
                # Con formato original, el tema puede venir en otro contenedor que el set (Opus o AAC).
                target = folder / f"{stem}{got[0].suffix.lower()}"
                shutil.move(str(got[0]), str(target))
                ok = True
                fuentes[index] = "video"
                urls[index] = result.get("url") or tema["candidatos"][0]["url"]
            shutil.rmtree(tmp, ignore_errors=True)

        if not ok:
            # Sin video bueno: se corta el tramo exacto del set.
            tmp = work_dir / f".corte-{index}"
            cut = audio.cut_chapters(full, [{"inicio_s": tema["inicio_s"], "fin_s": tema["fin_s"], "titulo": tema["titulo"]}], tmp)
            shutil.move(str(cut[0]), str(target))
            shutil.rmtree(tmp, ignore_errors=True)
            fuentes[index] = "corte"

        names[index] = target.name
        estados[k] = "listo"
        reporter.set_context(temas_estado=list(estados))

    reporter.set_context(tema_actual=None, temas_estado=list(estados))
    return {"carpeta": folder.name, "nombres": names, "fuentes": fuentes, "urls": urls}


def _process_track(job_id: str, track_id: int, url: str, iteration_count: int, known_meta: dict | None = None) -> None:
    reporter = Reporter()
    with _reporters_lock:
        _reporters[track_id] = reporter

    work_dir: Path | None = None
    t0 = time.monotonic()

    try:
        with SessionLocal() as db:
            # Si la canción se canceló mientras esperaba en la cola, no se baja.
            if not web_jobs.start_round(db, track_id):
                return
            out_dir = resolve_output_dir(db, job_id)
            with_account = job_service.get_job(db, job_id).user_id is not None
            plan = web_jobs.download_plan(db, track_id)

        # Cada canción baja a su propia carpeta temporal: si se cancela o falla, se borra entera y no
        # quedan archivos a medias en la carpeta del usuario.
        work_dir = out_dir / f".parcial-{track_id}"
        shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True)

        with progress.using(reporter), ytdlp_lib.browser_cookies_allowed(with_account):
            if plan["cortar"] and plan["temas"]:
                reporter.set_context(etapa="set", tema_actual=None, temas=len(plan["temas"]), temas_estado=["pendiente"] * len(plan["temas"]),
                                     temas_indices=[t["index"] for t in plan["temas"]])
            result = download(
                url,
                out_dir=work_dir,
                cookies_file=settings.COOKIES_FILE,
                cache_path=settings.YOUTUBE_BLOCKED_VIDEOS_PATH,
                js_runtime=_js_runtime,
                fmt=plan.get("formato") or "mp3",
                bitrate=settings.MP3_ENCODING_BITRATE,
                spotify_creds=_spotify_creds,
                proxy=settings.PROXY,
                known_meta=known_meta or plan["known_meta"],
                candidates=plan["candidates"],
            )
            reporter.check()

            temas = None
            if result["estado"] in ("successful", "incorrect") and plan["cortar"]:
                temas = _download_temas(work_dir, plan, reporter)

        files = _move_results(work_dir, out_dir) if result["estado"] in ("successful", "incorrect") else None
        if files is not None and temas is not None:
            paths = {i: out_dir / temas["carpeta"] / name for i, name in temas["nombres"].items()}
            files["temas"] = {i: str(p.relative_to(settings.MUSIC_DOWNLOADED_ROOT)) for i, p in paths.items()}
            files["fuentes"] = temas["fuentes"]
            files["urls"] = temas["urls"]
            # Peso: el set completo más los temas de esta descarga (la carpeta puede tener temas de otras).
            full = settings.MUSIC_DOWNLOADED_ROOT / files["archivo"] if files.get("archivo") else None
            files["bytes"] = (full.stat().st_size if full and full.is_file() else 0) + sum(p.stat().st_size for p in paths.values())

        with SessionLocal() as db:
            web_jobs.finish_round(db, track_id, result, time.monotonic() - t0, files)

    except TrackCancelled:
        with SessionLocal() as db:
            web_jobs.mark_cancelled(db, track_id, time.monotonic() - t0)

    except Exception as e:  # noqa: BLE001
        with SessionLocal() as db:
            web_jobs.finish_round(
                db, track_id,
                {"estado": "failed", "intentos": [{"proveedor": None, "resultado": "error", "mensaje": f"{type(e).__name__}: {e}"}], "mensaje": f"{type(e).__name__}: {e}"},
                time.monotonic() - t0,
            )

    finally:
        with _reporters_lock:
            _reporters.pop(track_id, None)
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)


def enqueue_track(job_id: str, track_id: int, url: str, iteration_count: int = 0, known_meta: dict | None = None) -> None:
    _executor.submit(_process_track, job_id, track_id, url, iteration_count, known_meta)


def enqueue_job(job_id: str, tracks: list[tuple[int, str, dict | None]], iteration_count: int = 0) -> None:
    for track_id, url, known_meta in tracks:
        enqueue_track(job_id, track_id, url, iteration_count, known_meta)


def resume_after_restart() -> None:
    """Al arrancar la API: vuelve a encolar lo que quedó a medias por un reinicio y borra las descargas sin cuenta que no se entregaron."""
    purge_anonymous()
    with SessionLocal() as db:
        to_download, to_resolve = web_jobs.resume_interrupted(db)
    for job_id, track_id, url in to_download:
        enqueue_track(job_id, track_id, url)
    enqueue_resolve([(track_id, url, None) for _, track_id, url in to_resolve])
