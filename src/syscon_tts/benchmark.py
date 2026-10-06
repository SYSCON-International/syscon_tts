"""Benchmark synthesis on the current host (``syscon-tts benchmark``).

Run it AFTER deployment, on the APU itself, so the numbers reflect the real
hardware. It needs Piper, so it only runs on Linux::

    syscon-tts benchmark
    syscon-tts benchmark en_us_kristin en_us_john --runs 5
    syscon-tts benchmark --threads 2          # compare against a thread cap
    syscon-tts benchmark --json > bench-$(hostname).json

For each installed voice it measures:

* cold load - time to load the model into memory (first use)
* RTF       - real-time factor = synth_time / audio_seconds (lower is better;
              < 1.0 means faster than real-time)
* latency   - wall-clock synthesis time per sample (warm)
* mem       - resident memory growth from loading that voice

By default every installed voice is benchmarked against three sample lengths.
"""

from __future__ import annotations

import io
import sys
import time
import wave
from typing import Optional

from .engine import TTSEngine

# Representative message lengths. Timing (RTF) is language-agnostic, so a single
# English set keeps results directly comparable across all voices.
SAMPLES = {
    "short": "Machine four fault detected.",
    "medium": (
        "Cavity pressure exceeded the configured limit on press four. "
        "Check the mold before restarting the cycle."
    ),
    "long": (
        "Attention operators. The overnight production run completed with a "
        "yield of ninety six percent. Three cavities on press seven reported "
        "short shots and have been flagged for inspection. Please review the "
        "shift report and confirm the material lot before the next run begins."
    ),
}


def rss_kb() -> int:
    """Resident set size in kB (Linux /proc; falls back to getrusage)."""
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    import resource

    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kB; macOS reports bytes.
    return maxrss // 1024 if sys.platform == "darwin" else maxrss


def wav_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data), "rb") as wav:
        return wav.getnframes() / float(wav.getframerate())


def benchmark_voice(engine: TTSEngine, voice_id: str, runs: int) -> dict:
    rss_before = rss_kb()
    t0 = time.perf_counter()
    engine.preload(voice_id)  # cold model load
    load_s = time.perf_counter() - t0
    rss_after = rss_kb()

    samples = {}
    for name, text in SAMPLES.items():
        best = float("inf")
        audio_s = 0.0
        for _ in range(runs):
            start = time.perf_counter()
            wav = engine.synthesize_wav(text, voice_id)
            elapsed = time.perf_counter() - start
            best = min(best, elapsed)
            audio_s = wav_seconds(wav)
        samples[name] = {
            "latency_s": round(best, 3),
            "audio_s": round(audio_s, 3),
            "rtf": round(best / audio_s, 3) if audio_s else None,
        }

    return {
        "voice": voice_id,
        "cold_load_s": round(load_s, 3),
        "mem_growth_mb": round((rss_after - rss_before) / 1024, 1),
        "samples": samples,
    }


def print_table(results: list, runs: int, threads: Optional[int]) -> None:
    print(f"\nSyscon TTS benchmark  (platform={sys.platform}, "
          f"threads={threads or 'default'}, best of {runs} runs)\n")
    header = (f"{'VOICE':<16} {'COLD LOAD':>9} {'MEM':>7}  "
              f"{'SHORT RTF':>9} {'MED RTF':>8} {'LONG RTF':>9}  {'LONG LAT':>8}")
    print(header)
    print("-" * len(header))
    for r in results:
        s = r["samples"]
        print(f"{r['voice']:<16} {r['cold_load_s']:>8}s {r['mem_growth_mb']:>5}MB  "
              f"{s['short']['rtf']:>9} {s['medium']['rtf']:>8} {s['long']['rtf']:>9}  "
              f"{s['long']['latency_s']:>7}s")
    print("\nRTF = synthesis_time / audio_seconds  (lower is better; < 1.0 = "
          "faster than real-time)")
    print("If RTF is uncomfortably high, switch the voice to its 'low' quality "
          "model; if the rest of the APU suffers during synthesis, cap --threads "
          "(see DEPLOY.md, 'Tuning for the APU CPU').")
