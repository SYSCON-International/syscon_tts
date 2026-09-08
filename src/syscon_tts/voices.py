"""Voice-profile registry.

A voice profile is one selectable voice, backed by a Piper ``.onnx`` model and
its ``.onnx.json`` config. Profiles come from two places:

* the **manifest** -- a curated catalogue shipped inside the wheel
  (``syscon_tts/data/voices.json``), whose entries carry download URLs so
  ``syscon-tts download-voices`` can fetch them. A site can layer its own
  manifest on top (``SYSCON_TTS_EXTRA_MANIFEST``) to add or replace entries
  without forking the packaged file.
* **discovery** -- any ``.onnx``/``.onnx.json`` pair sitting in the voices
  directory that no manifest entry claims. This is what makes a deployment's
  voice set its own: drop in a model from
  <https://huggingface.co/rhasspy/piper-voices> (or one you trained) and it is
  selectable immediately, with no new release of this package.

Voice ids are a durable contract -- the APU stores the selected id in its
settings table -- so ids in the manifest must never be renamed once released.

Reading the manifest never touches the model files, so this module imports and
works on every platform regardless of whether Piper is installed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

#: Locale spellings that should collapse onto one voice language. The Spanish
#: and Chinese entries are product decisions, not linguistics: PlantStar offers
#: Mexican Spanish and both Chinese scripts, while Piper publishes one Mandarin
#: voice set (``zh_CN``) that serves Simplified and Traditional sites alike --
#: the spoken language is the same, only the written script differs.
_LANGUAGE_ALIASES = {
    "en": "en_US",
    "es": "es_MX",
    "fr": "fr_FR",
    "de": "de_DE",
    "zh": "zh_CN",
    "zh_hans": "zh_CN",
    "zh_hant": "zh_CN",
    "zh_tw": "zh_CN",
    "zh_hk": "zh_CN",
    "zh_sg": "zh_CN",
    "cmn": "zh_CN",
}

#: Script subtags that carry no voice-selection meaning for us (``zh-Hant-TW``
#: and ``zh-Hans-CN`` are the same Piper voice).
_SCRIPT_SUBTAGS = frozenset({"hans", "hant", "latn", "cyrl", "arab"})

#: Piper's quality tiers, largest first.
QUALITY_ORDER = ("high", "medium", "low", "x_low")

#: Upstream license strings that clear a voice for use in a product we sell.
#: ``site-supplied`` covers voices discovered on disk: whoever put the file
#: there made that call, and second-guessing it would only produce warnings
#: nobody can act on. Compared case-insensitively after stripping.
PERMISSIVE_LICENSES = frozenset(
    {
        "public domain",
        "cc0",
        "unlicense",
        "cc by 4.0",
        "cc-by-4.0",
        "site-supplied",
    }
)


class VoiceError(Exception):
    """Base class for voice-related errors."""


class UnknownVoiceError(VoiceError):
    """Requested a voice id that is not in the manifest."""


class VoiceNotInstalledError(VoiceError):
    """Voice is in the manifest but its model files are not on disk."""


def normalize_language(code: str) -> str:
    """Canonicalize a locale into the catalogue's spelling.

    ``"en-us"``, ``"en_US"`` and ``"en"`` all become ``"en_US"``; ``"zh-hans"``
    and ``"zh-Hant-TW"`` both become ``"zh_CN"``. Unrecognized codes are
    returned in ``lang_REGION`` form rather than rejected, so a site can add a
    voice for a language this package has never heard of.
    """
    text = str(code or "").strip().replace("-", "_")
    if not text:
        return ""

    key = text.lower()
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]

    parts = [part for part in key.split("_") if part]
    language = parts[0]

    if len(parts) >= 2 and parts[1] in _SCRIPT_SUBTAGS:
        scripted = f"{language}_{parts[1]}"
        if scripted in _LANGUAGE_ALIASES:
            return _LANGUAGE_ALIASES[scripted]
        return _LANGUAGE_ALIASES.get(language, language)

    if len(parts) == 1:
        return _LANGUAGE_ALIASES.get(language, language)

    return _LANGUAGE_ALIASES.get(f"{language}_{parts[1]}", f"{language}_{parts[1].upper()}")


def primary_language(code: str) -> str:
    """The language part of a locale: ``"es_MX"`` -> ``"es"``."""
    return normalize_language(code).split("_")[0]


def quality_from_model_name(model: str) -> str:
    """Read Piper's quality tier out of a model file name.

    ``en_US-amy-medium.onnx`` -> ``medium``. Returns ``"unknown"`` for files
    that do not follow the convention, which is only cosmetic.
    """
    stem = Path(model).name
    for suffix in (".onnx.json", ".onnx"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    tail = stem.rsplit("-", 1)[-1] if "-" in stem else ""
    return tail if tail in QUALITY_ORDER else "unknown"


@dataclass(frozen=True)
class VoiceProfile:
    id: str
    name: str
    language: str          # BCP-47-ish locale, e.g. "en_US"
    gender: str
    model: str             # filename of the .onnx model
    config: str            # filename of the .onnx.json config
    model_url: str = ""    # download source (used by `syscon-tts download-voices`)
    config_url: str = ""
    quality: str = "medium"  # x_low | low | medium | high
    # What the upstream model card states. Piper's catalogue mixes public
    # domain voices with non-commercial ones, and PlantStar ships to paying
    # customers, so this is tracked per voice rather than assumed.
    license: str = "unknown"
    license_url: str = ""
    notes: str = ""
    # "manifest" for catalogued voices, "disk" for ones found in the voices
    # directory. Discovered voices have no download URL -- whoever put the file
    # there is responsible for it surviving a rebuild.
    source: str = "manifest"

    @property
    def requires_license_review(self) -> bool:
        """True when this voice must not ship to a customer unreviewed."""
        return self.license.strip().lower() not in PERMISSIVE_LICENSES

    def to_public_dict(self, installed: bool) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "language": self.language,
            "gender": self.gender,
            "quality": self.quality,
            "license": self.license,
            "requires_license_review": self.requires_license_review,
            "source": self.source,
            "installed": installed,
        }


def _profile_from_entry(entry: dict) -> VoiceProfile:
    try:
        model = entry["model"]
        return VoiceProfile(
            id=entry["id"],
            name=entry["name"],
            language=normalize_language(entry["language"]),
            gender=entry.get("gender", "unspecified"),
            model=model,
            config=entry["config"],
            model_url=entry.get("model_url", ""),
            config_url=entry.get("config_url", ""),
            quality=entry.get("quality") or quality_from_model_name(model),
            license=entry.get("license", "unknown"),
            license_url=entry.get("license_url", ""),
            notes=entry.get("notes", ""),
        )
    except KeyError as exc:  # pragma: no cover - manifest authoring error
        raise VoiceError(
            f"Voice manifest entry missing required field {exc}: {entry!r}"
        ) from exc


def _read_manifest(manifest_path: Path) -> List[VoiceProfile]:
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        raise VoiceError(f"Voice manifest not found: {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VoiceError(f"Voice manifest {manifest_path} is not valid JSON: {exc}") from exc
    return [_profile_from_entry(entry) for entry in data.get("voices", [])]


def _discover_profiles(voices_dir: Path, claimed: Iterable[str]) -> List[VoiceProfile]:
    """Build profiles for model files no manifest entry accounts for."""
    voices_dir = Path(voices_dir)
    if not voices_dir.is_dir():
        return []

    claimed_names = {name.lower() for name in claimed}
    found: List[VoiceProfile] = []
    for model_path in sorted(voices_dir.glob("*.onnx")):
        if model_path.name.lower() in claimed_names:
            continue
        config_path = model_path.with_name(model_path.name + ".json")
        if not config_path.is_file():
            # A model without its config cannot be loaded; leaving it out of
            # the listing is friendlier than offering a voice that will fail.
            continue
        found.append(_profile_from_files(model_path, config_path))
    return found


def _profile_from_files(model_path: Path, config_path: Path) -> VoiceProfile:
    stem = model_path.name[: -len(".onnx")]
    language = ""
    speaker = ""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        config = {}

    language_block = config.get("language")
    if isinstance(language_block, dict):
        language = language_block.get("code") or ""
    if not language:
        espeak = config.get("espeak")
        if isinstance(espeak, dict):
            language = espeak.get("voice") or ""
    if not language:
        # Fall back to the file-name convention: <locale>-<speaker>-<quality>.
        language = stem.split("-")[0]
    speaker = config.get("dataset") or (
        stem.split("-")[1] if "-" in stem else stem
    )

    quality = quality_from_model_name(model_path.name)
    language = normalize_language(language)
    return VoiceProfile(
        id=stem.lower().replace("-", "_"),
        name=f"{speaker} ({language}, {quality})",
        language=language,
        gender="unspecified",
        model=model_path.name,
        config=config_path.name,
        quality=quality,
        license="site-supplied",
        source="disk",
    )


class VoiceRegistry:
    """Loads voice profiles from manifests and disk, and reports install state."""

    def __init__(self, profiles: List[VoiceProfile], voices_dir: Path):
        self._profiles: Dict[str, VoiceProfile] = {p.id: p for p in profiles}
        self._voices_dir = Path(voices_dir)

    @classmethod
    def from_manifest(
        cls,
        manifest_path: Path,
        voices_dir: Path,
        extra_manifest: Optional[Path] = None,
        discover: bool = True,
    ) -> "VoiceRegistry":
        """Build a registry from the manifest, an optional overlay, and disk.

        Later sources win on id collisions: overlay entries replace bundled
        ones, and discovery never overrides either -- a model file already
        named by a manifest entry is left to that entry.
        """
        profiles = _read_manifest(manifest_path)
        if extra_manifest:
            overlay = _read_manifest(extra_manifest)
            by_id = {p.id: p for p in profiles}
            for profile in overlay:
                by_id[profile.id] = profile
            profiles = list(by_id.values())

        if not profiles:
            raise VoiceError(f"Voice manifest has no voices: {manifest_path}")

        if discover:
            claimed = {p.model for p in profiles}
            known_ids = {p.id for p in profiles}
            profiles += [
                p for p in _discover_profiles(voices_dir, claimed)
                if p.id not in known_ids
            ]

        return cls(profiles, voices_dir)

    @classmethod
    def from_settings(cls, settings) -> "VoiceRegistry":
        """Build the registry a :class:`~syscon_tts.config.Settings` describes."""
        return cls.from_manifest(
            settings.voices_manifest,
            settings.voices_dir,
            extra_manifest=settings.extra_manifest,
        )

    # -- listing -----------------------------------------------------------

    def all(self) -> List[VoiceProfile]:
        return list(self._profiles.values())

    def installed(self) -> List[VoiceProfile]:
        return [p for p in self._profiles.values() if self.is_installed(p)]

    def languages(self) -> List[str]:
        """Every language with at least one profile, in catalogue order."""
        seen: List[str] = []
        for profile in self._profiles.values():
            if profile.language not in seen:
                seen.append(profile.language)
        return seen

    # -- lookup ------------------------------------------------------------

    def get(self, voice_id: str) -> VoiceProfile:
        try:
            return self._profiles[voice_id]
        except KeyError:
            known = ", ".join(sorted(self._profiles)) or "(none)"
            raise UnknownVoiceError(
                f"Unknown voice '{voice_id}'. Available: {known}"
            ) from None

    def has(self, voice_id: str) -> bool:
        return voice_id in self._profiles

    def for_language(
        self, language: str, installed_only: bool = False
    ) -> List[VoiceProfile]:
        """Profiles for a locale, closest match first.

        Exact locale matches (``es_MX``) come before same-language matches
        (``es_ES``), so a Mexican-Spanish site gets its own voice when one is
        installed and still gets speech when only Castilian is.
        """
        wanted = normalize_language(language)
        if not wanted:
            return []
        base = wanted.split("_")[0]

        exact, related = [], []
        for profile in self._profiles.values():
            if installed_only and not self.is_installed(profile):
                continue
            if profile.language == wanted:
                exact.append(profile)
            elif profile.language.split("_")[0] == base:
                related.append(profile)
        return exact + related

    def default_for_language(self, language: str) -> VoiceProfile:
        """Best voice for a locale, preferring installed ones.

        Among equally close matches the catalogue order decides, so the
        preferred voice for a language is whichever entry the manifest lists
        first -- a site that wants a different one puts it at the top of its
        overlay manifest. Quality deliberately does not decide: a high-quality
        model is several times slower on APU-class hardware, and that trade is
        the operator's to make, not this function's.

        Raises :class:`UnknownVoiceError` when the catalogue has nothing for
        the language at all -- the caller decides whether that is fatal or a
        reason to fall back to the configured default voice.
        """
        candidates = self.for_language(language, installed_only=True)
        if not candidates:
            candidates = self.for_language(language)
        if not candidates:
            known = ", ".join(self.languages()) or "(none)"
            raise UnknownVoiceError(
                f"No voice for language '{language}'. Available languages: {known}"
            )
        return candidates[0]

    # -- install state -----------------------------------------------------

    def model_path(self, profile: VoiceProfile) -> Path:
        return self._voices_dir / profile.model

    def config_path(self, profile: VoiceProfile) -> Path:
        return self._voices_dir / profile.config

    def is_installed(self, profile: VoiceProfile) -> bool:
        return self.model_path(profile).exists() and self.config_path(profile).exists()
