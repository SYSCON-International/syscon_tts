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

That split is what lets developers on Windows/macOS exercise the full alert
pipeline against audio generated on a Linux box, without installing Piper.

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
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import tempfiles
from .config import Settings, load_settings
from .engine import SynthesisError, TTSEngine
from .errors import SysconTTSError
from .voices import UnknownVoiceError, VoiceRegistry, normalize_language

logger = logging.getLogger(__name__)


class InvalidAlertNameError(SysconTTSError, ValueError):
    """Text or a supplied file name would be unsafe to write."""


class AlertWriteError(SysconTTSError, OSError):
    """The alerts directory or the WAV in it could not be written."""


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

#: Readable part of the name when the text sanitizes to nothing.
_FALLBACK_NAME_PREFIX = "alert"

#: Extension of every file this module writes.
_WAV_SUFFIX = ".wav"

#: Longest file name, in bytes, Linux filesystems accept (``NAME_MAX``).
_NAME_MAX_BYTES = 255

#: Locks shared out among alert paths by hash; see ``AlertSynthesizer``.
_LOCK_STRIPES = 32


def sanitize_file_name(text: str, max_length: int = MAX_FILE_NAME_LEN) -> str:
    """Derive a file name (no extension) from message text.

    Matches ``django.utils.text.get_valid_filename(text)[:max_length]`` for
    any ``max_length`` of 3 or more. A shorter limit can cut a valid name
    down to ``.`` or ``..``, which this rejects and the slice would not.

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


def _utf8(text: str) -> bytes:
    """``text`` as UTF-8, or :class:`InvalidAlertNameError` if it is not Unicode.

    A lone surrogate (from a bad decode upstream) cannot be encoded, and
    hashing it would otherwise escape as a bare ``UnicodeEncodeError``.
    """
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidAlertNameError(
            f"Text has an unpaired surrogate at position {exc.start}; it is "
            "not valid Unicode."
        ) from None


def alert_file_name(text: str, voice_id: str) -> str:
    """Unique, readable file name (no extension) for one alert.

    ``<first 41 sanitized chars>_<8 hex chars>``: 50 characters at most, the
    APU's existing limit. The hash covers the *full* text and the voice, so
    messages sharing a long prefix ("... on press 4" / "... on press 7") and
    the same text in two voices never share a file.
    """
    digest = hashlib.sha1(_utf8(f"{text}|{voice_id}")).hexdigest()
    prefix_len = MAX_FILE_NAME_LEN - _NAME_HASH_LEN - 1
    try:
        prefix = sanitize_file_name(text, prefix_len)
    except InvalidAlertNameError:
        # Punctuation-only text is still a valid announcement; the hash alone
        # identifies it.
        prefix = _FALLBACK_NAME_PREFIX
    return f"{prefix}_{digest[:_NAME_HASH_LEN]}"


def validate_file_name(name: str) -> str:
    """Return ``name`` if it is one plain path component, else raise.

    Guards the ``file_name`` argument of :meth:`AlertSynthesizer.ensure`:
    ``../x``, ``/abs/path`` and ``C:x`` must not steer a write outside the
    alerts directory, whoever the caller is forwarding the name from. The
    length is checked here too, before any synthesis, rather than surfacing
    as ``ENAMETOOLONG`` after the audio has been rendered.
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
    limit = _NAME_MAX_BYTES - len(_WAV_SUFFIX)
    if len(name.encode("utf-8")) > limit:
        raise InvalidAlertNameError(
            f"File name {name[:20]!r}... is too long: at most {limit} bytes "
            "as UTF-8."
        )
    return name


