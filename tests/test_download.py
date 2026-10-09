"""Downloader tests. Nothing here touches the network."""

import hashlib
import io
import json
import os
import stat
import time
import urllib.error

import pytest

from syscon_tts import download as download_mod
from syscon_tts.config import default_manifest_path
from syscon_tts.download import (
    DownloadError,
    IntegrityError,
    LicenseReviewRequired,
    _fetch,
    download_voice,
    download_voices,
    verify_voice,
)
from syscon_tts.voices import UnknownVoiceError, VoiceProfile, VoiceRegistry


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
    with pytest.raises(UnknownVoiceError):
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


# -- integrity pins --------------------------------------------------------


def test_every_catalogued_asset_is_pinned(registry):
    # A URL on 'main' is a moving target: an upstream re-upload would change
    # how a voice id sounds on every newly provisioned site.
    for profile in registry.all():
        for url in (profile.model_url, profile.config_url):
            assert "/resolve/main/" not in url, url
            assert "/resolve/" in url, url
        for digest in (profile.model_sha256, profile.config_sha256):
            assert len(digest) == 64 and int(digest, 16) >= 0, profile.id


# -- fetching (urlopen replaced) -------------------------------------------


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def serve(monkeypatch):
    """Make urlopen return ``payload``, or raise ``error``."""
    state = {"payload": b"", "error": None, "urls": []}

    def fake_urlopen(url, timeout=None):
        state["urls"].append(url)
        if state["error"]:
            raise state["error"]
        return FakeResponse(state["payload"])

    monkeypatch.setattr(download_mod.urllib.request, "urlopen", fake_urlopen)
    return state


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_fetch_writes_the_file_atomically(serve, tmp_path):
    serve["payload"] = b"model bytes"
    dest = tmp_path / "voices" / "m.onnx"
    assert _fetch("https://x/m.onnx", dest, sha256=sha(b"model bytes")) == 11
    assert dest.read_bytes() == b"model bytes"
    assert list(dest.parent.glob("*.part")) == []


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX-only")
def test_fetched_models_are_readable_by_other_users(serve, tmp_path):
    # mkstemp creates 0600; a model fetched with sudo must still load in the
    # service account and in an unprivileged `doctor`.
    serve["payload"] = b"model bytes"
    dest = tmp_path / "voices" / "m.onnx"
    _fetch("https://x/m.onnx", dest)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o644


def test_fetch_refuses_bytes_that_do_not_match_the_pin(serve, tmp_path):
    serve["payload"] = b"something else entirely"
    dest = tmp_path / "m.onnx"
    with pytest.raises(IntegrityError):
        _fetch("https://x/m.onnx", dest, sha256=sha(b"model bytes"))
    # Nothing is left where the engine would load it.
    assert list(tmp_path.iterdir()) == []


def test_fetch_reports_http_errors(serve, tmp_path):
    serve["error"] = urllib.error.HTTPError("https://x", 404, "nf", {}, None)
    with pytest.raises(DownloadError, match="HTTP 404"):
        _fetch("https://x/m.onnx", tmp_path / "m.onnx")
    assert list(tmp_path.iterdir()) == []


def test_fetch_explains_network_failure(serve, tmp_path):
    serve["error"] = urllib.error.URLError("no route")
    with pytest.raises(DownloadError, match="air-gapped"):
        _fetch("https://x/m.onnx", tmp_path / "m.onnx")
    assert list(tmp_path.iterdir()) == []


def test_fetch_rejects_an_empty_response(serve, tmp_path):
    serve["payload"] = b""
    with pytest.raises(DownloadError, match="empty"):
        _fetch("https://x/m.onnx", tmp_path / "m.onnx")
    assert list(tmp_path.iterdir()) == []


def make_profile(model=b"model", config=b"{}"):
    return VoiceProfile(
        id="v", name="v", language="en_US", gender="unspecified",
        model="v.onnx", config="v.onnx.json",
        model_url="https://x/v.onnx", config_url="https://x/v.onnx.json",
        model_sha256=sha(model), config_sha256=sha(config),
    )


def test_download_voice_skips_files_already_present(serve, tmp_path):
    (tmp_path / "v.onnx").write_bytes(b"model")
    (tmp_path / "v.onnx.json").write_bytes(b"{}")
    results = download_voice(make_profile(), tmp_path)
    assert [r.skipped for r in results] == [True, True]
    assert serve["urls"] == []


def test_download_voice_force_refetches(serve, tmp_path):
    (tmp_path / "v.onnx").write_bytes(b"old")
    serve["payload"] = b"model"
    profile = make_profile(model=b"model", config=b"model")
    results = download_voice(profile, tmp_path, force=True)
    assert [r.skipped for r in results] == [False, False]
    assert (tmp_path / "v.onnx").read_bytes() == b"model"


def test_download_voice_without_url_is_an_error(tmp_path):
    profile = VoiceProfile(
        id="v", name="v", language="en_US", gender="unspecified",
        model="v.onnx", config="v.onnx.json",
    )
    with pytest.raises(DownloadError, match="no download URL"):
        download_voice(profile, tmp_path)


def test_verify_voice_reports_only_mismatches(tmp_path):
    profile = make_profile()
    (tmp_path / "v.onnx").write_bytes(b"model")
    (tmp_path / "v.onnx.json").write_bytes(b"tampered")
    problems = verify_voice(profile, tmp_path)
    assert len(problems) == 1
    assert "v.onnx.json" in problems[0]


def test_verify_voice_ignores_unpinned_and_missing_files(tmp_path):
    unpinned = VoiceProfile(
        id="v", name="v", language="en_US", gender="unspecified",
        model="v.onnx", config="v.onnx.json",
    )
    (tmp_path / "v.onnx").write_bytes(b"anything")
    assert verify_voice(unpinned, tmp_path) == []
    assert verify_voice(make_profile(), tmp_path / "empty") == []


# -- orphaned partial downloads ----------------------------------------------


def _orphan(directory, name, age_seconds, size=10):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_download_sweeps_partial_files_from_an_interrupted_run(
    registry, fetched, tmp_path
):
    voices = tmp_path / "voices"
    stale = [
        _orphan(voices, ".syscon-tts-abc.part", 2 * 3600),
        _orphan(voices, "tmpold.part", 2 * 3600),  # from 0.0.4 / 0.1.0
    ]
    live = _orphan(voices, ".syscon-tts-live.part", 5)  # another run, mid-file
    model = _orphan(voices, "en_US-kristin-medium.onnx", 2 * 3600)
    notes = []

    download_voices(registry, voices, language="en-us", on_progress=notes.append)

    assert not any(path.exists() for path in stale)
    assert live.exists() and model.exists()
    assert any("removed 2 partial download" in note for note in notes)


def test_fetch_uses_the_sweepable_temp_name(serve, tmp_path, monkeypatch):
    made = []
    real = download_mod.tempfiles.make_temp

    def spy(directory):
        fd, path = real(directory)
        made.append(path.name)
        return fd, path

    monkeypatch.setattr(download_mod.tempfiles, "make_temp", spy)
    serve["payload"] = b"model bytes"
    _fetch("https://x/m.onnx", tmp_path / "voices" / "m.onnx")
    assert made[0].startswith(".syscon-tts-") and made[0].endswith(".part")
