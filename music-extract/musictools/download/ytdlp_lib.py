import contextvars
import os
import re
import time
import yt_dlp

from contextlib import contextmanager
from pathlib import Path

from . import audio, blocked_cache, progress

_GEO_KEYWORDS = (
    "not available in your country",
    "not available in your region",
    "geo-restricted",
    "geo restricted",
    "blocked in your country",
)

_PREMIUM_KEYWORDS = ("youtube premium",)
_PRIVATE_KEYWORDS = ("private video", "sign in to confirm your age", "sign in")

_CLIENT_BLOCK_KEYWORDS = (
    "blocked it from display on this website or application",
    "watch on youtube",
)

_CLIENT_CHAIN = ["web", "web_safari", "mweb", "web_music", "tv"]

_VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/)([\w-]{11})")

def extract_video_id(url: str) -> str | None:

    m = _VIDEO_ID_RE.search(url)

    return m.group(1) if m else None

def is_client_blocked(message: str) -> bool:

    m = message.lower()

    return any(k in m for k in _CLIENT_BLOCK_KEYWORDS)

_ROTATABLE_KEYWORDS = _CLIENT_BLOCK_KEYWORDS + ("requested format is not available",)

def _is_rotatable(message: str) -> bool:

    m = message.lower()

    return any(k in m for k in _ROTATABLE_KEYWORDS)

def classify_error(message: str) -> str:

    m = message.lower()

    if is_client_blocked(m):
        return "client_blocked"

    if any(k in m for k in _GEO_KEYWORDS):
        return "geo_restricted"

    if any(k in m for k in _PREMIUM_KEYWORDS):
        return "premium_only"

    if any(k in m for k in _PRIVATE_KEYWORDS):
        return "needs_auth"

    return "error"

def _parse_js_runtime(js_runtime: str) -> dict:

    runtime, _, path = js_runtime.partition(":")

    return {runtime.lower(): {"path": path or None}}

# Cookies de un navegador para los videos que piden iniciar sesión (edad, privados): «firefox» o
# «firefox:/ruta/al/perfil». Solo se usan al reintentar una descarga que falló por needs_auth.
BROWSER_COOKIES = os.getenv("YTDLP_COOKIES_FROM_BROWSER") or None


# Las cookies del navegador son de la cuenta de Google del dueño: solo se prestan a descargas de usuarios con
# cuenta en owlo. Apagado por defecto; quien descarga lo enciende con `browser_cookies_allowed`.
_browser_allowed = contextvars.ContextVar("browser_cookies_allowed", default=False)


@contextmanager
def browser_cookies_allowed(allowed: bool):
    token = _browser_allowed.set(bool(allowed))
    try:
        yield
    finally:
        _browser_allowed.reset(token)


def browser_cookies() -> tuple | None:
    if not BROWSER_COOKIES:
        return None
    browser, _, profile = BROWSER_COOKIES.partition(":")
    return (browser.strip().lower(), profile.strip() or None, None, None)


def _with_browser_cookies(opts: dict) -> dict | None:
    """Las mismas opciones, pero con las cookies del navegador en lugar del archivo. None si no hay navegador."""
    spec = browser_cookies()
    if spec is None or not _browser_allowed.get() or opts.get("cookiesfrombrowser"):
        return None
    retry = {k: v for k, v in opts.items() if k != "cookiefile"}
    retry["cookiesfrombrowser"] = spec
    return retry


def _extract(opts: dict, url: str, *, download: bool):
    """extract_info; si YouTube pide iniciar sesión y hay navegador configurado, reintenta una vez con sus cookies."""
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl, ydl.extract_info(url, download=download)
    except yt_dlp.utils.DownloadError as e:
        retry = _with_browser_cookies(opts) if classify_error(str(e)) == "needs_auth" else None
        if retry is None:
            raise
        with yt_dlp.YoutubeDL(retry) as ydl:
            return ydl, ydl.extract_info(url, download=download)


def _common_opts(cookies_file: Path, js_runtime: str, proxy: str | None = None, client: str | None = None) -> dict:

    opts = {
        "quiet": True,
        "no_warnings": True,
        # El progreso va al reporter (progress_hooks), no a la consola.
        "noprogress": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "cookiefile": str(cookies_file),
        "js_runtimes": _parse_js_runtime(js_runtime),
    }

    if proxy:
        opts["proxy"] = proxy

    if client:

        opts["extractor_args"] = {"youtube": {"player_client": [client]}}

    return opts

