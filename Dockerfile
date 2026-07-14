# Offline TTS service image for Linux x86-64.
# Build:  docker build -t plantstar-tts .
# Run:    docker run -p 5002:5002 -v "$PWD/voices:/app/voices" plantstar-tts
#
# Voice models are mounted from the host (see scripts/download_voices.sh) so the
# image stays small and models can be updated without rebuilding.
FROM python:3.11-slim

# ffmpeg is optional; included so MP3 output works out of the box.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY src ./src
RUN pip install --no-cache-dir -e .

COPY config ./config
# voices/ is expected to be mounted at runtime; create the mount point.
RUN mkdir -p /app/voices

ENV PLANTSTAR_TTS_HOME=/app \
    PLANTSTAR_TTS_HOST=0.0.0.0 \
    PLANTSTAR_TTS_PORT=5002

EXPOSE 5002
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5002/health').status==200 else 1)"

CMD ["plantstar-tts", "serve"]
