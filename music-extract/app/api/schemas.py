from pydantic import BaseModel

from core.schemas import FailedUrl

class DownloadMusicManualRequest(BaseModel):
    urls: list[str]
    group_name: str | None = None

class DownloadPlaylistSpotifyRequest(BaseModel):
    playlist_name: str

class DownloadMusicManualResponse(BaseModel):
    job_id: str
    type: str
    group_name: str | None = None
    total: int

class DownloadPlaylistResponse(BaseModel):
    job_id: str
    type: str
    playlist_name: str
    total: int

class RetryResponse(BaseModel):
    job_id: str
    iteration_count: int | None = None
    requeued: int

class DownloadPlaylistSpotifyResponse(BaseModel):
    job_id: str
    type: str
    playlist_name: str
    total: int

class PlaylistNotFoundError(BaseModel):
    error: str
    playlist_name: str

class FailedUrlsResponse(BaseModel):
    job_id: str
    total_failed: int
    urls: list[FailedUrl]
