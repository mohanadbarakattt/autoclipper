FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 CLIPPER_ROOT=/app HF_HOME=/app/data/cache/hf

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates curl gnupg fontconfig \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y gnupg && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY clipper ./clipper
COPY scripts ./scripts
RUN python scripts/get_fonts.py

EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s \
  CMD curl -fs http://localhost:8090/api/overview > /dev/null || exit 1
CMD ["python", "-m", "clipper"]
