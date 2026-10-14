"""Worker-process isolation tests.

These run a real spawned worker, so they exercise the actual IPC and
crash-handling path -- not a stand-in. They need no Piper: the worker reports
``SynthesisUnavailableError`` on a host without it (the developer-machine
case), and a crash is triggered through the documented self-test hook, which
fires before the engine is touched. So the central security property -- a
native synthesis crash becomes a catchable error instead of killing the
caller -- is verified on every platform, including the Windows and macOS boxes
where Piper cannot run at all.
"""

import pytest

from syscon_tts.config import Settings, default_manifest_path
from syscon_tts.engine import (
    SynthesisError,
    SynthesisUnavailableError,
    SynthesizerClosedError,
    TTSEngine,
    piper_available,
)
from syscon_tts.voices import VoiceRegistry
from syscon_tts.worker import _CRASH_SENTINEL, _HANG_SENTINEL, WorkerEngine


def _settings(tmp_path, **overrides) -> Settings:
    """A worker pointed at an empty voices dir with one catalogued voice."""
    return Settings(
        voices_manifest=default_manifest_path(),
        voices_dir=tmp_path / "voices",
        alerts_dir=tmp_path / "alerts",
        default_voice="en_us_kristin",
        **overrides,
    )


@pytest.fixture
def worker(tmp_path):
    engine = WorkerEngine(_settings(tmp_path))
    try:
        yield engine
    finally:
        engine.close()


@pytest.mark.skipif(
    piper_available(),
    reason="This host can run Piper, so synthesis is not 'unavailable'.",
)
def test_unavailable_engine_propagates_as_itself(worker):
    # The worker builds the engine, the lazy Piper import fails, and the error
    # arrives on the caller side as the same class -- so ensure()'s cache
    # fallback still recognizes it.
    with pytest.raises(SynthesisUnavailableError):
        worker.synthesize_wav("hello", "en_us_kristin")


def test_a_worker_crash_becomes_synthesis_error(tmp_path, monkeypatch):
    # The whole point: a native fault in synthesis must not take down the
    # caller. The self-test hook aborts the worker the way espeak-ng's
    # segfault would.
    monkeypatch.setenv("SYSCON_TTS_WORKER_SELFTEST", "1")
    engine = WorkerEngine(_settings(tmp_path))
    try:
        with pytest.raises(SynthesisError) as excinfo:
            engine.synthesize_wav(_CRASH_SENTINEL, "en_us_kristin")
        assert "worker" in str(excinfo.value)
    finally:
        engine.close()


def test_the_worker_respawns_after_a_crash(tmp_path, monkeypatch):
    # A single poisoned message must not wedge the service: the next request
    # gets a FRESH worker. Proven by pid: the worker that answers after the
    # crash is a different process from the one that crashed. The probe asks
    # about an unknown voice -- rejected before Piper is touched -- so this runs
    # identically on Linux (Piper present, no models) and on Windows/macOS,
    # which is the leg the previous "render real audio" version failed on.
    from syscon_tts.voices import UnknownVoiceError

    monkeypatch.setenv("SYSCON_TTS_WORKER_SELFTEST", "1")
    engine = WorkerEngine(_settings(tmp_path))
    try:
        # A benign request spawns the first worker; record its pid.
        engine.loaded_voice_ids()
        first_pid = engine.worker_pid
        assert first_pid is not None

        with pytest.raises(SynthesisError):
            engine.synthesize_wav(_CRASH_SENTINEL, "en_us_kristin")
        assert engine.worker_pid is None  # the crashed worker was dropped

        with pytest.raises(UnknownVoiceError):
            engine.synthesize_wav("after the crash", "no_such_voice")
        second_pid = engine.worker_pid
        assert second_pid is not None
        assert second_pid != first_pid  # a genuinely new process answered
    finally:
        engine.close()


def test_a_hung_worker_times_out_and_is_killed(tmp_path, monkeypatch):
    # A crafted input that sends espeak-ng into a loop (rather than crashing)
    # must not block the announcement forever. The request is bounded by
    # worker_timeout; past it the worker is killed and the call raises.
    monkeypatch.setenv("SYSCON_TTS_WORKER_SELFTEST", "1")
    engine = WorkerEngine(_settings(tmp_path, worker_timeout=0.75))
    try:
        with pytest.raises(SynthesisError) as excinfo:
            engine.synthesize_wav(_HANG_SENTINEL, "en_us_kristin")
        assert "did not respond" in str(excinfo.value)
        # The worker it killed must not wedge the next request: a fresh one
        # answers (unavailable here, since this host has no Piper).
        if not piper_available():
            with pytest.raises(SynthesisUnavailableError):
                engine.synthesize_wav("after the hang", "en_us_kristin")
    finally:
        engine.close()


def test_replies_are_not_unpickled(tmp_path, monkeypatch):
    # Hardening against High #4: the caller must never run pickle on bytes the
    # worker sent, or a compromised worker could execute code in the root
    # parent. A sentinel in the reply frame proves pickle.loads is not called.
    import pickle

    exploded = {"called": False}
    real_loads = pickle.loads

    def tripwire(data, *args, **kwargs):
        exploded["called"] = True
        return real_loads(data, *args, **kwargs)

    monkeypatch.setattr(pickle, "loads", tripwire)
    engine = WorkerEngine(_settings(tmp_path))
    try:
        # loaded_voice_ids goes over the wire as a JSON reply, round trip.
        assert engine.loaded_voice_ids() == []
    finally:
        engine.close()
    assert not exploded["called"], "the caller unpickled a worker reply"


