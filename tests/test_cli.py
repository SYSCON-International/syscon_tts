"""CLI smoke tests.

The command line is how a host gets provisioned and diagnosed, so a broken
command shows up as a failed install at a customer site. These run on every
platform: nothing here needs Piper.
"""

import json
import os
import sys
import time
import types

import pytest
from test_alerts import FAKE_WAV

from syscon_tts import cli
from syscon_tts.alerts import (
    AlertSynthesizer,
    add_fingerprint,
    alert_file_name,
    request_fingerprint,
)
from syscon_tts.cli import EXIT_UNAVAILABLE, main
from syscon_tts.config import ENV_NAMES
from syscon_tts.engine import SynthesisUnavailableError, TTSEngine


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Point the CLI at empty, writable directories."""
    for name in ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SYSCON_TTS_VOICES_DIR", str(tmp_path / "voices"))
    monkeypatch.setenv("SYSCON_TTS_ALERTS_DIR", str(tmp_path / "alerts"))
    return tmp_path


def install_fake_voice(env, voice_id="en_us_kristin"):
    """Put placeholder model files where a voice is expected."""
    voices = env / "voices"
    voices.mkdir(exist_ok=True)
    stem = {"en_us_kristin": "en_US-kristin-medium"}[voice_id]
    (voices / f"{stem}.onnx").write_bytes(b"not the real model")
    (voices / f"{stem}.onnx.json").write_text("{}", encoding="utf-8")


# -- list-voices -------------------------------------------------------------


def test_list_voices_reports_license_state(env, capsys):
    assert main(["list-voices"]) == 0
    out = capsys.readouterr().out
    assert "en_us_kristin" in out
    assert "public domain" in out
    # The one voice that must not ship unreviewed is visibly marked.
    assert "review" in out


def test_list_voices_filters_by_language(env, capsys):
    assert main(["list-voices", "--language", "zh-hant"]) == 0
    rows = [
        line for line in capsys.readouterr().out.splitlines()
        if line.startswith(("en_", "es_", "zh_", "fr_", "de_"))
    ]
    assert [row.split()[0] for row in rows] == ["zh_cn_huayan"]


def test_list_voices_rejects_a_language_with_no_voices(env, capsys):
    assert main(["list-voices", "--language", "ja-jp"]) == 1


# -- doctor --------------------------------------------------------------------


def test_doctor_reports_the_environment(env, capsys):
    # Exit code is 1 here because no models are installed, which is the point:
    # provisioning should fail loudly rather than at the first announcement.
    assert main(["doctor"]) == 1
    captured = capsys.readouterr()
    assert "voices installed  0/7" in captured.out
    assert "default voice     en_us_kristin" in captured.out
    assert "data dir" in captured.out
    assert "download-voices" in captured.err


def test_doctor_flags_a_misconfigured_default_voice(env, monkeypatch, capsys):
    monkeypatch.setenv("SYSCON_TTS_DEFAULT_VOICE", "en_us_typo")
    assert main(["doctor"]) == 1
    assert "not in the catalogue" in capsys.readouterr().err


def test_doctor_reports_partial_downloads_without_removing_them(env, capsys):
    voices = env / "voices"
    voices.mkdir()
    orphan = voices / ".syscon-tts-abc.part"
    orphan.write_bytes(b"x" * 3_000_000)
    stamp = time.time() - 2 * 3600
    os.utime(orphan, (stamp, stamp))

    main(["doctor", "--quick"])

    assert "1 partial download(s) (3 MB)" in capsys.readouterr().out
    assert orphan.exists()  # doctor usually runs unprivileged: report only


def test_doctor_rejects_a_model_that_does_not_match_its_pin(env, capsys):
    install_fake_voice(env)
    assert main(["doctor"]) == 1
    err = capsys.readouterr().err
    assert "en_US-kristin-medium.onnx does not match its pinned SHA-256" in err
    assert "download-voices --force en_us_kristin" in err


def test_doctor_quick_skips_hashing(env, capsys):
    install_fake_voice(env)
    rc = main(["doctor", "--quick"])
    assert "SHA-256" not in capsys.readouterr().err
    # Still not "Ready" on a host without Piper; on one with it, it is.
    assert rc in (0, 1)


# -- alert ---------------------------------------------------------------------


def test_alert_serves_a_cache_hit_without_piper(env, capsys):
    # The Windows/macOS development path: audio generated on a Linux box plays
    # back everywhere.
    alerts = env / "alerts"
    alerts.mkdir(parents=True)
    name = alert_file_name("Press 4 fault", "en_us_kristin")
    fingerprint = request_fingerprint("Press 4 fault", "en_us_kristin", 1.0, 0.2)
    (alerts / f"{name}.wav").write_bytes(add_fingerprint(FAKE_WAV, fingerprint))

    assert main(["alert", "Press 4 fault"]) == 0
    assert "cached" in capsys.readouterr().out


def test_alert_on_a_host_without_piper_has_its_own_exit_code(env, monkeypatch, capsys):
    # Must differ from argparse's 2, so provisioning scripts can tell
    # "this host cannot synthesize" from "the command was mistyped".
    def unavailable(self, *args, **kwargs):
        raise SynthesisUnavailableError("no piper")

    monkeypatch.setattr(AlertSynthesizer, "ensure", unavailable)
    assert main(["alert", "Never generated"]) == EXIT_UNAVAILABLE == 69


def test_alert_usage_error_is_argparse_exit_two(env):
    with pytest.raises(SystemExit) as excinfo:
        main(["alert"])
    assert excinfo.value.code == 2


def test_alert_rejects_a_traversing_file_name(env, capsys):
    assert main(["alert", "x", "--file-name", "../escaped"]) == 1
    assert "not allowed" in capsys.readouterr().err
    assert not (env / "escaped.wav").exists()


# -- speak ---------------------------------------------------------------------


def test_speak_writes_the_rendered_audio(env, monkeypatch, capsys):
    calls = []

    def fake_synthesize(self, text, voice_id, fmt="wav", speed=1.0, sentence_silence=0.2):
        calls.append((text, voice_id, fmt))
        return b"AUDIO", "audio/wav"

    monkeypatch.setattr(TTSEngine, "synthesize", fake_synthesize)
    out = env / "hello.mp3"
    assert main(["speak", "-l", "es-mx", "-o", str(out), "hola"]) == 0
    assert out.read_bytes() == b"AUDIO"
    # Format is inferred from the extension; the voice from the locale.
    assert calls == [("hola", "es_mx_ald", "mp3")]


def test_speak_reports_synthesis_failure(env, monkeypatch, capsys):
    def unavailable(self, *args, **kwargs):
        raise SynthesisUnavailableError("no piper")

    monkeypatch.setattr(TTSEngine, "synthesize", unavailable)
    assert main(["speak", "-o", str(env / "x.wav"), "hi"]) == 1
    assert "no piper" in capsys.readouterr().err


# -- download-voices -----------------------------------------------------------


def test_download_voices_passes_the_selection_through(env, monkeypatch, capsys):
    seen = {}

    def fake_download(registry, voices_dir, **kwargs):
        seen.update(kwargs, voices_dir=voices_dir)
        return []

    monkeypatch.setattr(cli, "download_voices", fake_download)
    assert main(["download-voices", "--language", "es-mx", "--accept-license"]) == 0
    assert seen["language"] == "es-mx"
    assert seen["accept_license"] is True
    assert seen["voices_dir"] == env / "voices"


def test_download_voices_reports_errors(env, monkeypatch, capsys):
    from syscon_tts.download import DownloadError

    def failing(*args, **kwargs):
        raise DownloadError("Cannot write to /var/lib/syscon-tts/voices: permission denied.")

    monkeypatch.setattr(cli, "download_voices", failing)
    assert main(["download-voices"]) == 1
    assert "permission denied" in capsys.readouterr().err


# -- serve ---------------------------------------------------------------------


def test_serve_runs_the_app_factory(env, monkeypatch, capsys):
    calls = []
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.run = lambda app, **kwargs: calls.append((app, kwargs))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    assert main(["serve", "--port", "5099"]) == 0
    app, kwargs = calls[0]
    assert app == "syscon_tts.api:create_app"
    assert kwargs["factory"] is True
    assert (kwargs["host"], kwargs["port"]) == ("127.0.0.1", 5099)


# -- benchmark -----------------------------------------------------------------


def test_benchmark_without_installed_voices_fails(env, capsys):
    assert main(["benchmark"]) == 1
    assert "download-voices" in capsys.readouterr().err


def test_benchmark_emits_json(env, monkeypatch, capsys):
    install_fake_voice(env)
    from syscon_tts import benchmark

    monkeypatch.setattr(
        benchmark, "benchmark_voice",
        lambda engine, voice_id, runs: {"voice": voice_id, "threads": engine.threads},
    )
    assert main(["benchmark", "--json", "--threads", "2"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["threads"] == 2
    assert report["results"] == [{"voice": "en_us_kristin", "threads": 2}]
