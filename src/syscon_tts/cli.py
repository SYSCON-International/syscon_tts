"""Command-line interface.

Usage examples::

    syscon-tts doctor
    syscon-tts download-voices                    # fetch every voice
    syscon-tts download-voices en_us_amy          # or just one
    syscon-tts list-voices
    syscon-tts speak --voice en_us_amy --output hello.wav "Hello from PlantStar"
    syscon-tts alert "Press 4 cavity pressure exceeded."
    syscon-tts serve --host 127.0.0.1 --port 5002

Every command except ``download-voices`` runs fully offline. ``doctor``,
``list-voices``, and cache hits from ``alert`` work on any platform; ``speak``
and cache misses need Piper, and therefore Linux.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import sys
from pathlib import Path

from . import __version__
from .alerts import AlertSynthesizer, InvalidAlertNameError
from .config import load_settings
from .download import DownloadError, download_voices
from .engine import SynthesisError, SynthesisUnavailableError, TTSEngine, piper_available
from .voices import VoiceError, VoiceRegistry


def _build_registry_and_engine():
    settings = load_settings()
    registry = VoiceRegistry.from_manifest(settings.voices_manifest, settings.voices_dir)
    return settings, registry, TTSEngine(registry)


def _cmd_list_voices(args: argparse.Namespace) -> int:
    settings, registry, _ = _build_registry_and_engine()
    print(f"Default voice: {settings.default_voice}")
    print(f"Voices dir:    {settings.voices_dir}\n")
    header = f"{'ID':<22} {'LANGUAGE':<8} {'GENDER':<10} {'INSTALLED':<9} NAME"
    print(header)
    print("-" * len(header))
    for p in registry.all():
        installed = "yes" if registry.is_installed(p) else "no"
        print(f"{p.id:<22} {p.language:<8} {p.gender:<10} {installed:<9} {p.name}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Report whether this machine can generate audio, and why not if it can't."""
    settings = load_settings()
    print(f"syscon-tts {__version__}")
    print(f"  platform          {platform.system()} {platform.machine()}")
    print(f"  python            {platform.python_version()}")
    print(f"  manifest          {settings.voices_manifest}")
    print(f"  voices dir        {settings.voices_dir}")
    print(f"  alerts dir        {settings.alerts_dir}")
    print(f"  ffmpeg (mp3)      {'yes' if shutil.which('ffmpeg') else 'no'}")

    has_piper = piper_available()
    print(f"  piper (synthesis) {'yes' if has_piper else 'no'}")

    try:
        registry = VoiceRegistry.from_manifest(
            settings.voices_manifest, settings.voices_dir
        )
    except VoiceError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    installed = [p for p in registry.all() if registry.is_installed(p)]
    print(f"  voices installed  {len(installed)}/{len(registry.all())}")

    problems = []
    if not has_piper:
        if platform.system() == "Linux":
            problems.append(
                "Piper is missing on a Linux host. Reinstall with "
                "'pip install syscon-tts[piper]' using Python 3.9-3.11."
            )
        else:
            print(
                f"\nNote: {platform.system()} cannot generate audio -- Piper ships "
                "Linux-only wheels. Previously generated WAVs still play and serve "
                "normally from the alerts directory."
            )
    if not installed:
        problems.append(
            "No voice models on disk. Run 'syscon-tts download-voices'."
        )

    if problems:
        # stdout is block-buffered when piped while stderr is not, so the
        # report would otherwise appear after the problems it refers to.
        sys.stdout.flush()
        print("\nProblems:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("\nReady.")
    return 0


def _cmd_download_voices(args: argparse.Namespace) -> int:
    settings, registry, _ = _build_registry_and_engine()
    print(f"Downloading into {settings.voices_dir}")
    try:
        results = download_voices(
            registry,
            settings.voices_dir,
            voice_ids=args.voice_ids,
            force=args.force,
            on_progress=print,
        )
    except (DownloadError, VoiceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    fetched = sum(1 for r in results if not r.skipped)
    total_mb = sum(r.size for r in results if not r.skipped) / (1024 * 1024)
    print(f"Done. {fetched} file(s) downloaded ({total_mb:.1f} MB).")
    return 0


def _read_text(args: argparse.Namespace) -> str:
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8")
    if args.text:
        return args.text
    # Fall back to stdin (allows piping: `echo hi | syscon-tts speak ...`).
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("No text provided. Pass TEXT, --text-file, or pipe via stdin.")


def _cmd_speak(args: argparse.Namespace) -> int:
    settings, _, engine = _build_registry_and_engine()
    text = _read_text(args)
    voice_id = args.voice or settings.default_voice
    fmt = args.format or ("mp3" if args.output.lower().endswith(".mp3") else "wav")
    try:
        audio, _ = engine.synthesize(
            text,
            voice_id,
            fmt=fmt,
            speed=args.speed,
            sentence_silence=args.sentence_silence,
        )
    except (VoiceError, SynthesisError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    Path(args.output).write_bytes(audio)
    print(f"Wrote {len(audio)} bytes to {args.output} (voice={voice_id}, format={fmt})")
    return 0


def _cmd_alert(args: argparse.Namespace) -> int:
    """Resolve alert text the same way the APU does: cache first, then synthesize."""
    try:
        synth = AlertSynthesizer()
        result = synth.ensure(
            args.text,
            file_name=args.file_name,
            voice=args.voice,
            alerts_dir=Path(args.alerts_dir) if args.alerts_dir else None,
            force=args.force,
        )
    except InvalidAlertNameError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SynthesisUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (VoiceError, SynthesisError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    state = "cached" if result.cached else f"generated (voice={result.voice})"
    print(f"{result.path}  [{state}]")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "error: the HTTP server needs extra dependencies. "
            "Install them with: pip install 'syscon-tts[server]'",
            file=sys.stderr,
        )
        return 1
    settings = load_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    print(f"Starting Syscon TTS on http://{host}:{port}")
    uvicorn.run("syscon_tts.api:app", host=host, port=port, workers=args.workers)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="syscon-tts",
        description="Offline text-to-speech for the PlantStar APU.",
    )
    parser.add_argument("--version", action="version",
                        version=f"syscon-tts {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser(
        "doctor", help="Report platform, Piper, and voice-model status."
    )
    p_doctor.set_defaults(func=_cmd_doctor)

    p_list = sub.add_parser("list-voices", help="List available voice profiles.")
    p_list.set_defaults(func=_cmd_list_voices)

    p_dl = sub.add_parser(
        "download-voices", help="Download voice models (needs internet)."
    )
    p_dl.add_argument("voice_ids", nargs="*",
                      help="Voice ids to fetch (default: all in the manifest).")
    p_dl.add_argument("--force", action="store_true",
                      help="Re-download even if the file is already present.")
    p_dl.set_defaults(func=_cmd_download_voices)

    p_speak = sub.add_parser("speak", help="Synthesize text to an audio file.")
    p_speak.add_argument("text", nargs="?", help="Text to speak.")
    p_speak.add_argument("-v", "--voice", help="Voice id (see list-voices).")
    p_speak.add_argument("-o", "--output", required=True, help="Output file path.")
    p_speak.add_argument("-f", "--format", choices=["wav", "mp3"],
                         help="Output format (default: inferred from --output).")
    p_speak.add_argument("--text-file", help="Read text from a file instead of arg.")
    p_speak.add_argument("--speed", type=float, default=1.0,
                         help="Speed multiplier (1.0 = normal).")
    p_speak.add_argument("--sentence-silence", type=float, default=0.2,
                         help="Seconds of silence between sentences.")
    p_speak.set_defaults(func=_cmd_speak)

    p_alert = sub.add_parser(
        "alert",
        help="Resolve alert text to a WAV in the alerts dir (cache first).",
    )
    p_alert.add_argument("text", help="Alert message text.")
    p_alert.add_argument("-v", "--voice", help="Voice id (see list-voices).")
    p_alert.add_argument("--file-name",
                         help="Override the derived file name (no extension).")
    p_alert.add_argument("--alerts-dir",
                         help="Override the configured alerts directory.")
    p_alert.add_argument("--force", action="store_true",
                         help="Regenerate even if the WAV already exists.")
    p_alert.set_defaults(func=_cmd_alert)

    p_serve = sub.add_parser("serve", help="Run the HTTP API server.")
    p_serve.add_argument("--host", help="Bind host (default from config).")
    p_serve.add_argument("--port", type=int, help="Bind port (default from config).")
    p_serve.add_argument("--workers", type=int, default=1,
                         help="Number of worker processes.")
    p_serve.set_defaults(func=_cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except VoiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
