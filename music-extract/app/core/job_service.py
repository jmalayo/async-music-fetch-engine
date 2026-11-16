import json
import unicodedata
import uuid
import re
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from config.settings import settings
from infrastructure.persistence.models import Job, Track, Attempt, Retry
from core.schemas import DownloadStatusResponse, AttemptDetail, JobSummary, TrackDetailResponse, LastRetry, FailedUrl
from threading import Lock

_lock = Lock()

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def new_job_id() -> str:
    return uuid.uuid4().hex[:8]

def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[\s_\-]+", " ", s)
    return s.strip()

def slugify(s: str) -> str:
    s = normalize_name(s).replace(" ", "_")
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "sin_nombre"

def _is_spotify(url: str) -> bool:
    return "open.spotify.com" in url

def _is_youtube(url: str) -> bool:
    return "youtube.com/watch" in url or "youtu.be/" in url

def calculate_max_attempts(url: str) -> int:
    if _is_spotify(url):
        return settings.SPOTIFY_YOUTUBE_MATCH_LIMIT * len(settings.YOUTUBE_CLIENTS)
    if _is_youtube(url):
        return len(settings.YOUTUBE_CLIENTS) + 1
    return len(settings.YOUTUBE_CLIENTS)

def _find_previously_successful(db: Session, urls: list[str]) -> dict[str, Track]:
    if not urls:
        return {}

    rows = (
        db.query(Track)
        .filter(Track.url.in_(urls), Track.status == "successful")
        .order_by(Track.updated_at.desc())
        .all()
    )

    result: dict[str, Track] = {}
    for t in rows:
        result.setdefault(t.url, t)

    return result

def _mark_as_already_downloaded(db: Session, track: Track, prior: Track, now: str) -> None:
    track.status = "successful"
    track.title = prior.title
    track.artist = prior.artist
    track.elapsed_s = 0.0
    track.updated_at = now

    db.add(Attempt(
        track_id=track.track_id,
        provider=None,
        url_pick=track.url,
        title_pick=prior.title,
        status_attempt="successful",
        message=f"[SKIP] ya descargado segun la base de datos (job {prior.job_id}, track {prior.track_id})",
        timestamp_attempt=now,
    ))

def create_job(db: Session, job_type: str, playlist_name: str | None, urls: list[str], playlist_id: str | None = None, meta_by_url: dict[str, dict] | None = None) -> tuple[str, list[tuple[int, str, dict | None]]]:
    job_id = new_job_id()
    now = _now()
    meta_by_url = meta_by_url or {}

    with _lock:
        db.add(
            Job(
                job_id=job_id,
                type=job_type,
                playlist_name=playlist_name,
                playlist_id=playlist_id,
                amount=len(urls),
                status="in_progress",
                created_at=now,
                updated_at=now,
            )
        )

        prior_by_url = _find_previously_successful(db, urls)
        track_rows: list[tuple[int, str, dict | None]] = []

        for url in urls:
            meta = meta_by_url.get(url)

            track = Track(
                job_id=job_id,
                url=url,
                status="pending",
                max_attempts=calculate_max_attempts(url),
                elapsed_s=0.0,
                title=(meta or {}).get("name"),
                artist=((meta or {}).get("artists") or [None])[0],
                created_at=now,
                updated_at=now,
            )
            db.add(track)
            db.flush()

            prior = prior_by_url.get(url)
            if prior:
                _mark_as_already_downloaded(db, track, prior, now)
            else:
                track_rows.append((track.track_id, track.url, meta))

        if not track_rows:
            _update_job_status(db, job_id)

        db.commit()

    return job_id, track_rows

def get_job(db: Session, job_id: str) -> Job | None:
    return db.get(Job, job_id)

def resolve_job_by_name(db: Session, playlist_name: str) -> str | None:
    target = normalize_name(playlist_name)

    jobs = db.query(Job).filter(Job.type == "playlist-urls", Job.playlist_name.isnot(None)).all()
    candidates = [j for j in jobs if normalize_name(j.playlist_name) == target]

    if not candidates:
        return None

    candidates.sort(key=lambda j: j.created_at, reverse=True)
    return candidates[0].job_id

def find_existing_job(db: Session, job_type: str, name: str | None, external_id: str | None = None) -> str | None:
    if not name and not external_id:
        return None

    target_name = normalize_name(name or "")

    jobs = db.query(Job).filter(Job.type == job_type).all()

    for j in jobs:
        matches_id = external_id is not None and j.playlist_id == external_id
        matches_name = bool(target_name) and normalize_name(j.playlist_name or "") == target_name

        if matches_id or matches_name:
            return j.job_id

    return None

