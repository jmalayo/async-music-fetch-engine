import logging
import re
import shutil
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from . import progress
from .candidate_scoring import rank, score as score_candidate
from .notube import download_from_notube
from .spotify_metadata import get_track_metadata
from .ytdlp_lib import download_with_rotation, probe_info, search_flat
from .ytmusic_search import search as ytmusic_search

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_PRIVATE = {"premium_only", "needs_auth"}

MIN_SCORE = 60

# notube espera como máximo 90 s a que el sitio convierta: en audios más largos que esto no se intenta.
LONG_AUDIO_S = 900

@dataclass
class SpotifyCredentials:
    client_id: str
    client_secret: str

def default_js_runtime() -> str:
    """Deno si está (el que recomienda yt-dlp); si no, node (yt-dlp exige node 22 o más)."""
    deno = shutil.which("deno")
    if deno:
        return f"deno:{deno}"

    node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"

    return f"node:{node}"

@contextmanager
def _private_cookies_copy(cookies_file: Path):
    with tempfile.TemporaryDirectory(prefix="cookies-") as tmp_dir:
        private_copy = Path(tmp_dir) / f"cookies-{uuid.uuid4().hex}.txt"
        shutil.copyfile(cookies_file, private_copy)

        yield private_copy

def is_spotify(url: str) -> bool:
    return "open.spotify.com" in url

def is_youtube(url: str) -> bool:
    return "youtube.com/watch" in url or "youtu.be/" in url

def attempt_download(
    url: str,
    *,
    out_dir: Path,
    cookies_file: Path,
    cache_path: Path,
    js_runtime: str,
    fmt: str,
    bitrate: str,
    proxy: str | None = None,
):

    return download_with_rotation(
        url,
        cookies_file=cookies_file,
        out_dir=out_dir,
        fmt=fmt,
        bitrate=bitrate,
        js_runtime=js_runtime,
        cache_path=cache_path,
        proxy=proxy,
    )

