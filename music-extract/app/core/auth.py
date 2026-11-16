"""Cuentas y sesión de la web, sin dependencias externas.

- Contraseñas: PBKDF2-SHA256 con sal aleatoria (hashlib), guardadas como «pbkdf2_sha256$iteraciones$sal$hash».
- Sesión: una cookie HttpOnly con «id_usuario.vencimiento.firma», firmada con HMAC-SHA256 y una clave secreta
  (OWLO_SECRET_KEY del .env).
- No hay registro: las cuentas se crean con scripts/create_user.py (o por SQL), con su cuota de almacenamiento.
"""

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from config.settings import settings
from infrastructure.persistence.database import get_db
from infrastructure.persistence.models import User

_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS).hex()
    return f"pbkdf2_sha256${_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, digest)


def _secret() -> bytes:
    return settings.SECRET_KEY.encode()


def make_session(user_id: int) -> str:
    expires = int(time.time()) + settings.SESSION_MAX_AGE_DAYS * 86400
    payload = f"{user_id}.{expires}"
    signature = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def read_session(token: str | None) -> int | None:
    if not token:
        return None
    try:
        user_id, expires, signature = token.split(".")
    except ValueError:
        return None
    expected = hmac.new(_secret(), f"{user_id}.{expires}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature) or int(expires) < time.time():
        return None
    return int(user_id)


def create_user(db: Session, username: str, password: str, *, quota_bytes: int, is_admin: bool = False) -> User:
    username = username.strip()
    if not username or not password:
        raise ValueError("El usuario y la contraseña no pueden estar vacíos.")
    if db.query(User).filter(User.username == username).first():
        raise ValueError(f"Ya existe el usuario «{username}».")
    user = User(
        username=username,
        password_hash=hash_password(password),
        quota_bytes=quota_bytes,
        is_admin=is_admin,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(user)
    db.commit()
    return user


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.query(User).filter(User.username == username.strip()).first()
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


# --- Dependencias de FastAPI ---------------------------------------------------------------------

def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """El usuario de la cookie de sesión, o None si se usa la web sin cuenta."""
    user_id = read_session(request.cookies.get(settings.SESSION_COOKIE_NAME))
    return db.get(User, user_id) if user_id else None


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(401, "Inicia sesión para usar esta sección.")
    return user


def user_payload(user: User) -> dict:
    return {"id": user.id, "username": user.username, "is_admin": bool(user.is_admin), "cuota_bytes": user.quota_bytes}
