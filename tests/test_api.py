from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="HTTP server is an optional extra")

from fastapi.testclient import TestClient  # noqa: E402

from syscon_tts import engine as engine_mod  # noqa: E402
from syscon_tts.api import create_app  # noqa: E402
from syscon_tts.config import Settings, default_manifest_path  # noqa: E402
from syscon_tts.engine import SynthesisUnavailableError  # noqa: E402


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        voices_manifest=default_manifest_path(),
        voices_dir=tmp_path / "voices",
        alerts_dir=tmp_path / "alerts",
        host="127.0.0.1",
        port=5002,
        default_voice="en_us_kristin",
        max_text_chars=100,
    )
    app = create_app(settings)
    return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["voices_total"] >= 6
    assert "can_synthesize" in body


def test_list_voices(client):
    r = client.get("/voices")
    assert r.status_code == 200
    body = r.json()
    assert body["default"] == "en_us_kristin"
    ids = {v["id"] for v in body["voices"]}
    assert "en_us_kristin" in ids
    assert all("installed" in v for v in body["voices"])


def test_synthesize_success(client, monkeypatch):
    # Avoid loading the real Piper model; return fake audio bytes.
    def fake_synth(self, text, voice_id, fmt="wav", speed=1.0, sentence_silence=0.2):
        assert text == "Hello"
        assert voice_id == "en_us_kristin"
        return b"RIFFfakewavdata", "audio/wav"

    monkeypatch.setattr(engine_mod.TTSEngine, "synthesize", fake_synth)
    r = client.post("/synthesize", json={"text": "Hello", "voice": "en_us_kristin"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.headers["x-voice"] == "en_us_kristin"
    assert r.content == b"RIFFfakewavdata"


def test_synthesize_unknown_voice_returns_404(client):
    # No monkeypatch: registry.get() rejects the id before Piper is touched.
    r = client.post("/synthesize", json={"text": "Hi", "voice": "no_such_voice"})
    assert r.status_code == 404


def test_synthesize_text_too_long_returns_413(client):
    r = client.post("/synthesize", json={"text": "x" * 101})
    assert r.status_code == 413


@pytest.mark.parametrize(
    "payload, status",
    [
        ({}, 422),                                   # text is required
        ({"text": "hi", "format": "ogg"}, 400),      # unsupported format
        # -- speed bounds: inclusive at the edges, rejected past them. The edge
        #    cases pin `ge`/`le` so a mutation to `gt`/`lt` is caught.
        ({"text": "hi", "speed": 0.5}, 200),         # exactly the floor: allowed
        ({"text": "hi", "speed": 2.0}, 200),         # exactly the ceiling: allowed
        ({"text": "hi", "speed": 0}, 422),
        ({"text": "hi", "speed": 0.49}, 422),
        ({"text": "hi", "speed": 2.01}, 422),
        # -- silence bounds: 0 is allowed (pins `ge=0`), negative is not.
        ({"text": "hi", "sentence_silence": 0}, 200),
        ({"text": "hi", "sentence_silence": -0.1}, 422),
        ({"text": "hi", "sentence_silence": 1.0}, 200),  # exactly the cap
        ({"text": "hi", "sentence_silence": 1.01}, 422),
        # -- 413 is off-by-one sensitive: exactly the limit is allowed, one over
        #    is refused (client fixture sets max_text_chars=100).
        ({"text": "x" * 100}, 200),
        ({"text": "x" * 101}, 413),
    ],
)
def test_synthesize_rejects_bad_input(client, monkeypatch, payload, status):
    # The DoS guard (speed/silence bounds), the length cap and the format
    # mapping must be enforced exactly, not fall through to a 500. The engine is
    # stubbed so the accepted cases return 200 and the bad-format case maps to
    # 400 at the engine boundary.
    def fake_synth(self, text, voice_id, fmt="wav", speed=1.0, sentence_silence=0.2):
        from syscon_tts.engine import SynthesisError

        if fmt not in ("wav", "mp3"):
            raise SynthesisError(f"Unsupported format '{fmt}'.")
        return b"RIFFdata", "audio/wav"

    monkeypatch.setattr(engine_mod.TTSEngine, "synthesize", fake_synth)
    r = client.post("/synthesize", json=payload)
    assert r.status_code == status


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_synthesize_rejects_non_finite_speed(client, bad):
    # NaN/inf cannot be sent through a strict JSON encoder, so a client can only
    # produce them with a hand-built body -- which an attacker will. The server
    # must reject them (allow_inf_nan=False) with 422, not let NaN through to
    # the engine and 500. Posted as raw content for that reason.
    r = client.post(
        "/synthesize",
        content=f'{{"text": "hi", "speed": {bad}}}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


def test_synthesize_empty_text_is_exactly_400(client):
    # Blank/whitespace text is a client error decided before synthesis -- it
    # must be exactly 400, never a 500 or a host-dependent 501/503.
    for text in ("", "   ", "\n\t"):
        r = client.post("/synthesize", json={"text": text})
        assert r.status_code == 400, text


def test_synthesize_sheds_load_with_503_when_busy(tmp_path, monkeypatch):
    # The concurrency gate: with one slot, a second request that arrives while
    # the first is mid-synthesis gets 503 rather than stacking memory.
    import threading

    settings = Settings(
        voices_manifest=default_manifest_path(),
        voices_dir=tmp_path / "voices",
        alerts_dir=tmp_path / "alerts",
        default_voice="en_us_kristin",
        max_concurrent_synthesis=1,
    )
    app = create_app(settings)
    client = TestClient(app)

    in_synth = threading.Event()
    release = threading.Event()

    def blocking_synth(self, text, voice_id, fmt="wav", speed=1.0, sentence_silence=0.2):
        in_synth.set()
        release.wait(5)
        return b"RIFFdata", "audio/wav"

    monkeypatch.setattr(engine_mod.TTSEngine, "synthesize", blocking_synth)

    results = {}

    def first():
        results["first"] = client.post("/synthesize", json={"text": "one"}).status_code

    t = threading.Thread(target=first)
    t.start()
    assert in_synth.wait(5), "the first request never entered synthesis"
    # Second request arrives while the first holds the only slot.
    results["second"] = client.post("/synthesize", json={"text": "two"}).status_code
    release.set()
    t.join(5)

    assert results["first"] == 200
    assert results["second"] == 503


def test_synthesize_uses_default_voice(client, monkeypatch):
    captured = {}

    def fake_synth(self, text, voice_id, fmt="wav", speed=1.0, sentence_silence=0.2):
        captured["voice_id"] = voice_id
        return b"data", "audio/wav"

    monkeypatch.setattr(engine_mod.TTSEngine, "synthesize", fake_synth)
    r = client.post("/synthesize", json={"text": "Hi"})
    assert r.status_code == 200
    assert captured["voice_id"] == "en_us_kristin"


def test_synthesize_without_piper_returns_501(client, monkeypatch):
    # A host that can never synthesize is distinct from one that is merely
    # missing models (503) -- clients should not retry the former.
    def fake_synth(self, text, voice_id, fmt="wav", speed=1.0, sentence_silence=0.2):
        raise SynthesisUnavailableError("no piper here")

    monkeypatch.setattr(engine_mod.TTSEngine, "synthesize", fake_synth)
    r = client.post("/synthesize", json={"text": "Hi"})
    assert r.status_code == 501


def test_missing_voice_model_returns_503(client):
    # voices_dir is an empty tmp dir, so the model genuinely is not installed.
    # Without Piper the engine check comes first (see engine._load_voice), so
    # which status is correct depends on the host -- but it is decidable.
    r = client.post("/synthesize", json={"text": "Hi", "voice": "en_us_kristin"})
    assert r.status_code == (503 if engine_mod.piper_available() else 501)


def test_importing_the_module_builds_nothing():
    # Importing used to build an app, which read the environment and parsed
    # the manifest -- so a bad SYSCON_TTS_MANIFEST failed the *import*.
    import os
    import subprocess
    import sys

    env = dict(os.environ, SYSCON_TTS_MANIFEST=str(Path("/nonexistent/voices.json")))
    result = subprocess.run(
        [sys.executable, "-c", "import syscon_tts.api"],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_default_app_is_built_on_first_access(monkeypatch, tmp_path):
    import syscon_tts.api as api_mod

    monkeypatch.setenv("SYSCON_TTS_VOICES_DIR", str(tmp_path))
    monkeypatch.setattr(api_mod, "_default_app", None)
    app = api_mod.app
    assert app is api_mod.app  # cached after the first build
