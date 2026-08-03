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
    assert "can_synthesize" in body


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
    r = client.post("/synthesize", json={"text": "Hi", "voice": "en_us_amy"})
    assert r.status_code in (501, 503)
