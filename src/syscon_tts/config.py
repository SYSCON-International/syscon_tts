"""Runtime configuration.

Resolution order for every setting is: explicit argument > environment
variable > platform default. Nothing is derived from the source tree, so the
package behaves identically whether it is installed into ``site-packages``,
run from a checkout, or imported inside the APU's Django process.

Environment variables (all optional):

============================== =============================================
``SYSCON_TTS_MANIFEST``        Voice manifest JSON path
``SYSCON_TTS_VOICES_DIR``      Directory holding the ``.onnx`` voice models
``SYSCON_TTS_ALERTS_DIR``      Directory alert WAVs are written to
``SYSCON_TTS_DEFAULT_VOICE``   Voice used when a caller does not name one
``SYSCON_TTS_HOST``            Bind host for the optional HTTP server
``SYSCON_TTS_PORT``            Bind port for the optional HTTP server
``SYSCON_TTS_MAX_CHARS``       Maximum text length accepted per request
============================== =============================================

On the APU, point ``SYSCON_TTS_VOICES_DIR`` at ``MEDIA_ROOT/tts_voices`` and
``SYSCON_TTS_ALERTS_DIR`` at ``MEDIA_ROOT/public_alert_sounds``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Directory this package was installed into. Used only to locate bundled
# package data (the default voice manifest) -- never to locate user data.
PACKAGE_DIR = Path(__file__).resolve().parent

APP_NAME = "syscon-tts"


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


@dataclass
class Settings:
    """Resolved runtime settings."""

    voices_manifest: Path  # JSON file describing the available voice profiles
    voices_dir: Path       # directory holding the downloaded .onnx voice models
    alerts_dir: Path       # directory generated alert WAVs are written to
    host: str
    port: int
    default_voice: str
    max_text_chars: int

    def describe(self) -> dict:
        return {
            "voices_manifest": str(self.voices_manifest),
            "voices_dir": str(self.voices_dir),
            "alerts_dir": str(self.alerts_dir),
            "host": self.host,
            "port": self.port,
            "default_voice": self.default_voice,
            "max_text_chars": self.max_text_chars,
        }


def load_settings() -> Settings:
    """Build a :class:`Settings` from the environment (with sane defaults)."""

    data_dir = default_data_dir()
    return Settings(
        voices_manifest=_env_path("SYSCON_TTS_MANIFEST", default_manifest_path()),
        voices_dir=_env_path("SYSCON_TTS_VOICES_DIR", data_dir / "voices"),
        alerts_dir=_env_path("SYSCON_TTS_ALERTS_DIR", data_dir / "alerts"),
        host=os.environ.get("SYSCON_TTS_HOST", "127.0.0.1"),
        port=int(os.environ.get("SYSCON_TTS_PORT", "5002")),
        default_voice=os.environ.get("SYSCON_TTS_DEFAULT_VOICE", "en_us_amy"),
        max_text_chars=int(os.environ.get("SYSCON_TTS_MAX_CHARS", "20000")),
    )
