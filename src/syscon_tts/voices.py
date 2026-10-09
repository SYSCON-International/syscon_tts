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
  selectable the next time a registry is built, with no new release of this
  package. The CLI builds one per command, so it sees the file at once; a
  long-running process (the APU, the HTTP server) needs a restart.

Voice ids are a durable contract -- the APU stores the selected id in its
settings table -- so ids in the manifest must never be renamed once released.

Reading the manifest never touches the model files, so this module imports and
works on every platform regardless of whether Piper is installed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

from .errors import SysconTTSError

if TYPE_CHECKING:  # config imports this module, so only for annotations
    from .config import Settings

logger = logging.getLogger(__name__)

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

#: Piper's quality tiers, largest first.
QUALITY_ORDER = ("high", "medium", "low", "x_low")

#: Upstream license strings that clear a voice for use in a product we sell,
#: in the form :func:`license_key` produces. ``site supplied`` covers voices
#: discovered on disk: whoever put the file there made that call, and
#: second-guessing it would only produce warnings nobody can act on. Model
#: cards spell these many ways (``CC-BY 4.0``, ``CC BY-4.0``, a license URL),
#: which is why they are compared normalized rather than verbatim.
PERMISSIVE_LICENSES = frozenset(
    {
        "public domain",
        "cc0",
        "cc0 1.0",
        "creativecommons.org/publicdomain/zero/1.0",
        "unlicense",
        "the unlicense",
        "unlicense.org",
        "cc by 4.0",
        "creativecommons.org/licenses/by/4.0",
        "site supplied",
    }
)


def license_key(text: str) -> str:
    """Normalize a license string for comparison with :data:`PERMISSIVE_LICENSES`.

    Lower-cases, drops a URL's scheme, ``www.`` and trailing slash, and treats
    ``-`` and ``_`` as spaces: ``"CC-BY 4.0"``, ``"cc_by_4.0"`` and
    ``"CC BY 4.0"`` all become ``"cc by 4.0"``.
    """
    key = str(text or "").strip().lower()
    key = re.sub(r"^https?://", "", key)
    key = re.sub(r"^www\.", "", key).rstrip("/")
    if "/" not in key:  # leave URL paths alone; they use '-' meaningfully
        key = re.sub(r"[-_\s]+", " ", key)
    return key


def _is_script(subtag: str) -> bool:
    """BCP-47 script subtags are exactly four letters (``Latn``, ``Hant``)."""
    return len(subtag) == 4 and subtag.isalpha()


def _locale_parts(code: Any) -> List[str]:
    """Lower-cased subtags of a locale, minus POSIX codeset and modifier.

    ``"en_US.UTF-8"`` and ``"es_MX@euro"`` lose ``.UTF-8`` and ``@euro``.
    """
    text = str(code or "").strip().split("@", 1)[0].split(".", 1)[0]
    return [part for part in text.lower().replace("-", "_").split("_") if part]


def _has_region(code: Any) -> bool:
    """True when a locale names a region (``es-ES``, ``sr-Latn-RS``), not just a language."""
    parts = _locale_parts(code)
    if len(parts) >= 2 and _is_script(parts[1]):
        parts = parts[:1] + parts[2:]
    # BCP-47 shapes, so a file stem like "plant_voice" is not read as one.
    return (
        len(parts) >= 2
        and 2 <= len(parts[0]) <= 3 and parts[0].isalpha()
        and (
            (len(parts[1]) == 2 and parts[1].isalpha())
            or (len(parts[1]) == 3 and parts[1].isdigit())
        )
    )


class VoiceError(SysconTTSError):
    """Base class for voice-related errors."""


class UnknownVoiceError(VoiceError):
    """Requested a voice id that is not in the manifest."""


class VoiceNotInstalledError(VoiceError):
    """Voice is in the manifest but its model files are not on disk."""


