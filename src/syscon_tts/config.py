"""Runtime configuration.

Resolution order for every setting is: explicit argument > environment
variable > platform default. Nothing is derived from the source tree, so the
package behaves identically whether it is installed into ``site-packages``,
run from a checkout, or imported inside the APU's Django process.

Environment variables (all optional):

================================== =========================================
``SYSCON_TTS_MANIFEST``            Voice manifest JSON path
``SYSCON_TTS_EXTRA_MANIFEST``      Second manifest layered over the first
``SYSCON_TTS_VOICES_DIR``          Directory holding the ``.onnx`` voice models
``SYSCON_TTS_ALERTS_DIR``          Directory alert WAVs are written to
``SYSCON_TTS_DEFAULT_VOICE``       Voice used when no voice/language is named
``SYSCON_TTS_DEFAULT_VOICES``      Per-language defaults, ``es_MX=es_mx_ald,...``
``SYSCON_TTS_HOST``                Bind host for the optional HTTP server
``SYSCON_TTS_PORT``                Bind port for the optional HTTP server
``SYSCON_TTS_MAX_CHARS``           Maximum text length accepted per request
``SYSCON_TTS_MAX_LOADED_VOICES``   Voice models kept resident in memory
``SYSCON_TTS_FILE_MODE``           Mode for generated audio, e.g. ``0644``
``SYSCON_TTS_DIR_MODE``            Mode for directories this package creates
================================== =========================================

Callers that already know their paths -- the APU, which has them in Django
settings -- should skip the environment entirely and pass overrides::

    load_settings(alerts_dir=Path(settings.MEDIA_ROOT, "public_alert_sounds"))

Voice models default to ``/var/lib/syscon-tts/voices``. Deliberately *not*
under Django's ``MEDIA_ROOT``: the APU's ``backup_plantstar`` copies the whole
media tree into every backup, and voice models are large, immutable, and
re-downloadable -- they do not belong in one. Generated alert audio does live
under ``MEDIA_ROOT`` (it has to be web-served), but it is ephemeral.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

# Directory this package was installed into. Used only to locate bundled
# package data (the default voice manifest) -- never to locate user data.
PACKAGE_DIR = Path(__file__).resolve().parent

APP_NAME = "syscon-tts"

#: Voice used when the caller names neither a voice nor a language. Public
#: domain (LibriVox), so it can ship to a customer without a license review.
DEFAULT_VOICE = "en_us_kristin"

#: Mode applied to generated audio. ``tempfile.mkstemp`` creates files 0600 and
#: ``os.replace`` preserves that, which on the APU means the web server -- a
#: different user -- cannot read the WAV it was just asked to serve. Everything
#: written here is public announcement audio, so 0644 is the sane default;
#: sites wanting it group-restricted set ``SYSCON_TTS_FILE_MODE=0640``.
DEFAULT_FILE_MODE = 0o644

#: Mode applied to directories this package creates. Applied only on creation,
#: so it never fights the permissions of a directory that already exists.
DEFAULT_DIR_MODE = 0o755

#: How many Piper models stay resident. Each medium-quality model is roughly
#: 60 MB of RSS, so an unbounded cache on a multilingual site quietly grows
#: past what an APU has to spare.
DEFAULT_MAX_LOADED_VOICES = 3


def default_manifest_path() -> Path:
    """Path to the voice manifest bundled inside the wheel."""
    return PACKAGE_DIR / "data" / "voices.json"


def default_data_dir() -> Path:
    """Platform-appropriate directory for models and generated audio.

    On Linux a system-wide location is preferred when it is already present or
    creatable, because the APU runs the service from a system account; we fall
    back to the per-user XDG directory otherwise so unprivileged development
    installs still work.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / APP_NAME

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME

    system_dir = Path("/var/lib") / APP_NAME
    if _is_usable_dir(system_dir):
        return system_dir

    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return root / APP_NAME


def _is_usable_dir(path: Path) -> bool:
    """True if ``path`` exists and is writable, or could be created."""
    if path.is_dir():
        return os.access(path, os.W_OK)
    parent = path.parent
    return parent.is_dir() and os.access(parent, os.W_OK)


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


def _env_optional_path(name: str) -> Optional[Path]:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def _env_mode(name: str, default: int) -> int:
    """Read a file/directory mode, written the way chmod(1) takes it."""
    value = os.environ.get(name)
    if not value:
        return default
    try:
        # Base 0 would read "644" as decimal; modes are octal by convention.
        return int(value, 8)
    except ValueError:
        raise ValueError(
            f"{name}={value!r} is not an octal mode (expected e.g. 0644)."
        ) from None


