# OpenRecall core — the service plane (spec §2).
#
# Hardware-independent: gateway, HTTP API, stores, memory extraction. ASR and
# speaker embedding are NOT here. They need an accelerator, and a container on
# macOS cannot reach Metal, so they run in the inference service (which is
# native on the apple profile and a CUDA container on nvidia) and this image
# talks to it over HTTP via OPENRECALL_INFERENCE_URL.
#
# Build context is the REPO ROOT, so deploy/ and server/ are both reachable:
#   docker build -f deploy/Dockerfile -t openrecall-core:dev .
#
# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim AS build

# Build deps only. Kept in a separate stage so the runtime image carries no
# compiler and no apt lists.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends gcc

WORKDIR /build
COPY server/pyproject.toml ./
COPY server/src ./src

# `opus`  — Opus decoding stays in the core; the inference API takes decoded
#           PCM, so this is not optional even with remote inference.
# `llm`   — httpx. The extraction worker starts at boot and every extraction,
#           embedding and VLM call goes through it.
# Deliberately NOT installed: mlx, parakeet (Apple-Silicon-only, and inference
# is remote), speaker (Resemblyzer lives with the embedder), vad, video,
# denoise. Every heavy import in the package is lazy and inside a function, so
# their absence costs nothing on the paths this image runs.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/install '.[opus,llm]'


FROM python:${PYTHON_VERSION}-slim AS runtime

# libopus: opuslib loads it through ctypes at import time of the decoder.
# tini: PID 1 that reaps zombies and forwards signals. The gateway installs its
# own SIGTERM handler, but PID 1 semantics mean a bare python would not get the
# default-kill behaviour it expects.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        libopus0 tini

# Non-root. The UID must match the owner of the mounted data volume: secret
# creation on first boot writes into it and is NOT guarded against
# PermissionError, so a mismatch aborts the container with a bare traceback.
# Override at build time to match the host: --build-arg UID=$(id -u).
ARG UID=1000
ARG GID=1000
# Reuse an existing id rather than assuming it is free. A macOS host's default
# GID is 20, which already exists in Debian as `dialout`, so a plain groupadd
# fails with exit 4 and the build dies — on the apple profile, which is the
# default here. The same applies to any UID/GID that happens to be taken.
RUN if ! getent group ${GID} >/dev/null 2>&1; then groupadd --gid ${GID} sense; fi \
 && if ! getent passwd ${UID} >/dev/null 2>&1; then \
        useradd --uid ${UID} --gid ${GID} --create-home --shell /usr/sbin/nologin sense; \
    fi

COPY --from=build /install /usr/local
WORKDIR /app
COPY server/scripts ./scripts
COPY server/config.example.toml ./
COPY --chmod=0755 deploy/entrypoint.sh /usr/local/bin/entrypoint.sh

# The data volume. Created here so the image has a mountpoint with the right
# ownership even when the volume is empty on first run.
RUN mkdir -p /data && chown ${UID}:${GID} /data
VOLUME ["/data"]

# Numeric, not the name: when the UID already existed we did not create a
# "sense" user, and a name that does not resolve fails at container start.
USER ${UID}:${GID}

# Published unmapped, deliberately. /health advertises the gateway port so the
# phone can derive its WebSocket URL, and it reports the CONTAINER-internal
# port — remap 8765 and the relay builds a URL that does not resolve.
EXPOSE 8765 8766

ENV PYTHONUNBUFFERED=1 \
    OPENRECALL_DB=/data/events.db

# The gateway serves the HTTP API before it is useful, and /health requires a
# bearer token, so the check is a plain TCP connect to the WebSocket port —
# what the relay actually needs to be up.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import socket,sys; s=socket.create_connection(('127.0.0.1',8765),2); s.close()" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
