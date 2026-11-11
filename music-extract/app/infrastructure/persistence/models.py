from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from infrastructure.persistence.database import Base

class User(Base):
    """Usuario de la web. No hay registro: las cuentas se crean con scripts/create_user.py."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    quota_bytes = Column(Integer, nullable=False)
    is_admin = Column(Boolean, default=False, server_default="0", nullable=False)
    created_at = Column(String, nullable=False)


class Job(Base):
    __tablename__ = "jobs"

    job_id = Column(String(16), primary_key=True)
    type = Column(String(16), nullable=False)
    playlist_name = Column(String, nullable=True)
    playlist_id = Column(String(64), nullable=True)
    amount = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False)
    elapsed_time_s = Column(Float, nullable=True)
    # Flujo de la web: origen (spotify | youtube) y carpeta de destino elegida por el usuario.
    origen = Column(String(16), nullable=True)
    carpeta = Column(String(255), nullable=True)
    # Dueño del trabajo; None = descarga sin cuenta (archivos temporales, sin biblioteca).
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

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

    # Intentos del usuario (rondas). Vuelve a 0 al bajar bien o al cambiar la URL; cancelar no suma.
    rondas_usadas = Column(Integer, default=0, server_default="0", nullable=False)
    duration_s = Column(Float, nullable=True)
    ya_descargada = Column(Boolean, default=False, server_default="0", nullable=False)
    # Motivo clasificado del último fallo: geo_restricted, premium_only, needs_auth, client_blocked, stalled, no_candidates…
    motivo = Column(String(32), nullable=True)
    # Vista previa en JSON: candidatos puntuados, disponibilidad y capítulos del video.
    preview = Column(Text, nullable=True)
    # Opciones del tracklist en JSON: {"cortar": bool, "guardar_completo": bool}.
    tracklist = Column(Text, nullable=True)

    # Biblioteca: archivo descargado (ruta relativa a MUSIC_DOWNLOADED_ROOT; una carpeta si se cortó por tracklist
    # sin guardar el audio completo), su tamaño, cuántas veces se bajó al equipo, y si bajó después de fallar o con
    # una URL cambiada (recuperada con la sugerencia).
    archivo = Column(String(1000), nullable=True)
    bytes = Column(Integer, nullable=True)
    descargas = Column(Integer, default=0, server_default="0", nullable=False)
    recuperada = Column(Boolean, default=False, server_default="0", nullable=False)
    # Borrada de la biblioteca por el usuario: sus archivos ya no están en disco, pero la fila queda para Recientes.
    eliminada = Column(Boolean, default=False, server_default="0", nullable=False)

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
    client_id = Column(String(64), nullable=True)
    client_secret = Column(String(64), nullable=True)

    updated_at = Column(String, default=datetime.now(timezone.utc).isoformat(), onupdate=datetime.now(timezone.utc).isoformat(), nullable=False)
