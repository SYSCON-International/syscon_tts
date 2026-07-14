"""Voice-profile registry.

A voice profile is one selectable voice, backed by a Piper ``.onnx`` model and
its ``.onnx.json`` config. The catalogue of profiles lives in a JSON manifest
(``config/voices.json``) so new voices/languages can be added without code
changes -- add an entry and run ``scripts/download_voices.sh``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class VoiceError(Exception):
    """Base class for voice-related errors."""


class UnknownVoiceError(VoiceError):
    """Requested a voice id that is not in the manifest."""


class VoiceNotInstalledError(VoiceError):
    """Voice is in the manifest but its model files are not on disk."""


@dataclass(frozen=True)
class VoiceProfile:
    id: str
    name: str
    language: str          # BCP-47-ish locale, e.g. "en_US"
    gender: str
    model: str             # filename of the .onnx model
    config: str            # filename of the .onnx.json config
    model_url: str = ""    # download source (used by download_voices.sh)
    config_url: str = ""

    def to_public_dict(self, installed: bool) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "language": self.language,
            "gender": self.gender,
            "installed": installed,
        }


class VoiceRegistry:
    """Loads voice profiles from the JSON manifest and reports install state."""

    def __init__(self, profiles: list[VoiceProfile], voices_dir: Path):
        self._profiles: dict[str, VoiceProfile] = {p.id: p for p in profiles}
        self._voices_dir = Path(voices_dir)

    @classmethod
    def from_manifest(cls, manifest_path: Path, voices_dir: Path) -> "VoiceRegistry":
        manifest_path = Path(manifest_path)
        if not manifest_path.exists():
            raise VoiceError(f"Voice manifest not found: {manifest_path}")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        profiles = []
        for entry in data.get("voices", []):
            try:
                profiles.append(
                    VoiceProfile(
                        id=entry["id"],
                        name=entry["name"],
                        language=entry["language"],
                        gender=entry.get("gender", "unspecified"),
                        model=entry["model"],
                        config=entry["config"],
                        model_url=entry.get("model_url", ""),
                        config_url=entry.get("config_url", ""),
                    )
                )
            except KeyError as exc:  # pragma: no cover - manifest authoring error
                raise VoiceError(
                    f"Voice manifest entry missing required field {exc}: {entry!r}"
                ) from exc
        if not profiles:
            raise VoiceError(f"Voice manifest has no voices: {manifest_path}")
        return cls(profiles, voices_dir)

    def all(self) -> list[VoiceProfile]:
        return list(self._profiles.values())

    def get(self, voice_id: str) -> VoiceProfile:
        try:
            return self._profiles[voice_id]
        except KeyError:
            known = ", ".join(sorted(self._profiles)) or "(none)"
            raise UnknownVoiceError(
                f"Unknown voice '{voice_id}'. Available: {known}"
            ) from None

    def model_path(self, profile: VoiceProfile) -> Path:
        return self._voices_dir / profile.model

    def config_path(self, profile: VoiceProfile) -> Path:
        return self._voices_dir / profile.config

    def is_installed(self, profile: VoiceProfile) -> bool:
        return self.model_path(profile).exists() and self.config_path(profile).exists()