def normalize_language(code: str) -> str:
    """Canonicalize a locale into the catalogue's spelling.

    ``"en-us"``, ``"en_US"`` and ``"en"`` all become ``"en_US"``; ``"zh-hans"``
    and ``"zh-Hant-TW"`` both become ``"zh_CN"``. A script subtag is dropped
    unless an alias gives it meaning (``sr-Latn-RS`` -> ``sr_RS``), and so are
    a POSIX codeset and modifier (``en_US.UTF-8``, ``es_MX@euro``).
    Unrecognized codes are returned in ``lang_REGION`` form rather than
    rejected, so a site can add a voice for a language this package has never
    heard of.
    """
    parts = _locale_parts(code)
    if not parts:
        # Empty, or separators only ("-", "_"): there is no language in it.
        return ""

    key = "_".join(parts)
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]

    language, rest = parts[0], parts[1:]
    if rest and _is_script(rest[0]):
        scripted = f"{language}_{rest[0]}"
        if scripted in _LANGUAGE_ALIASES:
            return _LANGUAGE_ALIASES[scripted]
        rest = rest[1:]

    if not rest:
        return _LANGUAGE_ALIASES.get(language, language)
    region = rest[0]
    return _LANGUAGE_ALIASES.get(f"{language}_{region}", f"{language}_{region.upper()}")


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
    # Expected SHA-256 of each downloaded file. Empty means "not pinned",
    # which is the case for voices discovered on disk.
    model_sha256: str = ""
    config_sha256: str = ""
    quality: str = "unknown"  # x_low | low | medium | high | unknown
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
        return license_key(self.license) not in PERMISSIVE_LICENSES

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


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _required(entry: dict, key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise VoiceError(
            f"Voice manifest entry needs a non-empty string {key!r}: {entry!r}"
        )
    return value


def _optional(entry: dict, key: str, default: str = "") -> str:
    """A string field; JSON ``null`` means "not given"."""
    value = entry.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise VoiceError(f"Voice manifest field {key!r} must be a string: {entry!r}")
    return value


def _file_name(entry: dict, key: str) -> str:
    """A model/config name: one plain file name inside the voices directory.

    ``download-voices`` runs as root and writes to ``voices_dir / name``, so
    ``../x`` or an absolute path would let a manifest write anywhere.
    """
    name = _required(entry, key)
    if (
        name in (".", "..")
        or PurePosixPath(name).name != name
        or PureWindowsPath(name).name != name
    ):
        raise VoiceError(
            f"Voice manifest field {key!r} must be a plain file name, not a "
            f"path: {name!r}"
        )
    return name


def _sha256(entry: dict, key: str) -> str:
    value = _optional(entry, key).strip().lower()
    if value and not _SHA256.fullmatch(value):
        raise VoiceError(
            f"Voice manifest field {key!r} is not a SHA-256 hex digest: {value!r}"
        )
    return value


def _profile_from_entry(entry: Any) -> VoiceProfile:
    if not isinstance(entry, dict):
        raise VoiceError(f"Voice manifest entry is not a JSON object: {entry!r}")
    model = _file_name(entry, "model")
    return VoiceProfile(
        id=_required(entry, "id"),
        name=_required(entry, "name"),
        language=normalize_language(_required(entry, "language")),
        gender=_optional(entry, "gender", "unspecified"),
        model=model,
        config=_file_name(entry, "config"),
        model_url=_optional(entry, "model_url"),
        config_url=_optional(entry, "config_url"),
        model_sha256=_sha256(entry, "model_sha256"),
        config_sha256=_sha256(entry, "config_sha256"),
        quality=_optional(entry, "quality") or quality_from_model_name(model),
        license=_optional(entry, "license", "unknown"),
        license_url=_optional(entry, "license_url"),
        notes=_optional(entry, "notes"),
    )


def _read_json(path: Path) -> Any:
    """Parse a JSON file in UTF-8 (with or without a BOM), UTF-16 or UTF-32.

    ``json.loads`` on bytes detects the encoding itself; reading as UTF-8 text
    first would reject a BOM, which Windows editors like to add.
    """
    return json.loads(Path(path).read_bytes())


def _read_manifest(manifest_path: Path) -> List[VoiceProfile]:
    """Every profile in one manifest file. Any problem is a :class:`VoiceError`."""
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        raise VoiceError(f"Voice manifest not found: {manifest_path}")
    try:
        data = _read_json(manifest_path)
    except OSError as exc:  # a directory, unreadable, ...
        raise VoiceError(f"Cannot read voice manifest {manifest_path}: {exc}") from exc
    except ValueError as exc:  # bad JSON or undecodable bytes
        raise VoiceError(f"Voice manifest {manifest_path} is not valid JSON: {exc}") from exc
    voices = data.get("voices", []) if isinstance(data, dict) else None
    if not isinstance(voices, list):
        raise VoiceError(
            f"Voice manifest {manifest_path} must be a JSON object with a "
            "'voices' list."
        )

    profiles: List[VoiceProfile] = []
    seen = set()
    for entry in voices:
        profile = _profile_from_entry(entry)
        if profile.id in seen:
            # Last-wins would make whichever entry happens to be lower the
            # real one, silently. Replacing an id is what an overlay is for.
            raise VoiceError(
                f"Voice manifest {manifest_path} lists id {profile.id!r} twice."
            )
        seen.add(profile.id)
        profiles.append(profile)
    return profiles


def _discover_profiles(voices_dir: Path, claimed: Iterable[str]) -> List[VoiceProfile]:
    """Build profiles for model files no manifest entry accounts for."""
    voices_dir = Path(voices_dir)
    if not voices_dir.is_dir():
        return []

    claimed_names = {name.lower() for name in claimed}
    found: List[VoiceProfile] = []
    ids = set()
    for model_path in sorted(voices_dir.glob("*.onnx")):
        if model_path.name.lower() in claimed_names:
            continue
        config_path = model_path.with_name(model_path.name + ".json")
        if not config_path.is_file():
            # A model without its config cannot be loaded; leaving it out of
            # the listing is friendlier than offering a voice that will fail.
            continue
        if _world_writable(model_path) or _world_writable(config_path):
            # Anyone on the host could have swapped it, and it would be loaded
            # into a root process. Downloads are installed 0644.
            logger.warning(
                "Ignoring %s: it or its config is world-writable", model_path
            )
            continue
        profile = _profile_from_files(model_path, config_path)
        if profile.id in ids:
            # Two files differing only in case map to one id; the first wins
            # rather than whichever the dict happens to keep.
            logger.warning(
                "Ignoring %s: voice id %s is already taken", model_path, profile.id
            )
            continue
        ids.add(profile.id)
        found.append(profile)
    return found


def _world_writable(path: Path) -> bool:
    """True on POSIX when anyone may write ``path``. Always False on Windows."""
    if os.name != "posix":
        return False
    try:
        return bool(path.stat().st_mode & stat.S_IWOTH)
    except OSError:
        return False


def _profile_from_files(model_path: Path, config_path: Path) -> VoiceProfile:
    stem = model_path.name[: -len(".onnx")]
    try:
        config = _read_json(config_path)
    except (OSError, ValueError):  # ValueError covers bad JSON and bad bytes
        config = {}
    if not isinstance(config, dict):
        # One stray file (null, a list) must not take down the whole registry;
        # the file name below still identifies the voice.
        config = {}

    language_block = config.get("language")
    espeak = config.get("espeak")
    code = language_block.get("code") if isinstance(language_block, dict) else None
    espeak_voice = espeak.get("voice") if isinstance(espeak, dict) else None
    # The file-name convention: <locale>-<speaker>-<quality>.
    file_locale = stem.split("-")[0]
    # The most specific source wins. A config can say just "es" for a voice
    # whose file name says es_ES, and "es" alone would resolve to es_MX.
    sources = [code, file_locale, espeak_voice]
    language = next(
        (src for src in sources if src and _has_region(src)),
        next((src for src in (code, espeak_voice, file_locale) if src), ""),
    )
    dataset = config.get("dataset")
    speaker = dataset if isinstance(dataset, str) and dataset else (
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
        self._profiles: Dict[str, VoiceProfile] = {}
        for profile in profiles:
            if profile.id in self._profiles:
                raise VoiceError(f"Voice id {profile.id!r} appears twice.")
            self._profiles[profile.id] = profile
        self._voices_dir = Path(voices_dir)

    @classmethod
    def from_manifest(
        cls,
        manifest_path: Path,
        voices_dir: Path,
        extra_manifest: Optional[Path] = None,
        discover: bool = True,
    ) -> VoiceRegistry:
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
    def from_settings(cls, settings: Settings) -> VoiceRegistry:
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

        Among equally close matches the catalogue order decides: the bundled
        manifest's order, then new overlay entries in theirs. An overlay entry
        that reuses an id keeps that id's place, so reordering an overlay does
        not change the choice -- a site that wants a different voice for a
        language names it in ``SYSCON_TTS_DEFAULT_VOICES``, which
        :func:`~syscon_tts.alerts.resolve_voice_id` checks before calling
        this. Quality deliberately does not decide: a high-quality
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
        """True when both files are non-empty regular files.

        A 0-byte file (an interrupted copy) or a directory of the same name
        would otherwise count as installed and fail only at synthesis.
        """
        return _nonempty_file(self.model_path(profile)) and _nonempty_file(
            self.config_path(profile)
        )


def _nonempty_file(path: Path) -> bool:
    try:
        info = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size > 0
