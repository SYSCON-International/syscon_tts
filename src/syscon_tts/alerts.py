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
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Settings, load_settings
from .engine import TTSEngine
from .voices import VoiceRegistry


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
    """
    name = str(text).strip().replace(" ", "_")
    name = _UNSAFE_CHARS.sub("", name)
    name = name[:max_length]
    if name in ("", ".", ".."):
        raise InvalidAlertNameError(
            f"Text {text!r} does not yield a usable file name."
        )
    return name


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
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        registry: Optional[VoiceRegistry] = None,
        engine: Optional[TTSEngine] = None,
    ):
        self.settings = settings or load_settings()
        self.registry = registry or VoiceRegistry.from_manifest(
            self.settings.voices_manifest, self.settings.voices_dir
        )
        self.engine = engine or TTSEngine(self.registry)

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
    ) -> AlertAudio:
        """Return the WAV for ``text``, synthesizing it only if absent.

        ``file_name`` overrides the derived name -- pass the APU's own
        ``get_valid_filename(message)[:50]`` result to guarantee both sides
        agree on the path. Set ``force`` to regenerate even on a cache hit.

        Raises :class:`~syscon_tts.engine.SynthesisUnavailableError` when the
        file is missing and this machine cannot run Piper.
        """
        name = file_name or sanitize_file_name(text)
        directory = self.alerts_dir(alerts_dir)
        dest = directory / f"{name}.wav"

        if dest.is_file() and not force:
            return AlertAudio(path=dest, file_name=name, cached=True, voice=None)

        voice_id = voice or self.settings.default_voice
        audio = self.engine.synthesize_wav(
            text,
            voice_id,
            speed=speed,
            sentence_silence=sentence_silence,
        )
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(dest, audio)
        return AlertAudio(path=dest, file_name=name, cached=False, voice=voice_id)


def _atomic_write(dest: Path, payload: bytes) -> None:
    """Write ``payload`` to ``dest`` without exposing a partial file.

    The APU's websocket handler checks ``path.exists()`` before pushing the
    audio URL to clients, so a half-written file would be served as a truncated
    alert. Writing to a sibling temp file and renaming makes the file appear
    only once it is complete.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
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
