#!/usr/bin/env python3
"""Benchmark PlantStar TTS synthesis on the current host (e.g. the APU).

Run this AFTER deployment, inside the environment where the service runs, so
the numbers reflect the real hardware:

    # bare metal
    source .venv/bin/activate
    python scripts/benchmark.py

    # Docker (copy the script in first, then exec)
    docker cp scripts/benchmark.py plantstar-tts:/app/benchmark.py
    docker exec plantstar-tts python /app/benchmark.py

For each installed voice it measures:
  * cold load  - time to load the model into memory (first use)
  * RTF        - real-time factor = synth_time / audio_seconds (lower is better;
                 < 1.0 means faster than real-time)
  * latency    - wall-clock synthesis time per sample (warm)
  * mem        - resident memory growth from loading that voice

By default it benchmarks every installed voice against three sample lengths.
Restrict to specific voices by passing their ids, and use --json for machine
-readable output (e.g. to archive results per deployment).

    python scripts/benchmark.py en_us_amy en_us_ryan --runs 5
    python scripts/benchmark.py --json > bench-$(hostname).json
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import wave
from pathlib import Path

# Allow running straight from a checkout without `pip install`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from plantstar_tts.config import load_settings  # noqa: E402
from plantstar_tts.engine import TTSEngine  # noqa: E402
from plantstar_tts.voices import VoiceRegistry  # noqa: E402

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark PlantStar TTS synthesis.")
    parser.add_argument("voices", nargs="*", help="Voice ids (default: all installed).")
    parser.add_argument("--runs", type=int, default=3,
                        help="Warm runs per sample; the fastest is reported (default 3).")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    args = parser.parse_args(argv)

    settings = load_settings()
    registry = VoiceRegistry.from_manifest(settings.voices_manifest, settings.voices_dir)
    engine = TTSEngine(registry)

    if args.voices:
        requested = args.voices
    else:
        requested = [v.id for v in registry.all() if registry.is_installed(v)]

    if not requested:
        print("No installed voices to benchmark. Run scripts/download_voices.sh first.",
              file=sys.stderr)
        return 1

    results = []
    for voice_id in requested:
        profile = registry.get(voice_id)
        if not registry.is_installed(profile):
            print(f"skip {voice_id}: not installed", file=sys.stderr)
            continue
        results.append(benchmark_voice(engine, voice_id, args.runs))

    if args.json:
        print(json.dumps({"host_platform": sys.platform, "runs": args.runs,
                          "results": results}, indent=2))
        return 0

    # Human-readable table.
    print(f"\nPlantStar TTS benchmark  (platform={sys.platform}, "
          f"best of {args.runs} runs)\n")
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
          "model (see DEPLOY.md §6).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
