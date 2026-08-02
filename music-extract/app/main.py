import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI

from infrastructure.persistence.database import init_db
from api import downloads, stages, auth

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="music-extract API", lifespan=lifespan)

app.include_router(auth.router)
app.include_router(downloads.router)
app.include_router(stages.router)

