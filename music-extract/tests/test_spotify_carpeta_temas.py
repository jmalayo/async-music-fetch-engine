"""Conexión de Spotify desde la web, cambio de carpeta en la vista previa y temas excluidos del tracklist."""

from conftest import wait_until
from test_web_flow import create_youtube, preview_ready, settled, yt


# --- Spotify: conectar sin bloquear y desconectar ---------------------------------------------------------------

def test_spotify_authorize_devuelve_url_sin_bloquear(client, monkeypatch):
    from infrastructure.external import spotify_client, spotify_oauth
    from infrastructure.persistence.database import SessionLocal

    # El callback llega enseguida: se reemplaza la espera del puerto y el canje del código.
    monkeypatch.setattr(spotify_oauth, "wait_for_auth_code", lambda port, state: "codigo")
    monkeypatch.setattr(spotify_oauth, "exchange_code_for_token",
                        lambda code, **kw: {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600, "scope": "s"})
    monkeypatch.setattr(spotify_client, "get_me", lambda token: {"id": "yo", "display_name": "Yo"})

    r = client.post("/auth/spotify/authorize")
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://accounts.spotify.com/authorize?")

    status = wait_until(lambda: (s := client.get("/auth/spotify/account-status").json())["autenticado"] and s)
    assert status["id"] == "yo"
    with SessionLocal() as db:
        assert spotify_client.get_user_token(db) == "tok"

    r = client.post("/auth/spotify/logout")
    assert r.status_code == 200
    assert client.get("/auth/spotify/account-status").json() == {"autenticado": False}


def test_spotify_authorize_reusa_el_flujo_pendiente(client, monkeypatch):
    import threading

    from infrastructure.external import spotify_oauth

    release = threading.Event()
    monkeypatch.setattr(spotify_oauth, "wait_for_auth_code", lambda port, state: release.wait(5) and None)
    try:
        first = client.post("/auth/spotify/authorize").json()["url"]
        second = client.post("/auth/spotify/authorize").json()["url"]
        assert first == second  # un solo callback escuchando en el puerto
    finally:
        release.set()
        spotify_oauth._pending["thread"].join(5)


# --- Carpeta de destino desde la vista previa -------------------------------------------------------------------

def test_cambiar_carpeta_en_vista_previa(client, fake_engine, downloads_root):
    job_id = create_youtube(client, [yt("carp")], carpeta="Primera")
    track = preview_ready(client, job_id)["tracks"][0]

    r = client.patch(f"/jobs/{job_id}", json={"carpeta": "Otra Carpeta"})
    assert r.status_code == 200, r.text
    assert r.json()["carpeta"] == "Otra Carpeta"

    # Vacía vuelve a la de por defecto.
    assert client.patch(f"/jobs/{job_id}", json={"carpeta": ""}).json()["carpeta"] == "Canciones sueltas"
    client.patch(f"/jobs/{job_id}", json={"carpeta": "Otra Carpeta"})

    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, track["track_id"], {"successful"})
    assert (downloads_root / "prueba" / "otra_carpeta" / "Canal - Video.mp3").exists()

    # Confirmado, ya no se cambia.
    assert client.patch(f"/jobs/{job_id}", json={"carpeta": "Tarde"}).status_code == 409


def test_cambiar_carpeta_pide_sesion(client, anon_client, fake_engine):
    job_id = create_youtube(client, [yt("anon")])
    assert anon_client.patch(f"/jobs/{job_id}", json={"carpeta": "X"}).status_code == 401


# --- Temas del tracklist: elegir cuáles se cortan ---------------------------------------------------------------

CHAPTERS = [{"inicio_s": 0, "fin_s": None, "titulo": "Intro"}, {"inicio_s": 2, "fin_s": None, "titulo": "Medio"},
            {"inicio_s": 4, "fin_s": None, "titulo": "Final"}]


def test_selected_chapters_fija_el_fin_de_cada_tema():
    from core.web_jobs import selected_chapters

    assert selected_chapters(CHAPTERS, [1]) == [
        {"inicio_s": 0, "fin_s": 2, "titulo": "Intro"},  # no se alarga hasta el tema 3
        {"inicio_s": 4, "fin_s": None, "titulo": "Final"},
    ]
    assert selected_chapters(CHAPTERS, [])[0]["fin_s"] == 2


def test_excluir_temas_del_tracklist(client, fake_engine, downloads_root):
    url = yt("temas")
    fake_engine.behavior[f"{url}#seconds"] = 6.0
    fake_engine.resolved[url] = {"origen": "youtube", "title": "Set", "artist": "DJ", "duration_s": 6, "disponible": True,
                                 "motivo": None, "candidatos": [], "chapters": CHAPTERS}
    job_id = create_youtube(client, [url], carpeta="Sets", tracklist={"cortar": False, "guardar_completo": False})
    track = preview_ready(client, job_id)["tracks"][0]
    assert all(t["incluido"] for t in track["tracklist"]["temas"])

    r = client.patch(f"/jobs/{job_id}/tracks/{track['track_id']}", json={"accion": "tracklist", "cortar": True, "excluidos": [1, 99]})
    assert r.status_code == 200, r.text
    tracklist = r.json()["tracklist"]
    assert tracklist["cortar"] is True
    assert [t["incluido"] for t in tracklist["temas"]] == [True, False, True]

    # No se pueden quitar todos.
    r = client.patch(f"/jobs/{job_id}/tracks/{track['track_id']}", json={"accion": "tracklist", "excluidos": [0, 1, 2]})
    assert r.status_code == 409

    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, track["track_id"], {"successful"})
    folder = downloads_root / "prueba" / "sets" / "Set"
    assert sorted(p.name for p in folder.glob("*.mp3")) == ["01 - Intro.mp3", "03 - Final.mp3"]  # numerados según el tracklist

    # La biblioteca y los recientes muestran la sublista con lo que se excluyó.
    library = client.get("/library/folders/sets").json()["tracks"][0]
    assert [t["incluido"] for t in library["tracklist"]["temas"]] == [True, False, True]
    recent = next(t for t in client.get("/tracks/recent?limit=50").json()["items"] if t["track_id"] == track["track_id"])
    assert recent["tracklist"]["cortar"] is True
