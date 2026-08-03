import json
from pathlib import Path

import pytest

from syscon_tts.config import default_manifest_path
from syscon_tts.voices import (
    UnknownVoiceError,
    VoiceError,
    VoiceRegistry,
)


def _write_manifest(tmp_path: Path) -> Path:
    manifest = {
        "voices": [
            {
                "id": "test_voice",
                "name": "Test Voice",
                "language": "en_US",
                "gender": "female",
                "model": "test.onnx",
                "config": "test.onnx.json",
            }
        ]
    }
    p = tmp_path / "voices.json"
    p.write_text(json.dumps(manifest), encoding="utf-8")
    return p


def test_bundled_manifest_ships_with_the_package(tmp_path):
    # The manifest must resolve from package data, not the source tree, or the
    # installed wheel breaks on import.
    assert default_manifest_path().is_file()
    reg = VoiceRegistry.from_manifest(default_manifest_path(), tmp_path)
    ids = {v.id for v in reg.all()}
    assert "en_us_amy" in ids
    assert len(reg.all()) >= 6
    langs = {v.language for v in reg.all()}
    assert len(langs) >= 3


def test_bundled_manifest_entries_have_download_urls():
    reg = VoiceRegistry.from_manifest(default_manifest_path(), Path("."))
    for profile in reg.all():
        assert profile.model_url, f"{profile.id} has no model_url"
        assert profile.config_url, f"{profile.id} has no config_url"


def test_get_unknown_voice_raises(tmp_path):
    reg = VoiceRegistry.from_manifest(_write_manifest(tmp_path), tmp_path)
    with pytest.raises(UnknownVoiceError):
        reg.get("does_not_exist")


def test_is_installed_false_when_model_missing(tmp_path):
    reg = VoiceRegistry.from_manifest(_write_manifest(tmp_path), tmp_path)
    profile = reg.get("test_voice")
    assert reg.is_installed(profile) is False
    (tmp_path / "test.onnx").write_bytes(b"x")
    (tmp_path / "test.onnx.json").write_text("{}")
    assert reg.is_installed(profile) is True


def test_missing_manifest_raises(tmp_path):
    with pytest.raises(VoiceError):
        VoiceRegistry.from_manifest(tmp_path / "nope.json", tmp_path)


def test_public_dict_shape(tmp_path):
    reg = VoiceRegistry.from_manifest(_write_manifest(tmp_path), tmp_path)
    d = reg.get("test_voice").to_public_dict(installed=False)
    assert d == {
        "id": "test_voice",
        "name": "Test Voice",
        "language": "en_US",
        "gender": "female",
        "installed": False,
    }
