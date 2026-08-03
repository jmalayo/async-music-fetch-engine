from pydantic import BaseModel

from core.schemas import FailedUrl

class SpotifyLoginRequest(BaseModel):
    client_id: str | None = None
    client_secret: str | None = None

class DownloadMusicManualRequest(BaseModel):
    urls: list[str]
    group_name: str | None = None

class DownloadPlaylistSpotifyRequest(BaseModel):
    playlist_name: str

class PlaylistNotFoundError(BaseModel):
    error: str
    playlist_name: str

class FailedUrlsResponse(BaseModel):
    job_id: str
    total_failed: int
    urls: list[FailedUrl]
