"""Engine tests: model-cache bounds and thread safety.

The APU calls synthesis from a thread pool (a seconds-long CPU burn must not
block the Tornado IO loop), and runs several languages on a box with limited
RAM. Both of those are properties of this module, so both are tested here with
a stand-in for Piper rather than the real Linux-only runtime.
"""

import json
import sys
import threading
import time
import types

import pytest

from syscon_tts.engine import TTSEngine
from syscon_tts.voices import VoiceRegistry

VOICE_IDS = ["v1", "v2", "v3", "v4"]


class FakePiperVoice:
    """Minimal stand-in for piper.PiperVoice."""

    #: Bumped on every load, so tests can see cache hits and evictions.
    loads = 0
    #: Highest number of threads inside synthesize() at once, across instances.
    peak_concurrency = 0
    _in_flight = 0
    _counter_lock = threading.Lock()

    def __init__(self, config, session):
        FakePiperVoice.loads += 1
        self.config = config
        self.session = session

    def synthesize(self, text, wav_file, length_scale=1.0, sentence_silence=0.2):
        with FakePiperVoice._counter_lock:
            FakePiperVoice._in_flight += 1
            FakePiperVoice.peak_concurrency = max(
                FakePiperVoice.peak_concurrency, FakePiperVoice._in_flight
            )
        try:
            # Long enough that overlapping calls would be observed if the lock
            # were missing, short enough to keep the suite fast.
            time.sleep(0.02)
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00\x00" * 16)
        finally:
            with FakePiperVoice._counter_lock:
                FakePiperVoice._in_flight -= 1


class FakeSessionOptions:
    def __init__(self):
        self.intra_op_num_threads = 0
        self.inter_op_num_threads = 0


class FakeInferenceSession:
    def __init__(self, model_path, sess_options=None, providers=None):
        self.model_path = model_path
        self.sess_options = sess_options
        self.providers = providers


@pytest.fixture
def fake_piper(monkeypatch):
    """Stand-ins for piper, piper.config and onnxruntime.

    The engine builds the onnxruntime session itself (to set thread counts),
    so all three modules are replaced, not just ``piper``.
    """
    FakePiperVoice.loads = 0
    FakePiperVoice.peak_concurrency = 0
    FakePiperVoice._in_flight = 0
    piper = types.ModuleType("piper")
    piper.PiperVoice = FakePiperVoice
    piper_config = types.ModuleType("piper.config")
    piper_config.PiperConfig = types.SimpleNamespace(from_dict=lambda d: d)
    piper.config = piper_config
    ort = types.ModuleType("onnxruntime")
    ort.SessionOptions = FakeSessionOptions
    ort.InferenceSession = FakeInferenceSession
    monkeypatch.setitem(sys.modules, "piper", piper)
    monkeypatch.setitem(sys.modules, "piper.config", piper_config)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    return FakePiperVoice


@pytest.fixture
def registry(tmp_path):
    """Four installed voices, so eviction has something to evict."""
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    entries = []
    for voice_id in VOICE_IDS:
        (voices_dir / f"{voice_id}.onnx").write_bytes(b"model")
        (voices_dir / f"{voice_id}.onnx.json").write_text("{}", encoding="utf-8")
        entries.append(
            {
                "id": voice_id,
                "name": voice_id,
                "language": "en_US",
                "gender": "unspecified",
                "model": f"{voice_id}.onnx",
                "config": f"{voice_id}.onnx.json",
                "license": "public domain",
            }
        )
    manifest = tmp_path / "voices.json"
    manifest.write_text(json.dumps({"voices": entries}), encoding="utf-8")
    return VoiceRegistry.from_manifest(manifest, voices_dir)


def test_model_is_loaded_once_and_reused(fake_piper, registry):
    engine = TTSEngine(registry)
    engine.synthesize_wav("hello", "v1")
    engine.synthesize_wav("again", "v1")
    assert fake_piper.loads == 1


