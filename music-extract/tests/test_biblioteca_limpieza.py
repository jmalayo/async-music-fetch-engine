"""Biblioteca: borrar canciones del disco (sin tocar Recientes) y renombrar o borrar carpetas."""

from test_web_flow import create_youtube, preview_ready, settled, yt


def downloaded(client, carpeta, tag="lib"):
    """Baja una canción a `carpeta` y devuelve el track."""
    job_id = create_youtube(client, [yt(tag)], carpeta=carpeta)
    track = preview_ready(client, job_id)["tracks"][0]
    client.post(f"/jobs/{job_id}/confirm")
    return settled(client, job_id, track["track_id"], {"successful"})


def recent_ids(client):
    return {t["track_id"]: t for t in client.get("/tracks/recent?limit=200").json()["items"]}


def test_borrar_canciones_libera_espacio_y_sigue_en_recientes(client, fake_engine, downloads_root):
    track = downloaded(client, "Para Borrar")
    path = downloads_root / "prueba" / "para_borrar" / "Canal - Video.mp3"
    assert path.exists()
    used_before = client.get("/library").json()["usado_bytes"]

    r = client.post("/library/tracks/delete", json={"track_ids": [track["track_id"]]})
    assert r.status_code == 200, r.text
    assert r.json()["eliminadas"] == 1 and r.json()["liberado_bytes"] > 0

    assert not path.exists()
    assert client.get("/library").json()["usado_bytes"] == used_before - r.json()["liberado_bytes"]
    assert client.get("/library/folders/para_borrar").json()["tracks"] == []

    # Recientes la conserva, marcada y sin archivo para bajar.
    recent = recent_ids(client)[track["track_id"]]
    assert recent["eliminada"] is True and recent["archivo_disponible"] is False

    # Ya no cuenta como «ya descargada»: se puede volver a bajar.
    job_id = create_youtube(client, [track["url"]])
    assert preview_ready(client, job_id)["tracks"][0]["ya_descargada"] is False

    # Borrar otra vez lo mismo: no hay nada.
    assert client.post("/library/tracks/delete", json={"track_ids": [track["track_id"]]}).status_code == 404


def test_renombrar_carpeta_mueve_archivos(client, fake_engine, downloads_root):
    track = downloaded(client, "Vieja")
    r = client.patch("/library/folders/vieja", json={"nombre": "Nueva Mia"})
    assert r.status_code == 200, r.text
    assert r.json() == {"nombre": "Nueva Mia", "slug": "nueva_mia"}

    assert not (downloads_root / "prueba" / "vieja").exists()
    assert (downloads_root / "prueba" / "nueva_mia" / "Canal - Video.mp3").exists()
    folder = client.get("/library/folders/nueva_mia").json()
    assert folder["nombre"] == "Nueva Mia" and [t["track_id"] for t in folder["tracks"]] == [track["track_id"]]
    # El archivo se sigue pudiendo bajar con la ruta nueva.
    assert client.get(f"/tracks/{track['track_id']}/file").status_code == 200
    assert client.get("/library/folders/vieja").status_code == 404


def test_renombrar_carpeta_validaciones(client, fake_engine):
    downloaded(client, "Uno")
    downloaded(client, "Dos")
    assert client.patch("/library/folders/uno", json={"nombre": "dos"}).status_code == 409
    assert client.patch("/library/folders/uno", json={"nombre": "  "}).status_code == 422
    assert client.patch("/library/folders/no_existe", json={"nombre": "X"}).status_code == 404
    # Solo cambia mayúsculas: mismo slug, nombre visible nuevo.
    assert client.patch("/library/folders/uno", json={"nombre": "UNO"}).json()["slug"] == "uno"


def test_borrar_carpeta(client, fake_engine, downloads_root):
    track = downloaded(client, "Carpeta Fuera")
    r = client.delete("/library/folders/carpeta_fuera")
    assert r.status_code == 200, r.text
    assert not (downloads_root / "prueba" / "carpeta_fuera").exists()
    assert client.get("/library/folders/carpeta_fuera").status_code == 404
    assert "carpeta_fuera" not in {f["slug"] for f in client.get("/library").json()["carpetas"]}
    assert recent_ids(client)[track["track_id"]]["eliminada"] is True


def test_no_se_borra_lo_de_otro_usuario(anon_client, fake_engine):
    assert anon_client.post("/library/tracks/delete", json={"track_ids": [1]}).status_code == 401
    assert anon_client.delete("/library/folders/x").status_code == 401
