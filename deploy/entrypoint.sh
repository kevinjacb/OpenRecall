#!/bin/sh
# OpenRecall core entrypoint.
#
# Turns the container's environment into gateway flags, and fails fast on the
# handful of misconfigurations that are silent or confusing otherwise. Each
# check below exists because the failure it prevents was observed or traced in
# source — none are speculative.
set -eu

DB="${OPENRECALL_DB:-/data/events.db}"
DATA_DIR="$(dirname "$DB")"

# --- 1. The data volume must be writable BEFORE first boot -------------------
# Secret creation (auth.py, commands/signing.py) wraps only the chmod, not the
# write. On a volume owned by another UID the container dies with a bare
# PermissionError traceback and no indication that ownership is the problem.
#
# The check is a real write, not `[ -w ]`. Docker Desktop's macOS bind mounts
# present permission bits that do not reflect what the container can actually
# do: against a host directory mode 555, `[ -w ]` succeeds and the failure
# surfaces later as `sqlite3.OperationalError: unable to open database file`,
# pointing at the event store rather than at the volume. Observed 2026-09-17,
# on the apple profile this image is built for.
_probe="$DATA_DIR/.openrecall-write-probe.$$"
if ! ( : > "$_probe" ) 2>/dev/null; then
    echo "FATAL: $DATA_DIR is not writable by uid $(id -u)." >&2
    echo "The server creates its token and Ed25519 signing key there on first" >&2
    echo "boot. Fix the volume ownership on the host, e.g.:" >&2
    echo "    sudo chown -R $(id -u):$(id -g) <host path>" >&2
    echo "or rebuild the image with --build-arg UID=\$(id -u) --build-arg GID=\$(id -g)." >&2
    exit 1
fi
rm -f "$_probe"

# --- 2. The LLM endpoints must not point at the container itself -------------
# All three default to http://localhost:11434/v1 (Ollama). Inside a container
# that resolves to the container, not the host — and nothing in the code warns:
# the calls just fail and the exceptions are swallowed, so extraction silently
# produces nothing. Warn loudly rather than exiting, since a deployment with no
# LLM at all is still a usable capture-and-transcribe system.
for var in OPENRECALL_LLM_BASE_URL OPENRECALL_EMBED_BASE_URL OPENRECALL_VLM_BASE_URL; do
    eval "value=\${$var:-}"
    case "${value}" in
        ""|*localhost*|*127.0.0.1*)
            echo "WARNING: $var is '${value:-unset (defaults to localhost:11434)}'." >&2
            echo "  Inside a container that is the container itself. Memory extraction" >&2
            echo "  will fail silently. Set it to host.docker.internal (Docker Desktop)" >&2
            echo "  or the compose service name." >&2
            ;;
    esac
done

# --- 3. Inference location ---------------------------------------------------
# Unset means in-process, which this image cannot do: mlx/parakeet are not
# installed (Apple-Silicon-only) and neither is resemblyzer. Transcription
# would fail on every frame, which the worker now collapses into one log line —
# so it would look like silence rather than an error. Be explicit.
if [ -z "${OPENRECALL_INFERENCE_URL:-}" ]; then
    echo "FATAL: OPENRECALL_INFERENCE_URL is not set." >&2
    echo "This image ships no local ASR (mlx/parakeet are Apple-Silicon-only and" >&2
    echo "resemblyzer is not installed), so inference must be remote. Point it at" >&2
    echo "the inference service, e.g. http://host.docker.internal:8767 on the" >&2
    echo "apple profile or http://inference:8767 in compose." >&2
    exit 1
fi

# --- 4. The model names have no defaults ------------------------------------
# OpenAICompatibleEmbedder/ChatModel.from_env raise ValueError when their model
# name is unset (memory/embeddings.py:43, memory/llm.py:40), which surfaces as
# a boot traceback rather than a configuration message. The VLM is exempt: it
# is optional and already degrades with a clear warning.
_missing=""
for var in OPENRECALL_EMBED_MODEL OPENRECALL_LLM_MODEL; do
    eval "value=\${$var:-}"
    [ -z "$value" ] && _missing="$_missing $var"
done
if [ -n "$_missing" ]; then
    echo "FATAL: required model name(s) unset:$_missing" >&2
    echo "These have no defaults. Set them to models your LLM endpoint serves," >&2
    echo "e.g. OPENRECALL_EMBED_MODEL=nomic-embed-text and" >&2
    echo "OPENRECALL_LLM_MODEL=qwen2.5:7b for Ollama." >&2
    exit 1
fi

# --- 5. Secrets go to the volume, not a throwaway layer ----------------------
# --key-file and --token-file are independent flags that do NOT follow --db.
# Left at their defaults they resolve relative to the working directory, which
# is inside the image — so the phone would need re-pairing after every restart.
exec python -u /app/scripts/run_gateway.py \
    --host 0.0.0.0 \
    --port "${OPENRECALL_WS_PORT:-8765}" \
    --http-port "${OPENRECALL_HTTP_PORT:-8766}" \
    --db "$DB" \
    --key-file "${OPENRECALL_KEY_FILE:-$DATA_DIR/server_ed25519.key}" \
    --token-file "${OPENRECALL_TOKEN_FILE:-$DATA_DIR/server_token}" \
    --config "${OPENRECALL_CONFIG:-$DATA_DIR/config.toml}" \
    "$@"
