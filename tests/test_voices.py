import json
import os
from pathlib import Path

import pytest

from syscon_tts.config import DEFAULT_VOICE, default_manifest_path
from syscon_tts.voices import (
    PERMISSIVE_LICENSES,
    UnknownVoiceError,
    VoiceError,
    VoiceProfile,
    VoiceRegistry,
    license_key,
    normalize_language,
    quality_from_model_name,
)


def _write_manifest(tmp_path: Path, name: str = "voices.json", voices=None) -> Path:
    manifest = {
        "voices": voices
        or [
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
    p = tmp_path / name
    p.write_text(json.dumps(manifest), encoding="utf-8")
    return p


def _install(voices_dir: Path, stem: str, language: str = "en_US") -> None:
    """Drop a model/config pair on disk the way a provisioned host has them."""
    voices_dir.mkdir(parents=True, exist_ok=True)
    (voices_dir / f"{stem}.onnx").write_bytes(b"not-a-real-model")
    (voices_dir / f"{stem}.onnx.json").write_text(
        json.dumps({"language": {"code": language}, "dataset": stem.split("-")[1]}),
        encoding="utf-8",
    )


# -- bundled catalogue -----------------------------------------------------


def test_bundled_manifest_ships_with_the_package(tmp_path):
    # The manifest must resolve from package data, not the source tree, or the
    # installed wheel breaks on import.
    assert default_manifest_path().is_file()
    reg = VoiceRegistry.from_manifest(default_manifest_path(), tmp_path)
    ids = {v.id for v in reg.all()}
    assert DEFAULT_VOICE in ids
    assert len(reg.all()) >= 6
    assert len({v.language for v in reg.all()}) >= 3


def test_bundled_manifest_covers_every_apu_ui_locale(tmp_path):
    # The APU offers en-US, es-MX, zh-Hans and zh-Hant (base_settings.LANGUAGES).
    # A locale with no voice is a site that cannot make announcements.
    reg = VoiceRegistry.from_manifest(default_manifest_path(), tmp_path)
    for locale in ("en-us", "es-mx", "zh-hans", "zh-hant"):
        assert reg.for_language(locale), f"no voice for {locale}"


def test_bundled_manifest_entries_have_download_urls():
    reg = VoiceRegistry.from_manifest(default_manifest_path(), Path("."))
    for profile in reg.all():
        assert profile.model_url, f"{profile.id} has no model_url"
        assert profile.config_url, f"{profile.id} has no config_url"
        # A URL pointing at a different file than `model` names would download
        # successfully and then fail to load, which is a miserable way to find
        # a typo.
        assert profile.model_url.endswith(profile.model), profile.id
        assert profile.config_url.endswith(profile.config), profile.id


def test_bundled_manifest_names_are_ascii():
    # A UTF-8 em dash written through a cp1252 round-trip shipped as garbled
    # voice names in 0.0.1; these strings reach operator-facing UI.
    reg = VoiceRegistry.from_manifest(default_manifest_path(), Path("."))
    for profile in reg.all():
        assert profile.name.isascii(), f"{profile.id}: {profile.name!r}"


def test_bundled_manifest_ids_are_stable_and_well_formed():
    # Ids are stored in the APU's settings table. Renaming one silently breaks
    # every site already using it, so once the APU ships, ids may only be added
    # to. (es_mx_ald_x_low was withdrawn in 0.0.3, before any APU stored it.)
    released = {
        "en_us_kristin",
        "en_us_john",
        "es_mx_ald",
        "es_es_davefx",
        "zh_cn_huayan",
        "fr_fr_siwis",
        "de_de_thorsten",
    }
    reg = VoiceRegistry.from_manifest(default_manifest_path(), Path("."))
    ids = {p.id for p in reg.all()}
    assert released <= ids, f"released ids removed: {released - ids}"
    for voice_id in ids:
        assert voice_id.replace("_", "").isalnum() and voice_id.islower(), voice_id


def test_default_voice_is_licensed_for_commercial_use():
    # PlantStar ships to paying customers; the out-of-the-box voice must not
    # need a legal conversation first.
    reg = VoiceRegistry.from_manifest(default_manifest_path(), Path("."))
    assert not reg.get(DEFAULT_VOICE).requires_license_review


def test_license_review_voices_explain_themselves():
    reg = VoiceRegistry.from_manifest(default_manifest_path(), Path("."))
    for profile in reg.all():
        assert profile.license, profile.id
        if profile.requires_license_review:
            assert profile.notes, (
                f"{profile.id} needs a license review but says nothing about why"
            )
        else:
            assert license_key(profile.license) in PERMISSIVE_LICENSES


# -- language handling -----------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("en", "en_US"),
        ("en-us", "en_US"),
        ("en_US", "en_US"),
        ("EN-US", "en_US"),
        # The APU's UI locales for Chinese are script tags, and Piper publishes
        # one Mandarin voice set for both.
        ("zh-hans", "zh_CN"),
        ("zh-hant", "zh_CN"),
        ("zh-Hant-TW", "zh_CN"),
        ("zh", "zh_CN"),
        # PlantStar's Spanish is Mexican Spanish, so bare "es" resolves there
        # rather than to Castilian.
        ("es", "es_MX"),
        ("es-mx", "es_MX"),
        ("es_ES", "es_ES"),
        # Unknown languages pass through instead of raising: a site may add a
        # voice this package has never heard of.
        ("sw-ke", "sw_KE"),
        ("", ""),
        # Separators alone hold no language; this used to raise IndexError.
        ("-", ""),
        ("_-_", ""),
        # A script subtag is dropped unless an alias gives it meaning; the
        # region after it is kept.
        ("sr-Latn-RS", "sr_RS"),
        ("sr-Latn", "sr"),
        ("es-Latn-MX", "es_MX"),
        # POSIX locale names carry a codeset and a modifier.
        ("en_US.UTF-8", "en_US"),
        ("es_MX@euro", "es_MX"),
        ("de_DE.UTF-8@euro", "de_DE"),
        (".UTF-8", ""),
    ],
)
def test_normalize_language(given, expected):
    assert normalize_language(given) == expected


