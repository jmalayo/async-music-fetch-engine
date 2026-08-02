from pydantic import BaseModel

class LastRetry(BaseModel):
    iteration_count: int
    requeued: int
    fixed_count: int
    failed_count: int
    pending: int

class DownloadStatusResponse(BaseModel):
    job_id: str
    type: str
    playlist_name: str | None
    status: str
    total: int
    successful: int
    incorrect: int
    failed: int
    pending: int
    average_time_per_track_s: float | None
    elapsed_time_s: float
    estimated_remaining_time_s: float | None
    last_retry: LastRetry | None
    failed_urls: str

class JobSummary(BaseModel):
    job_id: str
    type: str
    playlist_name: str | None
    amount: int
    status: str
    updated_at: str

class AttemptDetail(BaseModel):
    attempt_id: int
    track_id: int
    provider: str | None
    url_pick: str | None
    video_id_pick: str | None
    title_pick: str | None
    channel_pick: str | None
    score_pick: float | None
    source_pick: str | None
    duration_s_pick: float | None
    status_attempt: str
    message: str | None
    timestamp_attempt: str

class TrackDetailResponse(BaseModel):
    track_id: int
    job_id: str
    url: str
    title: str | None
    artist: str | None
    status: str
    max_attempts: int
    elapsed_s: float
    updated_at: str
    attempts: list[AttemptDetail]

class FailedUrl(BaseModel):
    track_id: int
    url: str
    title: str | None
    artist: str | None
    max_attempts: int
    total_attempts: int
    retries_attempted: int
    last_provider_attempted: str | None
    status_attempt: str | None
    message: str | None
    updated_at: str
