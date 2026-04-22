# vox-engine-voxcpm: VoxCPM2 synthesis engine microservice
# Requires: nvidia-container-runtime on the host for GPU passthrough.

FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    HF_HOME=/cache/huggingface

# Runtime system deps only; uv provisions Python itself.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 curl ca-certificates \
        gcc g++ \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# uv handles both Python provisioning and dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Provision Python 3.12 via uv (standalone build, no apt PPA required)
RUN uv python install 3.12

# Install deps first for layer caching. uv.lock pins every transitive dep;
# pyproject.toml specifies >=3.12,<3.13 so uv picks the 3.12 we just installed.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

ENV PATH="/opt/venv/bin:${PATH}"

COPY engine /app/engine

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=5 --start-period=120s \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "engine.main:app", "--host", "0.0.0.0", "--port", "8000"]
