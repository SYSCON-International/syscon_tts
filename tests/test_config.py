"""Configuration and platform-gating tests.

The packaging bug these guard against: paths used to be derived from the source
tree, which silently breaks once the package is installed into site-packages.
"""

import re
from pathlib import Path

import pytest

import syscon_tts
from syscon_tts.config import (
    PACKAGE_DIR,
    default_data_dir,
    default_manifest_path,
    load_settings,
)
from syscon_tts.engine import (
    SynthesisError,
    SynthesisUnavailableError,
    piper_available,
)

ENV_VARS = [
    "SYSCON_TTS_MANIFEST",
    "SYSCON_TTS_EXTRA_MANIFEST",
    "SYSCON_TTS_VOICES_DIR",
    "SYSCON_TTS_ALERTS_DIR",
    "SYSCON_TTS_HOST",
    "SYSCON_TTS_PORT",
    "SYSCON_TTS_DEFAULT_VOICE",
    "SYSCON_TTS_DEFAULT_VOICES",
    "SYSCON_TTS_MAX_CHARS",
    "SYSCON_TTS_MAX_LOADED_VOICES",
    "SYSCON_TTS_FILE_MODE",
    "SYSCON_TTS_DIR_MODE",
]


def _clear_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_manifest_lives_inside_the_package(monkeypatch):
    _clear_env(monkeypatch)
    manifest = default_manifest_path()
    assert manifest.is_file()
    # Must resolve relative to the installed package, not a repo checkout.
    assert PACKAGE_DIR in manifest.parents


def test_defaults_do_not_point_into_the_source_tree(monkeypatch):
    _clear_env(monkeypatch)
    settings = load_settings()
    # Voice models and generated audio must never default to somewhere inside
    # the installed package -- site-packages is not a writable data location.
    assert PACKAGE_DIR not in settings.voices_dir.parents
    assert PACKAGE_DIR not in settings.alerts_dir.parents


