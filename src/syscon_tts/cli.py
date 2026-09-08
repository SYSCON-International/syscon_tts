"""Command-line interface.

Usage examples::

    syscon-tts doctor
    syscon-tts download-voices                     # fetch the whole catalogue
    syscon-tts download-voices --language es-mx    # or just one site's language
    syscon-tts download-voices en_us_kristin       # or one named voice
    syscon-tts list-voices
    syscon-tts speak --voice en_us_kristin --output hello.wav "Hello from PlantStar"
    syscon-tts alert --language es-mx "Presion de cavidad excedida en prensa 4."
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
from .alerts import AlertSynthesizer, InvalidAlertNameError, resolve_voice_id
from .config import load_settings
from .download import DownloadError, download_voices
from .engine import SynthesisError, SynthesisUnavailableError, TTSEngine, piper_available
from .voices import VoiceError, VoiceRegistry


def _build_registry_and_engine():
    settings = load_settings()
    registry = VoiceRegistry.from_settings(settings)
    return settings, registry, TTSEngine(registry, settings.max_loaded_voices)


def _cmd_list_voices(args: argparse.Namespace) -> int:
    settings, registry, _ = _build_registry_and_engine()
    profiles = registry.all()
    if args.language:
        profiles = registry.for_language(args.language)
        if not profiles:
            print(
                f"No voice for language '{args.language}'. "
                f"Known languages: {', '.join(registry.languages())}.",
                file=sys.stderr,
            )
            return 1

    print(f"Default voice: {settings.default_voice}")
    for language, voice_id in sorted(settings.default_voices.items()):
        print(f"  {language:<8} {voice_id}")
    print(f"Voices dir:    {settings.voices_dir}\n")

    header = (
        f"{'ID':<22} {'LANGUAGE':<8} {'QUALITY':<8} {'GENDER':<12} "
        f"{'INSTALLED':<10} {'LICENSE':<16} NAME"
    )
    print(header)
    print("-" * len(header))
    for p in profiles:
        installed = "yes" if registry.is_installed(p) else "no"
        license_note = p.license + (" (review)" if p.requires_license_review else "")
        print(
            f"{p.id:<22} {p.language:<8} {p.quality:<8} {p.gender:<12} "
            f"{installed:<10} {license_note:<16} {p.name}"
        )
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Report whether this machine can generate audio, and why not if it can't."""
    settings = load_settings()
    print(f"syscon-tts {__version__}")
    print(f"  platform          {platform.system()} {platform.machine()}")
    print(f"  python            {platform.python_version()}")
    print(f"  manifest          {settings.voices_manifest}")
    if settings.extra_manifest:
        print(f"  extra manifest    {settings.extra_manifest}")
    print(f"  voices dir        {settings.voices_dir}")
    print(f"  alerts dir        {settings.alerts_dir}")
    print(f"  audio file mode   {settings.file_mode:04o}")
    print(f"  models in memory  up to {settings.max_loaded_voices}")
    print(f"  ffmpeg (mp3)      {'yes' if shutil.which('ffmpeg') else 'no'}")

    has_piper = piper_available()
    print(f"  piper (synthesis) {'yes' if has_piper else 'no'}")

    try:
        registry = VoiceRegistry.from_settings(settings)
    except VoiceError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    installed = registry.installed()
    print(f"  voices installed  {len(installed)}/{len(registry.all())}")
    discovered = [p for p in installed if p.source == "disk"]
    if discovered:
        print(
            f"  found on disk     {len(discovered)} "
            f"({', '.join(p.id for p in discovered)})"
        )
    print(f"  default voice     {settings.default_voice}")
    for language, voice_id in sorted(settings.default_voices.items()):
        print(f"    {language:<8} {voice_id}")

    problems = []

    unreviewed = [p for p in installed if p.requires_license_review]
    if unreviewed:
        # Not a "problem" in the exit-code sense -- the models work fine. It is
        # a shipping question, and the operator is the one who can answer it.
        print(
            "\nLicense review needed before these go to a customer:\n"
            + "\n".join(
                f"  - {p.id}: {p.license}"
                + (f" ({p.license_url})" if p.license_url else "")
                for p in unreviewed
            )
        )

    for voice_id in [settings.default_voice, *settings.default_voices.values()]:
        if not registry.has(voice_id):
            problems.append(
                f"Configured voice '{voice_id}' is not in the catalogue. "
                "Check SYSCON_TTS_DEFAULT_VOICE / SYSCON_TTS_DEFAULT_VOICES."
            )
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
            language=args.language,
            accept_license=args.accept_license,
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
    settings, registry, engine = _build_registry_and_engine()
    text = _read_text(args)
    voice_id = resolve_voice_id(registry, settings, args.voice, args.language)
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
            language=args.language,
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
    p_list.add_argument("-l", "--language",
                        help="Only voices for this locale, e.g. es-mx, zh-hans.")
    p_list.set_defaults(func=_cmd_list_voices)

    p_dl = sub.add_parser(
        "download-voices", help="Download voice models (needs internet)."
    )
    p_dl.add_argument("voice_ids", nargs="*",
                      help="Voice ids to fetch (default: all in the manifest).")
    p_dl.add_argument("-l", "--language",
                      help="Fetch only this locale's voices, e.g. es-mx.")
    p_dl.add_argument("--force", action="store_true",
                      help="Re-download even if the file is already present.")
    p_dl.add_argument("--accept-license", action="store_true",
                      help="Also fetch voices whose license needs review "
                           "(see 'doctor'); confirms someone has cleared it.")
    p_dl.set_defaults(func=_cmd_download_voices)

    p_speak = sub.add_parser("speak", help="Synthesize text to an audio file.")
    p_speak.add_argument("text", nargs="?", help="Text to speak.")
    p_speak.add_argument("-v", "--voice", help="Voice id (see list-voices).")
    p_speak.add_argument("-l", "--language",
                         help="Pick a voice by locale instead, e.g. zh-hans.")
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
    p_alert.add_argument("-l", "--language",
                         help="Pick a voice by locale instead, e.g. es-mx.")
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
