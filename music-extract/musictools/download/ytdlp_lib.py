import re
import time
import yt_dlp

from pathlib import Path

from . import blocked_cache

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

def _common_opts(cookies_file: Path, js_runtime: str, proxy: str | None = None, client: str | None = None) -> dict:

    opts = {
        "quiet": True,
        "no_warnings": True,
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

    opts = _common_opts(cookies_file, js_runtime, proxy, client)

    opts.update({
        "format": "ba/b",
        "outtmpl": str(out_dir / "%(uploader)s - %(title)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": fmt,
            "preferredquality": bitrate.rstrip("k"),
        }],
    })

    try:

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        title = (info or {}).get("title", url)

        return True, "public", f"Descargado: {title}"

    except yt_dlp.utils.DownloadError as e:

        msg = str(e).strip()

        return False, classify_error(msg), msg[-300:]

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
