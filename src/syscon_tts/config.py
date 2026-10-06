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
``SYSCON_TTS_MAX_CHARS``           Maximum text length per HTTP request
``SYSCON_TTS_MAX_ALERT_CHARS``     Maximum alert text length (library path)
``SYSCON_TTS_MAX_LOADED_VOICES``   Voice models kept resident in memory
``SYSCON_TTS_THREADS``             CPU threads per synthesis (default: all)
``SYSCON_TTS_FILE_MODE``           Mode for generated audio, e.g. ``0644``
``SYSCON_TTS_DIR_MODE``            Mode for directories this package creates
================================== =========================================

Callers that already know their paths -- the APU, which has them in Django
settings -- should skip the environment entirely and pass overrides::

    load_settings(alerts_dir=Path(settings.MEDIA_ROOT, "public_alert_sounds"))

Overrides and environment values go through the same normalization, so
``default_voices={"es-mx": ...}`` and ``SYSCON_TTS_DEFAULT_VOICES=es-mx=...``
mean the same thing, and ``file_mode="0644"`` is read as octal.

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
from typing import Any, Dict, Optional, Tuple

# Directory this package was installed into. Used only to locate bundled
# package data (the default voice manifest) -- never to locate user data.
PACKAGE_DIR = Path(__file__).resolve().parent

APP_NAME = "syscon-tts"

#: Voice used when the caller names neither a voice nor a language. Public
#: domain (LibriVox), so it can ship to a customer without a license review.
DEFAULT_VOICE = "en_us_kristin"

#: System-wide data directory on Linux; see :func:`resolve_data_dir`.
SYSTEM_DATA_DIR = Path("/var/lib") / APP_NAME

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

#: Longest alert text :meth:`~syscon_tts.alerts.AlertSynthesizer.ensure`
#: accepts. Synthesis holds the voice's lock for its whole duration, so one
#: runaway message would block every other announcement in that voice. A
#: thousand characters is roughly a minute of speech -- far past anything a
#: plant floor wants to hear.
DEFAULT_MAX_ALERT_CHARS = 1000

#: Longest text the HTTP ``/synthesize`` endpoint accepts.
DEFAULT_MAX_TEXT_CHARS = 20000


def default_manifest_path() -> Path:
    """Path to the voice manifest bundled inside the wheel."""
    return PACKAGE_DIR / "data" / "voices.json"


