"""Downloader selection rules. Nothing here touches the network."""

import json

import pytest

from syscon_tts import download as download_mod
from syscon_tts.config import default_manifest_path
from syscon_tts.download import (
    DownloadError,
    LicenseReviewRequired,
    download_voices,
)
from syscon_tts.voices import VoiceRegistry


@pytest.fixture
def fetched(monkeypatch):
    """Record which profiles would have been downloaded."""
    calls = []

    def fake_download_voice(profile, voices_dir, force=False, on_progress=None):
        calls.append(profile.id)
        return []

    monkeypatch.setattr(download_mod, "download_voice", fake_download_voice)
    return calls


@pytest.fixture
def registry(tmp_path):
    return VoiceRegistry.from_manifest(default_manifest_path(), tmp_path / "voices")


def test_bulk_download_skips_voices_needing_license_review(registry, fetched, tmp_path):
    notes = []
    download_voices(registry, tmp_path, on_progress=notes.append)

    assert "zh_cn_huayan" not in fetched
    assert fetched, "everything else should still download"
    assert any("zh_cn_huayan" in note and "license" in note for note in notes)


def test_bulk_download_can_accept_licenses(registry, fetched, tmp_path):
    download_voices(registry, tmp_path, accept_license=True)
    assert "zh_cn_huayan" in fetched


def test_named_review_voice_is_refused(registry, fetched, tmp_path):
    with pytest.raises(LicenseReviewRequired) as excinfo:
        download_voices(registry, tmp_path, voice_ids=["zh_cn_huayan"])
    # The operator has to be told what the problem actually is.
    assert "unknown" in str(excinfo.value).lower()
    assert "--accept-license" in str(excinfo.value)
    assert fetched == []


def test_named_review_voice_downloads_once_accepted(registry, fetched, tmp_path):
    download_voices(
        registry, tmp_path, voice_ids=["zh_cn_huayan"], accept_license=True
    )
    assert fetched == ["zh_cn_huayan"]


def test_language_filter_fetches_one_locale(registry, fetched, tmp_path):
    # How a site provisions only what it will announce in, instead of pulling
    # every model in the catalogue.
    download_voices(registry, tmp_path, language="en-us")
    assert set(fetched) == {"en_us_kristin", "en_us_john"}


def test_unknown_language_is_an_error(registry, fetched, tmp_path):
    with pytest.raises(DownloadError):
        download_voices(registry, tmp_path, language="ja-jp")
    assert fetched == []


def test_unknown_voice_id_raises_before_any_download(registry, fetched, tmp_path):
    with pytest.raises(Exception):
        download_voices(registry, tmp_path, voice_ids=["es_mx_ald", "nope"])
    assert fetched == []


def test_discovered_voices_do_not_break_a_bulk_download(tmp_path, fetched):
    # A site-supplied model has no download URL. Before, that raised and took
    # the whole provisioning run with it.
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    (voices_dir / "en_US-site-medium.onnx").write_bytes(b"model")
    (voices_dir / "en_US-site-medium.onnx.json").write_text(
        json.dumps({"language": {"code": "en_US"}}), encoding="utf-8"
    )
    registry = VoiceRegistry.from_manifest(default_manifest_path(), voices_dir)

    notes = []
    download_voices(registry, voices_dir, on_progress=notes.append)

    assert "en_us_site_medium" not in fetched
    assert any("en_us_site_medium" in note for note in notes)
    assert "en_us_kristin" in fetched
