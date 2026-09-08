"""Voice-model downloader.

Written in Python rather than shell so it works identically on every platform,
and so it ships inside the wheel -- an operator who ``pip install``s the
package gets the downloader without needing a source checkout.

Only this module needs network access. Once the models are on disk the rest of
the package runs fully offline.
"""

from __future__ import annotations

import shutil
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from .voices import VoiceProfile, VoiceRegistry, normalize_language

#: Downloads are large (~60 MB per model); allow a generous connect timeout.
DEFAULT_TIMEOUT = 60


class DownloadError(Exception):
    """A voice asset could not be retrieved."""


class LicenseReviewRequired(DownloadError):
    """Refused to fetch a voice whose license does not clear commercial use.

    Not a hard block -- pass ``accept_license=True`` (``--accept-license`` on
    the command line) once somebody has actually reviewed it. The point is that
    the decision is made by a person, not by a default.
    """


@dataclass
class DownloadResult:
    voice_id: str
    file_name: str
    path: Path
    skipped: bool  # True when the file was already present
    size: int


def _fetch(url: str, dest: Path, timeout: int = DEFAULT_TIMEOUT) -> int:
    """Download ``url`` to ``dest`` atomically. Returns bytes written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    tmp = Path(tmp_name)
    try:
        # Wrap the descriptor first so it is closed even if urlopen raises --
        # otherwise the leaked handle also blocks the cleanup unlink on Windows.
        with open(fd, "wb") as handle:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                shutil.copyfileobj(response, handle)
        size = tmp.stat().st_size
        if size == 0:
            raise DownloadError(f"{url} returned an empty response.")
        tmp.replace(dest)
        return size
    except urllib.error.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        raise DownloadError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise DownloadError(
            f"Could not reach {url}: {exc.reason}. This step needs internet "
            "access; on an air-gapped APU, download on another machine and "
            "copy the voices directory across."
        ) from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def download_voice(
    profile: VoiceProfile,
    voices_dir: Path,
    force: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
) -> list[DownloadResult]:
    """Fetch the model and config for one voice profile."""
    results: list[DownloadResult] = []
    assets = (
        (profile.model, profile.model_url),
        (profile.config, profile.config_url),
    )
    for file_name, url in assets:
        dest = Path(voices_dir) / file_name
        if dest.is_file() and dest.stat().st_size > 0 and not force:
            if on_progress:
                on_progress(f"  ok   {file_name} (already present)")
            results.append(
                DownloadResult(profile.id, file_name, dest, True, dest.stat().st_size)
            )
            continue
        if not url:
            raise DownloadError(
                f"Voice '{profile.id}' has no download URL for {file_name}. "
                "Add one to the voice manifest, or copy the file in manually."
            )
        if on_progress:
            on_progress(f"  get  {file_name}")
        size = _fetch(url, dest)
        results.append(DownloadResult(profile.id, file_name, dest, False, size))
    return results


def download_voices(
    registry: VoiceRegistry,
    voices_dir: Path,
    voice_ids: Optional[Iterable[str]] = None,
    force: bool = False,
    on_progress: Optional[Callable[[str], None]] = None,
    language: Optional[str] = None,
    accept_license: bool = False,
) -> list[DownloadResult]:
    """Fetch the requested voices (the whole catalogue when nothing is named).

    ``language`` narrows the catalogue to one locale, which is how a site
    provisions only what it will actually announce in instead of pulling every
    model. Unknown ids raise before any download starts, so a typo does not
    leave a half-populated voices directory.

    Voices needing a license review are refused when named explicitly and
    skipped (with a note) during a bulk download, unless ``accept_license``
    says somebody has cleared them.
    """
    explicit = bool(voice_ids)
    if explicit:
        profiles = [registry.get(vid) for vid in voice_ids]
        blocked = [p for p in profiles if p.requires_license_review]
        if blocked and not accept_license:
            listing = "; ".join(
                f"{p.id} (license: {p.license}{', ' + p.license_url if p.license_url else ''})"
                for p in blocked
            )
            raise LicenseReviewRequired(
                f"Refusing to download {listing}. PlantStar ships to paying "
                "customers, and this voice's license does not clearly permit "
                "that. Re-run with --accept-license once it has been reviewed."
            )
    else:
        profiles = registry.all()
        if language:
            matches = registry.for_language(language)
            # Exact locale only when there is one: a site asking for es-MX
            # should not also pull 60 MB of Castilian it will never play. If
            # nothing matches exactly, the related-language voices are the
            # only way that site gets audio at all, so fetch those.
            wanted = normalize_language(language)
            profiles = [p for p in matches if p.language == wanted] or matches
            if not profiles:
                raise DownloadError(
                    f"No voice in the catalogue for language '{language}'. "
                    f"Known languages: {', '.join(registry.languages())}."
                )
        skipped = []
        if not accept_license:
            skipped = [p for p in profiles if p.requires_license_review]
            profiles = [p for p in profiles if not p.requires_license_review]
        for profile in skipped:
            if on_progress:
                on_progress(
                    f"  skip {profile.id} (license: {profile.license}; "
                    "name it explicitly with --accept-license to fetch it)"
                )

        # Voices discovered on disk have no download URL -- they are already
        # here by definition, and a bulk download must not fail over them. A
        # voice named explicitly still errors, because the caller asked for
        # something this package cannot do.
        without_urls = [p for p in profiles if not (p.model_url and p.config_url)]
        profiles = [p for p in profiles if p.model_url and p.config_url]
        for profile in without_urls:
            if on_progress:
                on_progress(
                    f"  skip {profile.id} (no download URL; supplied on disk)"
                )

    results: list[DownloadResult] = []
    for profile in profiles:
        results.extend(download_voice(profile, voices_dir, force, on_progress))
    return results