def resume_or_retry_job(db: Session, job_id: str, external_id: str | None, urls: list[str], meta_by_url: dict[str, dict] | None = None) -> tuple[list[tuple[int, str, dict | None]], int]:
    meta_by_url = meta_by_url or {}

    with _lock:
        existing = db.query(Track).filter(Track.job_id == job_id).all()
        existing_by_url = {t.url: t for t in existing}

        prior_by_url = _find_previously_successful(db, urls)

        to_enqueue: list[tuple[int, str, dict | None]] = []
        new_count = 0
        now = _now()

        for url in urls:
            track = existing_by_url.get(url)
            meta = meta_by_url.get(url)
            prior = prior_by_url.get(url)

            if track is None:
                track = Track(
                    job_id=job_id,
                    url=url,
                    status="pending",
                    max_attempts=calculate_max_attempts(url),
                    elapsed_s=0.0,
                    title=(meta or {}).get("name"),
                    artist=((meta or {}).get("artists") or [None])[0],
                    created_at=now,
                    updated_at=now,
                )
                db.add(track)
                db.flush()
                new_count += 1

                if prior:
                    _mark_as_already_downloaded(db, track, prior, now)
                else:
                    to_enqueue.append((track.track_id, track.url, meta))
                continue

            if track.status in ("pending", "in_progress"):
                if prior and prior.track_id != track.track_id:
                    _mark_as_already_downloaded(db, track, prior, now)
                    continue

                if track.status == "in_progress":
                    track.status = "pending"
                    track.updated_at = now

                to_enqueue.append((track.track_id, track.url, None))

        if to_enqueue:
            job = db.get(Job, job_id)

            if external_id and not job.playlist_id:
                job.playlist_id = external_id

            job.amount = job.amount + new_count
            job.status = "in_progress"
            job.updated_at = now
            job.elapsed_time_s = None

            db.commit()

            return to_enqueue, 0

        _update_job_status(db, job_id)
        db.commit()

    iteration_count, retry_tracks = retry(db, job_id)
    return retry_tracks, (iteration_count or 0)

def submit_job(db: Session, job_type: str, name: str | None, urls: list[str], external_id: str | None = None, meta_by_url: dict[str, dict] | None = None) -> tuple[str, list[tuple[int, str, dict | None]], int]:
    existing_job_id = find_existing_job(db, job_type, name, external_id)

    if existing_job_id:
        tracks, iteration_count = resume_or_retry_job(db, existing_job_id, external_id, urls, meta_by_url)
        return existing_job_id, tracks, iteration_count

    job_id, tracks = create_job(db, job_type, name, urls, playlist_id=external_id, meta_by_url=meta_by_url)
    return job_id, tracks, 0

def mark_in_progress(db: Session, track_id: int) -> None:
    with _lock:
        track = db.get(Track, track_id)
        track.status = "in_progress"
        track.updated_at = _now()
        db.commit()

ACTIVE_STATUSES = ["pending", "in_progress", "cancelling"]

def _update_job_status(db: Session, job_id: str) -> None:
    job = db.get(Job, job_id)

    # La vista previa todavía no empezó a descargar: su estado lo maneja web_jobs.
    if job.status in ("resolving", "preview"):
        return

    pending_count = db.query(Track).filter(Track.job_id == job_id, Track.status.in_(ACTIVE_STATUSES)).count()

    if pending_count > 0:
        return

    failed_count = db.query(Track).filter(Track.job_id == job_id, Track.status.in_(["failed", "incorrect", "skipped", "waiting_auth"])).count()

    now = datetime.now(timezone.utc)

    if job.status != "cancelled":
        job.status = "completed_with_errors" if failed_count > 0 else "completed"
    job.updated_at = now.isoformat()
    job.elapsed_time_s = round((now - datetime.fromisoformat(job.created_at)).total_seconds(), 1)

def _count_requeued(db: Session, job_id: str, iteration_count: int) -> int:
    retry_row = db.query(Retry).filter(Retry.job_id == job_id, Retry.iteration_count == iteration_count).first()

    if not retry_row:
        return 0

    attempt_ids = json.loads(retry_row.attempts_involved or "[]")

    if not attempt_ids:
        return 0

    return (
        db.query(Attempt.track_id)
        .filter(Attempt.attempt_id.in_(attempt_ids))
        .distinct()
        .count()
    )

