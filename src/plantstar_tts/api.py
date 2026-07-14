"""HTTP API (FastAPI).

Endpoints:
    GET  /health              liveness/readiness probe
    GET  /voices              list available voice profiles
    POST /synthesize          render text to an audio file
    GET  /                    service info

Build the app with :func:`create_app`; ``app`` at module level is the default
instance used by uvicorn / the CLI ``serve`` command.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings, load_settings
from .engine import SynthesisError, TTSEngine
from .voices import UnknownVoiceError, VoiceNotInstalledError, VoiceRegistry


class SynthesizeRequest(BaseModel):
    text: str = Field(..., description="Text to speak.")
    voice: str | None = Field(
        None, description="Voice id. Defaults to the server's default voice."
    )
    format: str = Field("wav", description="Output format: 'wav' or 'mp3'.")
    speed: float = Field(
        1.0, gt=0, le=4.0, description="Speed multiplier (1.0 = normal)."
    )
    sentence_silence: float = Field(
        0.2, ge=0, le=5.0, description="Seconds of silence between sentences."
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    registry = VoiceRegistry.from_manifest(
        settings.voices_manifest, settings.voices_dir
    )
    engine = TTSEngine(registry)

    app = FastAPI(
        title="PlantStar TTS",
        version=__version__,
        description="Offline text-to-speech service with selectable voice profiles.",
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.engine = engine

    @app.get("/")
    def info():
        return {
            "service": "PlantStar TTS",
            "version": __version__,
            "engine": "piper",
            "default_voice": settings.default_voice,
            "endpoints": ["/health", "/voices", "/synthesize"],
        }

    @app.get("/health")
    def health():
        installed = sum(1 for p in registry.all() if registry.is_installed(p))
        return {
            "status": "ok",
            "voices_total": len(registry.all()),
            "voices_installed": installed,
        }

    @app.get("/voices")
    def list_voices():
        return {
            "default": settings.default_voice,
            "voices": [
                p.to_public_dict(registry.is_installed(p)) for p in registry.all()
            ],
        }

    @app.post("/synthesize")
    def synthesize(req: SynthesizeRequest):
        if len(req.text) > settings.max_text_chars:
            raise HTTPException(
                status_code=413,
                detail=f"Text exceeds max length of {settings.max_text_chars} chars.",
            )
        voice_id = req.voice or settings.default_voice
        try:
            audio, media_type = engine.synthesize(
                req.text,
                voice_id,
                fmt=req.format,
                speed=req.speed,
                sentence_silence=req.sentence_silence,
            )
        except UnknownVoiceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except VoiceNotInstalledError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except SynthesisError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        ext = "mp3" if media_type == "audio/mpeg" else "wav"
        return Response(
            content=audio,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="speech.{ext}"',
                "X-Voice": voice_id,
            },
        )

    return app


# Default application instance for `uvicorn plantstar_tts.api:app`.
app = create_app()
