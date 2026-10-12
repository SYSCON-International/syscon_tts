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
from syscon_tts.engine import SynthesisError, SynthesisUnavailableError, piper_available
from syscon_tts.worker import _CRASH_SENTINEL, WorkerEngine


def _settings(tmp_path) -> Settings:
    """A worker pointed at an empty voices dir with one catalogued voice."""
    return Settings(
        voices_manifest=default_manifest_path(),
        voices_dir=tmp_path / "voices",
        alerts_dir=tmp_path / "alerts",
        default_voice="en_us_kristin",
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
    # gets a fresh worker. On a host without Piper that next request surfaces
    # as SynthesisUnavailableError, which proves a live worker answered it.
    monkeypatch.setenv("SYSCON_TTS_WORKER_SELFTEST", "1")
    engine = WorkerEngine(_settings(tmp_path))
    try:
        with pytest.raises(SynthesisError):
            engine.synthesize_wav(_CRASH_SENTINEL, "en_us_kristin")

        if not piper_available():
            with pytest.raises(SynthesisUnavailableError):
                engine.synthesize_wav("after the crash", "en_us_kristin")
        else:  # pragma: no cover - only on a provisioned Linux host
            audio = engine.synthesize_wav("after the crash", "en_us_kristin")
            assert audio.startswith(b"RIFF")
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