def test_cache_is_bounded(fake_piper, registry):
    # Each medium model is ~60 MB resident; an unbounded cache on a
    # multilingual site quietly eats an APU's memory.
    engine = TTSEngine(registry, max_loaded_voices=2)
    for voice_id in VOICE_IDS:
        engine.synthesize_wav("hello", voice_id)
    assert engine.loaded_voice_ids() == ["v3", "v4"]
    assert fake_piper.loads == 4


def test_eviction_is_least_recently_used(fake_piper, registry):
    engine = TTSEngine(registry, max_loaded_voices=2)
    engine.synthesize_wav("hello", "v1")
    engine.synthesize_wav("hello", "v2")
    engine.synthesize_wav("hello", "v1")  # v1 is now the most recent
    engine.synthesize_wav("hello", "v3")  # so v2 is the one to drop
    assert engine.loaded_voice_ids() == ["v1", "v3"]


def test_preload_warms_the_model(fake_piper, registry):
    engine = TTSEngine(registry)
    engine.preload("v1")
    assert engine.loaded_voice_ids() == ["v1"]
    engine.synthesize_wav("hello", "v1")
    assert fake_piper.loads == 1


def test_synthesis_on_one_voice_is_serialized(fake_piper, registry):
    # The APU calls this from a thread pool. Concurrent use of a single
    # PiperVoice has no documented guarantee, and parallel CPU burn is not a
    # win on a box that also runs the plant.
    engine = TTSEngine(registry)
    errors = []

    def speak():
        try:
            engine.synthesize_wav("hello", "v1")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=speak) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert fake_piper.peak_concurrency == 1
    assert fake_piper.loads == 1, "concurrent first calls loaded the model twice"


def test_output_is_a_wav_container(fake_piper, registry):
    audio = TTSEngine(registry).synthesize_wav("hello", "v1")
    assert audio.startswith(b"RIFF")
    assert audio[8:12] == b"WAVE"


def test_threads_default_leaves_onnxruntime_alone(fake_piper, registry):
    engine = TTSEngine(registry)
    engine.preload("v1")
    options = engine._cached("v1").session.sess_options
    assert options.intra_op_num_threads == 0  # onnxruntime's "all cores"


def test_threads_cap_reaches_the_session(fake_piper, registry):
    # Without a cap, every synthesis burst saturates every core of an APU
    # that is also doing machine monitoring.
    engine = TTSEngine(registry, threads=2)
    engine.preload("v1")
    session = engine._cached("v1").session
    assert session.sess_options.intra_op_num_threads == 2
    assert session.sess_options.inter_op_num_threads == 1
    assert session.providers == ["CPUExecutionProvider"]


# -- mp3 -----------------------------------------------------------------------


def test_mp3_without_ffmpeg_is_a_clear_error(fake_piper, registry, monkeypatch):
    from syscon_tts import engine as engine_mod
    from syscon_tts.engine import SynthesisError

    monkeypatch.setattr(engine_mod.shutil, "which", lambda name: None)
    with pytest.raises(SynthesisError, match="ffmpeg"):
        TTSEngine(registry).synthesize("hi", "v1", fmt="mp3")


def test_mp3_is_converted_through_ffmpeg(fake_piper, registry, monkeypatch):
    from syscon_tts import engine as engine_mod

    seen = {}

    def fake_run(cmd, input=None, capture_output=False):
        seen["cmd"], seen["input"] = cmd, input
        return types.SimpleNamespace(returncode=0, stdout=b"ID3mp3", stderr=b"")

    monkeypatch.setattr(engine_mod.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(engine_mod.subprocess, "run", fake_run)
    audio, media_type = TTSEngine(registry).synthesize("hi", "v1", fmt="mp3")
    assert (audio, media_type) == (b"ID3mp3", "audio/mpeg")
    assert seen["cmd"][0] == "/usr/bin/ffmpeg"
    assert seen["input"][:4] == b"RIFF"


def test_unknown_format_is_rejected(fake_piper, registry):
    from syscon_tts.engine import SynthesisError

    with pytest.raises(SynthesisError, match="Unsupported format"):
        TTSEngine(registry).synthesize("hi", "v1", fmt="ogg")