def parse_default_voices(value: str) -> Dict[str, str]:
    """Parse ``"es_MX=es_mx_ald,zh_CN=zh_cn_huayan"`` into a mapping.

    Keys are normalized to this package's language spelling, so ``zh-hans=...``
    and ``zh_CN=...`` both land on ``zh_CN``.
    """
    from .voices import normalize_language  # local import: avoids a cycle

    mapping: Dict[str, str] = {}
    for pair in value.split(","):
        pair = pair.strip()
        if not pair:
            continue
        language, separator, voice_id = pair.partition("=")
        language = language.strip()
        voice_id = voice_id.strip()
        if not separator or not language or not voice_id:
            raise ValueError(
                f"Bad language/voice pair {pair!r}; expected 'language=voice_id'."
            )
        mapping[normalize_language(language)] = voice_id
    return mapping


@dataclass
class Settings:
    """Resolved runtime settings.

    Every field has a default, so a caller who cares about two of them writes
    two of them.
    """

    # JSON file describing the available voice profiles.
    voices_manifest: Path = field(default_factory=default_manifest_path)
    # Optional second manifest layered over the first: a new id is added, a
    # reused id replaces the bundled entry. Lets a site add its own voice
    # without forking the packaged catalogue.
    extra_manifest: Optional[Path] = None
    # Directory holding the downloaded .onnx voice models.
    voices_dir: Path = field(default_factory=lambda: default_data_dir() / "voices")
    # Directory generated alert WAVs are written to.
    alerts_dir: Path = field(default_factory=lambda: default_data_dir() / "alerts")
    host: str = "127.0.0.1"
    port: int = 5002
    default_voice: str = DEFAULT_VOICE
    # language -> voice id, for sites that announce in more than one language.
    default_voices: Dict[str, str] = field(default_factory=dict)
    max_text_chars: int = 20000
    max_loaded_voices: int = DEFAULT_MAX_LOADED_VOICES
    file_mode: int = DEFAULT_FILE_MODE
    dir_mode: int = DEFAULT_DIR_MODE

    def describe(self) -> dict:
        return {
            "voices_manifest": str(self.voices_manifest),
            "extra_manifest": str(self.extra_manifest) if self.extra_manifest else None,
            "voices_dir": str(self.voices_dir),
            "alerts_dir": str(self.alerts_dir),
            "host": self.host,
            "port": self.port,
            "default_voice": self.default_voice,
            "default_voices": dict(self.default_voices),
            "max_text_chars": self.max_text_chars,
            "max_loaded_voices": self.max_loaded_voices,
            "file_mode": f"{self.file_mode:04o}",
            "dir_mode": f"{self.dir_mode:04o}",
        }


_PATH_FIELDS = frozenset(
    {"voices_manifest", "extra_manifest", "voices_dir", "alerts_dir"}
)


def _coerce(name: str, value: Any) -> Any:
    if name in _PATH_FIELDS and value is not None:
        return Path(value).expanduser()
    if name == "default_voices" and isinstance(value, str):
        return parse_default_voices(value)
    return value


def load_settings(**overrides: Any) -> Settings:
    """Build a :class:`Settings` from the environment, then apply ``overrides``.

    ``load_settings(alerts_dir=...)`` is the intended entry point for embedded
    callers: no environment plumbing, and every field left unnamed keeps its
    default. An override of ``None`` is ignored rather than blanking the
    resolved value, so ``load_settings(voice_dir=maybe_none)`` behaves.
    """
    data_dir = default_data_dir()

    settings = Settings(
        voices_manifest=_env_path("SYSCON_TTS_MANIFEST", default_manifest_path()),
        extra_manifest=_env_optional_path("SYSCON_TTS_EXTRA_MANIFEST"),
        voices_dir=_env_path("SYSCON_TTS_VOICES_DIR", data_dir / "voices"),
        alerts_dir=_env_path("SYSCON_TTS_ALERTS_DIR", data_dir / "alerts"),
        host=os.environ.get("SYSCON_TTS_HOST", "127.0.0.1"),
        port=int(os.environ.get("SYSCON_TTS_PORT", "5002")),
        default_voice=os.environ.get("SYSCON_TTS_DEFAULT_VOICE", DEFAULT_VOICE),
        default_voices=parse_default_voices(
            os.environ.get("SYSCON_TTS_DEFAULT_VOICES", "")
        ),
        max_text_chars=int(os.environ.get("SYSCON_TTS_MAX_CHARS", "20000")),
        max_loaded_voices=int(
            os.environ.get(
                "SYSCON_TTS_MAX_LOADED_VOICES", str(DEFAULT_MAX_LOADED_VOICES)
            )
        ),
        file_mode=_env_mode("SYSCON_TTS_FILE_MODE", DEFAULT_FILE_MODE),
        dir_mode=_env_mode("SYSCON_TTS_DIR_MODE", DEFAULT_DIR_MODE),
    )

    if not overrides:
        return settings

    known = {f.name for f in fields(Settings)}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise TypeError(
            f"Unknown setting(s): {', '.join(unknown)}. "
            f"Known settings: {', '.join(sorted(known))}."
        )
    for name, value in overrides.items():
        if value is not None:
            setattr(settings, name, _coerce(name, value))
    return settings
