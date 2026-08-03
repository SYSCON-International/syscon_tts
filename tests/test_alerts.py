"""Tests for the cache-first alerts API.

None of these need Piper, which is the point: the cache path is what lets a
Windows or macOS machine exercise the alert pipeline against audio generated
on a Linux host.
"""

import os
from pathlib import Path

import pytest

from syscon_tts.alerts import (
    AlertSynthesizer,
    InvalidAlertNameError,
    sanitize_file_name,
)
from syscon_tts.config import Settings, default_manifest_path
from syscon_tts.engine import SynthesisUnavailableError
from syscon_tts.voices import VoiceRegistry

FAKE_WAV = b"RIFF____WAVEfake"


class FakeEngine:
    """Stands in for TTSEngine; records calls so tests can assert cache hits."""

    def __init__(self, payload=FAKE_WAV, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def synthesize_wav(self, text, voice_id, speed=1.0, sentence_silence=0.2):
        self.calls.append((text, voice_id, speed, sentence_silence))
        if self.error:
            raise self.error
        return self.payload


def build(tmp_path, engine=None) -> AlertSynthesizer:
    settings = Settings(
        voices_manifest=default_manifest_path(),
        voices_dir=tmp_path / "voices",
        alerts_dir=tmp_path / "alerts",
        host="127.0.0.1",
        port=5002,
        default_voice="en_us_amy",
        max_text_chars=20000,
    )
    registry = VoiceRegistry.from_manifest(
        settings.voices_manifest, settings.voices_dir
    )
    return AlertSynthesizer(
        settings=settings, registry=registry, engine=engine or FakeEngine()
    )


# -- file naming -----------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Press 4 fault", "Press_4_fault"),
        ("  leading and trailing  ", "leading_and_trailing"),
        ('Quote"s and /slashes/', "Quotes_and_slashes"),
        ("dots.are.kept", "dots.are.kept"),
        ("hyphens-are-kept", "hyphens-are-kept"),
        ("tab\tand\nnewline", "tabandnewline"),
        (r"C:\path\to\thing", "Cpathtothing"),
        # Non-ASCII survives: the character class is Unicode-aware.
        ("Atenci\u00f3n: presi\u00f3n alta", "Atenci\u00f3n_presi\u00f3n_alta"),
        # Punctuation-only text still yields a usable name, not an error.
        ("!!! ???", "_"),
        ("Cavity pressure exceeded the limit on press 4.",
         "Cavity_pressure_exceeded_the_limit_on_press_4."),
    ],
)
def test_sanitize_matches_django_get_valid_filename(text, expected):
    # Verified byte-for-byte against django.utils.text.get_valid_filename, which
    # the APU already uses at PublicAddressWebSocketHandler.py:85. If these ever
    # diverge, the APU and this package compute different paths and every lookup
    # becomes a cache miss.
    assert sanitize_file_name(text) == expected


def test_sanitize_truncates_to_fifty_chars():
    # The APU does get_valid_filename(message)[:50].
    name = sanitize_file_name("a" * 200)
    assert len(name) == 50


@pytest.mark.parametrize("text", ["", ".", ".."])
def test_sanitize_rejects_unusable_names(text):
    # Django raises SuspiciousFileOperation for exactly these three.
    with pytest.raises(InvalidAlertNameError):
        sanitize_file_name(text)


# -- cache behaviour -------------------------------------------------------


def test_cache_miss_synthesizes_and_writes(tmp_path):
    engine = FakeEngine()
    synth = build(tmp_path, engine)

    result = synth.ensure("Press 4 fault")

    assert result.cached is False
    assert result.voice == "en_us_amy"
    assert result.path.read_bytes() == FAKE_WAV
    assert result.path.name == "Press_4_fault.wav"
    assert len(engine.calls) == 1


def test_cache_hit_does_not_touch_the_engine(tmp_path):
    engine = FakeEngine()
    synth = build(tmp_path, engine)
    synth.ensure("Press 4 fault")
    assert len(engine.calls) == 1

    again = synth.ensure("Press 4 fault")

    assert again.cached is True
    assert again.voice is None
    assert len(engine.calls) == 1, "second call should have been served from disk"


def test_cache_hit_works_without_piper(tmp_path):
    # Pre-generate the file, then use an engine that always reports Piper is
    # missing -- exactly the Windows/macOS situation.
    unavailable = FakeEngine(error=SynthesisUnavailableError("no piper"))
    synth = build(tmp_path, unavailable)
    target = tmp_path / "alerts" / "Press_4_fault.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(FAKE_WAV)

    result = synth.ensure("Press 4 fault")

    assert result.cached is True
    assert result.path == target
    assert unavailable.calls == []


def test_cache_miss_without_piper_raises(tmp_path):
    unavailable = FakeEngine(error=SynthesisUnavailableError("no piper"))
    synth = build(tmp_path, unavailable)

    with pytest.raises(SynthesisUnavailableError):
        synth.ensure("Never generated")


def test_force_regenerates_over_a_cache_hit(tmp_path):
    engine = FakeEngine(payload=b"NEW")
    synth = build(tmp_path, engine)
    target = tmp_path / "alerts" / "Press_4_fault.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"OLD")

    result = synth.ensure("Press 4 fault", force=True)

    assert result.cached is False
    assert target.read_bytes() == b"NEW"


def test_explicit_file_name_overrides_derivation(tmp_path):
    synth = build(tmp_path)
    result = synth.ensure("Any text at all", file_name="apu_supplied_name")
    assert result.path.name == "apu_supplied_name.wav"


def test_alerts_dir_override(tmp_path):
    synth = build(tmp_path)
    elsewhere = tmp_path / "public_alert_sounds"
    result = synth.ensure("Press 4 fault", alerts_dir=elsewhere)
    assert result.path.parent == elsewhere


def test_exists_reports_cache_state(tmp_path):
    synth = build(tmp_path)
    assert synth.exists("Press 4 fault") is False
    synth.ensure("Press 4 fault")
    assert synth.exists("Press 4 fault") is True


# -- durability ------------------------------------------------------------


def test_failed_synthesis_leaves_no_partial_file(tmp_path):
    # The APU checks path.exists() before pushing an audio URL to clients, so a
    # partial write would be served as a truncated alert.
    engine = FakeEngine(error=RuntimeError("boom"))
    synth = build(tmp_path, engine)

    with pytest.raises(RuntimeError):
        synth.ensure("Press 4 fault")

    alerts = tmp_path / "alerts"
    leftovers = list(alerts.glob("*")) if alerts.exists() else []
    assert leftovers == []


def test_result_is_path_like(tmp_path):
    synth = build(tmp_path)
    result = synth.ensure("Press 4 fault")
    assert Path(os.fspath(result)) == result.path
    with open(result, "rb") as handle:
        assert handle.read() == FAKE_WAV