def download(
    url: str,
    *,
    cookies_file: Path,
    out_dir: Path,
    fmt: str,
    bitrate: str,
    js_runtime: str,
    proxy: str | None = None,
    client: str | None = None,
) -> tuple[bool, str, str]:
    """Baja el mejor audio y lo convierte a `fmt` con ffmpeg propio, para informar el % de las dos fases.

    El progreso y la cancelación salen del reporter del hilo (`progress.current()`), si hay uno.
    """

    reporter = progress.current()
    opts = _common_opts(cookies_file, js_runtime, proxy, client)

    opts.update({
        "format": "ba/b",
        "outtmpl": str(out_dir / "%(uploader)s - %(title)s.%(ext)s"),
        "progress_hooks": [reporter.ytdlp_hook] if reporter else [],
    })

    try:

        ydl, info = _extract(opts, url, download=True)
        downloads = (info or {}).get("requested_downloads") or []
        src = Path(downloads[0]["filepath"]) if downloads and downloads[0].get("filepath") else Path(ydl.prepare_filename(info))

    except yt_dlp.utils.DownloadError as e:

        progress.check_cancelled()
        msg = str(e).strip()

        return False, classify_error(msg), msg[-300:]

    progress.check_cancelled()
    title = (info or {}).get("title", url)

    if src.suffix.lower() != f".{fmt}":
        try:
            audio.convert_to_mp3(src, src.with_suffix(f".{fmt}"), bitrate=bitrate, duration_s=(info or {}).get("duration"))
        except audio.StalledError as e:
            return False, "stalled", str(e)
        except audio.FfmpegError as e:
            return False, "error", f"Conversión fallida: {e}"
        finally:
            src.unlink(missing_ok=True)

    return True, "public", f"Descargado: {title}"

def _intento(video_id, client, ok, avail, msg, duracion_s, url):

    return {
        "proveedor": "yt-dlp",
        "player_client": client,
        "candidato_url": url,
        "candidato_video_id": video_id,
        "candidato_titulo": None,
        "candidato_canal": None,
        "candidato_score": None,
        "candidato_fuente": None,
        "etapa_error": None if ok else "descarga",
        "tipo_error": None if ok else avail,
        "resultado": "correcta" if ok else "fallida",
        "mensaje": msg,
        "duracion_s": duracion_s,
    }

def download_with_rotation(
    url: str,
    *,
    cookies_file: Path,
    out_dir: Path,
    fmt: str,
    bitrate: str,
    js_runtime: str,
    cache_path: Path,
    proxy: str | None = None,
) -> tuple[bool, str, str, list[dict]]:

    video_id = extract_video_id(url)
    intentos = []

    if video_id and video_id in blocked_cache.load(cache_path):

        msg = f"[cache] video_id {video_id} ya confirmado bloqueado (content-ID)"
        intentos.append(
            _intento(video_id, None, False, "client_blocked", msg, 0.0, url)
        )
        
        return False, "client_blocked", msg, intentos

    progress.check_cancelled()
    t0 = time.monotonic()

    ok, avail, msg = download(
        url, 
        cookies_file=cookies_file, 
        out_dir=out_dir, 
        fmt=fmt, 
        bitrate=bitrate,
        js_runtime=js_runtime, 
        proxy=proxy
    )

    intentos.append(
        _intento(video_id, None, ok, avail, msg, time.monotonic() - t0, url)
    )

    if ok or not _is_rotatable(msg):
        return ok, avail, msg, intentos

    chain = [
        c for c in _CLIENT_CHAIN 
            if c != "web_music" or "music.youtube.com" in url
    ]

    for client in chain:
        progress.check_cancelled()
        t0 = time.monotonic()
        ok, avail, msg = download(
            url,
            cookies_file=cookies_file,
            bitrate=bitrate,
            out_dir=out_dir,
            fmt=fmt,
            js_runtime=js_runtime,
            proxy=proxy,
            client=client,
        )

        intentos.append(_intento(video_id, client, ok, avail, msg, time.monotonic() - t0, url))

        if ok:
            return True, avail, msg, intentos

        if not _is_rotatable(msg):
            break

    if avail == "client_blocked" and video_id:
        blocked_cache.add(video_id, cache_path)

    return False, avail, msg, intentos

def search_flat(
    query: str,
    n: int,
    *,
    cookies_file: Path,
    js_runtime: str,
    proxy: str | None = None,
) -> list[dict]:

    opts = _common_opts(cookies_file, js_runtime, proxy)
    opts.update({"extract_flat": True, "skip_download": True})

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)

    except yt_dlp.utils.DownloadError:
        return []

    return info.get("entries") or [] if info else []

def probe_info(
    url: str,
    *,
    cookies_file: Path,
    js_runtime: str,
    proxy: str | None = None,
) -> tuple[dict | None, str | None]:
    """Metadatos de un video sin descargarlo: título, canal, duración y capítulos (el tracklist).

    Devuelve (info, None) o (None, motivo) con el motivo clasificado como en `classify_error`.
    """

    opts = _common_opts(cookies_file, js_runtime, proxy)
    opts["skip_download"] = True

    try:
        _, info = _extract(opts, url, download=False)

    except yt_dlp.utils.DownloadError as e:
        return None, classify_error(str(e))

    if not info:
        return None, "error"

    chapters = [
        {"inicio_s": round(c.get("start_time") or 0, 3), "fin_s": round(c["end_time"], 3) if c.get("end_time") is not None else None, "titulo": c.get("title") or ""}
        for c in (info.get("chapters") or [])
    ]

    return {
        "id": info.get("id"),
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration": info.get("duration"),
        "url": info.get("webpage_url") or url,
        "chapters": chapters,
        # Imagen del video (miniatura de YouTube, portada de TikTok) para la vista previa.
        "thumbnail": info.get("thumbnail"),
    }, None

def probe_title(
    url: str,
    *,
    cookies_file: Path,
    js_runtime: str,
    proxy: str | None = None,
) -> str | None:

    opts = _common_opts(cookies_file, js_runtime, proxy)
    opts["skip_download"] = True

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

    except yt_dlp.utils.DownloadError:
        return None

    return (info or {}).get("title")
