"""Piper TTS engine wrapper.

Loads Piper voice models lazily and keeps a bounded number of them cached in
memory, so the service does not pay model-load cost on every request without
letting a multilingual site grow unboundedly (each medium-quality model is
roughly 60 MB resident). Synthesis produces WAV bytes; optional MP3 output is
available when ``ffmpeg`` is present on the host.

Piper only installs on Linux (see :func:`piper_available`). Importing this
module is always safe -- the Piper import is deferred until synthesis is
actually attempted, and failure surfaces as
:class:`SynthesisUnavailableError` rather than an ``ImportError``. That lets
Windows and macOS machines import the package, list voices, and serve
previously generated audio without Piper present.

Thread safety: the APU calls this from a thread pool, because synthesis is a
seconds-long CPU burn that must not block the Tornado IO loop. Loading is
guarded per voice, and so is synthesis -- a single ``PiperVoice`` is not
documented to be re-entrant, and serializing is what you want anyway on a CPU
that has other jobs to do.
"""

from __future__ import annotations

import io
import platform
import shutil
import subprocess
import threading
import wave
from collections import OrderedDict
from typing import Dict, List

from .config import DEFAULT_MAX_LOADED_VOICES
from .voices import VoiceNotInstalledError, VoiceProfile, VoiceRegistry


class SynthesisError(Exception):
    """Raised when audio synthesis fails."""


class SynthesisUnavailableError(SynthesisError):
    """Raised when Piper is not usable on this machine.

    This is the expected condition on Windows and macOS. Callers that only
    need to *replay* previously generated audio should catch this and fall
    back to their cache rather than treating it as a hard failure.
    """


def piper_available() -> bool:
    """True if the Piper runtime can be imported on this machine."""
    try:
        import piper  # noqa: F401
    except Exception:
        return False
    return True


def _unavailable_reason() -> str:
    """Explain, in operator terms, why Piper cannot be used here."""
    system = platform.system()
    if system in ("Windows", "Darwin"):
        return (
            f"Piper speech synthesis is not available on {system}. The "
            "'piper-tts' dependency ships Linux-only binary wheels, so it is "
            "deliberately skipped when installing on this platform. This "
            "machine can still play and serve audio that was generated on a "
            "Linux host; it cannot generate new audio."
        )
    return (
        "Piper speech synthesis is unavailable: the 'piper-tts' package could "
        "not be imported. Install it with 'pip install syscon-tts[piper]' on a "
        "Linux x86-64 host running Python 3.9-3.11."
    )


