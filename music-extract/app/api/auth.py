import requests
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from config.settings import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_OAUTH_CALLBACK_PORT,
    SPOTIFY_OAUTH_REDIRECT_URI,
    SPOTIFY_OAUTH_SCOPES,
)
from api.schemas import SpotifyLoginRequest
from infrastructure.external import spotify_client, spotify_oauth
from infrastructure.persistence.database import get_db

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/spotify/login")
def start_spotify_auth(body: SpotifyLoginRequest | None = None, db: Session = Depends(get_db)):
    body = body or SpotifyLoginRequest()
    client_id = body.client_id or SPOTIFY_CLIENT_ID
    client_secret = body.client_secret or SPOTIFY_CLIENT_SECRET

    if not client_id or not client_secret:
        raise HTTPException(422, "Falta client_id/client_secret (ni se paso en el body ni esta configurado en el .env)")

    try:
        token_data = spotify_oauth.run_authorization_flow(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=SPOTIFY_OAUTH_REDIRECT_URI,
            port=SPOTIFY_OAUTH_CALLBACK_PORT,
            scopes=SPOTIFY_OAUTH_SCOPES,
        )
    except spotify_oauth.AuthorizationDenied as e:
        raise HTTPException(400, str(e))

    spotify_client.save_user_token(db, token_data, client_id=client_id, client_secret=client_secret)

    return {"ok": True, "scope": token_data.get("scope")}

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
