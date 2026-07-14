"""Runtime configuration.

All settings can be overridden with environment variables so the same code
runs unchanged whether launched from the CLI, a systemd unit, or a container.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Project root: <root>/src/plantstar_tts/config.py -> <root>
BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


@dataclass
class Settings:
    """Resolved runtime settings."""

    voices_manifest: Path  # JSON file describing the available voice profiles
    voices_dir: Path       # directory holding the downloaded .onnx voice models
    host: str
    port: int
    default_voice: str
    max_text_chars: int

    def describe(self) -> dict:
        return {
            "voices_manifest": str(self.voices_manifest),
            "voices_dir": str(self.voices_dir),
            "host": self.host,
            "port": self.port,
            "default_voice": self.default_voice,
            "max_text_chars": self.max_text_chars,
        }


def load_settings() -> Settings:
    """Build a :class:`Settings` from the environment (with sane defaults)."""

    root = _env_path("PLANTSTAR_TTS_HOME", BASE_DIR)
    return Settings(
        voices_manifest=_env_path(
            "PLANTSTAR_TTS_MANIFEST", root / "config" / "voices.json"
        ),
        voices_dir=_env_path("PLANTSTAR_TTS_VOICES_DIR", root / "voices"),
        host=os.environ.get("PLANTSTAR_TTS_HOST", "0.0.0.0"),
        port=int(os.environ.get("PLANTSTAR_TTS_PORT", "5002")),
        default_voice=os.environ.get("PLANTSTAR_TTS_DEFAULT_VOICE", "en_us_amy"),
        max_text_chars=int(os.environ.get("PLANTSTAR_TTS_MAX_CHARS", "20000")),
    )
