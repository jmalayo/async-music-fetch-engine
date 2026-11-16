from urllib.parse import urlparse

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from config.settings import settings
from api.schemas import SpotifyLoginRequest
from core import auth as accounts
from infrastructure.external import spotify_client, spotify_oauth
from infrastructure.persistence.database import SessionLocal, get_db
from infrastructure.persistence.models import SpotifyToken, User

router = APIRouter(prefix="/auth", tags=["Authentication"])

def require_admin(user: User = Depends(accounts.require_user)) -> User:

    if not user.is_admin:
        raise HTTPException(403, "Solo un administrador puede conectar o desconectar Spotify.")

    return user

@router.post("/spotify/login")
def start_spotify_auth(body: SpotifyLoginRequest | None = None, db: Session = Depends(get_db), _: User = Depends(require_admin)):
    body = body or SpotifyLoginRequest()
    client_id = body.client_id or settings.SPOTIFY_CLIENT_ID
    client_secret = body.client_secret or settings.SPOTIFY_CLIENT_SECRET

    if not client_id or not client_secret:
        raise HTTPException(422, "Falta client_id/client_secret (ni se paso en el body ni esta configurado en el .env)")

    try:
        token_data = spotify_oauth.run_authorization_flow(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=settings.SPOTIFY_OAUTH_REDIRECT_URI,
            port=settings.SPOTIFY_OAUTH_CALLBACK_PORT,
            scopes=settings.SPOTIFY_OAUTH_SCOPES,
        )
    except spotify_oauth.AuthorizationDenied as e:
        raise HTTPException(400, str(e))

    spotify_client.save_user_token(db, token_data, client_id=client_id, client_secret=client_secret)

    return {"ok": True, "scope": token_data.get("scope")}

@router.post("/spotify/authorize")
def spotify_authorize(body: SpotifyLoginRequest | None = None, _: User = Depends(require_admin)):
    """Para la web: devuelve la URL de Spotify y espera el callback en segundo plano (sin bloquear la petición).

    La web abre la URL en otra pestaña y consulta `/spotify/account-status` hasta que quede conectada.
    """
    body = body or SpotifyLoginRequest()
    client_id = body.client_id or settings.SPOTIFY_CLIENT_ID
    client_secret = body.client_secret or settings.SPOTIFY_CLIENT_SECRET
    if not client_id or not client_secret:
        raise HTTPException(422, "Falta client_id/client_secret (ni se paso en el body ni esta configurado en el .env)")

    def save(token_data: dict) -> None:
        with SessionLocal() as db:
            spotify_client.save_user_token(db, token_data, client_id=client_id, client_secret=client_secret)

    if not settings.SPOTIFY_OAUTH_REDIRECT_URI.startswith(("http://127.0.0.1", "http://localhost")):
        url = spotify_oauth.start_web_flow(client_id=client_id, client_secret=client_secret, redirect_uri=settings.SPOTIFY_OAUTH_REDIRECT_URI,
                                           scopes=settings.SPOTIFY_OAUTH_SCOPES, on_token=save)
        return {"url": url}

    url = spotify_oauth.start_authorization_flow(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=settings.SPOTIFY_OAUTH_REDIRECT_URI,
        port=settings.SPOTIFY_OAUTH_CALLBACK_PORT,
        scopes=settings.SPOTIFY_OAUTH_SCOPES,
        on_token=save,
    )
    return {"url": url}


@router.get("/spotify/callback", include_in_schema=False)
def spotify_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    """Spotify vuelve aquí (vía la web). Guarda el token y regresa a la web, que ya muestra «Conectado»."""
    parsed = urlparse(settings.SPOTIFY_OAUTH_REDIRECT_URI)
    home = f"{parsed.scheme}://{parsed.netloc}/"
    try:
        spotify_oauth.finish_web_flow(state=state, code=code, error=error)
    except spotify_oauth.AuthorizationDenied:
        return RedirectResponse(f"{home}?spotify=error", status_code=303)
    except requests.HTTPError:
        return RedirectResponse(f"{home}?spotify=error", status_code=303)
    return RedirectResponse(f"{home}?spotify=conectado", status_code=303)


@router.post("/spotify/logout")
def spotify_logout(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """Desconecta Spotify: borra el token guardado."""
    db.query(SpotifyToken).delete()
    db.commit()
    return {"autenticado": False}


@router.get("/spotify/account-status")
def spotify_auth_status(db: Session = Depends(get_db)):
    token = spotify_client.get_user_token(db)

    if token is None:
        return {"autenticado": False}

    try:
        me = spotify_client.get_me(token)
    except requests.HTTPError as e:
        raise HTTPException(502, f"Token guardado pero Spotify rechazo /v1/me: {e}")

    return {"autenticado": True, **me}
