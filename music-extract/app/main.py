import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI
from scalar_fastapi import get_scalar_api_reference

from infrastructure.persistence.database import init_db
from api import downloads, stages, auth

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="music-extract API", lifespan=lifespan, docs_url=None)

app.include_router(auth.router)
app.include_router(downloads.router)
app.include_router(stages.router)

@app.get("/docs", include_in_schema=False)
async def scalar_docs():
    return get_scalar_api_reference(openapi_url=app.openapi_url, title=app.title)

