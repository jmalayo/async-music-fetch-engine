import base64
import re
import time

import requests

_TRACK_ID_RE = re.compile(r"(?:open\.spotify\.com/track/|spotify:track:)([a-zA-Z0-9]+)")

_token_cache: dict[str, tuple[str, float]] = {}


def extract_track_id(url: str) -> str | None:
    m = _TRACK_ID_RE.search(url)
    return m.group(1) if m else None


def _get_app_token(client_id: str, client_secret: str) -> str:
    cached = _token_cache.get(client_id)

    if cached and cached[1] > time.monotonic():
        return cached[0]

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    r = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()

    token = data["access_token"]
    _token_cache[client_id] = (token, time.monotonic() + data.get("expires_in", 3600) - 30)

    return token


def get_track_metadata(
    url: str,
    timeout: int = 15,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict:

    if not client_id or not client_secret:
        raise RuntimeError("get_track_metadata requiere client_id y client_secret")

    track_id = extract_track_id(url)

    if not track_id:
        raise RuntimeError(f"No se pudo extraer el track ID de Spotify de {url}")

    token = _get_app_token(client_id, client_secret)

    r = requests.get(
        f"https://api.spotify.com/v1/tracks/{track_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )

    if r.status_code == 404:
        raise RuntimeError(f"Spotify no encontro el track {track_id}")

    r.raise_for_status()
    song = r.json()

    artists = [a.get("name", "") for a in (song.get("artists") or [])]

    return {
        "name": song.get("name", ""),
        "artists": artists,
        "duration_s": (song.get("duration_ms") or 0) / 1000,
        "isrc": (song.get("external_ids") or {}).get("isrc", ""),
        "album_name": (song.get("album") or {}).get("name", ""),
    }