def test_worker_renders_real_audio_end_to_end():
    # C-73: the worker's *success* path -- it actually returns WAV bytes -- was
    # never exercised by any test. This needs real Piper AND an installed model,
    # so it is skipped everywhere except a provisioned box and the CI synthesis
    # job (which downloads en_us_kristin into SYSCON_TTS_VOICES_DIR first).
    if not piper_available():
        pytest.skip("this host cannot run Piper")
    from syscon_tts.config import load_settings
    from syscon_tts.voices import VoiceRegistry

    settings = load_settings(default_voice="en_us_kristin")
    reg = VoiceRegistry.from_settings(settings)
    if not reg.is_installed(reg.get("en_us_kristin")):
        pytest.skip("en_us_kristin model is not installed")

    engine = WorkerEngine(settings)
    try:
        audio = engine.synthesize_wav("Machine four fault detected.", "en_us_kristin")
        assert audio.startswith(b"RIFF")
        assert len(audio) > 1000
        # The same worker handles a second request -- the model cache stays warm
        # across requests (that is why isolation costs nothing per announcement).
        pid = engine.worker_pid
        again = engine.synthesize_wav("Second announcement.", "en_us_kristin")
        assert again.startswith(b"RIFF")
        assert engine.worker_pid == pid
    finally:
        engine.close()


def test_unknown_voice_propagates(tmp_path):
    from syscon_tts.voices import UnknownVoiceError

    engine = WorkerEngine(_settings(tmp_path))
    try:
        with pytest.raises(UnknownVoiceError):
            engine.synthesize_wav("hello", "no_such_voice")
    finally:
        engine.close()


def test_close_is_idempotent(tmp_path):
    engine = WorkerEngine(_settings(tmp_path))
    engine.close()
    engine.close()  # must not raise even though nothing was ever spawned


def test_a_closed_engine_never_spawns_another_worker(tmp_path, monkeypatch):
    # A caller still holding an engine that was replaced and closed must get
    # an error, not a fresh worker that nothing will ever stop.
    monkeypatch.setenv("SYSCON_TTS_WORKER_SELFTEST", "1")
    engine = WorkerEngine(_settings(tmp_path))
    with pytest.raises(SynthesisError):
        engine.synthesize_wav(_CRASH_SENTINEL, "en_us_kristin")  # spawns one
    engine.close()

    spawned = []
    monkeypatch.setattr(engine, "_ensure_worker", lambda: spawned.append(True))
    with pytest.raises(SynthesizerClosedError):
        engine.synthesize_wav("hello", "en_us_kristin")
    with pytest.raises(SynthesizerClosedError):
        engine.preload("en_us_kristin")
    assert engine.loaded_voice_ids() == []
    assert spawned == []
    assert engine.worker_pid is None


def test_a_closed_alert_synthesizer_refuses_to_render(tmp_path):
    from syscon_tts.alerts import AlertSynthesizer

    settings = _settings(tmp_path)
    synth = AlertSynthesizer(settings=settings, engine=TTSEngine(VoiceRegistry.from_settings(settings)))
    synth.close()
    synth.close()  # idempotent, and the in-process engine has no close()

    with pytest.raises(SynthesizerClosedError):
        synth.ensure("the plant keeps announcing")
    with pytest.raises(SynthesizerClosedError):
        synth.preload()


def test_a_closed_alert_synthesizer_still_returns_a_cache_hit(tmp_path):
    # A hit needs no engine, so a caller racing a replacement still plays it.
    from unittest import mock

    from syscon_tts.alerts import AlertSynthesizer

    settings = _settings(tmp_path)
    engine = mock.Mock()
    engine.synthesize_wav.return_value = _silent_wav()
    synth = AlertSynthesizer(settings=settings, engine=engine)
    first = synth.ensure("press four is down")
    synth.close()

    engine.close.assert_called_once_with()
    again = synth.ensure("press four is down")
    assert again.cached and again.path == first.path


def _silent_wav() -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 100)
    return buf.getvalue()


def test_alert_synthesizer_survives_a_worker_crash(tmp_path, monkeypatch):
    # Medium #20, the end-to-end path the APU actually uses: a crash in the
    # worker must surface to AlertSynthesizer.ensure() as a catchable
    # SynthesisError, never take down the caller. We force the worker engine
    # (otherwise a Piper-less host would use the in-process engine and never
    # spawn one), then drive a crash vector through ensure() the way an alert
    # would arrive. The test process standing in for the root Tornado manager
    # catches the error and keeps running -- which is the whole property.
    from syscon_tts.alerts import AlertSynthesizer

    monkeypatch.setenv("SYSCON_TTS_WORKER_SELFTEST", "1")
    settings = _settings(tmp_path)
    worker = WorkerEngine(settings)
    synth = AlertSynthesizer(settings=settings, engine=worker)
    try:
        with pytest.raises(SynthesisError):
            synth.ensure(_CRASH_SENTINEL)
        # The manager is still alive and the next alert gets a fresh worker.
        if not piper_available():
            with pytest.raises(SynthesisUnavailableError):
                synth.ensure("the plant keeps announcing")
    finally:
        worker.close()