def test_for_language_prefers_the_exact_locale(tmp_path):
    reg = VoiceRegistry.from_manifest(default_manifest_path(), tmp_path)
    matches = reg.for_language("es-mx")
    assert matches[0].language == "es_MX"
    # Castilian still shows up, just after the Mexican voices -- better a
    # wrong accent than no announcement.
    assert any(p.language == "es_ES" for p in matches)


def test_default_for_language_prefers_installed_voices(tmp_path):
    voices_dir = tmp_path / "voices"
    _install(voices_dir, "es_ES-davefx-medium", "es_ES")
    reg = VoiceRegistry.from_manifest(default_manifest_path(), voices_dir)
    # es_MX is the closer match but is not on disk, so the installed Castilian
    # voice wins over a voice that cannot speak.
    assert reg.default_for_language("es").id == "es_es_davefx"


def test_default_for_language_falls_back_to_catalogue_order(tmp_path):
    reg = VoiceRegistry.from_manifest(default_manifest_path(), tmp_path)
    assert reg.default_for_language("es-mx").id == "es_mx_ald"


def test_default_for_unknown_language_raises(tmp_path):
    reg = VoiceRegistry.from_manifest(default_manifest_path(), tmp_path)
    with pytest.raises(UnknownVoiceError):
        reg.default_for_language("ja-jp")


# -- overlay manifests and disk discovery ----------------------------------


