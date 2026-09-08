"""Cache-first alert audio.

This is the module the PlantStar APU integrates against. It encapsulates the
one contract the rest of the APU actually depends on: a WAV file exists at
``<alerts_dir>/<file_name>.wav``. Everything downstream -- the websocket push,
``/media/`` serving, and the file-cleanup pass -- is agnostic about how that
file got there.

The lookup is deliberately **cache first**:

* If the WAV already exists, it is returned immediately. This path needs no
  Piper, so it works on Windows and macOS exactly as it does on Linux.
* Only on a cache miss is synthesis attempted, which requires Piper and
  therefore Linux. On other platforms this raises
  :class:`~syscon_tts.engine.SynthesisUnavailableError`, which callers can
  catch to degrade gracefully.

That split is what lets developers on Windows/macOS exercise the full alert
pipeline against audio generated on a Linux box, without installing Piper.

A multilingual site names a language rather than a voice::

    synth = AlertSynthesizer(alerts_dir=..., default_voices={"zh_CN": "zh_cn_huayan"})
    synth.ensure("Press 4 fault", language="zh-hans")

One utterance is rendered by one voice, so text that mixes languages is spoken
by whichever voice was selected -- split the message instead.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import Settings, load_settings
from .engine import TTSEngine
from .voices import UnknownVoiceError, VoiceRegistry, normalize_language


class InvalidAlertNameError(ValueError):
    """Text reduced to a file name that would be unsafe to write."""


# Django's ``get_valid_filename`` strips everything outside [-\w.] after
# collapsing spaces to underscores. Reproduced here (rather than imported) so
# the package has no Django dependency, while still computing byte-identical
# names to the APU's existing ``get_valid_filename(message)[:50]``.
_UNSAFE_CHARS = re.compile(r"(?u)[^-\w.]")

#: The APU truncates alert file names to this length.
MAX_FILE_NAME_LEN = 50


def sanitize_file_name(text: str, max_length: int = MAX_FILE_NAME_LEN) -> str:
    """Derive an alert file name (no extension) from message text.

    Matches ``django.utils.text.get_valid_filename(text)[:max_length]``.

    The character class is Unicode-aware, so a Chinese or Spanish message keeps
    its own characters in the file name. Two consequences worth knowing when
    the text is not English: the resulting ``/media/`` URL needs percent
    encoding, and the limit counts *characters*, so 50 Han characters is a much
    longer utterance than 50 Latin ones -- distinct messages collide sooner.
    Callers that care can pass their own ``file_name`` to :meth:`ensure`.
    """
    name = str(text).strip().replace(" ", "_")
    name = _UNSAFE_CHARS.sub("", name)
    name = name[:max_length]
    if name in ("", ".", ".."):
        raise InvalidAlertNameError(
            f"Text {text!r} does not yield a usable file name."
        )
    return name


def resolve_voice_id(
    registry: VoiceRegistry,
    settings: Settings,
    voice: Optional[str] = None,
    language: Optional[str] = None,
) -> str:
    """Pick the voice id for a request.

    An explicit ``voice`` always wins. Otherwise a ``language`` is matched
    against the configured per-language defaults first (so an operator's choice
    beats the catalogue's), then against the catalogue itself. A language
    nothing can serve falls back to the default voice rather than failing:
    an announcement in the wrong accent beats silence on a plant floor.

    A per-language default naming a voice that does not exist is *not* quietly
    ignored -- it surfaces as ``UnknownVoiceError`` at synthesis, because a typo
    in configuration should be visible rather than papered over.
    """
    if voice:
        return voice

    if language:
        wanted = normalize_language(language)
        configured = settings.default_voices.get(wanted)
        if configured:
            return configured
        try:
            return registry.default_for_language(wanted).id
        except UnknownVoiceError:
            pass

    return settings.default_voice


@dataclass(frozen=True)
class AlertAudio:
    """Result of resolving an alert to a playable WAV file."""

    path: Path
    file_name: str
    cached: bool          # True when the file already existed
    voice: Optional[str]  # None when served from cache without synthesis

    def __fspath__(self) -> str:
        """Allow the result to be used anywhere a path is accepted."""
        return str(self.path)


class AlertSynthesizer:
    """Resolves alert text to a WAV file on disk, synthesizing only on a miss.

    Holds a :class:`~syscon_tts.engine.TTSEngine`, so loaded voice models stay
    cached in memory across calls. Construct one and keep it -- building a new
    instance per alert throws away the model cache and reintroduces the
    multi-second cold-load cost on every message.

    Settings can be passed field by field instead of assembled first, which is
    how an embedded caller avoids the environment entirely::

        AlertSynthesizer(alerts_dir=Path(settings.MEDIA_ROOT, "public_alert_sounds"))
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        registry: Optional[VoiceRegistry] = None,
        engine: Optional[TTSEngine] = None,
        **setting_overrides: Any,
    ):
        if settings is not None and setting_overrides:
            raise TypeError(
                "Pass either a Settings object or individual setting "
                "overrides, not both."
            )
        self.settings = settings or load_settings(**setting_overrides)
        self.registry = registry or VoiceRegistry.from_settings(self.settings)
        self.engine = engine or TTSEngine(
            self.registry, self.settings.max_loaded_voices
        )

    # -- lookup ------------------------------------------------------------

    def alerts_dir(self, override: Optional[Path] = None) -> Path:
        return Path(override) if override else self.settings.alerts_dir

    def path_for(
        self, file_name: str, alerts_dir: Optional[Path] = None
    ) -> Path:
        """Absolute path the WAV for ``file_name`` would occupy."""
        return self.alerts_dir(alerts_dir) / f"{file_name}.wav"

    def exists(self, text: str, alerts_dir: Optional[Path] = None) -> bool:
        """True if ``text`` already has generated audio on disk."""
        return self.path_for(sanitize_file_name(text), alerts_dir).is_file()

    # -- voice selection ---------------------------------------------------

    def resolve_voice(
        self, voice: Optional[str] = None, language: Optional[str] = None
    ) -> str:
        """Pick the voice id for a request. See :func:`resolve_voice_id`."""
        return resolve_voice_id(self.registry, self.settings, voice, language)

    def preload(
        self, voice: Optional[str] = None, language: Optional[str] = None
    ) -> str:
        """Load a voice model now instead of on the first alert.

        Returns the voice id that was loaded. Call this when the process
        starts -- the first synthesis otherwise pays a multi-second model load
        while somebody is waiting to hear an announcement.
        """
        voice_id = self.resolve_voice(voice, language)
        self.engine.preload(voice_id)
        return voice_id

    # -- resolution --------------------------------------------------------

    def ensure(
        self,
        text: str,
        file_name: Optional[str] = None,
        voice: Optional[str] = None,
        alerts_dir: Optional[Path] = None,
        speed: float = 1.0,
        sentence_silence: float = 0.2,
        force: bool = False,
        language: Optional[str] = None,
    ) -> AlertAudio:
        """Return the WAV for ``text``, synthesizing it only if absent.

        ``file_name`` overrides the derived name -- pass the APU's own
        ``get_valid_filename(message)[:50]`` result to guarantee both sides
        agree on the path. ``language`` selects a voice by locale (``es-mx``,
        ``zh-hans``) when the caller does not name one. Set ``force`` to
        regenerate even on a cache hit.

        Raises :class:`~syscon_tts.engine.SynthesisUnavailableError` when the
        file is missing and this machine cannot run Piper.
        """
        name = file_name or sanitize_file_name(text)
        directory = self.alerts_dir(alerts_dir)
        dest = directory / f"{name}.wav"

        if dest.is_file() and not force:
            return AlertAudio(path=dest, file_name=name, cached=True, voice=None)

        voice_id = self.resolve_voice(voice, language)
        audio = self.engine.synthesize_wav(
            text,
            voice_id,
            speed=speed,
            sentence_silence=sentence_silence,
        )
        self._ensure_directory(directory)
        _atomic_write(dest, audio, self.settings.file_mode)
        return AlertAudio(path=dest, file_name=name, cached=False, voice=voice_id)

    def _ensure_directory(self, directory: Path) -> None:
        """Create the alerts directory, applying the configured mode once.

        The mode is applied only when this call creates the directory, so an
        existing one keeps whatever ownership and permissions the host set --
        on the APU that is ``root:www-data 0770``, which this package has no
        business overwriting.
        """
        if directory.is_dir():
            return
        directory.mkdir(parents=True, exist_ok=True)
        _apply_mode(directory, self.settings.dir_mode)