def gather_candidates(
    query: str,
    *,
    cookies_file: Path,
    js_runtime: str,
    proxy: str | None = None,
    candidates_per_source: int = 10,
    candidates_max: int = 15,
) -> list[dict]:

    per_source_cap = max(1, candidates_max // 2)

    seen: dict[str, dict] = {}

    ytmusic_count = 0
    for c in ytmusic_search(query, candidates_per_source):
        if ytmusic_count >= per_source_cap:
            break

        if c["id"] not in seen:
            seen[c["id"]] = c
            ytmusic_count += 1

    flat_count = 0
    for entry in search_flat(query, candidates_per_source, cookies_file=cookies_file, js_runtime=js_runtime, proxy=proxy):
        if flat_count >= per_source_cap:
            break

        video_id = entry.get("id")

        if not video_id or video_id in seen:
            continue

        seen[video_id] = {
            "id": video_id,
            "title": entry.get("title") or "",
            "channel": entry.get("channel") or entry.get("uploader") or "",
            "duration": entry.get("duration"),
            "url": entry.get("url") or f"https://www.youtube.com/watch?v={video_id}",
            "source": "yt-dlp",
        }
        
        flat_count += 1

    return list(seen.values())

def _intento_notube(url: str, ok: bool, msg: str, duracion_s: float) -> dict:

    return {
        "proveedor": "notube",
        "player_client": None,
        "candidato_url": url,
        "candidato_video_id": None,
        "candidato_titulo": None,
        "candidato_canal": None,
        "candidato_score": None,
        "candidato_fuente": None,
        "etapa_error": None if ok else "descarga",
        "tipo_error": None if ok else "error",
        "resultado": "correcta" if ok else "fallida",
        "mensaje": msg,
        "duracion_s": duracion_s,
    }

def download_youtube_url(
    url: str,
    *,
    out_dir: Path,
    cookies_file: Path,
    cache_path: Path,
    js_runtime: str,
    fmt: str,
    bitrate: str,
    proxy: str | None = None,
):

    info, _ = probe_info(
        url,
        cookies_file=cookies_file,
        js_runtime=js_runtime,
        proxy=proxy
    )
    title = (info or {}).get("title")
    duration = (info or {}).get("duration") or 0

    ok, avail, msg, intentos = attempt_download(
        url, 
        cookies_file=cookies_file,
        out_dir=out_dir,
        cache_path=cache_path,
        fmt=fmt,
        bitrate=bitrate,
        js_runtime=js_runtime,
        proxy=proxy
    )

    if ok:
        return True, msg, intentos, title

    if avail in _PRIVATE:
        return False, f"[SKIP] {avail} — no se procesa", intentos, title

    if avail == "client_blocked":
        return False, f"[SKIP] client_blocked (content-ID, cadena de clientes agotada) — {msg}", intentos, title

    if avail == "stalled":
        return False, f"[SKIP] stalled — {msg}", intentos, title

    if duration > LONG_AUDIO_S:
        return False, f"[FATAL] {msg} (audio de {duration / 60:.0f} min: se omite notube)", intentos, title

    if avail == "geo_restricted":
        logger.info("[GEO] -> notube")

        progress.check_cancelled()
        t0 = time.monotonic()
        ok2, msg2 = download_from_notube(url, out_dir)
        intentos.append(
            _intento_notube(url, ok2, msg2, time.monotonic() - t0)
        )
        progress.check_cancelled()

        return ok2, msg2, intentos, title

    logger.info("[yt-dlp fallo] -> notube fallback")

    progress.check_cancelled()
    t0 = time.monotonic()
    ok2, msg2 = download_from_notube(url, out_dir)
    intentos.append(
        _intento_notube(url, ok2, msg2, time.monotonic() - t0)
    )
    progress.check_cancelled()

    if not ok2:
        msg2 = f"[FATAL] {msg}"

    return ok2, msg2, intentos, title

def download_spotify_url(
    url: str,
    *,
    out_dir: Path,
    cookies_file: Path,
    cache_path: Path,
    js_runtime: str,
    fmt: str,
    bitrate: str,
    spotify_creds: SpotifyCredentials,
    proxy: str | None = None,
    candidates_per_source: int = 10,
    candidates_max: int = 15,
    top_n: int = 5,
    known_meta: dict | None = None,
    candidates: list[dict] | None = None,
):
    """Descarga el primer candidato que funcione. Si `candidates` viene (los de la vista previa, ya
    puntuados), no se vuelve a buscar: se baja exactamente lo que el usuario revisó."""

    meta = known_meta or get_track_metadata(url, client_id=spotify_creds.client_id, client_secret=spotify_creds.client_secret)
    artist = meta["artists"][0] if meta["artists"] else ""
    title = meta["name"]

    query = f"{artist} {title}".strip()

    if candidates:
        top = candidates
    else:
        found = gather_candidates(
            query,
            cookies_file=cookies_file,
            js_runtime=js_runtime,
            proxy=proxy,
            candidates_per_source=candidates_per_source,
            candidates_max=candidates_max
        )

        if not found:
            return "failed", f"[FATAL] Sin candidatos de YouTube para «{query}»", [], title, artist

        top = rank(found, artist, title, meta["duration_s"], top_n=top_n)

    intentos = []
    last_msg = ""

    for c in top:
        progress.check_cancelled()
        score = c.get("score") or 0

        ok, avail, msg, sub_intentos = attempt_download(
            c["url"], 
            out_dir=out_dir, 
            cookies_file=cookies_file, 
            cache_path=cache_path,
            js_runtime=js_runtime, 
            fmt=fmt, 
            bitrate=bitrate, 
            proxy=proxy
        )

        for it in sub_intentos:
            it["candidato_titulo"] = c.get("title")
            it["candidato_canal"] = c.get("channel")
            it["candidato_score"] = c.get("score")
            it["candidato_fuente"] = c.get("source")

        intentos.extend(sub_intentos)

        if ok:
            if score >= MIN_SCORE:
                return "successful", f"[{c.get('source', '?')}] {c['title']}", intentos, title, artist

            return "incorrect", f"[{c.get('source', '?')}] {c['title']} (score {score:.1f} < {MIN_SCORE})", intentos, title, artist

        if avail == "geo_restricted" and (c.get("duration") or 0) <= LONG_AUDIO_S:
            t0 = time.monotonic()
            ok2, msg2 = download_from_notube(c["url"], out_dir)
            progress.check_cancelled()

            it = _intento_notube(c["url"], ok2, msg2, time.monotonic() - t0)
            it["candidato_titulo"] = c.get("title")
            it["candidato_canal"] = c.get("channel")
            it["candidato_score"] = c.get("score")
            it["candidato_fuente"] = c.get("source")

            intentos.append(it)

            if ok2:
                if score >= MIN_SCORE:
                    return "successful", f"[notube] {c['title']}", intentos, title, artist

                return "incorrect", f"[notube] {c['title']} (score {score:.1f} < {MIN_SCORE})", intentos, title, artist

        last_msg = msg

    return "failed", f"[FATAL] Ningun candidato del top {len(top)} funciono — ultimo error: {last_msg}", intentos, title, artist

def download(
    url: str,
    *,
    out_dir: Path,
    cookies_file: Path,
    cache_path: Path,
    js_runtime: str | None = None,
    fmt: str = "mp3",
    bitrate: str = "192k",
    spotify_creds: SpotifyCredentials | None = None,
    proxy: str | None = None,
    candidates_per_source: int = 10,
    candidates_max: int = 15,
    top_n: int = 5,
    known_meta: dict | None = None,
    candidates: list[dict] | None = None,
) -> dict:

    js_runtime = js_runtime or default_js_runtime()

    _ESTADO_LABEL = {
        "successful": "completado",
        "incorrect": "incorrecto (posible match erroneo)",
        "failed": "fallido"
    }

    def _log(source: str, display_title: str, estado: str, intentos: list[dict]) -> None:
        n = len(intentos)
        veces = "intento" if n == 1 else "intentos"

        print(f"  [{source}]  {display_title}  ->  {_ESTADO_LABEL[estado]} ({n} {veces})")

    with _private_cookies_copy(cookies_file) as private_cookies:

        if is_spotify(url):

            if spotify_creds is None:
                return {
                    "url": url, 
                    "ok": False, 
                    "estado": "failed", 
                    "mensaje": "[FATAL] se requieren spotify_creds para URLs de Spotify", 
                    "intentos": [], 
                    "titulo": None, 
                    "artista": None
                }

            try:
                estado, msg, intentos, titulo, artista = download_spotify_url(
                    url, 
                    out_dir=out_dir, 
                    cookies_file=private_cookies, 
                    cache_path=cache_path,
                    js_runtime=js_runtime, 
                    fmt=fmt, 
                    bitrate=bitrate, 
                    spotify_creds=spotify_creds,
                    proxy=proxy, 
                    candidates_per_source=candidates_per_source,
                    candidates_max=candidates_max, 
                    top_n=top_n, 
                    known_meta=known_meta,
                    candidates=candidates,
                )

            except Exception as e:
                estado, msg, intentos, titulo, artista = "failed", f"[FATAL] {e}", [], None, None

            display_title = f"{artista} - {titulo}" if artista and titulo else (titulo or url)

            _log("SPOTIFY", display_title, estado, intentos)

            return {
                "url": url, 
                "ok": estado != "failed", 
                "estado": estado, 
                "mensaje": msg, 
                "intentos": intentos, 
                "titulo": titulo, 
                "artista": artista
            }

        if is_youtube(url):
            try:
                ok, msg, intentos, titulo = download_youtube_url(
                    url, 
                    out_dir=out_dir, 
                    cookies_file=private_cookies, 
                    cache_path=cache_path,
                    js_runtime=js_runtime, 
                    fmt=fmt, 
                    bitrate=bitrate, 
                    proxy=proxy
                )

            except Exception as e:
                ok, msg, intentos, titulo = False, f"[FATAL] {e}", [], None

            estado = "successful" if ok else "failed"
            _log("YOUTUBE", titulo or url, estado, intentos)

            return {
                "url": url, 
                "ok": ok, 
                "estado": estado, 
                "mensaje": msg, 
                "intentos": intentos, 
                "titulo": titulo, 
                "artista": None
            }

        try:
            ok, _, msg, intentos = attempt_download(
                url, 
                out_dir=out_dir, 
                cookies_file=private_cookies, 
                cache_path=cache_path,
                js_runtime=js_runtime, 
                fmt=fmt, 
                bitrate=bitrate, 
                proxy=proxy
            )

        except Exception as e:
            ok, msg, intentos = False, f"[FATAL] {e}", []

        estado = "successful" if ok else "failed"
        _log("OTRO", url, estado, intentos)

        return {
            "url": url, 
            "ok": ok, 
            "estado": estado, 
            "mensaje": msg, 
            "intentos": intentos, 
            "titulo": None, 
            "artista": None
        }
    

def _youtube_thumbnail(video_id: str | None) -> str | None:
    """Miniatura de un video de YouTube a partir de su id (11 caracteres), sin pedir nada a YouTube."""
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg" if video_id and len(video_id) == 11 else None


def _public_candidate(c: dict) -> dict:
    return {
        "thumbnail": c.get("thumbnail") or _youtube_thumbnail(c.get("id")),
        "id": c.get("id"),
        "title": c.get("title") or "",
        "channel": c.get("channel") or "",
        "duration": c.get("duration"),
        "url": c.get("url"),
        "source": c.get("source"),
        "score": round(c["score"], 1) if c.get("score") is not None else None,
    }


def resolve(
    url: str,
    *,
    cookies_file: Path,
    js_runtime: str | None = None,
    spotify_creds: SpotifyCredentials | None = None,
    proxy: str | None = None,
    candidates_per_source: int = 10,
    candidates_max: int = 15,
    top_n: int = 5,
    known_meta: dict | None = None,
) -> dict:
    """Vista previa de una canción: qué es y qué se bajaría, sin descargar audio.

    Spotify → metadatos + los `top_n` candidatos de YouTube puntuados (`candidatos[0]` es el mejor).
    YouTube → título, canal, duración, disponibilidad y capítulos (el tracklist).
    """

    js_runtime = js_runtime or default_js_runtime()

    with _private_cookies_copy(cookies_file) as private_cookies:

        if is_spotify(url):
            if spotify_creds is None:
                raise RuntimeError("se requieren spotify_creds para URLs de Spotify")

            meta = known_meta or get_track_metadata(url, client_id=spotify_creds.client_id, client_secret=spotify_creds.client_secret)
            artist = meta["artists"][0] if meta["artists"] else ""
            title = meta["name"]
            found = gather_candidates(
                f"{artist} {title}".strip(),
                cookies_file=private_cookies,
                js_runtime=js_runtime,
                proxy=proxy,
                candidates_per_source=candidates_per_source,
                candidates_max=candidates_max,
            )
            top = [_public_candidate(c) for c in rank(found, artist, title, meta["duration_s"], top_n=top_n)] if found else []

            return {
                "origen": "spotify",
                "title": title,
                "artist": artist,
                "duration_s": meta["duration_s"],
                "candidatos": top,
                "disponible": bool(top),
                "motivo": None if top else "no_candidates",
                "chapters": [],
            }

        info, motivo = probe_info(url, cookies_file=private_cookies, js_runtime=js_runtime, proxy=proxy)

        if info is None:
            return {"origen": "youtube", "title": None, "artist": None, "duration_s": None, "candidatos": [],
                    "disponible": False, "motivo": motivo, "chapters": []}

        return {
            "origen": "youtube",
            "title": info["title"],
            "artist": info["channel"],
            "duration_s": info["duration"],
            "candidatos": [],
            "disponible": True,
            "motivo": None,
            "chapters": info["chapters"],
            "thumbnail": info.get("thumbnail") or _youtube_thumbnail(info.get("id")),
        }


def score_url_for(
    url: str,
    *,
    artist: str,
    title: str,
    duration_s: float | None,
    cookies_file: Path,
    js_runtime: str | None = None,
    proxy: str | None = None,
) -> dict | None:
    """Puntúa una URL de YouTube que pegó el usuario contra los metadatos de la canción («Cambiar URL»)."""

    js_runtime = js_runtime or default_js_runtime()

    with _private_cookies_copy(cookies_file) as private_cookies:
        info, _ = probe_info(url, cookies_file=private_cookies, js_runtime=js_runtime, proxy=proxy)

    if info is None:
        return None

    candidate = {**info, "source": "usuario"}
    candidate["score"] = score_candidate(candidate, artist or "", title or "", duration_s or (info.get("duration") or 0))

    return _public_candidate(candidate)


# --- Temas de un tracklist como canciones completas ---------------------------------------------------------------

_TEMA_PREFIX = re.compile(r"^\s*(?:\(?\d{1,3}[.):\-]\)?\s+|\d{1,2}:\d{2}(?::\d{2})?\s*[-–—]?\s*)")
_TEMA_SEP = re.compile(r"\s+[-–—]\s+")


def split_tema_title(text: str) -> tuple[str, str]:
    """«01. Artista - Canción» → ("Artista", "Canción"). Sin separador, el artista queda vacío."""
    clean = _TEMA_PREFIX.sub("", text or "").strip()
    parts = _TEMA_SEP.split(clean, maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else ("", clean)


_NOISE = re.compile(r"\s*[\(\[][^\)\]]*(?:remaster|edit|version|versión|mix|live|mono|stereo|feat|ft\.)[^\)\]]*[\)\]]|\s+-\s+.*$|\s+(?:feat|ft)\.?\s.*$", re.I)


def query_variants(artist: str, title: str) -> list[str]:
    """Otras frases para buscar la misma canción, para «Volver a buscar»: además de «artista título», solo el título
    y el título sin agregados («(Remastered 2011)», «- Radio Edit», «feat. …»)."""
    clean = _NOISE.sub("", title or "").strip() or title
    queries = [f"{artist} {title}", f"{artist} {clean}", title, clean, f"{clean} {artist} audio"]
    return list(dict.fromkeys(q.strip() for q in queries if q and q.strip()))


def search_song(
    artist: str,
    title: str,
    duration_s: float | None,
    *,
    cookies_file: Path,
    js_runtime: str | None = None,
    proxy: str | None = None,
    top_n: int = 5,
    query: str | None = None,
) -> list[dict]:
    """Busca una canción en YouTube (como con Spotify) y devuelve los `top_n` candidatos puntuados. `query` cambia la
    frase de búsqueda; el score se sigue calculando contra `artist` y `title`."""
    js_runtime = js_runtime or default_js_runtime()
    with _private_cookies_copy(cookies_file) as private_cookies:
        found = gather_candidates(query or f"{artist} {title}".strip(), cookies_file=private_cookies, js_runtime=js_runtime, proxy=proxy)
    return [_public_candidate(c) for c in rank(found, artist, title, duration_s or 0, top_n=top_n)] if found else []


def download_song(
    *,
    artist: str,
    title: str,
    duration_s: float | None,
    candidates: list[dict],
    out_dir: Path,
    cookies_file: Path,
    cache_path: Path,
    js_runtime: str | None = None,
    fmt: str = "mp3",
    bitrate: str = "192k",
    proxy: str | None = None,
) -> dict:
    """Baja el primer candidato que funcione de una canción ya buscada (los de la vista previa)."""
    js_runtime = js_runtime or default_js_runtime()
    meta = {"name": title, "artists": [artist] if artist else [], "duration_s": duration_s or 0}
    with _private_cookies_copy(cookies_file) as private_cookies:
        try:
            estado, msg, intentos, _, _ = download_spotify_url(
                "", out_dir=out_dir, cookies_file=private_cookies, cache_path=cache_path, js_runtime=js_runtime,
                fmt=fmt, bitrate=bitrate, spotify_creds=None, proxy=proxy, known_meta=meta, candidates=candidates,
            )
        except progress.TrackCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            estado, msg, intentos = "failed", f"[FATAL] {e}", []
    # El video que se bajó de verdad (puede no ser el primer candidato, si ese falló).
    ok = next((it for it in reversed(intentos) if it.get("resultado") == "correcta" and it.get("candidato_url")), None)
    return {"estado": estado, "mensaje": msg, "intentos": intentos, "url": ok["candidato_url"] if ok else None}
