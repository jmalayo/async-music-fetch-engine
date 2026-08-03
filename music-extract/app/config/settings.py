import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent
MUSIC_EXTRACT_DIR = APP_DIR.parent

DATA_DIR = MUSIC_EXTRACT_DIR / "data"
MUSIC_DOWNLOADED_ROOT = DATA_DIR / "music-downloaded"
MUSIC_NORMALIZED_ROOT = DATA_DIR / "music-normalized"
COOKIES_FILE = DATA_DIR / "cookies" / "cookies-files" / "cookies.txt"
BLOCKED_CACHE_PATH = DATA_DIR / "blocked_cache.json"

DB_PATH = APP_DIR / "db" / "db_downloads.sqlite"
ALEMBIC_INI_PATH = APP_DIR / "db" / "alembic.ini"
ALEMBIC_SCRIPT_LOCATION = APP_DIR / "alembic"

THREADS = 3
TOP_N = 5
CLIENT_CHAIN_LEN = 5
MAX_RETRIES_PER_TRACK = 3
ETA_WINDOW = 20
TRACK_TIMEOUT_S = 180

FORMAT = "mp3"
BITRATE = "192k"

ENV_PATH = MUSIC_EXTRACT_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
PROXY = os.getenv("PROXY")

SPOTIFY_OAUTH_REDIRECT_URI = "http://127.0.0.1:8888/callback"
SPOTIFY_OAUTH_CALLBACK_PORT = 8888
SPOTIFY_OAUTH_SCOPES = "playlist-read-private playlist-read-collaborative"

PLAYLISTS_FETCHED_DIR = MUSIC_EXTRACT_DIR.parent / "spotify-api" / "playlists-fetched"

CSV_COLUMNS = [
    "name", "artists", "album", "release_date", "duration_ms", "duration",
    "track_number", "disc_number", "isrc", "spotify_id", "spotify_url", "popularity", "explicit",
]
