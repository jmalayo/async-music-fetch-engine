import tempfile
from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(ENV_FILE.parent.parent / ".env", ENV_FILE), extra="ignore")

    API_DIR: ClassVar[Path] = Path(__file__).resolve().parents[1]
    ROOT_DIR: ClassVar[Path] = API_DIR.parent

    COOKIES_FILE: Path = ROOT_DIR / "data" / "cookies" / "cookies.txt"
    YOUTUBE_BLOCKED_VIDEOS_PATH: Path = ROOT_DIR / "data" / "youtube_blocked_videos.json"
    MUSIC_DOWNLOADED_ROOT: Path = Path(tempfile.gettempdir()) / "owlo"

    DB_PATH: Path = Field(default=API_DIR / "db" / "db_downloads.sqlite", validation_alias="MUSIC_EXTRACT_DB_PATH")
    ALEMBIC_INI_PATH: ClassVar[Path] = API_DIR / "db" / "alembic.ini"
    ALEMBIC_SCRIPT_LOCATION: ClassVar[Path] = API_DIR / "alembic"

    PROCESSING_THREADS: int = 3
    SPOTIFY_YOUTUBE_MATCH_LIMIT: int = 5
    YOUTUBE_CLIENTS: list[str] = ["web", "web_safari", "mweb", "web_music", "tv"]
    MAX_RETRIES_PER_TRACK: int = 3

    FFMPEG_STALL_TIMEOUT_S: int = 60
    SOCKET_TIMEOUT_S: int = 15
    NOTUBE_MAX_AUDIO_DURATION_S: int = 900

    SECRET_KEY: str = Field(validation_alias="OWLO_SECRET_KEY")
    SESSION_COOKIE_NAME: str = "owlo_session"
    SESSION_MAX_AGE_DAYS: int = 14

    WEB_ORIGINS: list[str] = ["http://127.0.0.1:5174", "http://localhost:5174"]

    MP3_ENCODING_BITRATE: str = "192k"

    SPOTIFY_CLIENT_ID: str | None = None
    SPOTIFY_CLIENT_SECRET: str | None = None
    PROXY: str | None = None

    R2_ENDPOINT: str | None = None
    R2_BUCKET: str | None = None
    R2_ACCESS_KEY_ID: str | None = None
    R2_SECRET_ACCESS_KEY: str | None = None
    R2_TOKEN_VALUE: str | None = None

    YTDLP_COOKIES_FROM_BROWSER: str | None = None

    SPOTIFY_OAUTH_REDIRECT_URI: str = "http://127.0.0.1:8888/callback"
    SPOTIFY_OAUTH_CALLBACK_PORT: ClassVar[int] = 8888
    SPOTIFY_OAUTH_SCOPES: ClassVar[str] = "playlist-read-private playlist-read-collaborative"


settings = Settings()
