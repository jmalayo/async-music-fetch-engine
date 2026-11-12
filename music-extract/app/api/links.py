"""Títulos de los enlaces de la lista, antes de buscar nada: el oEmbed público de YouTube, TikTok y Spotify.

Es una consulta liviana (sin yt-dlp ni búsqueda) y sin cuenta. Solo se consulta el oEmbed fijo de cada sitio con el
enlace como parámetro, así que no se puede usar para pedir otras direcciones. Los resultados se guardan en memoria.
"""

import re
import threading
import time

import requests
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/links", tags=["Web"])

_OEMBED = (
    (re.compile(r"^https?://(?:www\.|m\.|music\.)?(?:youtube\.com/watch\?|youtu\.be/)", re.I), "youtube", "https://www.youtube.com/oembed"),
    (re.compile(r"^https?://(?:(?:www|vm|vt)\.)?tiktok\.com/", re.I), "tiktok", "https://www.tiktok.com/oembed"),
    (re.compile(r"^https?://open\.spotify\.com/(?:intl-[\w-]+/)?(?:track|playlist|album)/", re.I), "spotify", "https://open.spotify.com/oembed"),
)
_TTL_S = 6 * 3600
_MAX_TITLE = 120
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def _clean_tiktok(text: str) -> str:
    """El «título» de TikTok es la descripción del video: sin los hashtags del final y cortada si es muy larga."""
    text = re.sub(r"(?:\s*#\S+)+\s*$", "", text or "").strip() or (text or "").strip()
    return text[: _MAX_TITLE - 1] + "…" if len(text) > _MAX_TITLE else text


def fetch_meta(url: str) -> dict:
    url = (url or "").strip()
    match = next(((origen, endpoint) for regex, origen, endpoint in _OEMBED if regex.match(url)), None)
    if match is None:
        raise HTTPException(422, "Solo enlaces de YouTube, TikTok o Spotify.")
    origen, endpoint = match

    with _lock:
        cached = _cache.get(url)
        if cached and time.monotonic() - cached[0] < _TTL_S:
            return cached[1]

    try:
        r = requests.get(endpoint, params={"url": url, "format": "json"}, timeout=6, headers={"User-Agent": "owlo"})
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError):
        # Privado, borrado, con restricción de edad o el sitio no respondió: la web se queda con el enlace.
        raise HTTPException(404, "No se pudo leer el título de este enlace.")

    title = (data.get("title") or "").strip()
    author = (data.get("author_name") or "").strip() or None
    if origen == "tiktok" and not author:
        # El oEmbed de TikTok dejó de mandar título y autor: el usuario sale del mismo enlace (tiktok.com/@usuario/…).
        handle = re.search(r"tiktok\.com/(@[\w.-]+)", url)
        author = handle.group(1) if handle else None
    meta = {
        "origen": origen,
        "titulo": (_clean_tiktok(title) if origen == "tiktok" else title[:_MAX_TITLE]) or None,
        # Spotify no manda el artista en su oEmbed.
        "autor": author,
        "miniatura": data.get("thumbnail_url"),
    }
    with _lock:
        _cache[url] = (time.monotonic(), meta)
    return meta


@router.get("/meta")
def link_meta(url: str):
    """Título, autor y miniatura de un enlace (para mostrarlo en la lista antes de buscar). Sin cuenta."""
    return fetch_meta(url)
