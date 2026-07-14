"""Piper TTS engine wrapper.

Loads Piper voice models lazily and keeps them cached in memory so the service
does not pay model-load cost on every request. Synthesis produces WAV bytes;
optional MP3 output is available when ``ffmpeg`` is present on the host.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import threading
import wave
from pathlib import Path

from .voices import VoiceNotInstalledError, VoiceProfile, VoiceRegistry


class SynthesisError(Exception):
    """Raised when audio synthesis fails."""


class TTSEngine:
    def __init__(self, registry: VoiceRegistry):
        self._registry = registry
        self._loaded: dict[str, object] = {}
        self._lock = threading.Lock()

    # -- model loading -----------------------------------------------------

    def _load_voice(self, profile: VoiceProfile):
        cached = self._loaded.get(profile.id)
        if cached is not None:
            return cached

        with self._lock:
            # Re-check inside the lock in case another thread just loaded it.
            cached = self._loaded.get(profile.id)
            if cached is not None:
                return cached

            model_path = self._registry.model_path(profile)
            config_path = self._registry.config_path(profile)
            if not self._registry.is_installed(profile):
                raise VoiceNotInstalledError(
                    f"Voice '{profile.id}' is not installed. Expected model at "
                    f"{model_path}. Run scripts/download_voices.sh to fetch it."
                )

            try:
                from piper import PiperVoice  # imported lazily; heavy dependency
            except ImportError as exc:  # pragma: no cover - install-time issue
                raise SynthesisError(
                    "The 'piper-tts' package is not installed. "
                    "Run: pip install -r requirements.txt"
                ) from exc

            voice = PiperVoice.load(str(model_path), config_path=str(config_path))
            self._loaded[profile.id] = voice
            return voice

    def preload(self, voice_id: str) -> None:
        """Force a voice to load now (useful at startup)."""
        self._load_voice(self._registry.get(voice_id))

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
    ) -> tuple[bytes, str]:
        """Render ``text`` and return ``(audio_bytes, media_type)``.

        Supported ``fmt`` values: ``wav`` (always) and ``mp3`` (requires
        ``ffmpeg`` on the host).
        """
        fmt = fmt.lower()
        wav_bytes = self.synthesize_wav(text, voice_id, speed, sentence_silence)
        if fmt == "wav":
            return wav_bytes, "audio/wav"
        if fmt == "mp3":
            return self._wav_to_mp3(wav_bytes), "audio/mpeg"
        raise SynthesisError(f"Unsupported format '{fmt}'. Use 'wav' or 'mp3'.")

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
