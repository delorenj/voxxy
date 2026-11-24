# vox-tts: CPU-only core; synthesis runs in sidecar engine containers.

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

# Runtime system deps only; uv provisions Python itself.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 curl ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# uv handles both Python provisioning and dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Provision Python 3.12 via uv (standalone build, no apt PPA required)
RUN uv python install 3.12

# Install deps first for layer caching. uv.lock pins every transitive;
# pyproject.toml specifies >=3.12,<3.13 so uv picks the 3.12 we just installed.
COPY pyproject.toml uv.lock /app/
RUN uv sync --frozen --no-install-project

ENV PATH="/opt/venv/bin:${PATH}"

COPY app /app/app
COPY scripts /app/scripts

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
