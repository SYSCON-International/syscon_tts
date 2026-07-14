from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from plantstar_tts import engine as engine_mod
from plantstar_tts.api import create_app
from plantstar_tts.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client():
    settings = Settings(
        voices_manifest=REPO_ROOT / "config" / "voices.json",
        voices_dir=REPO_ROOT / "voices",
        host="127.0.0.1",
        port=5002,
        default_voice="en_us_amy",
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


def test_list_voices(client):
    r = client.get("/voices")
    assert r.status_code == 200
    body = r.json()
    assert body["default"] == "en_us_amy"
    ids = {v["id"] for v in body["voices"]}
    assert "en_us_amy" in ids
    assert all("installed" in v for v in body["voices"])


def test_synthesize_success(client, monkeypatch):
    # Avoid loading the real Piper model; return fake audio bytes.
    def fake_synth(self, text, voice_id, fmt="wav", speed=1.0, sentence_silence=0.2):
        assert text == "Hello"
        assert voice_id == "en_us_amy"
        return b"RIFFfakewavdata", "audio/wav"

    monkeypatch.setattr(engine_mod.TTSEngine, "synthesize", fake_synth)
    r = client.post("/synthesize", json={"text": "Hello", "voice": "en_us_amy"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.headers["x-voice"] == "en_us_amy"
    assert r.content == b"RIFFfakewavdata"


def test_synthesize_unknown_voice_returns_404(client):
    # No monkeypatch: registry.get() rejects the id before Piper is touched.
    r = client.post("/synthesize", json={"text": "Hi", "voice": "no_such_voice"})
    assert r.status_code == 404


def test_synthesize_text_too_long_returns_413(client):
    r = client.post("/synthesize", json={"text": "x" * 101})
    assert r.status_code == 413


def test_synthesize_uses_default_voice(client, monkeypatch):
    captured = {}

    def fake_synth(self, text, voice_id, fmt="wav", speed=1.0, sentence_silence=0.2):
        captured["voice_id"] = voice_id
        return b"data", "audio/wav"

    monkeypatch.setattr(engine_mod.TTSEngine, "synthesize", fake_synth)
    r = client.post("/synthesize", json={"text": "Hi"})
    assert r.status_code == 200
    assert captured["voice_id"] == "en_us_amy"
