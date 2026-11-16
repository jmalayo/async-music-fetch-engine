"""settings.py manda: sus valores llegan a la descarga y las descargas sin cuenta no quedan en el servidor."""

from test_cuentas_y_biblioteca import run_job, yt


def test_la_configuracion_llega_a_musictools():
    from config.settings import settings
    from infrastructure.external import downloader_service  # noqa: F401 — al importarse configura musictools
    from musictools.download import audio, core, ytdlp_lib

    assert core.TOP_N == settings.SPOTIFY_YOUTUBE_MATCH_LIMIT
    assert core.NOTUBE_MAX_AUDIO_DURATION_S == settings.NOTUBE_MAX_AUDIO_DURATION_S
    assert ytdlp_lib.CLIENT_CHAIN == settings.YOUTUBE_CLIENTS
    assert ytdlp_lib.SOCKET_TIMEOUT_S == settings.SOCKET_TIMEOUT_S
    assert audio.STALL_TIMEOUT_S == settings.FFMPEG_STALL_TIMEOUT_S
    assert ytdlp_lib._common_opts("c.txt", "deno")["socket_timeout"] == settings.SOCKET_TIMEOUT_S


def test_sin_cuenta_el_archivo_se_borra_al_entregarlo(anon_client, fake_engine):
    from core import folders

    job_id = run_job(anon_client, [yt("entr")])
    track = anon_client.get(f"/jobs/{job_id}/tracks").json()[0]
    assert any((folders.anon_root() / job_id).iterdir())

    assert anon_client.get(f"/tracks/{track['track_id']}/file").status_code == 200
    assert not (folders.anon_root() / job_id).exists()
    assert anon_client.get(f"/tracks/{track['track_id']}/file").status_code == 404