def _rounds_involved(db: Session, job_id: str, track: Track) -> int:
    attempt_ids = {a.attempt_id for a in track.attempts}

    if not attempt_ids:
        return 0

    all_involved: set[int] = set()
    rounds = 0

    for r in db.query(Retry).filter(Retry.job_id == job_id).all():
        involved = set(json.loads(r.attempts_involved or "[]"))
        all_involved |= involved

        if involved & attempt_ids:
            rounds += 1

    if attempt_ids - all_involved:
        rounds += 1

    return rounds

def record_result(db: Session, track_id: int, iteration_count: int, status: str, attempts: list[dict], title: str | None, artist: str | None, elapsed_s: float, before_commit=None) -> None:
    """Guarda el resultado de una descarga. `before_commit(track)` permite ajustar la canción en la
    misma transacción (lo usa web_jobs para la regla de intentos)."""
    now = _now()

    with _lock:
        track = db.get(Track, track_id)

        new_attempts = []
        for it in attempts:
            a = Attempt(
                track_id=track_id,
                provider=it.get("proveedor"),
                url_pick=it.get("candidato_url"),
                video_id_pick=it.get("candidato_video_id"),
                title_pick=it.get("candidato_titulo"),
                channel_pick=it.get("candidato_canal"),
                score_pick=it.get("candidato_score"),
                source_pick=it.get("candidato_fuente"),
                duration_s_pick=it.get("duracion_s"),
                status_attempt="successful" if it.get("resultado") == "correcta" else "failed",
                message=it.get("mensaje"),
                timestamp_attempt=now,
            )
            db.add(a)
            new_attempts.append(a)

        db.flush()

        retry_row = None
        if iteration_count > 0:
            retry_row = db.query(Retry).filter(Retry.job_id == track.job_id, Retry.iteration_count == iteration_count).one()

            if new_attempts:
                involved = json.loads(retry_row.attempts_involved or "[]")
                involved.extend(a.attempt_id for a in new_attempts)
                retry_row.attempts_involved = json.dumps(involved)

        track.status = status
        if title:
            track.title = title
        if artist:
            track.artist = artist
        track.elapsed_s = (track.elapsed_s or 0.0) + elapsed_s
        track.updated_at = now

        if retry_row is not None:
            if status == "successful":
                retry_row.fixed_count += 1
            else:
                retry_row.failed_count += 1

            requeued = _count_requeued(db, track.job_id, iteration_count)
            if retry_row.fixed_count + retry_row.failed_count >= requeued and not retry_row.completed_at:
                retry_row.completed_at = now

        if before_commit is not None:
            before_commit(track)

        _update_job_status(db, track.job_id)
        db.commit()

def _calculate_eta(db: Session, job_id: str, pending_count: int) -> float | None:
    rows = (
        db.query(Track.elapsed_s)
        .filter(Track.job_id == job_id, Track.status.in_(["successful", "incorrect", "failed"]))
        .all()
    )

    if len(rows) < 3:
        return None

    average = sum(r[0] for r in rows) / len(rows)

    return round(average * pending_count, 1)    

def get_latest_job_id(db: Session) -> str | None:
    job = db.query(Job).order_by(Job.updated_at.desc()).first()
    return job.job_id if job else None

def get_status(db: Session, job_id: str) -> DownloadStatusResponse | None:
    job = db.get(Job, job_id)
    
    if not job:
        return None

    track_query = db.query(Track).filter(Track.job_id == job_id)
    successful_count = track_query.filter(Track.status == "successful").count()
    incorrect_count = track_query.filter(Track.status == "incorrect").count()
    failed_count = track_query.filter(Track.status == "failed").count()
    skipped_count = track_query.filter(Track.status == "skipped").count()
    pending_count = track_query.filter(Track.status.in_(["pending", "in_progress"])).count()

    eta = _calculate_eta(db, job_id, pending_count)

    duration_rows = (
        db.query(Track.elapsed_s)
        .filter(Track.job_id == job_id, Track.status.in_(["successful", "incorrect", "failed"]))
        .all()
    )

    if len(duration_rows) < 3:
        average_time = None
    else:
        average_time = round(sum(r[0] for r in duration_rows) / len(duration_rows), 1)

    if job.elapsed_time_s is not None:
        elapsed = job.elapsed_time_s
    else:
        created = datetime.fromisoformat(job.created_at)
        elapsed = round((datetime.now(timezone.utc) - created).total_seconds(), 1)

    last_retry_row = db.query(Retry).filter(Retry.job_id == job_id).order_by(Retry.iteration_count.desc()).first()

    last_retry = None
    if last_retry_row:
        requeued = _count_requeued(db, job_id, last_retry_row.iteration_count)
        round_pending = requeued - last_retry_row.fixed_count - last_retry_row.failed_count
        last_retry = LastRetry(
            iteration_count=last_retry_row.iteration_count,
            requeued=requeued,
            fixed_count=last_retry_row.fixed_count,
            failed_count=last_retry_row.failed_count,
            pending=round_pending,
        )

    return DownloadStatusResponse(
        job_id=job_id,
        type=job.type,
        playlist_name=job.playlist_name,
        status=job.status,
        total=job.amount,
        successful=successful_count,
        incorrect=incorrect_count,
        failed=failed_count,
        skipped=skipped_count,
        pending=pending_count,
        average_time_per_track_s=average_time,
        elapsed_time_s=elapsed,
        estimated_remaining_time_s=eta,
        last_retry=last_retry,
        failed_urls=f"/view-failed-urls/{job_id}",
    )

