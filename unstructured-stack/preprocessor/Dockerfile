# syntax=docker/dockerfile:1.7
ARG UV_IMAGE_TAG=python3.12-bookworm-slim
FROM ghcr.io/astral-sh/uv:${UV_IMAGE_TAG}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_NO_CACHE=1

WORKDIR /app

# Dependency layer first so code edits don't re-resolve deps. uv only — no pip.
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml

COPY preprocess.py ./

# Mount points; the real content arrives as bind mounts from docker-compose.yml.
RUN mkdir -p /data/inbox /data/archive /data/failed /data/state /vault/corporate

# Non-root. Compose overrides this with ${PREPROCESSOR_UID}:${PREPROCESSOR_GID} so the
# uid can be matched to whoever owns the vault on the host.
USER 1000:1000

ENTRYPOINT ["python", "-u", "/app/preprocess.py"]
