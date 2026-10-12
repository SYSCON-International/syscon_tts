"""Process isolation for Piper synthesis.

The pinned ``piper-phonemize 1.1.0`` bundles a 2023 ``espeak-ng`` that can
**segfault on crafted text** -- reproduced at 150-200 characters, inside the
APU's 200-character public-address limit. When synthesis runs in the caller's
own process -- on the APU that is the *root* Tornado socket manager -- such a
crash is memory corruption that takes the whole process down: every websocket
it serves drops, plant-floor announcements go silent until a restart, and a
corrupted root process is a possible route to code execution.

This module runs synthesis in a separate worker process. A crash there becomes
a :class:`~syscon_tts.engine.SynthesisError` in the caller instead of killing
it, and the worker can drop to an unprivileged account first, so the fault
never has root to work with.

Why spawn, not fork
-------------------
The worker is *spawned*, never forked. The APU is multithreaded (synthesis is
driven from a Tornado thread pool), and forking a process that holds
onnxruntime's thread pool and arbitrary interpreter locks is its own source of
deadlocks. A spawned child starts from a clean interpreter, builds its own
:class:`~syscon_tts.engine.TTSEngine`, and keeps voice models resident across
requests -- so the model cache the engine provides is preserved. It simply
lives in the worker rather than in the caller.

The worker only ever *reads* voice models and returns WAV bytes; the caller
writes the file. So the unprivileged account needs read access to the voices
directory and nothing more (models are installed ``0644``; see the changelog).

Concurrency
-----------
One worker, one request at a time, serialized by a lock on the caller side.
That matches the engine's existing per-voice serialization: synthesis is a
CPU-bound burn and parallel synthesis on one box that also runs the plant is
not a win.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import threading
from typing import List, Optional

from .config import Settings
from .engine import (
    SynthesisError,
    SynthesisUnavailableError,
    TTSEngine,
)
from .errors import SysconTTSError
from .voices import (
    UnknownVoiceError,
    VoiceError,
    VoiceNotInstalledError,
    VoiceRegistry,
)

logger = logging.getLogger(__name__)

#: Errors the worker may raise that the caller must see as themselves rather
#: than as a generic failure (``ensure`` and the CLI branch on these). Anything
#: not listed is rebuilt as :class:`SynthesisError`.
_ERROR_CLASSES = {
    cls.__name__: cls
    for cls in (
        SynthesisUnavailableError,
        SynthesisError,
        VoiceNotInstalledError,
        UnknownVoiceError,
        VoiceError,
        SysconTTSError,
    )
}

#: Internal diagnostic. When ``SYSCON_TTS_WORKER_SELFTEST=1`` is set, a
#: synthesis request for this exact text makes the worker abort the way a
#: native fault would, so the isolation can be verified on a host without
#: Piper (and in CI on every platform). Never set in production; the sentinel
#: cannot occur in real alert text, which is validated Unicode without NULs.
_CRASH_SENTINEL = "\x00syscon-tts-selftest-crash\x00"


def _drop_privileges(user: str) -> None:
    """Drop to ``user``'s uid/gid. No-op unless we are root on a POSIX host.

    Dropping is only possible as root; when the worker already runs
    unprivileged (the systemd server, a developer checkout) there is nothing
    to drop and the configured account is ignored rather than fought.
    """
    if os.name != "posix" or not hasattr(os, "geteuid"):
        return
    if os.geteuid() != 0:
        logger.debug("Not root; leaving synthesis worker as the current user.")
        return
    import pwd

    pw = pwd.getpwnam(user)
    os.setgroups([])
    os.initgroups(user, pw.pw_gid)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    logger.info("Synthesis worker dropped to %s (uid=%d).", user, pw.pw_uid)


def _worker_main(conn, settings: Settings, user: Optional[str]) -> None:
    """Worker entry point: serve synthesis requests until the pipe closes.

    Runs in the spawned child. Builds the engine once and reuses it, so voice
    models load at most once per worker lifetime.
    """
    selftest = os.environ.get("SYSCON_TTS_WORKER_SELFTEST") == "1"
    if user:
        try:
            _drop_privileges(user)
        except Exception as exc:  # pragma: no cover - depends on host accounts
            # Refuse to serve with more privilege than asked for.
            conn.send(("fatal", f"Could not drop to {user!r}: {exc}"))
            conn.close()
            return

    engine: Optional[TTSEngine] = None

    def _engine() -> TTSEngine:
        nonlocal engine
        if engine is None:
            registry = VoiceRegistry.from_settings(settings)
            engine = TTSEngine(
                registry,
                settings.max_loaded_voices,
                threads=settings.threads,
            )
        return engine

    while True:
        try:
            request = conn.recv()
        except EOFError:
            return  # caller closed the pipe; shut down quietly

        op = request[0]
        if op == "stop":
            return

        if op == "synthesize":
            _, text, voice_id, speed, sentence_silence = request
            # Checked before the engine is touched, so the fault can be
            # exercised on a host without Piper.
            if selftest and text == _CRASH_SENTINEL:
                os.abort()
            try:
                audio = _engine().synthesize_wav(
                    text, voice_id, speed=speed, sentence_silence=sentence_silence
                )
                conn.send(("ok", audio))
            except SysconTTSError as exc:
                conn.send(("err", type(exc).__name__, str(exc)))
            except Exception as exc:  # pragma: no cover - unexpected
                conn.send(("err", "SynthesisError", f"Synthesis failed: {exc}"))
        elif op == "preload":
            try:
                _engine().preload(request[1])
                conn.send(("ok", None))
            except SysconTTSError as exc:
                conn.send(("err", type(exc).__name__, str(exc)))
        elif op == "loaded":
            try:
                conn.send(("ok", _engine().loaded_voice_ids()))
            except SysconTTSError as exc:
                conn.send(("err", type(exc).__name__, str(exc)))
        else:  # pragma: no cover - protocol error
            conn.send(("err", "SynthesisError", f"Unknown worker op {op!r}."))


class WorkerEngine:
    """Drop-in for :class:`TTSEngine` that runs synthesis in a worker process.

    Interface-compatible with the parts of ``TTSEngine`` the alert pipeline
    uses (:meth:`synthesize_wav`, :meth:`synthesize`, :meth:`preload`,
    :meth:`loaded_voice_ids`, :attr:`threads`), so
    :class:`~syscon_tts.alerts.AlertSynthesizer` can hold one in place of the
    in-process engine with no other change.

    The worker is spawned lazily on first use and respawned after a crash, so
    constructing this costs nothing until synthesis is actually needed, and a
    single bad message cannot wedge the service.
    """

    #: How long to wait for a crashed worker to be reaped before giving up.
    _REAP_TIMEOUT = 5.0

    def __init__(self, settings: Settings):
        self._settings = settings
        self._user = settings.worker_user
        self._threads = settings.threads
        self._ctx = multiprocessing.get_context("spawn")
        self._lock = threading.Lock()
        self._proc = None
        self._conn = None

    # -- lifecycle ---------------------------------------------------------

    def _ensure_worker(self) -> None:
        """Spawn the worker if it is not running. Caller holds ``_lock``."""
        if self._proc is not None and self._proc.is_alive():
            return
        self._teardown()
        parent_conn, child_conn = self._ctx.Pipe()
        proc = self._ctx.Process(
            target=_worker_main,
            args=(child_conn, self._settings, self._user),
            name="syscon-tts-synth",
            daemon=True,
        )
        proc.start()
        child_conn.close()  # the child holds the only copy it needs
        self._proc = proc
        self._conn = parent_conn

    def _teardown(self) -> None:
        """Forget and stop the current worker. Caller holds ``_lock``."""
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        if self._proc is not None and self._proc.is_alive():
            self._proc.terminate()
        self._proc = None

    def close(self) -> None:
        """Stop the worker. Safe to call more than once."""
        with self._lock:
            if self._conn is not None and self._proc is not None and self._proc.is_alive():
                try:
                    self._conn.send(("stop",))
                except OSError:
                    pass
            self._teardown()

    # -- request plumbing --------------------------------------------------

    def _exit_description(self) -> str:
        """Why the worker is gone, in operator terms."""
        code = self._proc.exitcode if self._proc is not None else None
        if code is None:
            return "the synthesis worker stopped responding"
        if code < 0:
            try:
                name = signal.Signals(-code).name
            except ValueError:  # pragma: no cover - exotic signal
                name = f"signal {-code}"
            return f"the synthesis worker was killed by {name}"
        return f"the synthesis worker exited with status {code}"

    def _request(self, message):
        """Send one request and return its result, isolating a worker crash.

        A crash (segfault, abort, OOM kill) closes the pipe; the broken
        ``recv`` is turned into a :class:`SynthesisError` and the worker is
        dropped so the next call respawns a clean one.
        """
        with self._lock:
            self._ensure_worker()
            try:
                self._conn.send(message)
                response = self._conn.recv()
            except (EOFError, BrokenPipeError, OSError):
                if self._proc is not None:
                    self._proc.join(self._REAP_TIMEOUT)
                reason = self._exit_description()
                self._teardown()
                raise SynthesisError(
                    f"Synthesis failed: {reason}. The text may have triggered "
                    "an espeak-ng fault; the service survived it. Retry, or "
                    "capture the text for the crash corpus."
                ) from None

            tag = response[0]
            if tag == "ok":
                return response[1]
            if tag == "fatal":
                self._teardown()
                raise SynthesisError(f"Synthesis failed: {response[1]}")
            # ("err", class_name, message)
            cls = _ERROR_CLASSES.get(response[1], SynthesisError)
            raise cls(response[2])

    # -- TTSEngine-compatible surface --------------------------------------

    def synthesize_wav(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        sentence_silence: float = 0.2,
    ) -> bytes:
        return self._request(
            ("synthesize", text, voice_id, speed, sentence_silence)
        )

    def synthesize(
        self,
        text: str,
        voice_id: str,
        fmt: str = "wav",
        speed: float = 1.0,
        sentence_silence: float = 0.2,
    ) -> tuple[bytes, str]:
        """Render ``text``; the WAV is produced in the worker, MP3 in-process.

        Only the Piper step is the crash risk, so only it is isolated. The
        ffmpeg conversion stays here, reusing the engine's implementation.
        """
        fmt = fmt.lower()
        if fmt not in ("wav", "mp3"):
            raise SynthesisError(f"Unsupported format '{fmt}'. Use 'wav' or 'mp3'.")
        wav_bytes = self.synthesize_wav(text, voice_id, speed, sentence_silence)
        if fmt == "wav":
            return wav_bytes, "audio/wav"
        return TTSEngine._wav_to_mp3(wav_bytes), "audio/mpeg"

    def preload(self, voice_id: str) -> None:
        self._request(("preload", voice_id))

    def loaded_voice_ids(self) -> List[str]:
        try:
            return self._request(("loaded",))
        except SynthesisError:
            return []

    @property
    def threads(self) -> Optional[int]:
        return self._threads
