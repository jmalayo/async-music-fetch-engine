"""Búsquedas guardadas: lo que encontró una vista previa no se vuelve a buscar (tabla busquedas)."""

from test_mix_entero_o_partes import SET3, mix
from test_web_flow import create_youtube, preview_ready, yt


def preview(client, url, **tracklist):
    job_id = create_youtube(client, [url], tracklist=tracklist or None)
    return job_id, preview_ready(client, job_id)["tracks"][0]


def test_un_enlace_ya_buscado_no_se_vuelve_a_buscar(client, fake_engine):
    url = yt("guar")
    preview(client, url)
    _, track = preview(client, url)
    assert fake_engine.resolves.count(url) == 1
    assert track["title"] == "Video"  # lo guardado se usa tal cual


def test_volver_a_buscar_ignora_lo_guardado_y_lo_reemplaza(client, fake_engine):
    url = yt("forz")
    job_id, track = preview(client, url)
    fake_engine.resolved[url] = {"origen": "youtube", "title": "Nuevo resultado", "artist": "C", "duration_s": 90,
                                 "disponible": True, "motivo": None, "candidatos": [], "chapters": []}
    client.post(f"/jobs/{job_id}/tracks/{track['track_id']}/research")
    assert preview_ready(client, job_id)["tracks"][0]["title"] == "Nuevo resultado"
    assert fake_engine.resolves.count(url) == 2

    # La siguiente vista previa ya usa el resultado nuevo, sin buscar.
    _, again = preview(client, url)
    assert again["title"] == "Nuevo resultado" and fake_engine.resolves.count(url) == 2


def test_lo_guardado_no_vence(client, fake_engine):
    from infrastructure.persistence.database import SessionLocal
    from infrastructure.persistence.models import Busqueda

    url = yt("venc")
    preview(client, url)
    with SessionLocal() as db:
        db.get(Busqueda, url).updated_at = "2020-01-01T00:00:00+00:00"
        db.commit()
    preview(client, url)
    assert fake_engine.resolves.count(url) == 1


def test_lo_que_no_se_pudo_leer_no_se_guarda(client, fake_engine):
    url = yt("nole")
    fake_engine.resolved[url] = {"origen": "youtube", "title": None, "artist": None, "duration_s": None,
                                 "disponible": False, "motivo": "error", "candidatos": [], "chapters": []}
    preview(client, url)
    preview(client, url)
    assert fake_engine.resolves.count(url) == 2


def test_tracklist_reusa_lo_guardado_y_solo_busca_los_temas_una_vez(client, fake_engine):
    url = mix(fake_engine, "gtl", SET3)
    preview(client, url)  # sin tracklist: se guarda sin buscar los temas
    assert fake_engine.searches == []

    _, track = preview(client, url, cortar=True)  # con tracklist: no relee el video, solo busca los temas
    assert fake_engine.resolves.count(url) == 1
    assert len(fake_engine.searches) == 3 and all(t["video"] for t in track["tracklist"]["temas"])

    _, track = preview(client, url, cortar=True)  # otra vez: los temas también están guardados
    assert fake_engine.resolves.count(url) == 1 and len(fake_engine.searches) == 3
    assert all(t["video"] for t in track["tracklist"]["temas"])


def _spotify_preview(client, fake_engine, title):
    url = "https://open.spotify.com/track/" + "".join(ch for ch in title if ch.isalnum()).ljust(22, "x")[:22]
    r = client.post("/jobs", json={"origen": "spotify", "entrada": url})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    return job_id, preview_ready(client, job_id)["tracks"][0]


def test_volver_a_buscar_ofrece_dos_sugerencias_sin_cambiar_el_elegido(client, fake_engine):
    # La búsqueda normal da un candidato (score 95); las otras frases traen dos más (92 y 80).
    fake_engine.temas["Canción"] = "segundo"
    job_id, track = _spotify_preview(client, fake_engine, "Sugerencias")
    picked = track["mejor"]["url_pick"]
    assert track["sugerencias"] == []

    client.post(f"/jobs/{job_id}/tracks/{track['track_id']}/research")
    track = preview_ready(client, job_id)["tracks"][0]
    assert track["mejor"]["url_pick"] == picked  # el elegido no cambia solo
    assert len(track["sugerencias"]) == 2
    assert all(s["url"] != picked for s in track["sugerencias"])
    assert [s["score"] for s in track["sugerencias"]] == sorted((s["score"] for s in track["sugerencias"]), reverse=True)

    # Rechazar una: se va; aceptar la otra: pasa a ser el elegido y no quedan sugerencias.
    first, second = track["sugerencias"]
    r = client.patch(f"/jobs/{job_id}/tracks/{track['track_id']}", json={"accion": "rechazar_sugerencia", "url": first["url"]})
    assert [s["url"] for s in r.json()["sugerencias"]] == [second["url"]]
    r = client.patch(f"/jobs/{job_id}/tracks/{track['track_id']}", json={"accion": "aceptar_sugerencia", "url": second["url"]})
    assert r.json()["mejor"]["url_pick"] == second["url"] and r.json()["sugerencias"] == []
    assert client.patch(f"/jobs/{job_id}/tracks/{track['track_id']}", json={"accion": "aceptar_sugerencia", "url": first["url"]}).status_code == 404