def test_extra_manifest_adds_and_replaces_entries(tmp_path):
    base = _write_manifest(tmp_path, "base.json")
    overlay = _write_manifest(
        tmp_path,
        "overlay.json",
        voices=[
            {
                "id": "test_voice",  # replaces the base entry
                "name": "Site Voice",
                "language": "es_MX",
                "gender": "male",
                "model": "site.onnx",
                "config": "site.onnx.json",
                "license": "CC0",
            },
            {
                "id": "extra_voice",  # and adds a new one
                "name": "Extra Voice",
                "language": "de_DE",
                "gender": "male",
                "model": "extra.onnx",
                "config": "extra.onnx.json",
            },
        ],
    )
    reg = VoiceRegistry.from_manifest(base, tmp_path, extra_manifest=overlay)
    assert reg.get("test_voice").name == "Site Voice"
    assert reg.get("test_voice").language == "es_MX"
    assert reg.has("extra_voice")


def test_models_on_disk_are_discovered(tmp_path):
    # The point of per-deployment voices: drop the files in, get a voice, with
    # no new release of this package.
    voices_dir = tmp_path / "voices"
    _install(voices_dir, "ko_KO-custom-medium", "ko_KO")
    reg = VoiceRegistry.from_manifest(_write_manifest(tmp_path), voices_dir)

    profile = reg.get("ko_ko_custom_medium")
    assert profile.source == "disk"
    assert profile.language == "ko_KO"
    assert profile.quality == "medium"
    assert reg.is_installed(profile)
    # A site that put the file there has already made the licensing call.
    assert not profile.requires_license_review
    assert reg.for_language("ko")


@pytest.mark.parametrize(
    "config_bytes",
    [b"null", b"[]", b'"text"', "{}".encode("utf-16"), b"{not json", b'{"dataset": 7}'],
)
def test_one_malformed_config_does_not_break_the_registry(tmp_path, config_bytes):
    # A stray file in the voices directory must cost at most its own metadata,
    # never every other voice: the registry backs all synthesis.
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    (voices_dir / "ko_KO-odd-medium.onnx").write_bytes(b"not-a-real-model")
    (voices_dir / "ko_KO-odd-medium.onnx.json").write_bytes(config_bytes)
    reg = VoiceRegistry.from_manifest(_write_manifest(tmp_path), voices_dir)

    assert reg.has("test_voice")
    profile = reg.get("ko_ko_odd_medium")
    assert profile.language == "ko_KO"  # from the file name
    assert isinstance(profile.name, str)


def test_discovery_does_not_shadow_catalogued_voices(tmp_path):
    voices_dir = tmp_path / "voices"
    _install(voices_dir, "es_MX-ald-medium", "es_MX")
    reg = VoiceRegistry.from_manifest(default_manifest_path(), voices_dir)
    assert reg.get("es_mx_ald").source == "manifest"
    assert not reg.has("es_mx_ald_medium")


def test_model_without_config_is_not_offered(tmp_path):
    # Offering a voice that cannot load is worse than not listing it.
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    (voices_dir / "en_US-orphan-medium.onnx").write_bytes(b"x")
    reg = VoiceRegistry.from_manifest(_write_manifest(tmp_path), voices_dir)
    assert not reg.has("en_us_orphan_medium")


def test_discovery_can_be_switched_off(tmp_path):
    voices_dir = tmp_path / "voices"
    _install(voices_dir, "en_US-custom-medium")
    reg = VoiceRegistry.from_manifest(
        _write_manifest(tmp_path), voices_dir, discover=False
    )
    assert not reg.has("en_us_custom_medium")


# -- basics ----------------------------------------------------------------


@pytest.mark.parametrize(
    "model,expected",
    [
        ("en_US-kristin-medium.onnx", "medium"),
        ("es_MX-ald-x_low.onnx", "x_low"),
        ("zh_CN-huayan-medium.onnx.json", "medium"),
        ("whatever.onnx", "unknown"),
    ],
)
def test_quality_from_model_name(model, expected):
    assert quality_from_model_name(model) == expected


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
        "quality": "unknown",
        "license": "unknown",
        "requires_license_review": True,
        "source": "manifest",
        "installed": False,
    }


