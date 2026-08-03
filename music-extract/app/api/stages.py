from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from infrastructure.persistence.database import get_db
from core.schemas import DownloadStatusResponse, JobSummary
from core import job_service
from api.schemas import FailedUrlsResponse
from fastapi.responses import StreamingResponse
import pandas as pd
import io

router = APIRouter(prefix="/stages", tags=["Estados de descarga"])

@router.get("/view-download-status", response_model=DownloadStatusResponse)
def get_status(job_id: str | None = None, db: Session = Depends(get_db)):
    if job_id is None:
        job_id = job_service.get_latest_job_id(db)

        if job_id is None:
            raise HTTPException(404, "No hay jobs registrados todavia")

    status = job_service.get_status(db, job_id)

    if not status:
        raise HTTPException(404, f"No existe el job {job_id}")

    return status

@router.get("/view-failed-urls", response_model=FailedUrlsResponse)
def get_failed_urls(job_id: str | None = None, db: Session = Depends(get_db)):
    
    if job_id is None:
        job_id = job_service.get_latest_job_id(db)

        if job_id is None:
            raise HTTPException(404, "No hay jobs registrados todavia")

    job = job_service.get_job(db, job_id)

    if not job:
        raise HTTPException(404, f"No existe el job {job_id}")

    urls = job_service.list_failed(db, job_id)

    return FailedUrlsResponse(job_id=job_id, total_failed=len(urls), urls=urls)

@router.get("/view-all-jobs", response_model=list[JobSummary])
def get_history(db: Session = Depends(get_db)):
    
    return job_service.list_jobs(db)