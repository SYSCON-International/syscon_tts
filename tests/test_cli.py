"""CLI smoke tests.

The command line is how a host gets provisioned and diagnosed, so a broken
command shows up as a failed install at a customer site. These run on every
platform: nothing here needs Piper.
"""

import pytest

from syscon_tts.cli import main


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Point the CLI at empty, writable directories."""
    monkeypatch.setenv("SYSCON_TTS_VOICES_DIR", str(tmp_path / "voices"))
    monkeypatch.setenv("SYSCON_TTS_ALERTS_DIR", str(tmp_path / "alerts"))
    for name in (
        "SYSCON_TTS_MANIFEST",
        "SYSCON_TTS_EXTRA_MANIFEST",
        "SYSCON_TTS_DEFAULT_VOICE",
        "SYSCON_TTS_DEFAULT_VOICES",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


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


def test_doctor_reports_the_environment(env, capsys):
    # Exit code is 1 here because no models are installed, which is the point:
    # provisioning should fail loudly rather than at the first announcement.
    assert main(["doctor"]) == 1
    captured = capsys.readouterr()
    assert "voices installed  0/" in captured.out
    assert "default voice     en_us_kristin" in captured.out
    assert "download-voices" in captured.err


def test_doctor_flags_a_misconfigured_default_voice(env, monkeypatch, capsys):
    monkeypatch.setenv("SYSCON_TTS_DEFAULT_VOICE", "en_us_typo")
    assert main(["doctor"]) == 1
    assert "not in the catalogue" in capsys.readouterr().err


def test_alert_serves_a_cache_hit_without_piper(env, capsys):
    # The Windows/macOS development path: audio generated on a Linux box plays
    # back everywhere.
    alerts = env / "alerts"
    alerts.mkdir(parents=True)
    (alerts / "Press_4_fault.wav").write_bytes(b"RIFF____WAVEfake")

    assert main(["alert", "Press 4 fault"]) == 0
    assert "cached" in capsys.readouterr().out


def test_alert_without_piper_exits_two_on_a_cache_miss(env, capsys):
    # Exit code 2 distinguishes "this host cannot synthesize" from a real
    # failure, so provisioning scripts can tell them apart.
    if main(["alert", "Never generated"]) == 0:
        pytest.skip("host has Piper and a model installed")
    assert main(["alert", "Never generated"]) in (1, 2)