# -- licenses ----------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    ["CC-BY 4.0", "CC BY-4.0", "cc_by_4.0", "CC BY 4.0 ", "CC0-1.0", "Public-Domain",
     "https://unlicense.org/", "The Unlicense",
     "https://creativecommons.org/licenses/by/4.0/"],
)
def test_model_card_spellings_of_permissive_licenses_clear_review(spelling):
    assert license_key(spelling) in PERMISSIVE_LICENSES


@pytest.mark.parametrize(
    "spelling",
    ["CC BY-NC-SA 4.0", "CC-BY-NC 4.0", "unknown", "", "see dataset URL",
     "https://creativecommons.org/licenses/by-nc/4.0/"],
)
def test_restrictive_or_unknown_licenses_still_need_review(spelling):
    assert license_key(spelling) not in PERMISSIVE_LICENSES


def test_profile_quality_defaults_to_unknown():
    profile = VoiceProfile(
        id="x", name="x", language="en_US", gender="x", model="x.onnx",
        config="x.onnx.json",
    )
    assert profile.quality == "unknown"


# -- manifest validation -----------------------------------------------------


def _entry(**overrides):
    entry = {
        "id": "v", "name": "V", "language": "en_US",
        "model": "v.onnx", "config": "v.onnx.json",
    }
    entry.update(overrides)
    return entry


@pytest.mark.parametrize(
    "field,value",
    [("model", "../../etc/cron.d/x"), ("model", "/etc/x.onnx"),
     ("config", "sub/v.onnx.json"), ("config", "..\\v.onnx.json"),
     ("model", "C:v.onnx"), ("model", ".."), ("model", "")],
)
def test_manifest_file_names_must_not_be_paths(tmp_path, field, value):
    # download-voices runs as root and writes to voices_dir / name.
    manifest = _write_manifest(tmp_path, voices=[_entry(**{field: value})])
    with pytest.raises(VoiceError):
        VoiceRegistry.from_manifest(manifest, tmp_path)


def test_null_optional_fields_mean_not_given(tmp_path):
    manifest = _write_manifest(tmp_path, voices=[_entry(
        model_sha256=None, license=None, notes=None, quality=None, gender=None,
    )])
    profile = VoiceRegistry.from_manifest(manifest, tmp_path).get("v")
    assert profile.model_sha256 == ""
    assert profile.license == "unknown"
    assert profile.gender == "unspecified"


@pytest.mark.parametrize("digest", ["abc", "z" * 64, "a" * 63, 12])
def test_malformed_digests_are_rejected(tmp_path, digest):
    manifest = _write_manifest(tmp_path, voices=[_entry(model_sha256=digest)])
    with pytest.raises(VoiceError):
        VoiceRegistry.from_manifest(manifest, tmp_path)


def test_digest_is_normalized_to_lower_case(tmp_path):
    manifest = _write_manifest(tmp_path, voices=[_entry(model_sha256="AB" * 32)])
    assert VoiceRegistry.from_manifest(manifest, tmp_path).get("v").model_sha256 == "ab" * 32


@pytest.mark.parametrize("missing", ["id", "name", "language", "model", "config"])
def test_missing_required_field_is_a_voice_error(tmp_path, missing):
    entry = _entry()
    del entry[missing]
    manifest = _write_manifest(tmp_path, voices=[entry])
    with pytest.raises(VoiceError, match=missing):
        VoiceRegistry.from_manifest(manifest, tmp_path)


@pytest.mark.parametrize(
    "content",
    [b"[]", b"null", b'"voices"', b'{"voices": {}}', b'{"voices": ["v"]}',
     b'{"voices": [null]}', b"\xff\xfe\x00garbage", b"{not json"],
)
def test_malformed_manifests_are_voice_errors(tmp_path, content):
    manifest = tmp_path / "voices.json"
    manifest.write_bytes(content)
    with pytest.raises(VoiceError):
        VoiceRegistry.from_manifest(manifest, tmp_path)


