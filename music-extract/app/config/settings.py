import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent
MUSIC_EXTRACT_DIR = APP_DIR.parent

DATA_DIR = MUSIC_EXTRACT_DIR / "data"
# MUSIC_DOWNLOADED_ROOT y MUSIC_EXTRACT_DB_PATH se pueden cambiar por variable de entorno
# (los tests y las pruebas locales usan carpetas temporales para no tocar la base real).
MUSIC_DOWNLOADED_ROOT = Path(os.getenv("MUSIC_DOWNLOADED_ROOT", DATA_DIR / "music-downloaded"))
MUSIC_NORMALIZED_ROOT = DATA_DIR / "music-normalized"
COOKIES_FILE = DATA_DIR / "cookies" / "cookies-files" / "cookies.txt"
BLOCKED_CACHE_PATH = DATA_DIR / "blocked_cache.json"

DB_PATH = Path(os.getenv("MUSIC_EXTRACT_DB_PATH", APP_DIR / "db" / "db_downloads.sqlite"))
ALEMBIC_INI_PATH = APP_DIR / "db" / "alembic.ini"
ALEMBIC_SCRIPT_LOCATION = APP_DIR / "alembic"

THREADS = 3
TOP_N = 5
CLIENT_CHAIN_LEN = 5
MAX_RETRIES_PER_TRACK = 3
# Días que vale una búsqueda guardada de la vista previa (tabla busquedas). Después se vuelve a buscar.
RESOLVE_CACHE_DAYS = int(os.getenv("RESOLVE_CACHE_DAYS", "7"))
ETA_WINDOW = 20

# Sin límite de duración: una descarga se corta solo si se estanca. yt-dlp corta la conexión tras
# `socket_timeout` (15 s) sin datos; ffmpeg se corta si pasa STALL_TIMEOUT_S sin avanzar.
STALL_TIMEOUT_S = 60
# Por encima de esta duración se salta notube (su espera de conversión es de 90 s).
LONG_AUDIO_S = 900

# Sesión de la web: clave para firmar la cookie. Si no se define, se genera una y se guarda en data/.secret_key.
SECRET_KEY = os.getenv("OWLO_SECRET_KEY")
SECRET_KEY_FILE = DATA_DIR / ".secret_key"
SESSION_COOKIE = "owlo_session"
SESSION_DAYS = 14
# Almacenamiento por usuario (bytes) y carpeta de las descargas sin cuenta, que se borran a las 24 h.
USER_QUOTA_BYTES = int(os.getenv("USER_QUOTA_BYTES", 1024 ** 3))
ANON_DIR_NAME = "_sin-cuenta"
ANON_RETENTION_HOURS = 24

# Orígenes del front permitidos por CORS, separados por coma.
WEB_ORIGINS = [o.strip() for o in os.getenv("WEB_ORIGINS", "http://127.0.0.1:5174,http://localhost:5174").split(",") if o.strip()]

FORMAT = "mp3"
BITRATE = "192k"

ENV_PATH = MUSIC_EXTRACT_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)
# El .env de la raíz del repo también vale (ahí están las llaves de R2); el de music-extract manda si se repite.
load_dotenv(dotenv_path=MUSIC_EXTRACT_DIR.parent / ".env")

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
PROXY = os.getenv("PROXY")

# Biblioteca en la nube (Cloudflare R2, compatible con S3). Sin endpoint, bucket y llaves (o token) no se sube nada.
R2_ENDPOINT = os.getenv("R2_ENDPOINT")
R2_BUCKET = os.getenv("R2_BUCKET")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
# Alternativa a las dos llaves: solo el token de R2 (de él se sacan las llaves al arrancar).
R2_TOKEN_VALUE = os.getenv("R2_TOKEN_VALUE")

# Retorno de Spotify. Por defecto, un servidor local en el puerto 8888 (solo funciona desde la PC). Con la web
# publicada, una ruta del motor detrás de la web, p. ej. https://<web>/api/auth/spotify/callback (se registra
# igual en el panel de Spotify Developer).
SPOTIFY_OAUTH_REDIRECT_URI = os.getenv("SPOTIFY_OAUTH_REDIRECT_URI") or "http://127.0.0.1:8888/callback"
SPOTIFY_OAUTH_LOCAL = SPOTIFY_OAUTH_REDIRECT_URI.startswith(("http://127.0.0.1", "http://localhost"))
SPOTIFY_OAUTH_CALLBACK_PORT = 8888
SPOTIFY_OAUTH_SCOPES = "playlist-read-private playlist-read-collaborative"

PLAYLISTS_FETCHED_DIR = MUSIC_EXTRACT_DIR.parent / "spotify-api" / "playlists-fetched"

CSV_COLUMNS = [
    "name", "artists", "album", "release_date", "duration_ms", "duration",
    "track_number", "disc_number", "isrc", "spotify_id", "spotify_url", "popularity", "explicit",
]
