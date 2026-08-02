from datetime import datetime, timezone

from sqlalchemy import Column, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from infrastructure.persistence.database import Base

class Job(Base):
    __tablename__ = "jobs"

    job_id = Column(String(16), primary_key=True)
    type = Column(String(16), nullable=False)
    playlist_name = Column(String, nullable=True)
    playlist_id = Column(String(64), nullable=True)
    amount = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False)

    created_at = Column(String, default=datetime.now(timezone.utc).isoformat(), onupdate=datetime.now(timezone.utc).isoformat(), nullable=False)
    updated_at = Column(String, default=datetime.now(timezone.utc).isoformat(), onupdate=datetime.now(timezone.utc).isoformat(), nullable=False)

    tracks = relationship("Track", back_populates="job", cascade="all, delete-orphan", passive_deletes=True)
    retries = relationship("Retry", back_populates="job", order_by="Retry.iteration_count", cascade="all, delete-orphan", passive_deletes=True)

class Track(Base):
    __tablename__ = "tracks"

    track_id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(16), ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False)
    url = Column(String(500), nullable=False)
    title = Column(String(255), nullable=True)
    artist = Column(String(255), nullable=True)
    status = Column(String(16), nullable=False)
    max_attempts = Column(Integer, nullable=False)
    elapsed_s = Column(Float, default=0, nullable=False)

    created_at = Column(String, default=datetime.now(timezone.utc).isoformat(), onupdate=datetime.now(timezone.utc).isoformat(), nullable=False)
    updated_at = Column(String, default=datetime.now(timezone.utc).isoformat(), onupdate=datetime.now(timezone.utc).isoformat(), nullable=False)

    job = relationship("Job", back_populates="tracks")
    attempts = relationship("Attempt", back_populates="track", order_by="Attempt.attempt_id", cascade="all, delete-orphan", passive_deletes=True)

class Attempt(Base):
    __tablename__ = "attempts"

    attempt_id = Column(Integer, primary_key=True, autoincrement=True)
    track_id = Column(Integer, ForeignKey("tracks.track_id", ondelete="CASCADE"), nullable=False)
    provider = Column(String(16), nullable=True)

    url_pick = Column(String(500), nullable=True)
    video_id_pick = Column(String(16), nullable=True)
    title_pick = Column(String(255), nullable=True)
    channel_pick = Column(String(255), nullable=True)
    score_pick = Column(Float, nullable=True)
    source_pick = Column(String(32), nullable=True)
    duration_s_pick = Column(Float, nullable=True)

    status_attempt = Column(String(16), nullable=False)
    message = Column(Text, nullable=True)
    timestamp_attempt = Column(String, default=datetime.now(timezone.utc).isoformat(), nullable=False)

    track = relationship("Track", back_populates="attempts")

class Retry(Base):
    __tablename__ = "retries"

    retry_id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(16), ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False)

    iteration_count = Column(Integer, nullable=False)

    fixed_count = Column(Integer, default=0, nullable=False)
    failed_count = Column(Integer, default=0, nullable=False)
    attempts_involved = Column(Text, default="[]", nullable=False)

    triggered_at = Column(String, default=datetime.now(timezone.utc).isoformat(), onupdate=datetime.now(timezone.utc).isoformat(), nullable=False)
    completed_at = Column(String, nullable=True)

    job = relationship("Job", back_populates="retries")

class SpotifyToken(Base):
    __tablename__ = "spotify_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=False)
    expires_at = Column(String, nullable=False)
    scope = Column(String(255), nullable=True)

    updated_at = Column(String, default=datetime.now(timezone.utc).isoformat(), onupdate=datetime.now(timezone.utc).isoformat(), nullable=False)