def test_manifest_that_is_a_directory_is_a_voice_error(tmp_path):
    (tmp_path / "voices.json").mkdir()
    with pytest.raises(VoiceError):
        VoiceRegistry.from_manifest(tmp_path / "voices.json", tmp_path)


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
def test_manifest_with_a_bom_or_in_utf16_is_read(tmp_path, encoding):
    # Windows editors add a BOM; it must not reject an otherwise good file.
    manifest = tmp_path / "voices.json"
    manifest.write_text(json.dumps({"voices": [_entry()]}), encoding=encoding)
    assert VoiceRegistry.from_manifest(manifest, tmp_path).has("v")


def test_duplicate_ids_in_one_manifest_are_an_error(tmp_path):
    manifest = _write_manifest(
        tmp_path, voices=[_entry(), _entry(name="Second", model="w.onnx")]
    )
    with pytest.raises(VoiceError, match="twice"):
        VoiceRegistry.from_manifest(manifest, tmp_path)


# -- discovery ---------------------------------------------------------------


def _drop_in(voices_dir: Path, stem: str, config) -> None:
    voices_dir.mkdir(parents=True, exist_ok=True)
    (voices_dir / f"{stem}.onnx").write_bytes(b"model")
    (voices_dir / f"{stem}.onnx.json").write_text(json.dumps(config), encoding="utf-8")


def test_file_name_region_beats_a_bare_config_language(tmp_path):
    # Bare "es" would resolve to es_MX; the file name says Castilian.
    voices = tmp_path / "voices"
    _drop_in(voices, "es_ES-custom-medium", {"language": {"code": "es"}})
    reg = VoiceRegistry.from_manifest(_write_manifest(tmp_path), voices)
    assert reg.get("es_es_custom_medium").language == "es_ES"


def test_config_region_beats_an_unconventional_file_name(tmp_path):
    voices = tmp_path / "voices"
    _drop_in(voices, "plant_voice", {"language": {"code": "es_ES"}})
    reg = VoiceRegistry.from_manifest(_write_manifest(tmp_path), voices)
    assert reg.get("plant_voice").language == "es_ES"


def test_bare_config_language_beats_an_unconventional_file_name(tmp_path):
    voices = tmp_path / "voices"
    _drop_in(voices, "plant_voice", {"espeak": {"voice": "de"}})
    reg = VoiceRegistry.from_manifest(_write_manifest(tmp_path), voices)
    assert reg.get("plant_voice").language == "de_DE"


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX-only")
def test_world_writable_models_are_not_discovered(tmp_path):
    # Anyone could have replaced it, and it would load into a root process.
    voices = tmp_path / "voices"
    _drop_in(voices, "ko_KO-odd-medium", {})
    os.chmod(voices / "ko_KO-odd-medium.onnx", 0o666)
    reg = VoiceRegistry.from_manifest(_write_manifest(tmp_path), voices)
    assert not reg.has("ko_ko_odd_medium")


def test_registry_refuses_duplicate_ids(tmp_path):
    profile = VoiceProfile(
        id="x", name="x", language="en_US", gender="x", model="x.onnx",
        config="x.onnx.json",
    )
    with pytest.raises(VoiceError):
        VoiceRegistry([profile, profile], tmp_path)


# -- install state -----------------------------------------------------------


def test_empty_model_file_is_not_installed(tmp_path):
    # An interrupted copy leaves a 0-byte file that would only fail at synthesis.
    reg = VoiceRegistry.from_manifest(_write_manifest(tmp_path), tmp_path)
    (tmp_path / "test.onnx").write_bytes(b"")
    (tmp_path / "test.onnx.json").write_text("{}", encoding="utf-8")
    assert reg.is_installed(reg.get("test_voice")) is False


def test_directory_named_like_a_model_is_not_installed(tmp_path):
    reg = VoiceRegistry.from_manifest(_write_manifest(tmp_path), tmp_path)
    (tmp_path / "test.onnx").mkdir()
    (tmp_path / "test.onnx.json").write_text("{}", encoding="utf-8")
    assert reg.is_installed(reg.get("test_voice")) is False
