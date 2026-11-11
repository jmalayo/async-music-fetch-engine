"""Pruebas del flujo web sin red: `resolve` y `download` se reemplazan por versiones falsas.

La base y la carpeta de descargas son temporales (variables de entorno), así que nunca se toca
`app/db/db_downloads.sqlite` ni `data/music-downloaded/`.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="music-extract-tests-"))
os.environ["MUSIC_EXTRACT_DB_PATH"] = str(_TMP / "test.sqlite")
os.environ["MUSIC_DOWNLOADED_ROOT"] = str(_TMP / "music-downloaded")
os.environ.setdefault("SPOTIFY_CLIENT_ID", "test")
os.environ.setdefault("SPOTIFY_CLIENT_SECRET", "test")
os.environ["OWLO_SECRET_KEY"] = "clave-de-prueba"

APP_DIR = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from infrastructure.external import downloader_service  # noqa: E402


TEST_USER = ("prueba", "clave-123")


@pytest.fixture(scope="session")
def client():
    """Cliente con la sesión de un usuario de prueba iniciada (la mayoría de las pruebas usa la web con cuenta)."""
    from core import auth
    from infrastructure.persistence.database import SessionLocal

    with TestClient(main.app) as c:
        with SessionLocal() as db:
            auth.create_user(db, *TEST_USER)
        r = c.post("/auth/login", json={"username": TEST_USER[0], "password": TEST_USER[1]})
        assert r.status_code == 200, r.text
        yield c


@pytest.fixture
def anon_client(client):
    """Cliente sin sesión: la web sin cuenta."""
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def downloads_root():
    return Path(os.environ["MUSIC_DOWNLOADED_ROOT"])


def wait_until(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"No se cumplió la condición en {timeout} s (último valor: {last!r})")


def make_mp3(path: Path, seconds: float = 3.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-codec:a", "libmp3lame", "-b:a", "128k", str(path)],
        check=True,
    )
    return path


@pytest.fixture
def fake_engine(monkeypatch):
    """Motor falso configurable por URL. `engine.behavior[url]` decide qué pasa al descargar."""

    class Engine:
        def __init__(self):
            self.behavior = {}
            self.resolved = {}
            self.calls = []

        def resolve(self, url, **kwargs):
            if url in self.resolved:
                return self.resolved[url]
            if "open.spotify.com" in url:
                meta = kwargs.get("known_meta") or {"name": "Canción", "artists": ["Artista"], "duration_s": 200}
                return {"origen": "spotify", "title": meta["name"], "artist": (meta["artists"] or [""])[0],
                        "duration_s": meta["duration_s"], "disponible": True, "motivo": None, "chapters": [],
                        "candidatos": [{"id": "x", "title": f"{meta['name']} (Official Audio)", "channel": "Topic",
                                        "duration": meta["duration_s"], "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
                                        "source": "ytmusic", "score": 95.0}]}
            return {"origen": "youtube", "title": "Video", "artist": "Canal", "duration_s": 180, "disponible": True,
                    "motivo": None, "candidatos": [], "chapters": []}

        def download(self, url, *, out_dir, **kwargs):
            from musictools.download import progress

            self.calls.append((url, kwargs.get("candidates")))
            action = self.behavior.get(url, "ok")
            if callable(action):
                action = action(len([c for c in self.calls if c[0] == url]))

            if action == "slow":
                reporter = progress.current()
                for i in range(400):
                    progress.check_cancelled()
                    reporter.update(fase="descargando", porcentaje=float(min(i, 99)), bytes=i, bytes_total=400, velocidad_bps=10, eta_s=1)
                    time.sleep(0.02)
                action = "ok"

            if action == "ok":
                make_mp3(out_dir / "Canal - Video.mp3", seconds=self.behavior.get(f"{url}#seconds", 1.0))
                return {"url": url, "ok": True, "estado": "successful", "mensaje": "ok", "intentos": [], "titulo": None, "artista": None}

            if action == "needs_auth":
                return {"url": url, "ok": False, "estado": "failed", "mensaje": "[SKIP] needs_auth — no se procesa",
                        "intentos": [{"proveedor": "yt-dlp", "resultado": "fallida", "tipo_error": "needs_auth", "mensaje": "Sign in"}],
                        "titulo": None, "artista": None}

            return {"url": url, "ok": False, "estado": "failed", "mensaje": "[FATAL] Ningun candidato del top 5 funciono",
                    "intentos": [{"proveedor": "yt-dlp", "resultado": "fallida", "tipo_error": "error", "mensaje": "boom"}],
                    "titulo": None, "artista": None}

        # Temas de un tracklist: `engine.temas[titulo]` = "ok" (por defecto), "sin_video" (no se encuentra) o "falla".
        def search_song(self, artist, title, duration_s, **kwargs):
            self.searches.append((artist, title))
            if self.temas.get(title) == "sin_video":
                return []
            slug = "".join(ch for ch in title if ch.isalnum())[:8].ljust(8, "x")
            first = {"id": f"tem{slug}", "title": f"{artist} - {title}", "channel": "Topic", "duration": duration_s,
                     "url": f"https://www.youtube.com/watch?v=tem{slug}", "source": "ytmusic", "score": 92.0,
                     "thumbnail": f"https://i.ytimg.com/vi/tem{slug}/mqdefault.jpg"}
            if self.temas.get(title) == "segundo":
                return [first, {**first, "id": f"seg{slug}", "url": f"https://www.youtube.com/watch?v=seg{slug}", "score": 80.0}]
            return [first]

        def download_song(self, *, title, out_dir, candidates, **kwargs):
            self.song_downloads.append(title)
            if self.temas.get(title) == "falla":
                return {"estado": "failed", "mensaje": "boom", "intentos": []}
            make_mp3(out_dir / f"{title}.mp3", seconds=0.5)
            # El primero «falla» y se baja el segundo, si hay: el enlace debe ser el que se bajó.
            used = candidates[-1]["url"]
            return {"estado": "successful", "mensaje": "ok", "intentos": [], "url": used}

    engine = Engine()
    engine.temas, engine.searches, engine.song_downloads = {}, [], []
    monkeypatch.setattr(downloader_service, "resolve", engine.resolve)
    monkeypatch.setattr(downloader_service, "download", engine.download)
    monkeypatch.setattr(downloader_service, "search_song", engine.search_song)
    monkeypatch.setattr(downloader_service, "download_song", engine.download_song)
    monkeypatch.setattr(
        downloader_service, "score_url_for",
        lambda url, **kw: {"id": "u", "title": "Elegido", "channel": "C", "duration": 200, "url": url, "source": "usuario", "score": 88.0},
    )
    return engine
