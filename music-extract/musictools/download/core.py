import logging
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

from .candidate_scoring import rank
from .notube import download_from_notube
from .spotify_metadata import get_track_metadata
from .ytdlp_lib import download_with_rotation, probe_title, search_flat
from .ytmusic_search import search as ytmusic_search

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_PRIVATE = {"premium_only", "needs_auth"}

MIN_SCORE = 60

@dataclass
class SpotifyCredentials:
    client_id: str
    client_secret: str

def default_js_runtime() -> str:

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

    title = probe_title(
        url,
        cookies_file=cookies_file,
        js_runtime=js_runtime,
        proxy=proxy
    )

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

    if avail == "geo_restricted":
        logger.info("[GEO] -> notube")

        t0 = time.monotonic()
        ok2, msg2 = download_from_notube(url, out_dir)
        intentos.append(
            _intento_notube(url, ok2, msg2, time.monotonic() - t0)
        )

        return ok2, msg2, intentos, title

    logger.info("[yt-dlp fallo] -> notube fallback")

    t0 = time.monotonic()
    ok2, msg2 = download_from_notube(url, out_dir)
    intentos.append(
        _intento_notube(url, ok2, msg2, time.monotonic() - t0)
    )

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
):

    meta = known_meta or get_track_metadata(url, client_id=spotify_creds.client_id, client_secret=spotify_creds.client_secret)
    artist = meta["artists"][0] if meta["artists"] else ""
    title = meta["name"]

    query = f"{artist} {title}".strip()

    candidates = gather_candidates(
        query, 
        cookies_file=cookies_file, 
        js_runtime=js_runtime, 
        proxy=proxy,
        candidates_per_source=candidates_per_source,
        candidates_max=candidates_max
    )

    if not candidates:
        return False, f"[FATAL] Sin candidatos de YouTube para «{query}»", [], title, artist

    top = rank(candidates, artist, title, meta["duration_s"], top_n=top_n)

    intentos = []
    last_msg = ""

    for c in top:
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

        if avail == "geo_restricted":
            t0 = time.monotonic()
            ok2, msg2 = download_from_notube(c["url"], out_dir)

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
                    known_meta=known_meta
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
    