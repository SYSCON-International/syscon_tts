"""Syscon TTS - offline text-to-speech for the PlantStar APU.

Wraps the Piper neural TTS engine (CPU-only, fully offline) behind a small
library, a command-line tool, and an optional HTTP server. This is the only
speech backend PlantStar ships; there is no VoiceText fallback.

Piper only installs on Linux, which is where every APU runs. Importing this
package is nonetheless safe on Windows and macOS: the Piper import is deferred
until synthesis is attempted, so developers on those platforms can install the
package, inspect voices, and replay audio generated elsewhere. Attempting to
generate new audio there raises
:class:`~syscon_tts.engine.SynthesisUnavailableError`.

Typical APU usage -- resolve alert text to a WAV, synthesizing only on a cache
miss::

    from syscon_tts import AlertSynthesizer

    # Build once and keep it: the instance caches loaded voice models.
    synth = AlertSynthesizer(
        alerts_dir=Path(settings.MEDIA_ROOT, "public_alert_sounds"),
    )
    result = synth.ensure("Press 4 cavity pressure exceeded.")
    print(result.path, result.cached)

Multilingual sites select a voice by locale rather than by id::

    synth.ensure(message, language="es-mx")
"""

from .alerts import (
    AlertAudio,
    AlertSynthesizer,
    InvalidAlertNameError,
    ensure_alert_wav,
    resolve_voice_id,
    sanitize_file_name,
)
from .config import Settings, load_settings
from .download import DownloadError, LicenseReviewRequired, download_voices
from .engine import (
    SynthesisError,
    SynthesisUnavailableError,
    TTSEngine,
    piper_available,
)
from .voices import (
    UnknownVoiceError,
    VoiceError,
    VoiceNotInstalledError,
    VoiceProfile,
    VoiceRegistry,
    normalize_language,
)

__version__ = "0.0.2"

__all__ = [
    "AlertAudio",
    "AlertSynthesizer",
    "DownloadError",
    "InvalidAlertNameError",
    "LicenseReviewRequired",
    "Settings",
    "SynthesisError",
    "SynthesisUnavailableError",
    "TTSEngine",
    "UnknownVoiceError",
    "VoiceError",
    "VoiceNotInstalledError",
    "VoiceProfile",
    "VoiceRegistry",
    "__version__",
    "download_voices",
    "ensure_alert_wav",
    "load_settings",
    "normalize_language",
    "piper_available",
    "resolve_voice_id",
    "sanitize_file_name",
]
