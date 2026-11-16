"""Dónde vive cada carpeta (nube, local o ambos) y cómo se sincroniza; sus datos están en la base, así que sobreviven
aunque se borre el disco temporal de descargas (por ejemplo, al recrear el contenedor)."""

import shutil


def folder(client, slug):
    return next((f for f in client.get("/folders").json()["carpetas"] if f["slug"] == slug), None)


def test_crear_y_cambiar_donde_vive_la_carpeta(client):
    r = client.post("/folders", json={"nombre": "Viaje Local", "almacenamiento": "ambos"})
    assert r.status_code == 201, r.text
    assert r.json()["almacenamiento"] == "ambos" and r.json()["sincronizacion"] == "manual"

    r = client.patch("/library/folders/viaje_local/almacenamiento", json={"almacenamiento": "local", "sincronizacion": "auto"})
    assert r.status_code == 200, r.text
    assert folder(client, "viaje_local")["almacenamiento"] == "local"
    assert client.get("/library/folders/viaje_local").json()["sincronizacion"] == "auto"

    assert client.patch("/library/folders/viaje_local/almacenamiento", json={"almacenamiento": "disquete"}).status_code == 422
    assert client.post("/folders", json={"nombre": "Otra", "sincronizacion": "a veces"}).status_code == 422


def test_por_defecto_en_la_nube_y_renombrar_conserva_todo(client):
    client.post("/folders", json={"nombre": "Para Renombrar"})
    assert folder(client, "para_renombrar")["almacenamiento"] == "nube"
    client.patch("/library/folders/para_renombrar/almacenamiento", json={"almacenamiento": "ambos"})
    client.post("/library/folders/para_renombrar/favorite", json={"favorita": True})

    assert client.patch("/library/folders/para_renombrar", json={"nombre": "Ya Renombrada"}).status_code == 200
    renamed = folder(client, "ya_renombrada")
    assert renamed["almacenamiento"] == "ambos" and renamed["favorita"] is True and renamed["nombre"] == "Ya Renombrada"
    assert folder(client, "para_renombrar") is None


def test_la_carpeta_sobrevive_aunque_se_borre_el_disco_temporal(client, downloads_root):
    client.post("/folders", json={"nombre": "Sobreviviente", "almacenamiento": "local"})
    shutil.rmtree(downloads_root / "prueba" / "sobreviviente")

    kept = folder(client, "sobreviviente")
    assert kept is not None and kept["nombre"] == "Sobreviviente" and kept["almacenamiento"] == "local"
    assert client.post("/folders", json={"nombre": "Sobreviviente"}).status_code == 409
    assert client.delete("/library/folders/sobreviviente").status_code == 200
    assert folder(client, "sobreviviente") is None
