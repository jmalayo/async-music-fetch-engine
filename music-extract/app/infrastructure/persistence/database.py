from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from alembic import command
from alembic.config import Config

from config.settings import settings

settings.DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(f"sqlite:///{settings.DB_PATH}", connect_args={"check_same_thread": False})

@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA journal_mode=DELETE")
    dbapi_conn.execute("PRAGMA foreign_keys=ON")

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db() -> None:
    cfg = Config(str(settings.ALEMBIC_INI_PATH))
    cfg.set_main_option("script_location", str(settings.ALEMBIC_SCRIPT_LOCATION))
    command.upgrade(cfg, "head")