def _apply_mode(path: Path, mode: Optional[int]) -> None:
    """Best-effort chmod. A no-op on Windows, and never fatal."""
    if mode is None:
        return
    try:
        os.chmod(path, mode)
    except OSError:
        # Windows implements only the read-only bit, and on Linux the path may
        # be owned by another account. Neither is worth failing an alert over.
        pass


def _atomic_write(dest: Path, payload: bytes, file_mode: Optional[int]) -> None:
    """Write ``payload`` to ``dest`` without exposing a partial file.

    The APU's websocket handler checks ``path.exists()`` before pushing the
    audio URL to clients, so a half-written file would be served as a truncated
    alert. Writing to a sibling temp file and renaming makes the file appear
    only once it is complete.

    ``mkstemp`` creates the temp file 0600 and ``os.replace`` preserves that,
    which would leave audio the web server cannot read -- hence the explicit
    chmod before the rename rather than after, so the file is never visible at
    the wrong mode.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _apply_mode(tmp, file_mode)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# Module-level convenience wrapper. Builds a fresh synthesizer per call, so it
# is fine for one-off scripts and the CLI but wasteful in a long-running
# process -- construct an AlertSynthesizer there instead.
def ensure_alert_wav(text: str, **kwargs) -> AlertAudio:
    """One-shot :meth:`AlertSynthesizer.ensure`."""
    return AlertSynthesizer().ensure(text, **kwargs)
