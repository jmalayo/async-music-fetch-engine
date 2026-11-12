"""Spotify con la web publicada: el retorno llega a una ruta del motor, y solo un administrador conecta o desconecta."""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def web_redirect(monkeypatch):
    from api import auth as auth_api

    uri = "https://owlo.example.com/api/auth/spotify/callback"
    monkeypatch.setattr(auth_api, "SPOTIFY_OAUTH_REDIRECT_URI", uri)
    monkeypatch.setattr(auth_api, "SPOTIFY_OAUTH_LOCAL", False)
    return uri


def test_retorno_por_la_web_guarda_el_token_y_vuelve_a_la_web(client, web_redirect, monkeypatch):
    from infrastructure.external import spotify_client, spotify_oauth
    from infrastructure.persistence.database import SessionLocal

    canjes = []
    monkeypatch.setattr(spotify_oauth, "exchange_code_for_token",
                        lambda code, **kw: canjes.append((code, kw["redirect_uri"])) or {"access_token": "tok-web", "refresh_token": "r", "expires_in": 3600, "scope": "s"})

    url = client.post("/auth/spotify/authorize").json()["url"]
    query = parse_qs(urlparse(url).query)
    assert query["redirect_uri"] == [web_redirect]
    state = query["state"][0]

    r = client.get(f"/auth/spotify/callback?code=abc&state={state}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://owlo.example.com/?spotify=conectado"
    assert canjes == [("abc", web_redirect)]
    with SessionLocal() as db:
        assert spotify_client.get_user_token(db) == "tok-web"

    # El mismo state no sirve dos veces.
    r = client.get(f"/auth/spotify/callback?code=abc&state={state}", follow_redirects=False)
    assert r.headers["location"].endswith("?spotify=error")


def test_retorno_con_state_desconocido_o_rechazado(client, web_redirect):
    r = client.get("/auth/spotify/callback?code=abc&state=inventado", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?spotify=error")

    state = parse_qs(urlparse(client.post("/auth/spotify/authorize").json()["url"]).query)["state"][0]
    r = client.get(f"/auth/spotify/callback?error=access_denied&state={state}", follow_redirects=False)
    assert r.headers["location"].endswith("?spotify=error")


def test_conectar_y_desconectar_spotify_pide_admin(client, anon_client):
    from core import auth
    from infrastructure.persistence.database import SessionLocal

    # Sin cuenta: 401.
    assert anon_client.post("/auth/spotify/authorize").status_code == 401
    assert anon_client.post("/auth/spotify/logout").status_code == 401

    # Con cuenta pero sin ser admin: 403.
    with SessionLocal() as db:
        auth.create_user(db, "comun", "clave-123")
    with TestClient(main.app) as c:
        assert c.post("/auth/login", json={"username": "comun", "password": "clave-123"}).status_code == 200
        assert c.post("/auth/spotify/authorize").status_code == 403
        assert c.post("/auth/spotify/logout").status_code == 403
        # Ver el estado sí puede (la web muestra si Spotify está conectado).
        assert c.get("/auth/spotify/account-status").status_code not in (401, 403)
