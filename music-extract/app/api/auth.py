from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from config.settings import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    SPOTIFY_OAUTH_CALLBACK_PORT,
    SPOTIFY_OAUTH_REDIRECT_URI,
    SPOTIFY_OAUTH_SCOPES,
)
from infrastructure.external import spotify_client, spotify_oauth
from infrastructure.persistence.database import get_db

router = APIRouter(prefix="/auth", tags=["Autenticacion"])

@router.post("/spotify/iniciar")
def start_spotify_auth(db: Session = Depends(get_db)):
    try:
        token_data = spotify_oauth.run_authorization_flow(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            redirect_uri=SPOTIFY_OAUTH_REDIRECT_URI,
            port=SPOTIFY_OAUTH_CALLBACK_PORT,
            scopes=SPOTIFY_OAUTH_SCOPES,
        )
    except spotify_oauth.AuthorizationDenied as e:
        raise HTTPException(400, str(e))

    spotify_client.save_user_token(db, token_data)

    return {"ok": True, "scope": token_data.get("scope")}

@router.get("/spotify/estado")
def spotify_auth_status(db: Session = Depends(get_db)):
    token = spotify_client.get_user_token(db)

    return {"autenticado": token is not None}
