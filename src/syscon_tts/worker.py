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
it, and the worker runs unprivileged, so the fault never has root to work with.

Why a fresh interpreter, not ``multiprocessing``
------------------------------------------------
The worker is a **fresh Python interpreter** -- ``python -I`` running
:mod:`syscon_tts._worker_entry` under :class:`subprocess.Popen` -- not a
``multiprocessing`` child. ``multiprocessing`` "spawn" re-imports the
embedder's ``__main__``; on the APU that is ``ProcessSpawner.py``, whose
module-level ``django.setup()`` would then re-run **as root**, doubling the
worker's memory and its root-time attack surface, and failing outright on any
host whose main script has no ``if __name__ == "__main__"`` guard. A clean
``-I`` interpreter imports none of that, builds its own
:class:`~syscon_tts.engine.TTSEngine`, and keeps voice models resident across
requests, so the model cache is preserved -- it simply lives in the worker.

Forking was never an option: the APU is multithreaded (synthesis is driven from
a Tornado thread pool), and forking a process that holds onnxruntime's thread
pool and arbitrary interpreter locks is its own source of deadlocks.

Privilege separation
--------------------
On the APU the parent is root, so the worker is dropped to ``worker_user`` at
exec, by the OS (:class:`subprocess.Popen`'s ``user``/``group``), before this
package's code runs in the child. The worker only ever *reads* voice models and
returns WAV bytes; the caller writes the file. So the unprivileged account
needs read access to the voices directory and nothing more (models are
installed ``0640``/``0644``; see the changelog and DEPLOY). When no
``worker_user`` is set on a root host, the worker would run as root; that is a
deployment defect, so it is logged loudly on the first spawn.

Why the caller never unpickles the worker's replies
---------------------------------------------------
The worker is the process most likely to be corrupted -- it runs the fragile
espeak-ng on attacker-influenced text -- so the caller (root, on the APU) must
not unpickle what it sends. Every frame is raw length-prefixed bytes: requests
are JSON, replies are a one-byte tag plus either raw audio or JSON. The caller
decodes with :func:`json.loads`, which executes nothing, maps an error only to a
fixed whitelist of exception classes, and caps the frame size. A worker that
*hangs* rather than crashes is bounded too: every request waits at most
``worker_timeout`` seconds, then the worker is killed.

Concurrency
-----------
One worker, one request at a time, serialized by a lock on the caller side.
That matches the engine's existing per-voice serialization: synthesis is a
CPU-bound burn and parallel synthesis on one box that also runs the plant is
not a win.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import struct
import subprocess
import sys
import threading
from typing import List, Optional

from .config import Settings
from .engine import (
    SynthesisError,
    SynthesisUnavailableError,
    SynthesizerClosedError,
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

#: Largest frame accepted in either direction, a guard against a compromised
#: worker forcing an unbounded allocation in the root parent. A 1000-character
#: alert (``max_alert_chars``) renders to a few MB of WAV; 64 MB is far past any
#: real announcement, and a frame over it is treated as a crash.
_MAX_FRAME = 64 * 1024 * 1024

# Reply tags: the first byte of every worker->caller frame says how to read the
# rest, so the caller never has to unpickle to find out.
_TAG_OK_BYTES = b"O"  # payload is raw audio bytes (synthesize)
_TAG_OK_JSON = b"J"   # payload is a JSON value (loaded_voice_ids list, or null)
_TAG_ERR = b"E"       # payload is JSON [class_name, message]; a known failure
_TAG_FATAL = b"F"     # payload is a UTF-8 message; the worker is giving up

#: 4-byte big-endian length prefix on every frame.
_LEN = struct.Struct(">I")

#: Internal diagnostics. When ``SYSCON_TTS_WORKER_SELFTEST=1`` is set, a
#: synthesis request for one of these exact texts makes the worker misbehave
#: the way a native fault would -- :data:`_CRASH_SENTINEL` aborts (a segfault),
#: :data:`_HANG_SENTINEL` blocks forever (an espeak-ng infinite loop). Both let
#: the isolation and the timeout be verified on a host without Piper (and in CI
#: on every platform). Never set in production; neither sentinel can occur in
#: real alert text, which is validated Unicode without NULs.
_CRASH_SENTINEL = "\x00syscon-tts-selftest-crash\x00"
_HANG_SENTINEL = "\x00syscon-tts-selftest-hang\x00"


# -- framing ------------------------------------------------------------------


def _write_frame(stream, data: bytes) -> None:
    """Write one length-prefixed frame and flush it."""
    stream.write(_LEN.pack(len(data)))
    stream.write(data)
    stream.flush()


def _read_exact(stream, n: int) -> bytes:
    """Read exactly ``n`` bytes, or raise ``EOFError`` if the stream ends."""
    chunks = []
    remaining = n
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(stream, maxlen: int = _MAX_FRAME) -> bytes:
    """Read one length-prefixed frame. Raises ``EOFError`` or ``OSError``."""
    (length,) = _LEN.unpack(_read_exact(stream, _LEN.size))
    if length > maxlen:
        raise OSError(f"frame of {length} bytes exceeds the {maxlen}-byte limit")
    return _read_exact(stream, length)


# -- worker side (runs in the spawned interpreter) ----------------------------


def _serve(settings: Settings, read_stream, write_stream) -> None:
    """Serve synthesis requests until the request stream closes.

    Runs in the spawned child (launched by :mod:`syscon_tts._worker_entry`).
    Builds the engine once and reuses it, so voice models load at most once per
    worker lifetime. Privilege was already dropped by the parent's exec.
    """
    selftest = os.environ.get("SYSCON_TTS_WORKER_SELFTEST") == "1"
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

    def _reply(tag: bytes, payload: bytes) -> None:
        _write_frame(write_stream, tag + payload)

    def _reply_ok_json(value) -> None:
        _reply(_TAG_OK_JSON, json.dumps(value).encode("utf-8"))

    def _reply_error(exc: BaseException) -> None:
        _reply(_TAG_ERR, json.dumps([type(exc).__name__, str(exc)]).encode("utf-8"))

    while True:
        try:
            raw = _read_frame(read_stream)
        except EOFError:
            return  # caller closed the pipe; shut down quietly
        except OSError:  # pragma: no cover - corrupt frame
            _reply(_TAG_FATAL, b"Malformed request frame.")
            return

        try:
            request = json.loads(raw)
        except ValueError:  # pragma: no cover - corrupt frame
            _reply(_TAG_FATAL, b"Malformed request frame.")
            return

        op = request[0] if request else None
        if op == "stop":
            return

        if op == "synthesize":
            _, text, voice_id, speed, sentence_silence = request
            # Checked before the engine is touched, so the fault can be
            # exercised on a host without Piper.
            if selftest and text == _CRASH_SENTINEL:
                os.abort()
            if selftest and text == _HANG_SENTINEL:
                threading.Event().wait()  # block forever, no busy loop
            try:
                audio = _engine().synthesize_wav(
                    text, voice_id, speed=speed, sentence_silence=sentence_silence
                )
                _reply(_TAG_OK_BYTES, audio)
            except SysconTTSError as exc:
                _reply_error(exc)
            except Exception as exc:  # pragma: no cover - unexpected
                _reply(
                    _TAG_ERR,
                    json.dumps(["SynthesisError", f"Synthesis failed: {exc}"]).encode("utf-8"),
                )
        elif op == "preload":
            try:
                _engine().preload(request[1])
                _reply_ok_json(None)
            except SysconTTSError as exc:
                _reply_error(exc)
        elif op == "loaded":
            try:
                _reply_ok_json(_engine().loaded_voice_ids())
            except SysconTTSError as exc:
                _reply_error(exc)
        else:  # pragma: no cover - protocol error
            _reply(
                _TAG_ERR,
                json.dumps(["SynthesisError", f"Unknown worker op {op!r}."]).encode("utf-8"),
            )


# -- caller side --------------------------------------------------------------

#: Sentinel the reader thread enqueues when the worker's stream ends.
_EOF = object()


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
        self._timeout = settings.worker_timeout
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._replies: queue.Queue = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._root_warned = False
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def _warn_if_root(self) -> None:
        """Warn once if the worker will run as root with no account to drop to.

        The isolation keeps a crash from killing the caller either way, but a
        crash in a *root* worker is still memory corruption with root's
        privileges -- the exact thing ``worker_user`` exists to defuse. On the
        APU the worker inherits root from the Tornado manager, so an operator
        who never set ``SYSCON_TTS_WORKER_USER`` needs to be told.
        """
        if self._root_warned or self._user:
            return
        if os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0:
            logger.warning(
                "Synthesis worker is running as root. Set SYSCON_TTS_WORKER_USER "
                "(or worker_user=) to an unprivileged account that can read the "
                "voices directory, so an espeak-ng fault cannot corrupt a root "
                "process."
            )
        self._root_warned = True

    def _privilege_kwargs(self) -> dict:
        """``Popen`` kwargs that drop the worker to ``worker_user`` at exec.

        Only when we are root on POSIX and an account is configured. The OS
        performs the drop before the worker's interpreter runs any of this
        package's code, so there is no root window inside the worker. Empty
        otherwise -- an already-unprivileged service, or a developer box, keeps
        the current user, and Windows has no such mechanism.
        """
        if not (self._user and os.name == "posix" and hasattr(os, "geteuid")):
            return {}
        if os.geteuid() != 0:
            return {}
        import grp
        import pwd

        pw = pwd.getpwnam(self._user)
        groups = sorted({g.gr_gid for g in grp.getgrall() if self._user in g.gr_mem}
                        | {pw.pw_gid})
        return {"user": pw.pw_uid, "group": pw.pw_gid, "extra_groups": groups}

    def _ensure_worker(self) -> None:
        """Spawn the worker if it is not running. Caller holds ``_lock``."""
        if self._proc is not None and self._proc.poll() is None:
            return
        self._warn_if_root()
        self._teardown()

        argv = [sys.executable, "-I", os.path.join(os.path.dirname(__file__), "_worker_entry.py")]
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # stderr inherited, so the worker's warnings and any traceback reach
            # the embedder's log instead of being swallowed.
            close_fds=True,
            **self._privilege_kwargs(),
        )
        self._proc = proc
        self._replies = queue.Queue()
        self._reader = threading.Thread(
            target=self._reader_loop, args=(proc.stdout, self._replies),
            name="syscon-tts-synth-reader", daemon=True,
        )
        self._reader.start()
        # The worker's first frame is its settings, sent as JSON (never pickled).
        _write_frame(proc.stdin, json.dumps(self._settings.describe()).encode("utf-8"))

    @staticmethod
    def _reader_loop(stdout, replies: queue.Queue) -> None:
        """Read reply frames off the worker's stdout until it ends.

        Runs in a per-worker daemon thread. A blocking read cannot be given a
        timeout portably (``select`` does not take a Windows pipe), so the read
        lives here and the request path waits on the queue with a timeout
        instead. The thread ends on EOF -- which a crash, a kill, or a clean
        stop all produce -- after enqueuing the :data:`_EOF` sentinel.
        """
        try:
            while True:
                replies.put(_read_frame(stdout))
        except (EOFError, OSError, ValueError):
            replies.put(_EOF)

    def _teardown(self) -> None:
        """Forget and stop the current worker. Caller holds ``_lock``.

        Order matters: the reader thread may be blocked in a ``read`` on
        ``stdout``. Killing the process first makes that read return EOF so the
        thread exits; only then is it safe to close the streams. Closing
        ``stdout`` while the read is in flight would deadlock on the buffer lock.
        """
        proc = self._proc
        reader = self._reader
        self._proc = None
        self._reader = None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:  # pragma: no cover - already gone
                pass
        if reader is not None:
            reader.join(self._REAP_TIMEOUT)
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            proc.wait(self._REAP_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError):  # pragma: no cover
            pass

    def close(self) -> None:
        """Stop the worker for good. Safe to call more than once.

        Waits for a request already in flight. Every later request raises
        :class:`~syscon_tts.engine.SynthesizerClosedError` instead of spawning
        a new worker -- otherwise a caller still holding a replaced engine
        would quietly start a process nothing will ever stop.
        """
        with self._lock:
            self._closed = True
            if self._proc is not None and self._proc.poll() is None:
                try:
                    _write_frame(self._proc.stdin, json.dumps(["stop"]).encode("utf-8"))
                except OSError:
                    pass
            self._teardown()

    # -- request plumbing --------------------------------------------------

    @property
    def worker_pid(self) -> Optional[int]:
        """PID of the live worker, or ``None``. Used by tests to prove respawn."""
        proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    def _exit_description(self) -> str:
        """Why the worker is gone, in operator terms."""
        code = self._proc.poll() if self._proc is not None else None
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

        A crash (segfault, abort, OOM kill) ends the worker's stream; the reader
        thread enqueues :data:`_EOF`, which becomes a :class:`SynthesisError`
        and the worker is dropped so the next call respawns a clean one. A
        worker that hangs rather than crashing is bounded by ``worker_timeout``
        and killed. The reply is framed bytes decoded by :meth:`_decode_reply`;
        nothing from the worker is ever unpickled.
        """
        with self._lock:
            if self._closed:
                raise SynthesizerClosedError(
                    "This synthesis engine has been closed; use the one that "
                    "replaced it."
                )
            self._ensure_worker()
            try:
                _write_frame(self._proc.stdin, json.dumps(message).encode("utf-8"))
            except OSError:
                return self._crash_error()

            try:
                reply = self._replies.get(timeout=self._timeout)
            except queue.Empty:
                self._teardown()
                raise SynthesisError(
                    f"Synthesis failed: the worker did not respond within "
                    f"{self._timeout:g}s and was stopped. The text may have "
                    "sent espeak-ng into a loop; the service survived it. "
                    "Retry, or capture the text for the crash corpus."
                ) from None

            if reply is _EOF:
                return self._crash_error()
            return self._decode_reply(reply)

    def _crash_error(self) -> None:
        """Reap a dead worker and raise the standard crash ``SynthesisError``."""
        if self._proc is not None:
            try:
                self._proc.wait(self._REAP_TIMEOUT)
            except subprocess.TimeoutExpired:  # pragma: no cover - slow reap
                pass
        reason = self._exit_description()
        self._teardown()
        raise SynthesisError(
            f"Synthesis failed: {reason}. The text may have triggered an "
            "espeak-ng fault; the service survived it. Retry, or capture the "
            "text for the crash corpus."
        ) from None

    def _decode_reply(self, frame: bytes):
        """Turn one framed reply into a value or a typed exception.

        The only decoding is :func:`json.loads`, which runs no code, and an
        error class is taken only from the fixed :data:`_ERROR_CLASSES`
        whitelist -- a worker cannot name an arbitrary callable to instantiate.
        """
        tag, payload = frame[:1], frame[1:]
        if tag == _TAG_OK_BYTES:
            return payload
        if tag == _TAG_FATAL:
            self._teardown()
            raise SynthesisError(
                f"Synthesis failed: {payload.decode('utf-8', 'replace')}"
            )
        # Parse first, raise after -- so a whitelisted error that is itself a
        # ValueError (e.g. InvalidAlertNameError) is not mistaken for a parse
        # failure of the frame.
        if tag in (_TAG_OK_JSON, _TAG_ERR):
            try:
                value = json.loads(payload)
            except ValueError:  # pragma: no cover - corrupt frame
                value = None
            else:
                if tag == _TAG_OK_JSON:
                    return value
                if isinstance(value, list) and len(value) == 2:
                    name, message = value
                    cls = _ERROR_CLASSES.get(name, SynthesisError)
                    raise cls(message)
        self._teardown()
        raise SynthesisError("Synthesis failed: malformed reply from the worker.")

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
