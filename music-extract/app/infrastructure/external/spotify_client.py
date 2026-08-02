import base64
import csv
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from sqlalchemy.orm import Session

from config.settings import CSV_COLUMNS, PLAYLISTS_FETCHED_DIR, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
from core import job_service
from infrastructure.external import spotify_oauth
from infrastructure.persistence.models import SpotifyToken

_PLAYLIST_ID_RE = re.compile(r"(?:open\.spotify\.com/playlist/|spotify:playlist:)([a-zA-Z0-9]+)")

def extract_playlist_id(text: str) -> str | None:
    m = _PLAYLIST_ID_RE.search(text)
    return m.group(1) if m else None

def _format_duration(ms: int) -> str:
    total_s = int((ms or 0) / 1000)
    return f"{total_s // 60:02d}:{total_s % 60:02d}"

def get_app_token() -> str:
    auth = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()

    r = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    r.raise_for_status()

    return r.json()["access_token"]

def save_user_token(db: Session, token_data: dict) -> None:
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])).isoformat()

    row = db.query(SpotifyToken).first()

    if row is None:
        row = SpotifyToken(refresh_token=token_data.get("refresh_token", ""))
        db.add(row)

    row.access_token = token_data["access_token"]

    if token_data.get("refresh_token"):
        row.refresh_token = token_data["refresh_token"]

    row.expires_at = expires_at
    row.scope = token_data.get("scope")

    db.commit()

def get_user_token(db: Session) -> str | None:
    row = db.query(SpotifyToken).first()

    if row is None:
        return None

    expires_at = datetime.fromisoformat(row.expires_at)

    if expires_at > datetime.now(timezone.utc) + timedelta(seconds=30):
        return row.access_token

    data = spotify_oauth.refresh_access_token(
        row.refresh_token, client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET,
    )
    save_user_token(db, data)

    return data["access_token"]

def get_all_user_playlists(token: str) -> list[dict]:
    playlists = []
    url = "https://api.spotify.com/v1/me/playlists"
    params = {"limit": 50}

    while url:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()

        playlists.extend(item for item in data.get("items", []) if item)

        url = data.get("next")
        params = None

    return playlists

def find_user_playlist(playlist_name: str, token: str) -> dict | None:
    me = requests.get("https://api.spotify.com/v1/me", headers={"Authorization": f"Bearer {token}"}, timeout=15)
    me.raise_for_status()
    user_id = me.json()["id"]

    target = job_service.normalize_name(playlist_name)
    all_playlists = get_all_user_playlists(token)

    def is_readable(p: dict) -> bool:
        return (p.get("owner") or {}).get("id") == user_id or p.get("collaborative") is True

    candidates = [p for p in all_playlists if is_readable(p)]

    for p in candidates:
        if job_service.normalize_name(p.get("name", "")) == target:
            return {"id": p["id"], "name": p["name"]}

    for p in candidates:
        normalized_name = job_service.normalize_name(p.get("name", ""))
        if target in normalized_name or normalized_name in target:
            return {"id": p["id"], "name": p["name"]}

    return None

def search_playlist(playlist_name: str, token: str) -> dict | None:
    target = job_service.normalize_name(playlist_name)

    r = requests.get(
        "https://api.spotify.com/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": playlist_name, "type": "playlist", "limit": 10},
        timeout=15,
    )
    r.raise_for_status()

    items = (r.json().get("playlists") or {}).get("items") or []

    for item in items:
        if not item:
            continue
        normalized_name = job_service.normalize_name(item.get("name", ""))
        if normalized_name == target:
            return {"id": item["id"], "name": item["name"]}

    for item in items:
        if not item:
            continue
        normalized_name = job_service.normalize_name(item.get("name", ""))
        if target in normalized_name or normalized_name in target:
            return {"id": item["id"], "name": item["name"]}

    return None

def get_playlist_by_id(playlist_id: str, token: str) -> dict | None:
    r = requests.get(
        f"https://api.spotify.com/v1/playlists/{playlist_id}",
        headers={"Authorization": f"Bearer {token}"},
        params={"fields": "id,name"},
        timeout=15,
    )

    if r.status_code == 404:
        return None

    r.raise_for_status()

    data = r.json()
    return {"id": data["id"], "name": data["name"]}

class InsufficientSpotifyAuth(Exception):
    pass

def get_playlist_tracks(playlist_id: str, token: str) -> list[dict]:
    tracks = []
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/items"
    params = {"limit": 50}

    while url:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=15)

        if r.status_code == 403:
            raise InsufficientSpotifyAuth(
                "Spotify devolvio 403 al pedir las canciones de esta playlist. "
                "Esto pasa con playlists editoriales/algoritmicas de Spotify y con playlists de terceros "
                "de las que no sos dueno ni colaborador, incluso autenticado con OAuth de usuario."
            )

        r.raise_for_status()
        data = r.json()

        for item in data.get("items", []):
            t = item.get("item")

            if not t or not t.get("id"):
                continue

            tracks.append({
                "name": t.get("name", ""),
                "artists": "; ".join(a.get("name", "") for a in (t.get("artists") or [])),
                "album": (t.get("album") or {}).get("name", ""),
                "release_date": (t.get("album") or {}).get("release_date", ""),
                "duration_ms": t.get("duration_ms", 0),
                "duration": _format_duration(t.get("duration_ms", 0)),
                "track_number": t.get("track_number", ""),
                "disc_number": t.get("disc_number", ""),
                "isrc": (t.get("external_ids") or {}).get("isrc", ""),
                "spotify_id": t.get("id", ""),
                "spotify_url": (t.get("external_urls") or {}).get("spotify", ""),
                "popularity": t.get("popularity", ""),
                "explicit": "si" if t.get("explicit") else "no",
            })

        url = data.get("next")
        params = None

    return tracks

def write_csv(playlist_name: str, tracks: list[dict]) -> Path:
    PLAYLISTS_FETCHED_DIR.mkdir(parents=True, exist_ok=True)
    destination = PLAYLISTS_FETCHED_DIR / f"{job_service.slugify(playlist_name)}.csv"

    with destination.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, delimiter=";")
        writer.writeheader()
        writer.writerows(tracks)

    return destination

def search_and_get_tracks(playlist_name_or_url: str, db: Session) -> tuple[dict | None, list[dict]]:
    user_token = get_user_token(db)
    token = user_token or get_app_token()

    playlist_id = extract_playlist_id(playlist_name_or_url)

    if playlist_id:
        playlist = get_playlist_by_id(playlist_id, token)

    elif user_token:
        playlist = find_user_playlist(playlist_name_or_url, token)

    else:
        playlist = search_playlist(playlist_name_or_url, token)

    if not playlist:
        return None, []

    tracks = get_playlist_tracks(playlist["id"], token)

    return playlist, tracks