def resolve_data_dir() -> Tuple[Path, str]:
    """Platform-appropriate data directory, and the rule that chose it.

    On Linux the system-wide ``/var/lib/syscon-tts`` wins whenever it exists,
    whether or not the current user can write to it: provisioning creates it
    with ``sudo``, and voice models only need to be *readable*. Choosing by
    writability would send ``syscon-tts doctor`` run as an ordinary user to an
    empty ``~/.local/share`` and report the host unprovisioned. Commands that
    write (``download-voices``) fail with an explicit permission error instead.

    When it does not exist yet it is still chosen if it could be created; the
    per-user XDG directory is the fallback, so unprivileged development
    installs keep working.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / APP_NAME, "Windows default"

    if sys.platform == "darwin":
        return (
            Path.home() / "Library" / "Application Support" / APP_NAME,
            "macOS default",
        )

    system_dir = SYSTEM_DATA_DIR
    if system_dir.is_dir():
        return system_dir, f"{system_dir} exists"
    if system_dir.parent.is_dir() and os.access(system_dir.parent, os.W_OK):
        return system_dir, f"{system_dir} can be created"

    xdg = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return (
        root / APP_NAME,
        f"per-user fallback: {system_dir} is absent and cannot be created",
    )


def default_data_dir() -> Path:
    """Platform-appropriate directory for models and generated audio.

    See :func:`resolve_data_dir` for how it is chosen.
    """
    return resolve_data_dir()[0]


def parse_mode(value: Any, name: str = "mode") -> int:
    """Read a file/directory mode, written the way chmod(1) takes it.

    Strings are octal -- base 0 would read ``"644"`` as decimal. Ints are
    taken as given, so ``0o644`` works too.
    """
    if isinstance(value, bool):
        raise ValueError(f"{name}={value!r} is not a file mode.")
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip(), 8)
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
    two of them. Values are normalized on construction, however they arrive.
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
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS
    max_alert_chars: int = DEFAULT_MAX_ALERT_CHARS
    max_loaded_voices: int = DEFAULT_MAX_LOADED_VOICES
    # onnxruntime intra-op threads per synthesis. None keeps onnxruntime's
    # default of one per physical core, which saturates the whole CPU for the
    # length of each utterance -- set it on a host with other work to do.
    threads: Optional[int] = None
    file_mode: int = DEFAULT_FILE_MODE
    dir_mode: int = DEFAULT_DIR_MODE

    def __post_init__(self) -> None:
        # Validating here rather than in load_settings also covers callers who
        # construct Settings(...) directly.
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None and f.name not in _OPTIONAL_FIELDS:
                raise ValueError(f"Setting {f.name!r} cannot be None.")
            setattr(self, f.name, _coerce(f.name, value))

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
            "max_alert_chars": self.max_alert_chars,
            "max_loaded_voices": self.max_loaded_voices,
            "threads": self.threads,
            "file_mode": f"{self.file_mode:04o}",
            "dir_mode": f"{self.dir_mode:04o}",
        }


_PATH_FIELDS = frozenset(
    {"voices_manifest", "extra_manifest", "voices_dir", "alerts_dir"}
)
_INT_FIELDS = frozenset(
    {"port", "max_text_chars", "max_alert_chars", "max_loaded_voices", "threads"}
)
_MODE_FIELDS = frozenset({"file_mode", "dir_mode"})
_OPTIONAL_FIELDS = frozenset({"extra_manifest", "threads"})

#: The environment variable behind each setting.
ENV_NAMES = {
    "voices_manifest": "SYSCON_TTS_MANIFEST",
    "extra_manifest": "SYSCON_TTS_EXTRA_MANIFEST",
    "voices_dir": "SYSCON_TTS_VOICES_DIR",
    "alerts_dir": "SYSCON_TTS_ALERTS_DIR",
    "host": "SYSCON_TTS_HOST",
    "port": "SYSCON_TTS_PORT",
    "default_voice": "SYSCON_TTS_DEFAULT_VOICE",
    "default_voices": "SYSCON_TTS_DEFAULT_VOICES",
    "max_text_chars": "SYSCON_TTS_MAX_CHARS",
    "max_alert_chars": "SYSCON_TTS_MAX_ALERT_CHARS",
    "max_loaded_voices": "SYSCON_TTS_MAX_LOADED_VOICES",
    "threads": "SYSCON_TTS_THREADS",
    "file_mode": "SYSCON_TTS_FILE_MODE",
    "dir_mode": "SYSCON_TTS_DIR_MODE",
}


def _coerce(name: str, value: Any) -> Any:
    """Normalize one setting value, whichever way it arrived."""
    if value is None:
        return None
    if name in _PATH_FIELDS:
        return Path(value).expanduser()
    if name == "default_voices":
        if isinstance(value, str):
            return parse_default_voices(value)
        from .voices import normalize_language  # local import: avoids a cycle

        return {normalize_language(k): str(v) for k, v in dict(value).items()}
    if name in _MODE_FIELDS:
        return parse_mode(value, name)
    if name in _INT_FIELDS:
        if isinstance(value, bool):
            raise ValueError(f"{name}={value!r} is not an integer.")
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name}={value!r} is not an integer.") from None
        if number < 1:
            raise ValueError(f"{name}={value!r} must be at least 1.")
        return number
    return value


def load_settings(**overrides: Any) -> Settings:
    """Build a :class:`Settings` from the environment, then apply ``overrides``.

    ``load_settings(alerts_dir=...)`` is the intended entry point for embedded
    callers: no environment plumbing, and every field left unnamed keeps its
    default. An override of ``None`` is ignored rather than blanking the
    resolved value, so ``load_settings(voices_dir=maybe_none)`` behaves.
    """
    known = {f.name for f in fields(Settings)}
    unknown = sorted(set(overrides) - known)
    if unknown:
        raise TypeError(
            f"Unknown setting(s): {', '.join(unknown)}. "
            f"Known settings: {', '.join(sorted(known))}."
        )

    values: Dict[str, Any] = {}
    for name in known:
        if overrides.get(name) is not None:
            values[name] = overrides[name]
            continue
        env_name = ENV_NAMES[name]
        env_value = os.environ.get(env_name)
        if env_value:
            try:
                values[name] = _coerce(name, env_value)
            except ValueError as exc:
                raise ValueError(f"{env_name}: {exc}") from None

    # One data-dir lookup for both defaults rather than one per field.
    if "voices_dir" not in values or "alerts_dir" not in values:
        data_dir = default_data_dir()
        values.setdefault("voices_dir", data_dir / "voices")
        values.setdefault("alerts_dir", data_dir / "alerts")

    return Settings(**values)
