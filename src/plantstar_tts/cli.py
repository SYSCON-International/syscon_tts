"""Command-line interface.

Usage examples::

    plantstar-tts list-voices
    plantstar-tts speak --voice en_us_amy --output hello.wav "Hello from PlantStar"
    plantstar-tts speak -v de_de_thorsten -f mp3 -o ansage.mp3 --text-file notice.txt
    plantstar-tts serve --host 0.0.0.0 --port 5002

The ``speak`` and ``list-voices`` commands run fully offline against the local
voice models; ``serve`` starts the HTTP API.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import load_settings
from .engine import SynthesisError, TTSEngine
from .voices import VoiceError, VoiceRegistry


def _build_registry_and_engine():
    settings = load_settings()
    registry = VoiceRegistry.from_manifest(settings.voices_manifest, settings.voices_dir)
    return settings, registry, TTSEngine(registry)


def _cmd_list_voices(args: argparse.Namespace) -> int:
    settings, registry, _ = _build_registry_and_engine()
    print(f"Default voice: {settings.default_voice}\n")
    header = f"{'ID':<22} {'LANGUAGE':<8} {'GENDER':<10} {'INSTALLED':<9} NAME"
    print(header)
    print("-" * len(header))
    for p in registry.all():
        installed = "yes" if registry.is_installed(p) else "no"
        print(f"{p.id:<22} {p.language:<8} {p.gender:<10} {installed:<9} {p.name}")
    return 0


def _read_text(args: argparse.Namespace) -> str:
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8")
    if args.text:
        return args.text
    # Fall back to stdin (allows piping: `echo hi | plantstar-tts speak ...`).
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


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("error: uvicorn is not installed. Run: pip install -r requirements.txt",
              file=sys.stderr)
        return 1
    settings = load_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    print(f"Starting PlantStar TTS on http://{host}:{port}")
    uvicorn.run("plantstar_tts.api:app", host=host, port=port, workers=args.workers)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plantstar-tts",
        description="Offline text-to-speech for the PlantStar APU server.",
    )
    parser.add_argument("--version", action="version",
                        version=f"plantstar-tts {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-voices", help="List available voice profiles.")
    p_list.set_defaults(func=_cmd_list_voices)

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
