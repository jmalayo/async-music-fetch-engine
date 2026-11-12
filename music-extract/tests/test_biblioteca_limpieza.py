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


def test_mover_canciones_a_otra_carpeta(client, fake_engine, downloads_root):
    track = downloaded(client, "Mover Origen")
    r = client.post("/library/tracks/move", json={"track_ids": [track["track_id"]], "carpeta": "Mover Destino"})
    assert r.status_code == 200, r.text
    assert r.json()["carpeta"]["slug"] == "mover_destino"

    assert not (downloads_root / "prueba" / "mover_origen" / "Canal - Video.mp3").exists()
    assert (downloads_root / "prueba" / "mover_destino" / "Canal - Video.mp3").exists()
    assert [t["track_id"] for t in client.get("/library/folders/mover_destino").json()["tracks"]] == [track["track_id"]]
    assert client.get("/library/folders/mover_origen").json()["tracks"] == []
    assert client.get(f"/tracks/{track['track_id']}/file").status_code == 200  # sigue bajándose desde la nueva
    recent = recent_ids(client)[track["track_id"]]
    assert recent["trabajo"]["carpeta"] == "Mover Destino"
    # Ya está en esa carpeta: no hay nada que mover.
    assert client.post("/library/tracks/move", json={"track_ids": [track["track_id"]], "carpeta": "Mover Destino"}).status_code == 404


def test_mover_un_tracklist_lleva_sus_temas(client, fake_engine, downloads_root):
    from test_mix_entero_o_partes import SET3, bajar, mix

    track = bajar(client, mix(fake_engine, "mvtl", SET3), "Mover Sets")
    client.post("/library/tracks/move", json={"track_ids": [track["track_id"]], "carpeta": "Sets Nuevos"})
    folder = downloads_root / "prueba" / "sets_nuevos"
    assert (folder / "Canal - Video.mp3").exists()
    assert len(list((folder / "Mix mvtl").glob("*.mp3"))) == 3
    assert not (downloads_root / "prueba" / "mover_sets" / "Mix mvtl").exists()
    assert client.get(f"/tracks/{track['track_id']}/file?tema=0").status_code == 200


def test_carpetas_favoritas_van_primero(client, fake_engine):
    downloaded(client, "Zeta Favorita")
    downloaded(client, "Alfa Normal")
    assert client.post("/library/folders/zeta_favorita/favorite", json={"favorita": True}).status_code == 200

    picker = client.get("/folders").json()["carpetas"]
    assert picker[0]["slug"] == "zeta_favorita" and picker[0]["favorita"] is True
    # En la Biblioteca no se reordena (la tarjeta se queda en su lugar), pero sí dice que es favorita.
    assert next(f for f in client.get("/library").json()["carpetas"] if f["slug"] == "zeta_favorita")["favorita"] is True
    assert client.get("/library/folders/zeta_favorita").json()["favorita"] is True

    # Renombrar no le quita la favorita.
    client.patch("/library/folders/zeta_favorita", json={"nombre": "Zeta Favorita 2"})
    assert client.get("/library/folders/zeta_favorita_2").json()["favorita"] is True
    client.post("/library/folders/zeta_favorita_2/favorite", json={"favorita": False})
    assert client.get("/library/folders/zeta_favorita_2").json()["favorita"] is False


def test_biblioteca_solo_muestra_lo_descargado(client, fake_engine):
    from test_web_flow import create_youtube, preview_ready, settled

    url = yt("fall")
    fake_engine.behavior[url] = "fail"
    job_id = create_youtube(client, [url], carpeta="Solo Bajadas")
    track = preview_ready(client, job_id)["tracks"][0]
    client.post(f"/jobs/{job_id}/confirm")
    settled(client, job_id, track["track_id"], {"failed"})
    ok = downloaded(client, "Solo Bajadas")
    assert [t["track_id"] for t in client.get("/library/folders/solo_bajadas").json()["tracks"]] == [ok["track_id"]]
