from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from infrastructure.persistence.database import get_db
from core.schemas import DownloadStatusResponse, JobSummary
from core import job_service
from api.schemas import FailedUrlsResponse
from fastapi.responses import StreamingResponse
import pandas as pd
import io

def _build_response(db: Session, job_id: str, format: str | None):
    urls = job_service.list_failed(db, job_id)

    if format == "csv":
        df = pd.DataFrame([u.model_dump() for u in urls])
        buffer = io.StringIO()
        df.to_csv(buffer, sep=",", index=False)
        buffer.seek(0)

        return StreamingResponse(
            iter([buffer.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={job_id}_failed.csv"},
        )

    return FailedUrlsResponse(job_id=job_id, total_failed=len(urls), urls=urls)

router = APIRouter(prefix="/stages", tags=["Estados de descarga"])

@router.get("/view-download-status/{job_id}", response_model=DownloadStatusResponse)
def get_status(job_id: str, db: Session = Depends(get_db)):
    status = job_service.get_status(db, job_id)

    if not status:
        raise HTTPException(404, f"No existe el job {job_id}")

    return status

@router.get("/view-failed-urls/{job_id}", response_model=FailedUrlsResponse)
def get_failed_urls(job_id: str, format: str | None = None, db: Session = Depends(get_db)):
    job = job_service.get_job(db, job_id)

    if not job:
        raise HTTPException(404, f"No existe el job {job_id}")

    return _build_response(db, job_id, format)

@router.get("/view-history", response_model=list[JobSummary])
def get_history(db: Session = Depends(get_db)):
    return job_service.list_jobs(db)