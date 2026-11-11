from musictools.download import audio, progress
from musictools.download.progress import Reporter, TrackCancelled

from conftest import make_mp3


def test_convertir_informa_el_porcentaje(tmp_path):
    src = make_mp3(tmp_path / "in.mp3", seconds=8)
    wav = tmp_path / "in.wav"
    import subprocess
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(src), str(wav)], check=True)

    reporter = Reporter()
    seen = []
    original = reporter.update
    reporter.update = lambda **s: (seen.append(s), original(**s))
    with progress.using(reporter):
        out = audio.convert_to_mp3(wav, tmp_path / "out.mp3", duration_s=8)

    assert out.exists()
    assert seen[0]["fase"] == "convirtiendo"
    assert max(s["porcentaje"] or 0 for s in seen) >= 95
    assert seen[-1]["segundos_total"] == 8


def test_cancelar_corta_la_conversion(tmp_path):
    import pytest

    wav = tmp_path / "long.wav"
    import subprocess
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=600", str(wav)], check=True)

    reporter = Reporter()
    reporter.cancel()
    with progress.using(reporter), pytest.raises(TrackCancelled):
        audio.convert_to_mp3(wav, tmp_path / "out.mp3", duration_s=600)
    assert not (tmp_path / "out.mp3").exists()


def test_cortar_por_capitulos(tmp_path):
    src = make_mp3(tmp_path / "mix.mp3", seconds=6)
    out = audio.cut_chapters(src, [{"inicio_s": 0, "titulo": "Uno"}, {"inicio_s": 3, "titulo": "Dos: final"}], tmp_path / "temas")
    assert [p.name for p in out] == ["01 - Uno.mp3", "02 - Dos final.mp3"]
    assert 2.5 < audio.probe_duration(out[0]) < 3.5
    assert 2.5 < audio.probe_duration(out[1]) < 3.5
