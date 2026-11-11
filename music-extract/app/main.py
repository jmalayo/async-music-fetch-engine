import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import WEB_ORIGINS
from infrastructure.persistence.database import init_db
from infrastructure.external import downloader_service
from api import downloads, stages, auth, jobs, folders, session, library

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Lo que quedó a medias por un reinicio (descargas y vistas previas) vuelve a la cola.
    downloader_service.resume_after_restart()
    yield

app = FastAPI(title="music-extract API", lifespan=lifespan)

# La web corre en otro puerto (Vite): en desarrollo pasa por el proxy /api, pero si se abre directo
# contra la API, CORS tiene que permitir su origen.
app.add_middleware(
    CORSMiddleware,
    allow_origins=WEB_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(session.router)
app.include_router(auth.router)
app.include_router(downloads.router)
app.include_router(stages.router)
app.include_router(jobs.router)
app.include_router(jobs.tracks_router)
app.include_router(folders.router)
app.include_router(library.router)
