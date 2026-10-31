# voice-host - the owned speech service.
# Base image: TTS presets only. Add --build-arg WITH_CLONE=true to bake in the clone engine.
FROM python:3.11-slim

ARG WITH_CLONE=false
ENV PYTHONUNBUFFERED=1 \
    PIPER_VOICE=en_US-lessac-medium \
    DATA_DIR=/data \
    PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# The clone engine is large and optional: without it the container still serves the voice
# catalogue, stores reference audio and speaks with Piper.
COPY requirements-clone.txt ./
RUN if [ "$WITH_CLONE" = "true" ]; then pip install --no-cache-dir -r requirements-clone.txt; fi

# One Piper voice baked in so /speak works on first boot; more can be added to the volume.
RUN mkdir -p /data/piper && \
    python -c "import piper, shutil, os; from piper.download_voices import download_voice; \
download_voice('en_US-lessac-medium', '/data/piper')" || echo "piper voice download skipped; mount one at /data/piper"

COPY server.py ./

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s \
  CMD curl -fsS -H "Authorization: Bearer $HOST_TOKEN" http://127.0.0.1:8080/health || exit 1

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT}"]