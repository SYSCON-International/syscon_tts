"""Tests for the cache-first alerts API.

None of these need Piper, which is the point: the cache path is what lets a
Windows or macOS machine exercise the alert pipeline against audio generated
on a Linux host.
"""

import io
import logging
import os
import stat
import threading
import time
import wave
from pathlib import Path

import pytest

from syscon_tts import alerts as alerts_mod
from syscon_tts.alerts import (
    AlertSynthesizer,
    AlertTextTooLongError,
    InvalidAlertNameError,
    add_fingerprint,
    alert_file_name,
    read_fingerprint,
    request_fingerprint,
    sanitize_file_name,
)
from syscon_tts.config import Settings, default_manifest_path
from syscon_tts.engine import SynthesisError, SynthesisUnavailableError
from syscon_tts.voices import VoiceRegistry


def make_wav(marker: bytes = b"\x00\x00") -> bytes:
    """A real (tiny) WAV, so the fingerprint chunk has a RIFF file to join."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(marker * 8)
    return buffer.getvalue()


FAKE_WAV = make_wav()


class FakeEngine:
    """Stands in for TTSEngine; records calls so tests can assert cache hits."""

    def __init__(self, payload=FAKE_WAV, error=None, delay=0.0):
        self.payload = payload
        self.error = error
        self.delay = delay
        self.calls = []
        self.preloaded = []

    def synthesize_wav(self, text, voice_id, speed=1.0, sentence_silence=0.2):
        self.calls.append((text, voice_id, speed, sentence_silence))
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.payload

    def preload(self, voice_id):
        self.preloaded.append(voice_id)


def build(tmp_path, engine=None, **setting_kwargs) -> AlertSynthesizer:
    settings = Settings(
        voices_manifest=default_manifest_path(),
        voices_dir=tmp_path / "voices",
        alerts_dir=tmp_path / "alerts",
        default_voice="en_us_kristin",
        **setting_kwargs,
    )
    registry = VoiceRegistry.from_settings(settings)
    return AlertSynthesizer(
        settings=settings, registry=registry, engine=engine or FakeEngine()
    )


def pregenerate(tmp_path, text, voice="en_us_kristin", name=None, **kwargs):
    """Write the WAV a Linux host would have produced for ``text``."""
    speed = kwargs.get("speed", 1.0)
    silence = kwargs.get("sentence_silence", 0.2)
    target = tmp_path / "alerts" / f"{name or alert_file_name(text, voice)}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        add_fingerprint(FAKE_WAV, request_fingerprint(text, voice, speed, silence))
    )
    return target


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
    # the APU already uses at PublicAddressWebSocketHandler.py:85.
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


def test_alert_file_name_is_readable_and_fits_the_apu_limit():
    name = alert_file_name("Press 4 fault", "en_us_kristin")
    assert name.startswith("Press_4_fault_")
    assert len(name) <= 50
    assert len(alert_file_name("x" * 500, "en_us_kristin")) == 50


def test_messages_sharing_a_long_prefix_get_different_files():
    # Both sanitize to "Cavity_pressure_exceeded_the_configured_limit_on_p",
    # so a truncated name alone would announce press 4 for a press 7 alert.
    four = "Cavity pressure exceeded the configured limit on press 4"
    seven = "Cavity pressure exceeded the configured limit on press 7"
    assert sanitize_file_name(four) == sanitize_file_name(seven)
    assert alert_file_name(four, "v") != alert_file_name(seven, "v")


def test_same_text_in_two_voices_gets_different_files():
    assert alert_file_name("hola", "es_mx_ald") != alert_file_name("hola", "es_es_davefx")


def test_punctuation_only_text_still_gets_a_name():
    assert alert_file_name("!!!", "v").startswith("alert_")
    assert alert_file_name("!!! ???", "v").startswith("__")


# -- caller-supplied file names ---------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["../escaped", "..", ".", "", "a/b", "a\\b", "/abs/path", "C:evil", "x\x00y"],
)
def test_unsafe_file_names_are_rejected(tmp_path, bad):
    synth = build(tmp_path)
    with pytest.raises(InvalidAlertNameError):
        synth.ensure("x", file_name=bad)
    assert not (tmp_path / "escaped.wav").exists()


def test_safe_explicit_file_name_is_used(tmp_path):
    synth = build(tmp_path)
    result = synth.ensure("Any text at all", file_name="apu_supplied_name")
    assert result.path.name == "apu_supplied_name.wav"


# -- cache behaviour -------------------------------------------------------


def test_cache_miss_synthesizes_and_writes(tmp_path):
    engine = FakeEngine()
    synth = build(tmp_path, engine)

    result = synth.ensure("Press 4 fault")

    assert result.cached is False
    assert result.voice == "en_us_kristin"
    assert result.path.name == alert_file_name("Press 4 fault", "en_us_kristin") + ".wav"
    assert result.file_name == synth.file_name_for("Press 4 fault")
    assert len(engine.calls) == 1


def test_written_file_is_a_playable_wav_carrying_its_fingerprint(tmp_path):
    synth = build(tmp_path)
    result = synth.ensure("Press 4 fault")
    # The extra chunk must not upset a WAV reader.
    with wave.open(str(result.path), "rb") as wav_file:
        assert wav_file.getnframes() == 8
    assert read_fingerprint(result.path) == request_fingerprint(
        "Press 4 fault", "en_us_kristin", 1.0, 0.2
    )


def test_cache_hit_does_not_touch_the_engine(tmp_path):
    engine = FakeEngine()
    synth = build(tmp_path, engine)
    synth.ensure("Press 4 fault")

    again = synth.ensure("Press 4 fault")

    assert again.cached is True
    assert again.voice == "en_us_kristin"
    assert len(engine.calls) == 1, "second call should have been served from disk"


def test_cache_hit_works_without_piper(tmp_path):
    # Pre-generate the file, then use an engine that always reports Piper is
    # missing -- exactly the Windows/macOS situation.
    unavailable = FakeEngine(error=SynthesisUnavailableError("no piper"))
    synth = build(tmp_path, unavailable)
    target = pregenerate(tmp_path, "Press 4 fault")

    result = synth.ensure("Press 4 fault")

    assert result.cached is True
    assert result.path == target
    assert unavailable.calls == []


def test_cache_miss_without_piper_raises(tmp_path):
    unavailable = FakeEngine(error=SynthesisUnavailableError("no piper"))
    synth = build(tmp_path, unavailable)

    with pytest.raises(SynthesisUnavailableError):
        synth.ensure("Never generated")


def test_changing_the_voice_is_not_a_cache_hit(tmp_path):
    # Same text and same explicit file name, different voice: the old file
    # must not be announced in place of the new one.
    engine = FakeEngine()
    synth = build(tmp_path, engine)
    synth.ensure("hola", file_name="greeting", voice="es_mx_ald")

    result = synth.ensure("hola", file_name="greeting", voice="es_es_davefx")

    assert result.cached is False
    assert len(engine.calls) == 2


def test_changing_the_speed_is_not_a_cache_hit(tmp_path):
    engine = FakeEngine()
    synth = build(tmp_path, engine)
    synth.ensure("Press 4 fault")
    assert synth.ensure("Press 4 fault", speed=1.5).cached is False


def test_a_colliding_explicit_name_is_re_rendered(tmp_path):
    # The APU's old naming (get_valid_filename(msg)[:50]) collides for these
    # two. If a caller still passes such names, the second message must
    # replace the first rather than be served the first's audio.
    four = "Cavity pressure exceeded the configured limit on press 4"
    seven = "Cavity pressure exceeded the configured limit on press 7"
    engine = FakeEngine()
    synth = build(tmp_path, engine)
    synth.ensure(four, file_name=sanitize_file_name(four))

    result = synth.ensure(seven, file_name=sanitize_file_name(seven))

    assert result.cached is False
    assert engine.calls[-1][0] == seven


def test_file_without_a_fingerprint_is_re_rendered(tmp_path):
    # A WAV from an older version, or from VoiceText, cannot be vouched for.
    engine = FakeEngine()
    synth = build(tmp_path, engine)
    target = tmp_path / "alerts" / f"{synth.file_name_for('Press 4 fault')}.wav"
    target.parent.mkdir(parents=True)
    target.write_bytes(FAKE_WAV)

    assert synth.ensure("Press 4 fault").cached is False
    assert len(engine.calls) == 1


@pytest.mark.parametrize("junk", [b"", b"RIFF", b"not a wav at all", b"RIFF\xff\xff\xff\xffWAVE"])
def test_reading_a_fingerprint_from_junk_returns_none(tmp_path, junk):
    path = tmp_path / "junk.wav"
    path.write_bytes(junk)
    assert read_fingerprint(path) is None


def test_non_wav_engine_output_is_rejected(tmp_path):
    synth = build(tmp_path, FakeEngine(payload=b"not a wav"))
    with pytest.raises(SynthesisError):
        synth.ensure("Press 4 fault")


def test_force_regenerates_over_a_cache_hit(tmp_path):
    engine = FakeEngine(payload=make_wav(b"\x01\x00"))
    synth = build(tmp_path, engine)
    target = pregenerate(tmp_path, "Press 4 fault")

    result = synth.ensure("Press 4 fault", force=True)

    assert result.cached is False
    assert result.path == target
    assert len(engine.calls) == 1


def test_concurrent_requests_for_one_message_render_once(tmp_path):
    # The APU calls ensure() once per connected client for the same message,
    # all at once from a thread pool.
    engine = FakeEngine(delay=0.05)
    synth = build(tmp_path, engine)
    results = []

    def worker():
        results.append(synth.ensure("Press 4 fault"))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(engine.calls) == 1
    assert sorted(r.cached for r in results) == [False, True, True, True, True]


def test_overlong_text_is_refused_before_synthesis(tmp_path):
    # One runaway message would hold the voice lock for minutes and block
    # every other announcement in that voice.
    engine = FakeEngine()
    synth = build(tmp_path, engine, max_alert_chars=20)
    with pytest.raises(AlertTextTooLongError):
        synth.ensure("x" * 21)
    assert engine.calls == []
    assert synth.ensure("x" * 20).cached is False


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
        assert handle.read(4) == b"RIFF"


# -- permissions -----------------------------------------------------------

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="file modes are only meaningful on POSIX"
)


@posix_only
def test_generated_audio_is_readable_by_another_user(tmp_path):
    # mkstemp creates 0600 and os.replace preserves it, which on the APU means
    # the web server cannot read the WAV it was just asked to serve.
    synth = build(tmp_path)
    result = synth.ensure("Press 4 fault")
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o644


@posix_only
def test_mode_is_set_through_the_descriptor_not_the_path(tmp_path, monkeypatch):
    # The APU writes as root into a www-data-writable directory, where a
    # by-path chmod can be redirected through a symlink.
    def refuse(*args, **kwargs):
        raise AssertionError("chmod by path")

    monkeypatch.setattr(os, "chmod", refuse)
    (tmp_path / "alerts").mkdir()
    result = build(tmp_path).ensure("Press 4 fault")
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o644


@posix_only
def test_created_parent_directories_also_get_the_configured_mode(tmp_path):
    alerts = tmp_path / "a" / "b" / "alerts"
    synth = build(tmp_path, dir_mode=0o750)
    synth.ensure("Press 4 fault", alerts_dir=alerts)
    for path in (tmp_path / "a", tmp_path / "a" / "b", alerts):
        assert stat.S_IMODE(path.stat().st_mode) == 0o750


@posix_only
def test_file_mode_is_configurable(tmp_path):
    synth = build(tmp_path, file_mode=0o640)
    result = synth.ensure("Press 4 fault")
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o640


@posix_only
def test_created_alerts_directory_uses_the_configured_mode(tmp_path):
    synth = build(tmp_path, dir_mode=0o770)
    synth.ensure("Press 4 fault")
    assert stat.S_IMODE((tmp_path / "alerts").stat().st_mode) == 0o770


@posix_only
def test_existing_alerts_directory_keeps_its_permissions(tmp_path):
    # On the APU that directory is root:www-data 0770, set up by the system.
    # This package has no business rewriting it.
    alerts = tmp_path / "alerts"
    alerts.mkdir()
    os.chmod(alerts, 0o750)
    synth = build(tmp_path, dir_mode=0o700)
    synth.ensure("Press 4 fault")
    assert stat.S_IMODE(alerts.stat().st_mode) == 0o750


# -- voice selection -------------------------------------------------------


def test_language_selects_a_voice_from_the_catalogue(tmp_path):
    engine = FakeEngine()
    synth = build(tmp_path, engine)
    result = synth.ensure("Presion alta", language="es-mx")
    assert result.voice == "es_mx_ald"
    assert engine.calls[0][1] == "es_mx_ald"


def test_configured_language_default_beats_the_catalogue(tmp_path):
    synth = build(tmp_path, default_voices={"es_MX": "es_es_davefx"})
    assert synth.ensure("Presion alta", language="es-mx").voice == "es_es_davefx"


def test_language_default_keys_accept_any_locale_spelling(tmp_path):
    # Passed as a keyword, the key must be normalized exactly as the
    # SYSCON_TTS_DEFAULT_VOICES environment path normalizes it.
    synth = build(tmp_path, default_voices={"es-mx": "es_es_davefx"})
    assert synth.settings.default_voices == {"es_MX": "es_es_davefx"}
    assert synth.ensure("Presion alta", language="es-mx").voice == "es_es_davefx"


def test_explicit_voice_beats_language(tmp_path):
    synth = build(tmp_path)
    result = synth.ensure("hola", voice="de_de_thorsten", language="es-mx")
    assert result.voice == "de_de_thorsten"


def test_unservable_language_falls_back_to_the_default_voice(tmp_path):
    # Silence on a plant floor is worse than the wrong accent.
    synth = build(tmp_path)
    assert synth.ensure("hello", language="ja-jp").voice == "en_us_kristin"


def _install(voices_dir: Path, *models: str) -> None:
    voices_dir.mkdir(parents=True, exist_ok=True)
    for model in models:
        (voices_dir / model).write_bytes(b"model")
        (voices_dir / f"{model}.json").write_text("{}", encoding="utf-8")


def test_uninstalled_language_voice_falls_back_to_the_installed_default(tmp_path):
    # An es-mx site provisioned with en-us only: speak English rather than
    # raise VoiceNotInstalledError on every alert.
    _install(tmp_path / "voices", "en_US-kristin-medium.onnx")
    synth = build(tmp_path)
    assert synth.resolve_voice(language="es-mx") == "en_us_kristin"
    assert synth.ensure("Presion alta", language="es-mx").voice == "en_us_kristin"


def test_uninstalled_configured_language_default_also_falls_back(tmp_path):
    _install(tmp_path / "voices", "en_US-kristin-medium.onnx")
    synth = build(tmp_path, default_voices={"es-mx": "es_mx_ald"})
    assert synth.resolve_voice(language="es-mx") == "en_us_kristin"


def test_fallback_warning_is_logged_once_not_per_call(tmp_path, caplog, monkeypatch):
    # The APU resolves a voice for file_name_for() and again per connected
    # client, so a per-call warning repeats N+1 times per announcement.
    monkeypatch.setattr(alerts_mod, "_warned", set())
    _install(tmp_path / "voices", "en_US-kristin-medium.onnx")
    synth = build(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="syscon_tts"):
        for _ in range(4):
            synth.ensure("Presion alta", language="es-mx")
    fallbacks = [r for r in caplog.records if "falling back" in r.getMessage()]
    assert [r.levelno for r in fallbacks] == [logging.WARNING] + [logging.DEBUG] * 3


def test_installed_language_voice_is_used(tmp_path):
    _install(
        tmp_path / "voices", "en_US-kristin-medium.onnx", "es_MX-ald-medium.onnx"
    )
    synth = build(tmp_path)
    assert synth.resolve_voice(language="es-mx") == "es_mx_ald"


def test_language_voice_is_kept_when_nothing_is_installed(tmp_path):
    # A development box replaying cached audio has no models at all. Falling
    # back there would change the voice, and with it every file name.
    synth = build(tmp_path)
    assert synth.resolve_voice(language="es-mx") == "es_mx_ald"


def test_misconfigured_language_default_is_not_papered_over(tmp_path):
    _install(tmp_path / "voices", "en_US-kristin-medium.onnx")
    synth = build(tmp_path, default_voices={"es-mx": "es_mx_typo"})
    assert synth.resolve_voice(language="es-mx") == "es_mx_typo"


def test_chinese_script_tags_reach_the_mandarin_voice(tmp_path):
    synth = build(tmp_path)
    for locale in ("zh-hans", "zh-hant"):
        assert synth.ensure(
            "warning", file_name=f"warn_{locale}", language=locale
        ).voice == "zh_cn_huayan"


def test_preload_resolves_and_loads(tmp_path):
    engine = FakeEngine()
    synth = build(tmp_path, engine)
    assert synth.preload(language="es-mx") == "es_mx_ald"
    assert engine.preloaded == ["es_mx_ald"]


# -- construction ----------------------------------------------------------


def test_setting_overrides_replace_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("SYSCON_TTS_ALERTS_DIR", raising=False)
    synth = AlertSynthesizer(alerts_dir=tmp_path / "media" / "public_alert_sounds")
    assert synth.settings.alerts_dir == tmp_path / "media" / "public_alert_sounds"


def test_settings_object_and_overrides_together_is_an_error(tmp_path):
    with pytest.raises(TypeError):
        AlertSynthesizer(settings=Settings(), alerts_dir=tmp_path)


# -- errors ------------------------------------------------------------------


def test_every_ensure_error_shares_one_base():
    # The APU should catch one class, not a tuple that includes OSError.
    from syscon_tts import (
        AlertWriteError,
        SynthesisError,
        SysconTTSError,
        VoiceError,
    )
    from syscon_tts.download import DownloadError

    for cls in (
        InvalidAlertNameError, AlertWriteError, SynthesisError, VoiceError,
        DownloadError,
    ):
        assert issubclass(cls, SysconTTSError)
    assert issubclass(AlertWriteError, OSError)  # existing handlers still work


def test_write_failure_is_an_alert_write_error(tmp_path):
    (tmp_path / "alerts").write_text("a file where the directory should be")
    with pytest.raises(alerts_mod.AlertWriteError):
        build(tmp_path).ensure("Press 4 fault")


@pytest.mark.parametrize("call", ["ensure", "file_name_for"])
def test_lone_surrogate_is_a_clear_error(tmp_path, call):
    synth = build(tmp_path)
    with pytest.raises(InvalidAlertNameError, match="surrogate"):
        getattr(synth, call)("Press 4 \udc80 fault")


def test_lone_surrogate_with_an_explicit_name_is_a_clear_error(tmp_path):
    with pytest.raises(InvalidAlertNameError, match="surrogate"):
        build(tmp_path).ensure("Press \ud800", file_name="press")


def test_overlong_file_name_is_refused_before_synthesis(tmp_path):
    engine = FakeEngine()
    synth = build(tmp_path, engine)
    with pytest.raises(InvalidAlertNameError, match="too long"):
        synth.ensure("Press 4 fault", file_name="a" * 252)
    # Bytes, not characters: 84 Han characters are 252 bytes of UTF-8.
    with pytest.raises(InvalidAlertNameError, match="too long"):
        synth.ensure("Press 4 fault", file_name="\u58d3" * 84)
    assert engine.calls == []


def test_longest_allowed_file_name_is_accepted(tmp_path):
    result = build(tmp_path).ensure("Press 4 fault", file_name="a" * 251)
    assert result.path.is_file()


def test_ensure_options_are_keyword_only(tmp_path):
    with pytest.raises(TypeError):
        build(tmp_path).ensure("Press 4 fault", "name", "es-mx")


# -- locking and housekeeping ------------------------------------------------


def test_cache_hit_does_not_wait_for_a_busy_lock_stripe(tmp_path):
    # Another alert rendering under the same stripe must not hold up a hit.
    pregenerate(tmp_path, "Press 4 fault")
    synth = build(tmp_path)
    for lock in synth._path_locks:
        lock.acquire()
    results = []
    try:
        worker = threading.Thread(
            target=lambda: results.append(synth.ensure("Press 4 fault"))
        )
        worker.start()
        worker.join(timeout=5)
    finally:
        for lock in synth._path_locks:
            lock.release()
    assert results and results[0].cached


def test_orphaned_temp_files_are_swept(tmp_path):
    alerts = tmp_path / "alerts"
    alerts.mkdir()
    old = time.time() - 2 * 3600
    stale = [alerts / ".syscon-tts-abc.part", alerts / "tmpxyz.part"]
    for path in stale:
        path.write_bytes(b"half a wav")
        os.utime(path, (old, old))
    fresh = alerts / ".syscon-tts-live.part"  # a render in progress
    fresh.write_bytes(b"")
    unrelated = alerts / "notes.part"
    unrelated.write_bytes(b"")
    os.utime(unrelated, (old, old))

    build(tmp_path).ensure("Press 4 fault")

    assert not any(path.exists() for path in stale)
    assert fresh.exists() and unrelated.exists()


def test_temp_files_use_the_sweepable_prefix(tmp_path, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(alerts_mod.os, "replace", spy)
    build(tmp_path).ensure("Press 4 fault")
    assert seen[0].startswith(".syscon-tts-") and seen[0].endswith(".part")
