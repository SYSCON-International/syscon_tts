"""Cache-first alert audio.

This is the module the PlantStar APU integrates against. It encapsulates the
one contract the rest of the APU actually depends on: a WAV file exists at
``<alerts_dir>/<file_name>.wav``. Everything downstream -- the websocket push,
``/media/`` serving, and the file-cleanup pass -- is agnostic about how that
file got there.

The lookup is deliberately **cache first**:

* If the WAV already exists *and was rendered from the same request* -- same
  text, voice, speed and sentence silence -- it is returned immediately. This
  path needs no Piper, so it works on Windows and macOS exactly as it does on
  Linux.
* Otherwise synthesis is attempted, which requires Piper and therefore Linux.
  On other platforms this raises
  :class:`~syscon_tts.engine.SynthesisUnavailableError`, which callers can
  catch to degrade gracefully.

"The same request" is checked, not assumed. Every WAV written here carries a
fingerprint of its inputs in a standard RIFF ``LIST/INFO`` chunk, which
players ignore, and a file whose fingerprint does not match -- or that has
none -- is a cache miss. Keeping it inside the WAV rather than in a sidecar
file means the APU's cleanup pass deletes it along with the audio.

File names are unique per message by default: :func:`alert_file_name` keeps
the first 41 characters of the sanitized text, so a directory listing stays
readable, and appends a hash of the full text and voice. Without the hash, two
messages differing only after character 50 would share one file, and a
listener still waiting to play the first would hear the second.

That split is what lets developers on Windows/macOS exercise the full alert
pipeline against audio generated on a Linux box, without installing Piper.

A multilingual site names a language rather than a voice::

    synth = AlertSynthesizer(alerts_dir=..., default_voices={"zh_CN": "zh_cn_huayan"})
    synth.ensure("Press 4 fault", language="zh-hans")

One utterance is rendered by one voice, so text that mixes languages is spoken
by whichever voice was selected -- split the message instead.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import struct
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import Settings, load_settings
from .engine import SynthesisError, TTSEngine
from .voices import UnknownVoiceError, VoiceRegistry, normalize_language

logger = logging.getLogger("syscon_tts")


class InvalidAlertNameError(ValueError):
    """Text or a supplied file name would be unsafe to write."""


class AlertTextTooLongError(SynthesisError):
    """Alert text is longer than ``Settings.max_alert_chars``."""


# Django's ``get_valid_filename`` strips everything outside [-\w.] after
# collapsing spaces to underscores. Reproduced here (rather than imported) so
# the package has no Django dependency, while still computing byte-identical
# names to the APU's existing ``get_valid_filename(message)[:50]``.
_UNSAFE_CHARS = re.compile(r"(?u)[^-\w.]")

#: A caller-supplied file name must be one plain path component made of the
#: characters :func:`sanitize_file_name` can produce.
_SAFE_FILE_NAME = re.compile(r"(?u)[-\w.]+")

#: The APU truncates alert file names to this length.
MAX_FILE_NAME_LEN = 50

#: Hex digits of hash :func:`alert_file_name` appends.
_NAME_HASH_LEN = 8


def sanitize_file_name(text: str, max_length: int = MAX_FILE_NAME_LEN) -> str:
    """Derive a file name (no extension) from message text.

    Matches ``django.utils.text.get_valid_filename(text)[:max_length]``.

    The character class is Unicode-aware, so a Chinese or Spanish message keeps
    its own characters in the file name. Two consequences worth knowing when
    the text is not English: the resulting ``/media/`` URL needs percent
    encoding, and the limit counts *characters*, so 50 Han characters is a much
    longer utterance than 50 Latin ones.

    On its own this is not unique -- messages that differ only past
    ``max_length`` collide. :func:`alert_file_name` is what :meth:`ensure`
    uses.
    """
    name = str(text).strip().replace(" ", "_")
    name = _UNSAFE_CHARS.sub("", name)
    name = name[:max_length]
    if name in ("", ".", ".."):
        raise InvalidAlertNameError(
            f"Text {text!r} does not yield a usable file name."
        )
    return name


def alert_file_name(text: str, voice_id: str) -> str:
    """Unique, readable file name (no extension) for one alert.

    ``<first 41 sanitized chars>_<8 hex chars>``: 50 characters at most, the
    APU's existing limit. The hash covers the *full* text and the voice, so
    messages sharing a long prefix ("... on press 4" / "... on press 7") and
    the same text in two voices never share a file.
    """
    digest = hashlib.sha1(f"{text}|{voice_id}".encode()).hexdigest()
    prefix_len = MAX_FILE_NAME_LEN - _NAME_HASH_LEN - 1
    try:
        prefix = sanitize_file_name(text, prefix_len)
    except InvalidAlertNameError:
        # Punctuation-only text is still a valid announcement; the hash alone
        # identifies it.
        prefix = "alert"
    return f"{prefix}_{digest[:_NAME_HASH_LEN]}"


def validate_file_name(name: str) -> str:
    """Return ``name`` if it is one plain path component, else raise.

    Guards the ``file_name`` argument of :meth:`AlertSynthesizer.ensure`:
    ``../x``, ``/abs/path`` and ``C:x`` must not steer a write outside the
    alerts directory, whoever the caller is forwarding the name from.
    """
    if (
        not isinstance(name, str)
        or name in (".", "..")
        or not _SAFE_FILE_NAME.fullmatch(name)
    ):
        raise InvalidAlertNameError(
            f"File name {name!r} is not allowed: use letters, digits, '_', "
            "'-' and '.' only, with no path separators."
        )
    return name


def request_fingerprint(
    text: str, voice_id: str, speed: float, sentence_silence: float
) -> str:
    """Identity of a rendering: what must match for a cached WAV to be reused."""
    payload = "\x00".join(
        (text, voice_id, repr(float(speed)), repr(float(sentence_silence)))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
            logger.warning(
                "No voice for language %r; falling back to default voice %s",
                language, settings.default_voice,
            )

    return settings.default_voice


@dataclass(frozen=True)
class AlertAudio:
    """Result of resolving an alert to a playable WAV file."""

    path: Path
    file_name: str
    cached: bool  # True when an existing file was reused
    voice: str    # the voice the audio was rendered in

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
            self.registry,
            self.settings.max_loaded_voices,
            threads=self.settings.threads,
        )
        # Concurrent ensure() calls for one file wait for each other, so the
        # second finds the first's WAV instead of rendering it again -- the
        # APU calls ensure() once per connected client for the same message.
        # Striped rather than one lock per path, so the table never grows.
        self._path_locks = [threading.Lock() for _ in range(32)]

    # -- lookup ------------------------------------------------------------

    def alerts_dir(self, override: Optional[Path] = None) -> Path:
        return Path(override) if override else self.settings.alerts_dir

    def path_for(
        self, file_name: str, alerts_dir: Optional[Path] = None
    ) -> Path:
        """Absolute path the WAV for ``file_name`` would occupy."""
        return self.alerts_dir(alerts_dir) / f"{validate_file_name(file_name)}.wav"

    def file_name_for(
        self,
        text: str,
        voice: Optional[str] = None,
        language: Optional[str] = None,
    ) -> str:
        """The file name :meth:`ensure` derives for ``text``.

        For callers that need the name before the audio exists -- the APU
        builds the client URL from it. Pass the same ``voice`` / ``language``
        that will be passed to :meth:`ensure`.
        """
        return alert_file_name(text, self.resolve_voice(voice, language))

    def exists(
        self,
        text: str,
        alerts_dir: Optional[Path] = None,
        voice: Optional[str] = None,
        language: Optional[str] = None,
    ) -> bool:
        """True if a WAV for ``text`` is on disk under its derived name."""
        name = self.file_name_for(text, voice, language)
        return self.path_for(name, alerts_dir).is_file()

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
        """Return the WAV for ``text``, synthesizing it only if needed.

        ``file_name`` overrides the derived name (:meth:`file_name_for`) and
        must be a single plain path component. ``language`` selects a voice by
        locale (``es-mx``, ``zh-hans``) when the caller does not name one. Set
        ``force`` to regenerate even on a cache hit.

        An existing file is reused only when its embedded fingerprint matches
        this request; a stale or colliding file is rendered again, never
        announced.

        Raises :class:`AlertTextTooLongError` past ``max_alert_chars``,
        :class:`InvalidAlertNameError` for an unsafe ``file_name``, and
        :class:`~syscon_tts.engine.SynthesisUnavailableError` when the file
        has to be rendered and this machine cannot run Piper.
        """
        if len(text) > self.settings.max_alert_chars:
            raise AlertTextTooLongError(
                f"Alert text is {len(text)} characters; the limit is "
                f"{self.settings.max_alert_chars} (max_alert_chars)."
            )
        voice_id = self.resolve_voice(voice, language)
        if file_name is None:
            name = alert_file_name(text, voice_id)
        else:
            name = validate_file_name(file_name)
        directory = self.alerts_dir(alerts_dir)
        dest = directory / f"{name}.wav"
        fingerprint = request_fingerprint(text, voice_id, speed, sentence_silence)

        with self._path_locks[hash(str(dest)) % len(self._path_locks)]:
            if not force and dest.is_file():
                if read_fingerprint(dest) == fingerprint:
                    logger.debug("Cache hit: %s", dest.name)
                    return AlertAudio(
                        path=dest, file_name=name, cached=True, voice=voice_id
                    )
                logger.info(
                    "Re-rendering %s: the file on disk came from a different "
                    "request", dest.name,
                )
            else:
                logger.debug("Cache miss: %s", dest.name)

            audio = self.engine.synthesize_wav(
                text,
                voice_id,
                speed=speed,
                sentence_silence=sentence_silence,
            )
            self._ensure_directory(directory)
            _atomic_write(
                dest, add_fingerprint(audio, fingerprint), self.settings.file_mode
            )
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


# -- fingerprint chunk -------------------------------------------------------
#
# Stored as a LIST/INFO/ICMT (comment) chunk, the standard home for free-form
# WAV metadata, appended after the audio data where players skip it.

_FINGERPRINT_PREFIX = b"syscon-tts:"


def add_fingerprint(wav: bytes, fingerprint: str) -> bytes:
    """Return ``wav`` with ``fingerprint`` appended as a ``LIST/INFO`` chunk."""
    if len(wav) < 12 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise SynthesisError("Engine did not return a RIFF/WAVE file.")
    if len(wav) % 2:
        wav += b"\x00"  # chunks start on even offsets
    comment = _FINGERPRINT_PREFIX + fingerprint.encode("ascii") + b"\x00"
    if len(comment) % 2:
        comment += b"\x00"
    info = b"INFO" + b"ICMT" + struct.pack("<I", len(comment)) + comment
    chunk = b"LIST" + struct.pack("<I", len(info)) + info
    body = wav[12:] + chunk
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body


def read_fingerprint(path: Path) -> Optional[str]:
    """The fingerprint :func:`add_fingerprint` stored in ``path``, if any.

    Walks chunk headers with seeks rather than reading the audio. Anything
    unexpected returns None: a file this cannot vouch for is a cache miss.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return None
            while True:
                chunk_header = handle.read(8)
                if len(chunk_header) < 8:
                    return None
                chunk_id = chunk_header[:4]
                size = struct.unpack("<I", chunk_header[4:])[0]
                if chunk_id == b"LIST" and size <= 4096:
                    found = _fingerprint_from_info(handle.read(size))
                    if found:
                        return found
                    handle.seek(size % 2, os.SEEK_CUR)
                else:
                    handle.seek(size + size % 2, os.SEEK_CUR)
    except OSError:
        return None


def _fingerprint_from_info(data: bytes) -> Optional[str]:
    if data[:4] != b"INFO":
        return None
    offset = 4
    while offset + 8 <= len(data):
        sub_id = data[offset:offset + 4]
        size = struct.unpack("<I", data[offset + 4:offset + 8])[0]
        value = data[offset + 8:offset + 8 + size]
        if sub_id == b"ICMT" and value.startswith(_FINGERPRINT_PREFIX):
            found = value[len(_FINGERPRINT_PREFIX):].rstrip(b"\x00")
            return found.decode("ascii", "replace")
        offset += 8 + size + size % 2
    return None


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