class TTSEngine:
    def __init__(
        self,
        registry: VoiceRegistry,
        max_loaded_voices: int = DEFAULT_MAX_LOADED_VOICES,
    ):
        self._registry = registry
        self._max_loaded = max(1, int(max_loaded_voices))
        # Least-recently-used first, so eviction pops from the front.
        self._loaded: "OrderedDict[str, object]" = OrderedDict()
        self._voice_locks: Dict[str, threading.Lock] = {}
        # Guards the two maps above -- never held while loading or speaking.
        self._lock = threading.Lock()

    # -- model loading -----------------------------------------------------

    def _voice_lock(self, voice_id: str) -> threading.Lock:
        with self._lock:
            lock = self._voice_locks.get(voice_id)
            if lock is None:
                lock = threading.Lock()
                self._voice_locks[voice_id] = lock
            return lock

    def _cached(self, voice_id: str):
        with self._lock:
            voice = self._loaded.get(voice_id)
            if voice is not None:
                self._loaded.move_to_end(voice_id)
            return voice

    def _remember(self, voice_id: str, voice) -> None:
        with self._lock:
            self._loaded[voice_id] = voice
            self._loaded.move_to_end(voice_id)
            while len(self._loaded) > self._max_loaded:
                # Dropping a model another thread is mid-synthesis with is
                # safe: that thread holds its own reference, and the memory is
                # reclaimed once it finishes.
                self._loaded.popitem(last=False)

    def _load_voice(self, profile: VoiceProfile):
        cached = self._cached(profile.id)
        if cached is not None:
            return cached

        # One loader per voice; a second caller waits rather than loading a
        # duplicate 60 MB model.
        with self._voice_lock(profile.id):
            cached = self._cached(profile.id)
            if cached is not None:
                return cached

            # Check the engine before the models. If Piper cannot run here,
            # downloading voices would not help, so reporting "voice not
            # installed" first would send the operator down a dead end.
            try:
                from piper import PiperVoice  # imported lazily; Linux-only
            except Exception as exc:
                raise SynthesisUnavailableError(_unavailable_reason()) from exc

            model_path = self._registry.model_path(profile)
            config_path = self._registry.config_path(profile)
            if not self._registry.is_installed(profile):
                raise VoiceNotInstalledError(
                    f"Voice '{profile.id}' is not installed. Expected model at "
                    f"{model_path}. Run 'syscon-tts download-voices "
                    f"{profile.id}' to fetch it."
                )

            voice = PiperVoice.load(str(model_path), config_path=str(config_path))

        self._remember(profile.id, voice)
        return voice

    def preload(self, voice_id: str) -> None:
        """Force a voice to load now (useful at startup)."""
        self._load_voice(self._registry.get(voice_id))

    def loaded_voice_ids(self) -> List[str]:
        """Currently resident voices, least recently used first."""
        with self._lock:
            return list(self._loaded)

    # -- synthesis ---------------------------------------------------------

    def synthesize_wav(
        self,
        text: str,
        voice_id: str,
        speed: float = 1.0,
        sentence_silence: float = 0.2,
    ) -> bytes:
        """Render ``text`` to WAV bytes using the given voice.

        ``speed`` is a multiplier: 1.0 = normal, 2.0 = twice as fast,
        0.5 = half speed. Piper uses ``length_scale`` (inverse of speed).
        """
        if not text or not text.strip():
            raise SynthesisError("Text is empty.")
        if speed <= 0:
            raise SynthesisError("Speed must be greater than 0.")

        profile = self._registry.get(voice_id)
        voice = self._load_voice(profile)
        length_scale = 1.0 / speed

        buffer = io.BytesIO()
        try:
            # Serialize per voice: concurrent synthesis on one model has no
            # documented guarantee, and parallel CPU burn is not a win here.
            with self._voice_lock(profile.id):
                with wave.open(buffer, "wb") as wav_file:
                    voice.synthesize(
                        text,
                        wav_file,
                        length_scale=length_scale,
                        sentence_silence=sentence_silence,
                    )
        except VoiceNotInstalledError:
            raise
        except Exception as exc:  # pragma: no cover - piper runtime failure
            raise SynthesisError(f"Synthesis failed: {exc}") from exc
        return buffer.getvalue()

    def synthesize(
        self,
        text: str,
        voice_id: str,
        fmt: str = "wav",
        speed: float = 1.0,
        sentence_silence: float = 0.2,
    ) -> "tuple[bytes, str]":
        """Render ``text`` and return ``(audio_bytes, media_type)``.

        Supported ``fmt`` values: ``wav`` (always) and ``mp3`` (requires
        ``ffmpeg`` on the host).
        """
        fmt = fmt.lower()
        if fmt not in ("wav", "mp3"):
            raise SynthesisError(f"Unsupported format '{fmt}'. Use 'wav' or 'mp3'.")
        wav_bytes = self.synthesize_wav(text, voice_id, speed, sentence_silence)
        if fmt == "wav":
            return wav_bytes, "audio/wav"
        return self._wav_to_mp3(wav_bytes), "audio/mpeg"

    @staticmethod
    def _wav_to_mp3(wav_bytes: bytes) -> bytes:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise SynthesisError(
                "MP3 output requires 'ffmpeg', which was not found on PATH. "
                "Install ffmpeg or request WAV output."
            )
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "wav", "-i", "pipe:0", "-f", "mp3", "pipe:1"],
            input=wav_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise SynthesisError(
                f"ffmpeg conversion failed: {proc.stderr.decode('utf-8', 'replace')}"
            )
        return proc.stdout
