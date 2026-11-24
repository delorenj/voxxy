# Voxxy core, self-hosted edition: CPU-only; speech runs in the engine container.

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    HF_HOME=/cache/huggingface

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 curl ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

WORKDIR /app

RUN uv python install 3.12

COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-install-project

ENV PATH="/opt/venv/bin:${PATH}"

COPY app /app/app
# When this release came out, for the license's 12 months of updates (written by the bundle build).
COPY RELEASE.json /app/RELEASE.json

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