def list_jobs(db: Session) -> list[JobSummary]:
    jobs = db.query(Job).order_by(Job.updated_at.desc()).all()

    return [
        JobSummary(
            job_id=j.job_id,
            type=j.type,
            playlist_name=j.playlist_name,
            amount=j.amount,
            status=j.status,
            updated_at=j.updated_at,
        )
        for j in jobs
    ]

def get_track(db: Session, job_id: str, track_id: int) -> TrackDetailResponse | None:
    track = db.get(Track, track_id)
    if not track or track.job_id != job_id:
        return None

    return TrackDetailResponse(
        track_id=track.track_id,
        job_id=track.job_id,
        url=track.url,
        title=track.title,
        artist=track.artist,
        status=track.status,
        max_attempts=track.max_attempts,
        elapsed_s=track.elapsed_s,
        updated_at=track.updated_at,
        attempts=[
            AttemptDetail(
                attempt_id=a.attempt_id, track_id=a.track_id,
                provider=a.provider, url_pick=a.url_pick, video_id_pick=a.video_id_pick,
                title_pick=a.title_pick, channel_pick=a.channel_pick, score_pick=a.score_pick,
                source_pick=a.source_pick, duration_s_pick=a.duration_s_pick,
                status_attempt=a.status_attempt, message=a.message, timestamp_attempt=a.timestamp_attempt,
            )
            for a in track.attempts
        ],
    )

def list_failed(db: Session, job_id: str) -> list[FailedUrl]:
    tracks = db.query(Track).filter(Track.job_id == job_id, Track.status.in_(["failed", "skipped"])).all()

    result = []
    for t in tracks:
        last_attempt = t.attempts[-1] if t.attempts else None
        retry_rounds = _rounds_involved(db, job_id, t)

        result.append(FailedUrl(
            track_id=t.track_id,
            url=t.url,
            title=t.title,
            artist=t.artist,
            status=t.status,
            max_attempts=t.max_attempts,
            total_attempts=len(t.attempts),
            retries_attempted=retry_rounds,
            last_provider_attempted=last_attempt.provider if last_attempt else None,
            status_attempt=last_attempt.status_attempt if last_attempt else None,
            message=last_attempt.message if last_attempt else None,
            updated_at=t.updated_at,
        ))
    return result

def retry(db: Session, job_id: str) -> tuple[int | None, list[tuple[int, str, dict | None]]]:
    with _lock:
        failed_tracks = db.query(Track).filter(Track.job_id == job_id, Track.status.in_(["failed", "skipped"])).all()

        # Solo cuentan los intentos de una canción que nunca bajó: `rondas_usadas` vuelve a 0 al bajar bien.
        candidates = [t for t in failed_tracks if (t.rondas_usadas or 0) < settings.MAX_RETRIES_PER_TRACK]

        if not candidates:
            return None, []

        iteration_count = (db.query(func.max(Retry.iteration_count)).filter(Retry.job_id == job_id).scalar() or 0) + 1
        now = _now()

        db.add(Retry(job_id=job_id, iteration_count=iteration_count, fixed_count=0, failed_count=0, triggered_at=now, completed_at=None))

        for t in candidates:
            t.status = "pending"
            t.updated_at = now

        job = db.get(Job, job_id)
        job.status = "in_progress"
        job.updated_at = now
        job.elapsed_time_s = None

        db.commit()

        result = [(t.track_id, t.url, None) for t in candidates]

    return iteration_count, result
