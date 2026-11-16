"""Inicio y cierre de sesión de la web. Sin registro: las cuentas se crean con scripts/create_user.py."""

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config.settings import settings
from core import auth
from infrastructure.persistence.database import get_db
from infrastructure.persistence.models import User

router = APIRouter(prefix="/auth", tags=["Sesión"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = auth.authenticate(db, body.username, body.password)
    if user is None:
        raise HTTPException(401, "Usuario o contraseña incorrectos.")
    response.set_cookie(
        settings.SESSION_COOKIE_NAME,
        auth.make_session(user.id),
        max_age=settings.SESSION_MAX_AGE_DAYS * 86400,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return auth.user_payload(user)


@router.post("/logout", status_code=204)
def logout(response: Response):
    response.delete_cookie(settings.SESSION_COOKIE_NAME, path="/")


@router.get("/me")
def me(user: User | None = Depends(auth.current_user)):
    """El usuario de la sesión, o {"usuario": null} si se usa sin cuenta."""
    return {"usuario": auth.user_payload(user) if user else None}
