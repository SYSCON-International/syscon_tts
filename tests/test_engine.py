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

    def __init__(self, model_path):
        self.model_path = model_path

    @classmethod
    def load(cls, model_path, config_path=None):
        cls.loads += 1
        return cls(model_path)

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


@pytest.fixture
def fake_piper(monkeypatch):
    FakePiperVoice.loads = 0
    FakePiperVoice.peak_concurrency = 0
    FakePiperVoice._in_flight = 0
    module = types.ModuleType("piper")
    module.PiperVoice = FakePiperVoice
    monkeypatch.setitem(sys.modules, "piper", module)
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
