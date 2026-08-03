"""Syscon TTS - offline text-to-speech for the PlantStar APU.

Wraps the Piper neural TTS engine (CPU-only, fully offline) behind a small
library, a command-line tool, and an optional HTTP server.

Piper only installs on Linux, which is where every APU runs. Importing this
package is nonetheless safe on Windows and macOS: the Piper import is deferred
until synthesis is attempted, so developers on those platforms can install the
package, inspect voices, and replay audio generated elsewhere. Attempting to
generate new audio there raises
:class:`~syscon_tts.engine.SynthesisUnavailableError`.

Typical APU usage -- resolve alert text to a WAV, synthesizing only on a cache
miss::

    from syscon_tts import AlertSynthesizer

    synth = AlertSynthesizer()          # build once, keep it: caches models
    result = synth.ensure("Press 4 cavity pressure exceeded.")
    print(result.path, result.cached)
"""

from .alerts import (
    AlertAudio,
    AlertSynthesizer,
    InvalidAlertNameError,
    ensure_alert_wav,
    sanitize_file_name,
)
from .config import Settings, load_settings
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
)

__version__ = "0.0.1"

__all__ = [
    "AlertAudio",
    "AlertSynthesizer",
    "InvalidAlertNameError",
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
    "ensure_alert_wav",
    "load_settings",
    "piper_available",
    "sanitize_file_name",
]
