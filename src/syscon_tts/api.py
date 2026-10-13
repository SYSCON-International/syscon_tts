"""HTTP API (FastAPI).

Optional -- install with ``pip install 'syscon-tts[server]'``. The APU does not
need it: it imports :class:`~syscon_tts.alerts.AlertSynthesizer` in-process, so
Django never inherits a web-framework dependency it does not use. The server
exists for local operation and smoke-testing a provisioned host.

Endpoints:
    GET  /health              liveness/readiness probe
    GET  /voices              list available voice profiles
    POST /synthesize          render text to an audio file
    GET  /                    service info

Build the app with :func:`create_app`. The CLI ``serve`` command runs it as a
uvicorn factory (``syscon_tts.api:create_app --factory``). Importing this
module does nothing else: an app is built only when asked for, so a bad
manifest path cannot fail an import. ``syscon_tts.api:app`` still works for
``uvicorn syscon_tts.api:app`` and builds the default app on first access.
"""

from __future__ import annotations

import math
import threading
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .alerts import resolve_voice_id
from .config import Settings, load_settings
from .engine import (
    SynthesisError,
    SynthesisUnavailableError,
    TTSEngine,
    piper_available,
)
from .voices import UnknownVoiceError, VoiceNotInstalledError, VoiceRegistry


class SynthesizeRequest(BaseModel):
    text: str = Field(..., description="Text to speak.")
    voice: Optional[str] = Field(
        None, description="Voice id. Defaults to the server's default voice."
    )
    language: Optional[str] = Field(
        None,
        description=(
            "Locale to pick a voice for (es-mx, zh-hans) when 'voice' is not "
            "given. Falls back to the default voice if nothing matches."
        ),
    )
    format: str = Field("wav", description="Output format: 'wav' or 'mp3'.")
    # A tiny speed (1/speed = length_scale) or long per-sentence silence turns
    # a short request into minutes of audio and gigabytes of RAM while the
    # voice lock is held -- an unauthenticated OOM. The bounds keep one request
    # bounded; they match the engine-side guards in synthesize_wav.
    speed: float = Field(
        1.0, ge=0.5, le=2.0, allow_inf_nan=False,
        description="Speed multiplier (0.5-2.0; 1.0 = normal).",
    )
    sentence_silence: float = Field(
        0.2, ge=0, le=1.0, allow_inf_nan=False,
        description="Seconds of silence between sentences (0-1).",
    )


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    registry = VoiceRegistry.from_settings(settings)
    engine = TTSEngine(registry, settings.max_loaded_voices, threads=settings.threads)

    app = FastAPI(
        title="Syscon TTS",
        version=__version__,
        description="Offline text-to-speech service with selectable voice profiles.",
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.engine = engine
    # Synthesis is CPU-bound and each request's memory grows with its text, so
    # a burst of concurrent /synthesize calls can stack gigabytes while they
    # queue on the engine's per-voice lock. FastAPI runs these sync endpoints in
    # a threadpool, so without a gate they all enter at once. Admit a bounded
    # number and shed the rest with 503 instead of letting the box OOM.
    gate = threading.BoundedSemaphore(max(1, settings.max_concurrent_synthesis))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # pydantic echoes the offending input in its error detail. A non-finite
        # float (NaN/inf, which allow_inf_nan=False rejects) there would make the
        # default JSON response fail to encode -- turning a clean 422 into a 500.
        # Sanitize non-finite inputs and drop the context so the 422 renders.
        safe = []
        for err in exc.errors():
            item = dict(err)
            value = item.get("input")
            if isinstance(value, float) and not math.isfinite(value):
                item["input"] = str(value)
            item.pop("ctx", None)
            safe.append(item)
        return JSONResponse(status_code=422, content=jsonable_encoder({"detail": safe}))

    @app.get("/")
    def info():
        return {
            "service": "Syscon TTS",
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
            "can_synthesize": piper_available(),
            "voices_total": len(registry.all()),
            "voices_installed": installed,
        }

    @app.get("/voices")
    def list_voices(language: Optional[str] = None):
        profiles = registry.for_language(language) if language else registry.all()
        return {
            "default": settings.default_voice,
            "defaults_by_language": dict(settings.default_voices),
            "voices": [
                p.to_public_dict(registry.is_installed(p)) for p in profiles
            ],
        }

    @app.post("/synthesize")
    def synthesize(req: SynthesizeRequest):
        if len(req.text) > settings.max_text_chars:
            raise HTTPException(
                status_code=413,
                detail=f"Text exceeds max length of {settings.max_text_chars} chars.",
            )
        if not req.text.strip():
            raise HTTPException(status_code=400, detail="Text must not be empty.")
        if not gate.acquire(blocking=False):
            raise HTTPException(
                status_code=503,
                detail="The synthesizer is busy; retry shortly.",
                headers={"Retry-After": "1"},
            )
        voice_id = resolve_voice_id(registry, settings, req.voice, req.language)
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
        except SynthesisUnavailableError as exc:
            # 501: this host will never be able to serve the request, as
            # opposed to 503's "try again once the models are installed".
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        except SynthesisError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            gate.release()

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


_default_app: Optional[FastAPI] = None


def __getattr__(name: str):
    """Build the default ``app`` lazily, for ``uvicorn syscon_tts.api:app``."""
    global _default_app
    if name == "app":
        if _default_app is None:
            _default_app = create_app()
        return _default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
