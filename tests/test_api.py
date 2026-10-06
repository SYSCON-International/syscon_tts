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
