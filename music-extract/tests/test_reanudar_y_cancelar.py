"""Seguridad: reanudar todas las canciones de un trabajo y cancelarlo enseguida las termina a todas."""

from conftest import wait_until
from test_web_flow import create_youtube, preview_ready, yt

FINAL = {"successful", "incorrect", "failed", "skipped", "waiting_auth", "cancelada", "rechazada"}


def confirmed_with_omitted(client, fake_engine, n=4):
    """Un trabajo confirmado con todas sus canciones quitadas (omitidas) y descargas lentas."""
    urls = [yt("rea") for _ in range(n)]
    for u in urls:
        fake_engine.behavior[u] = "slow"
    job_id = create_youtube(client, urls)
    tracks = preview_ready(client, job_id)["tracks"]
    for t in tracks:
        client.patch(f"/jobs/{job_id}/tracks/{t['track_id']}", json={"accion": "quitar"})
    client.post(f"/jobs/{job_id}/confirm")
    return job_id, urls


def statuses(client, job_id):
    return [t["status"] for t in client.get(f"/jobs/{job_id}/tracks").json()]


def test_reanudar_todas_vuelve_a_encolar_omitidas_y_canceladas(client, fake_engine):
    job_id, urls = confirmed_with_omitted(client, fake_engine, n=2)
    for u in urls:
        fake_engine.behavior[u] = "ok"
    r = client.post(f"/jobs/{job_id}/resume")
    assert r.status_code == 200, r.text
    wait_until(lambda: statuses(client, job_id) == ["successful", "successful"])


def test_cancelar_mientras_se_reanudan_las_termina_a_todas(client, fake_engine):
    job_id, urls = confirmed_with_omitted(client, fake_engine, n=4)
    assert client.post(f"/jobs/{job_id}/resume").status_code == 200
    assert client.post(f"/jobs/{job_id}/cancel").status_code == 202

    # Todas terminan canceladas: ninguna queda en cola ni bajando, y ninguna llega a bajarse.
    final = wait_until(lambda: (s := statuses(client, job_id)) and all(x in FINAL for x in s) and s)
    assert set(final) == {"cancelada"}
    assert client.get(f"/jobs/{job_id}").json()["status"] == "cancelled"

    # Las que estaban en cola nunca arrancan después de cancelar.
    calls_after = len(fake_engine.calls)
    import time

    time.sleep(0.5)
    assert len(fake_engine.calls) == calls_after
    assert set(statuses(client, job_id)) == {"cancelada"}


def test_una_pendiente_en_un_trabajo_cancelado_no_arranca():
    """El worker revisa el trabajo antes de arrancar: si se canceló, la canción no se baja."""
    from core import job_service, web_jobs
    from infrastructure.persistence.database import SessionLocal
    from infrastructure.persistence.models import Job, Track

    with SessionLocal() as db:
        job_id = job_service.new_job_id()
        now = job_service._now()
        db.add(Job(job_id=job_id, type="manual-urls", amount=1, status="cancelled", origen="youtube", carpeta="X",
                   created_at=now, updated_at=now))
        track = Track(job_id=job_id, url=yt("can"), status="pending", max_attempts=1, elapsed_s=0.0, created_at=now, updated_at=now)
        db.add(track)
        db.commit()
        assert web_jobs.start_round(db, track.track_id) is False
        db.refresh(track)
        assert track.status == "cancelada"