def request_fingerprint(
    text: str, voice_id: str, speed: float, sentence_silence: float
) -> str:
    """Identity of a rendering: what must match for a cached WAV to be reused."""
    payload = "\x00".join(
        (text, voice_id, repr(float(speed)), repr(float(sentence_silence)))
    )
    return hashlib.sha256(_utf8(payload)).hexdigest()


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

    The same goes for a language whose voice is catalogued but not installed
    (an es-mx site provisioned with en-us only): the default voice speaks,
    provided *it* is installed. When it is not either -- a development box with
    no models, replaying cached audio -- the language's own voice is kept, so
    file names do not change with what happens to be on disk.

    A per-language default naming a voice that does not exist is *not* quietly
    ignored -- it surfaces as ``UnknownVoiceError`` at synthesis, because a typo
    in configuration should be visible rather than papered over.
    """
    if voice:
        return voice

    if language:
        wanted = normalize_language(language)
        chosen = settings.default_voices.get(wanted)
        if not chosen:
            try:
                chosen = registry.default_for_language(wanted).id
            except UnknownVoiceError:
                _warn_once(
                    ("no-voice", wanted, settings.default_voice),
                    "No voice for language %r; falling back to default voice %s",
                    language, settings.default_voice,
                )
        if chosen:
            if (
                not registry.has(chosen)  # a typo: let synthesis report it
                or _installed(registry, chosen)
                or not _installed(registry, settings.default_voice)
            ):
                return chosen
            _warn_once(
                ("not-installed", wanted, chosen, settings.default_voice),
                "Voice %s for language %r is not installed; falling back to "
                "default voice %s",
                chosen, language, settings.default_voice,
            )

    return settings.default_voice


#: Fallbacks already logged. The APU resolves a voice for file_name_for() and
#: then once per connected client, so an unconditional warning would repeat
#: N+1 times per announcement. Bounded because the HTTP API passes callers'
#: language strings through here.
_warned: set = set()
_warned_lock = threading.Lock()
_MAX_WARNED = 64


def _warn_once(key: tuple, message: str, *args: Any) -> None:
    """Log a fallback at WARNING the first time, at DEBUG after that."""
    with _warned_lock:
        first = key not in _warned
        if first:
            if len(_warned) >= _MAX_WARNED:
                _warned.clear()
            _warned.add(key)
    logger.log(logging.WARNING if first else logging.DEBUG, message, *args)


def _installed(registry: VoiceRegistry, voice_id: str) -> bool:
    """True for a known voice whose files are on disk."""
    return registry.has(voice_id) and registry.is_installed(registry.get(voice_id))


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
        self._path_locks = [threading.Lock() for _ in range(_LOCK_STRIPES)]
        # Directories already swept for orphaned temp files.
        self._swept: set = set()
        self._swept_lock = threading.Lock()

    # -- lookup ------------------------------------------------------------

    def _resolve_alerts_dir(self, override: Optional[Path] = None) -> Path:
        """The per-call ``alerts_dir`` override if given, else the setting."""
        return Path(override) if override else self.settings.alerts_dir

    def path_for(
        self, file_name: str, alerts_dir: Optional[Path] = None
    ) -> Path:
        """Path the WAV for ``file_name`` would occupy.

        Relative when the alerts directory is configured as a relative path.
        """
        name = validate_file_name(file_name)
        return self._resolve_alerts_dir(alerts_dir) / f"{name}{_WAV_SUFFIX}"

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
        """True if a WAV for ``text`` is on disk under its derived name.

        Checks the name only. The file may come from a different request (another
        speed, say), so True does not promise that :meth:`ensure` will be a
        cache hit.
        """
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
        *,
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

        Everything after ``text`` is keyword-only, so ``ensure(msg, name,
        "es-mx")`` cannot pass a locale as the voice.

        Every error below derives from
        :class:`~syscon_tts.errors.SysconTTSError`:

        * :class:`AlertTextTooLongError` -- ``text`` is past
          ``max_alert_chars``.
        * :class:`InvalidAlertNameError` -- ``file_name`` is unsafe or too
          long, or ``text`` is not valid Unicode.
        * :class:`~syscon_tts.voices.UnknownVoiceError` -- the voice id does
          not exist.
        * :class:`~syscon_tts.voices.VoiceNotInstalledError` -- the voice's
          model files are not on disk.
        * :class:`~syscon_tts.engine.SynthesisUnavailableError` -- the file
          has to be rendered and this machine cannot run Piper.
        * :class:`~syscon_tts.engine.SynthesisError` -- Piper failed.
        * :class:`AlertWriteError` -- the directory or file could not be
          written. It is also an ``OSError``.
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
        dest = self.path_for(name, alerts_dir)
        directory = dest.parent
        fingerprint = request_fingerprint(text, voice_id, speed, sentence_silence)
        self._sweep_once(directory)

        # First look without the lock. A hit then never waits behind another
        # alert's seconds-long synthesis that happens to share its stripe. It
        # is safe because files only ever appear whole, by rename.
        if not force and read_fingerprint(dest) == fingerprint:
            logger.debug("Cache hit: %s", dest.name)
            return AlertAudio(path=dest, file_name=name, cached=True, voice=voice_id)

        with self._path_locks[hash(str(dest)) % len(self._path_locks)]:
            # Look again: another caller may have rendered it while this one
            # waited for the lock.
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
            payload = add_fingerprint(audio, fingerprint)
            try:
                self._ensure_directory(directory)
                _atomic_write(dest, payload, self.settings.file_mode)
            except OSError as exc:
                raise AlertWriteError(
                    exc.errno, f"Could not write {dest}: {exc.strerror or exc}"
                ) from exc
        return AlertAudio(path=dest, file_name=name, cached=False, voice=voice_id)

    def _ensure_directory(self, directory: Path) -> None:
        """Create the alerts directory and any missing parents at ``dir_mode``.

        Only directories this call creates get the mode, so an existing one
        keeps whatever ownership and permissions the host set. On the APU that
        is ``root:www-data 0770``, which this package has no business
        overwriting. ``mkdir(parents=True)`` would leave the parents at the
        umask's mode instead.
        """
        if directory.is_dir():
            return
        missing = []
        current = directory
        while not current.is_dir() and current.parent != current:
            missing.append(current)
            current = current.parent
        for path in reversed(missing):
            try:
                # Private until the chmod below widens it, so the directory
                # is never briefly more open than configured.
                os.mkdir(path, 0o700)
            except FileExistsError:
                if path.is_dir():
                    continue  # created concurrently; not ours to chmod
                raise
            _apply_dir_mode(path, self.settings.dir_mode)

    def _sweep_once(self, directory: Path) -> None:
        """Delete temp files orphaned in ``directory``, once per process.

        A render killed between the temp file and the rename (SIGKILL, power
        loss) leaves its ``.part`` file behind, and nothing else removes it:
        the APU's cleanup only knows the names it handed out, and the backup
        copies the whole media tree. A process start is exactly when such
        orphans can exist, so once per directory per synthesizer is enough.
        """
        with self._swept_lock:
            if directory in self._swept:
                return
            self._swept.add(directory)
        tempfiles.sweep_stale(directory)


# -- fingerprint chunk -------------------------------------------------------
#
# Stored as a LIST/INFO/ICMT (comment) chunk, the standard home for free-form
# WAV metadata, appended after the audio data where players skip it.

_FINGERPRINT_PREFIX = b"syscon-tts:"

#: ``RIFF`` + 4-byte size + ``WAVE``.
_RIFF_HEADER_LEN = 12

#: 4-byte id + 4-byte little-endian size, for chunks and INFO sub-chunks.
_CHUNK_HEADER_LEN = 8

#: Largest ``LIST`` chunk read looking for a fingerprint. Ours is under 100
#: bytes; a bigger one belongs to someone else and is skipped, not read.
_MAX_INFO_CHUNK_LEN = 4096


def add_fingerprint(wav: bytes, fingerprint: str) -> bytes:
    """Return ``wav`` with ``fingerprint`` appended as a ``LIST/INFO`` chunk."""
    if not _is_riff_wave(wav):
        raise SynthesisError("Engine did not return a RIFF/WAVE file.")
    if len(wav) % 2:
        wav += b"\x00"  # chunks start on even offsets
    comment = _FINGERPRINT_PREFIX + fingerprint.encode("ascii") + b"\x00"
    if len(comment) % 2:
        comment += b"\x00"
    info = b"INFO" + b"ICMT" + struct.pack("<I", len(comment)) + comment
    chunk = b"LIST" + struct.pack("<I", len(info)) + info
    body = wav[_RIFF_HEADER_LEN:] + chunk
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body


def read_fingerprint(path: Path) -> Optional[str]:
    """The fingerprint :func:`add_fingerprint` stored in ``path``, if any.

    Walks chunk headers with seeks rather than reading the audio. Anything
    unexpected returns None: a file this cannot vouch for is a cache miss.
    """
    try:
        with open(path, "rb") as handle:
            if not _is_riff_wave(handle.read(_RIFF_HEADER_LEN)):
                return None
            while True:
                chunk_header = handle.read(_CHUNK_HEADER_LEN)
                if len(chunk_header) < _CHUNK_HEADER_LEN:
                    return None
                chunk_id = chunk_header[:4]
                size = struct.unpack("<I", chunk_header[4:])[0]
                if chunk_id == b"LIST" and size <= _MAX_INFO_CHUNK_LEN:
                    found = _fingerprint_from_info(handle.read(size))
                    if found:
                        return found
                    handle.seek(size % 2, os.SEEK_CUR)
                else:
                    handle.seek(size + size % 2, os.SEEK_CUR)
    except OSError:
        return None


def _is_riff_wave(header: bytes) -> bool:
    return (
        len(header) >= _RIFF_HEADER_LEN
        and header[:4] == b"RIFF"
        and header[8:12] == b"WAVE"
    )


def _fingerprint_from_info(data: bytes) -> Optional[str]:
    if data[:4] != b"INFO":
        return None
    offset = 4
    while offset + _CHUNK_HEADER_LEN <= len(data):
        sub_id = data[offset:offset + 4]
        size = struct.unpack("<I", data[offset + 4:offset + _CHUNK_HEADER_LEN])[0]
        start = offset + _CHUNK_HEADER_LEN
        value = data[start:start + size]
        if sub_id == b"ICMT" and value.startswith(_FINGERPRINT_PREFIX):
            found = value[len(_FINGERPRINT_PREFIX):].rstrip(b"\x00")
            return found.decode("ascii", "replace")
        offset = start + size + size % 2
    return None


def _apply_fd_mode(fd: int, mode: Optional[int]) -> None:
    """Best-effort chmod through a descriptor, which no symlink can redirect.

    Never by path: on the APU this runs as root in a directory www-data can
    write. ``os.fchmod`` is POSIX-only before Python 3.13, and Windows has no
    mode worth setting, so its absence is a no-op. Failure is never fatal --
    a mode is not worth failing an alert over.
    """
    if mode is None or not hasattr(os, "fchmod"):
        return
    try:
        os.fchmod(fd, mode)
    except OSError:
        pass


def _apply_dir_mode(path: Path, mode: Optional[int]) -> None:
    """:func:`_apply_fd_mode` for a directory, opened without following links."""
    flags = getattr(os, "O_DIRECTORY", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if mode is None or flags is None or nofollow is None:
        return  # Windows
    try:
        fd = os.open(path, os.O_RDONLY | flags | nofollow)
    except OSError:
        return
    try:
        _apply_fd_mode(fd, mode)
    finally:
        os.close(fd)


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

    The chmod goes through the open descriptor, never the path. On the APU this
    runs as root inside a directory www-data can write, so a by-path chmod
    could be redirected through a symlink swapped in for the temp file.
    """
    fd, tmp = tempfiles.make_temp(dest.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            _apply_fd_mode(handle.fileno(), file_mode)
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