def test_env_vars_override_every_path(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SYSCON_TTS_VOICES_DIR", str(tmp_path / "v"))
    monkeypatch.setenv("SYSCON_TTS_ALERTS_DIR", str(tmp_path / "a"))
    monkeypatch.setenv("SYSCON_TTS_DEFAULT_VOICE", "de_de_thorsten")
    monkeypatch.setenv("SYSCON_TTS_PORT", "9999")

    settings = load_settings()

    assert settings.voices_dir == tmp_path / "v"
    assert settings.alerts_dir == tmp_path / "a"
    assert settings.default_voice == "de_de_thorsten"
    assert settings.port == 9999


def test_default_host_is_loopback(monkeypatch):
    # Binding 0.0.0.0 by default would expose the service on every interface of
    # a plant-floor appliance.
    _clear_env(monkeypatch)
    assert load_settings().host == "127.0.0.1"


def test_describe_is_json_safe(monkeypatch):
    _clear_env(monkeypatch)
    described = load_settings().describe()
    assert set(described) == {
        "voices_manifest", "extra_manifest", "voices_dir", "alerts_dir",
        "host", "port", "default_voice", "default_voices", "max_text_chars",
        "max_loaded_voices", "file_mode", "dir_mode",
    }
    assert all(
        v is None or isinstance(v, (str, int, dict)) for v in described.values()
    )


def test_settings_can_be_built_without_the_environment(monkeypatch, tmp_path):
    # The embedded caller (the APU) knows its own paths and must not have to
    # set process-wide environment variables to pass them in.
    _clear_env(monkeypatch)
    settings = load_settings(alerts_dir=tmp_path / "alerts")
    assert settings.alerts_dir == tmp_path / "alerts"
    assert settings.voices_manifest == default_manifest_path()  # untouched
    assert settings.host == "127.0.0.1"


def test_overrides_accept_strings_and_ignore_none(monkeypatch, tmp_path):
    _clear_env(monkeypatch)
    baseline = load_settings()
    settings = load_settings(voices_dir=str(tmp_path), alerts_dir=None)
    assert settings.voices_dir == tmp_path
    assert settings.alerts_dir == baseline.alerts_dir


def test_unknown_override_is_rejected(monkeypatch):
    # A typo'd keyword silently doing nothing would be discovered in
    # production, as audio in the wrong place.
    _clear_env(monkeypatch)
    with pytest.raises(TypeError):
        load_settings(alert_dir="/tmp/typo")


def test_per_language_defaults_from_env(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv(
        "SYSCON_TTS_DEFAULT_VOICES", "zh-hans=zh_cn_huayan, es-mx=es_mx_ald"
    )
    settings = load_settings()
    # Keys are normalized, so the APU's locale spelling matches at lookup time.
    assert settings.default_voices == {
        "zh_CN": "zh_cn_huayan",
        "es_MX": "es_mx_ald",
    }


def test_malformed_per_language_defaults_raise(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SYSCON_TTS_DEFAULT_VOICES", "zh_cn_huayan")
    with pytest.raises(ValueError):
        load_settings()


def test_file_mode_is_read_as_octal(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("SYSCON_TTS_FILE_MODE", "640")
    assert load_settings().file_mode == 0o640


def test_default_data_dir_is_absolute():
    assert default_data_dir().is_absolute()


# -- platform gating -------------------------------------------------------


def test_declared_version_matches_pyproject():
    """__init__.__version__ and pyproject's version must agree.

    They are declared in two places, and only pyproject drives the published
    distribution -- so a drift ships a package whose `--version` disagrees with
    what PyPI says, and the release workflow's tag check would not catch it.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.is_file():  # installed without the source tree
        pytest.skip("pyproject.toml not available")

    # tomllib is 3.11+; fall back to a narrow regex on older interpreters
    # rather than taking a dependency just for this check.
    try:
        import tomllib

        declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        declared = declared["project"]["version"]
    except ImportError:
        match = re.search(
            r'^version\s*=\s*"([^"]+)"',
            pyproject.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert match, "could not find version in pyproject.toml"
        declared = match.group(1)

    assert syscon_tts.__version__ == declared, (
        f"__init__.py says {syscon_tts.__version__} but pyproject.toml says "
        f"{declared}; bump both together"
    )


def test_package_imports_without_piper():
    # The whole cross-platform story depends on this: importing the package
    # must never require the Linux-only engine.
    assert syscon_tts.__version__
    assert callable(piper_available)
    assert isinstance(piper_available(), bool)


def test_unavailable_error_is_catchable_as_synthesis_error():
    # Callers that only care "synthesis failed" should not need to know about
    # the platform-specific subclass.
    assert issubclass(SynthesisUnavailableError, SynthesisError)


def test_missing_piper_is_reported_before_missing_models(tmp_path, monkeypatch):
    # On a host that can never synthesize, "voice not installed" would send the
    # operator to download models that still would not help. The engine check
    # must come first.
    import builtins

    from syscon_tts.config import Settings, default_manifest_path
    from syscon_tts.engine import TTSEngine
    from syscon_tts.voices import VoiceNotInstalledError, VoiceRegistry

    real_import = builtins.__import__

    def no_piper(name, *args, **kwargs):
        if name == "piper" or name.startswith("piper."):
            raise ImportError("simulated: no piper on this platform")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_piper)

    settings = Settings(
        voices_manifest=default_manifest_path(),
        voices_dir=tmp_path / "empty",  # no models here either
        alerts_dir=tmp_path / "alerts",
        host="127.0.0.1",
        port=5002,
        default_voice="en_us_kristin",
        max_text_chars=20000,
    )
    registry = VoiceRegistry.from_manifest(
        settings.voices_manifest, settings.voices_dir
    )
    engine = TTSEngine(registry)

    try:
        engine.synthesize_wav("hello", "en_us_kristin")
    except SynthesisUnavailableError as exc:
        assert "not available" in str(exc) or "could not be imported" in str(exc)
    except VoiceNotInstalledError:
        raise AssertionError(
            "reported missing models when the real problem is a missing engine"
        )
    else:
        raise AssertionError("expected SynthesisUnavailableError")


def test_public_api_is_exported():
    for name in (
        "AlertSynthesizer",
        "SynthesisUnavailableError",
        "ensure_alert_wav",
        "piper_available",
        "sanitize_file_name",
    ):
        assert hasattr(syscon_tts, name), name
