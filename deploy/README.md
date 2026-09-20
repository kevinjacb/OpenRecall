# Deploying OpenRecall

**Jump to:**

- [Profiles](#profiles)
- [Quick start — cpu profile](#quick-start--cpu-profile)
- [Quick start — nvidia / CUDA box](#quick-start--nvidia--cuda-box)
- [Quick start — apple profile](#quick-start--apple-profile)
- [Configuration](#configuration)
- [The data volume](#the-data-volume)
- [Backups](#backups)
- [Migrating from a Mac to the box](#migrating-from-a-mac-to-the-box)
- [Troubleshooting](#troubleshooting)
- [Remote access — Cloudflare Tunnel](#remote-access--cloudflare-tunnel)

---

The system splits into two planes:

- **Service plane** — gateway, HTTP API, stores, memory extraction. Hardware
  independent, containerised identically everywhere. This is the `core` image.
- **Inference plane** — ASR and speaker embedding. Hardware specific, supplied
  per profile.

The split exists because **a container on macOS cannot reach Metal**. Docker
Desktop runs a Linux VM with no GPU passthrough, so a containerised MLX backend
would silently fall back to CPU. Rather than accept that, inference runs where
the accelerator is and the core talks to it over HTTP.

## Profiles

| Profile | Core | Inference | Status |
|---|---|---|---|
| `apple` | container | **native host process** (MLX) | working |
| `cpu` | container | container (faster-whisper) | working |
| `nvidia` | container | **native host process** (faster-whisper on CUDA) | works; the GPU itself is unverified |
| `cloud` | container | not started | endpoints only |

`./deploy/deploy.sh` detects which one applies. `--detect` prints the decision
without changing anything.

## Quick start — cpu profile

The only profile that is genuinely one command, because nothing runs natively:

```bash
./deploy/deploy.sh --profile cpu      # or just ./deploy/deploy.sh on a CPU box
```

That builds and starts the core plus a CPU inference container. The first run
downloads the ASR model into a named volume; allow a few minutes.

**The model default is `small.en`, and that is deliberate.** Measured on an
M-series CPU at int8:

| model | compute per second of audio | vs realtime |
|---|---|---|
| `large-v3-turbo` | 2.05 s/s | **0.49x — falls behind live audio** |
| `small.en` | 0.285 s/s | 3.51x |
| `tiny.en` | 0.05 s/s | ~20x |

At 0.49x the gateway never catches up, and its bounded ASR queue starts
dropping real audio packets. Raise `OPENRECALL_ASR_MODEL` only after measuring
on the hardware you are deploying to.

## Quick start — nvidia / CUDA box

Inference runs **natively on the host**, the same shape as the apple profile
and for the same reason: the host is where the accelerator driver is. That is
why this profile needs **no CUDA base image, no GPU device reservation and no
nvidia-container-toolkit** — nothing GPU-touching runs inside a container.
Containerising inference as well is a later convenience, not a prerequisite.

### 1. Start the inference sidecar (native, on the host)

```bash
cd server
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[fasterwhisper,speaker,opus,cuda]'

# CTranslate2 is built against CUDA 12 and loads libcublas.so.12 + cuDNN 9;
# a CUDA 13 toolkit ships libcublas.so.13. The `cuda` extra installs the CUDA 12
# runtime beside your toolkit — no downgrade, the driver is backward compatible
# — and the backend dlopens it at startup, so NO LD_LIBRARY_PATH is needed.

OPENRECALL_ASR_BACKEND=faster_whisper \
OPENRECALL_FASTER_WHISPER_DEVICE=cuda \
OPENRECALL_FASTER_WHISPER_COMPUTE_TYPE=float16 \
OPENRECALL_FASTER_WHISPER_MODEL=large-v3-turbo \
  python -u scripts/run_inference.py --host 0.0.0.0 --port 8767
```

Three things that bite here:

- **You should not need `LD_LIBRARY_PATH`.** The backend loads the pip-installed
  CUDA libraries itself before the model, so they are resident by the time
  CTranslate2 asks for them. If you do set it, note it is read at *process
  start* — same shell, and restart after changing it.
- **Do not copy the recipe from faster-whisper's docs.** The `nvidia-*-cu12`
  wheels are PEP 420 namespace packages, so
  `os.path.dirname(nvidia.cublas.lib.__file__)` raises `TypeError: ... not
  NoneType` — `__file__` is `None`. Use `__path__`:
  ```bash
  export LD_LIBRARY_PATH=$(python -c "import os, nvidia; \
    print(':'.join(sorted({r for b in nvidia.__path__ \
      for r, _, fs in os.walk(b) if any('.so' in f for f in fs)})))")
  ```
- **`--host 0.0.0.0` is required** — the default `127.0.0.1` is not reachable
  from inside the core container.
- **A clean model load proves nothing.** CTranslate2 loads cuBLAS lazily at the
  first compute, so a CUDA mismatch logs `model large-v3-turbo ready` and only
  then fails inside the warmup transcribe.

Verify before moving on:

```bash
curl -s localhost:8767/info
# {"embed_dim":256,"asr_backend":"FasterWhisperStreamingBackend","ready":true,
#  "components":{"asr":true,"embed":true}, ...}
```

`ready` must be `true` and **both** components `true`. `ready:false` with
`embed:false` means the embedder failed while ASR is fine — check the log.
`/info` answers 503 when it is not ready, so it is a real health check.

If you prefer a file to exported variables, the same settings live in
`server/config.toml` (copy `config.example.toml`):

```toml
[asr]
backend = "faster_whisper"

[faster_whisper]
model = "large-v3-turbo"
device = "cuda"
compute_type = "float16"
```

On a 12 GB card `large-v3-turbo` at float16 is roughly 1.5–2 GB, leaving room
for Ollama. If VRAM is tight, `compute_type=int8_float16` roughly halves it.
**Measure before committing to a budget** — the numbers in the design doc were
written for a different ASR engine.

### 2. Start the core

```bash
./deploy/deploy.sh          # detects nvidia via nvidia-smi
```

### What is verified, and what is not

The whole path — core container, native sidecar over
`host.docker.internal`, transcripts returning — is exercised and working. The
one thing **not** verified is CUDA itself: there is no NVIDIA GPU on the
development machine, so every run used the identical code path with
`device=cpu`. Expect the device argument to be the only difference; confirm it
with the `device=cuda` log line and by watching `nvidia-smi` during a session.

Worth re-checking on the GPU: CTranslate2 showed **no** MLX-style thread
affinity on CPU (its docs explicitly support calls from multiple threads), but
that was measured on CPU. A violation would not show up in tests — the fakes
have no affinity.

## Quick start — apple profile

**This is not `docker compose up` alone, and it is not honest to pretend it
is.** The inference sidecar runs natively, so there are two steps.

### 1. Start the inference sidecar (native, on the host)

```bash
cd server
pip install -e '.[mlx,speaker]'        # add '.[parakeet]' for the TDT backend
python -u scripts/run_inference.py --host 0.0.0.0 --port 8767
```

`--host 0.0.0.0` is **required**. The sidecar defaults to `127.0.0.1`, which is
correct for a native-only setup but unreachable from inside a container — the
opposite default from the gateway. Getting this wrong looks like silence: the
gateway's transcription failures collapse into a single log line by design.

### 2. Start the core (container)

```bash
docker compose -f deploy/docker-compose.yml up -d
docker compose -f deploy/docker-compose.yml logs -f core
```

The bearer token and command-signing public key are printed at boot. Copy the
token to the phone; provision the public key on the device.

### Ports are published unmapped, on purpose

`/health` advertises the gateway port so the phone can derive its WebSocket
URL, and it reports the **container-internal** value. Publishing `9765:8765`
hands the relay a URL that does not resolve. Leave 8765 and 8766 as they are.

## Configuration

Everything is `OPENRECALL_*` environment variables; `server/config.example.toml`
documents the full set. The four that the container specifically needs:

| Variable | Why |
|---|---|
| `OPENRECALL_INFERENCE_URL` | Required. This image ships no local ASR, so inference must be remote. The entrypoint refuses to start without it. |
| `OPENRECALL_LLM_BASE_URL`, `OPENRECALL_EMBED_BASE_URL`, `OPENRECALL_VLM_BASE_URL` | Default to `localhost:11434` in code, which inside a container is the container itself. Memory extraction fails *silently* if these are wrong. |
| `OPENRECALL_EMBED_MODEL`, `OPENRECALL_LLM_MODEL` | No defaults in code. Boot aborts without them. |
| `OPENRECALL_UID` / `OPENRECALL_GID` | Must match the owner of the data volume — see below. |

## The data volume

One mount, `/data`, holds everything durable:

- 8 SQLite databases — `events`, `commands`, `segment_meta`, `settings`,
  `atoms`, `reminders`, `memory_index`, `speakers`
- `audio/` — the recordings (`.opusraw` + `.peaks` per session)
- `blobs/` — content-addressed media
- `server_token`, `hermes.token` — bearer tokens
- `server_ed25519.key` — the command-signing private key

**Ownership matters before the first boot.** Both secrets are created on first
run, and that write is not guarded against `PermissionError` — a volume owned by
another UID aborts the container. Build with your own UID:

```bash
OPENRECALL_UID=$(id -u) OPENRECALL_GID=$(id -g) \
  docker compose -f deploy/docker-compose.yml up -d --build
```

The entrypoint checks this with a real write probe rather than a permission
test, because Docker Desktop's macOS bind mounts report permission bits that do
not reflect what the container can actually do.

## Backups

**An always-on box holds the user's entire recorded memory. Back the volume
up.** This is a deployment requirement, not a nice-to-have.

A stopped-server file copy is consistent: no database uses WAL, so there are no
`-wal`/`-shm` sidecars to tear. Note this holds *by omission* — nothing in the
code sets `journal_mode`, SQLite's compiled default is `DELETE`, and no test
pins it. **If anyone switches a store to WAL, this backup procedure silently
stops being correct.**

```bash
# Consistent backup: stop, copy, start.
docker compose -f deploy/docker-compose.yml stop core
tar -czf "openrecall-$(date +%F).tar.gz" -C /path/to/data .
docker compose -f deploy/docker-compose.yml start core
```

`audio/` dominates the size; the databases are ~16 MB. For a hot backup without
stopping, use `sqlite3 <db> ".backup"` per database and copy `audio/`
separately — accepting that the two are then a few seconds out of step.

## Migrating from a Mac to the box

1. Stop the gateway on the Mac.
2. Copy `server/data/` to the box's volume — all 8 databases, `audio/`,
   `blobs/`, and both secrets.
3. Start the stack there.

Two things to decide **before** you provision device firmware:

- **Keep `server_token`**, or the phone needs re-pairing.
- **Decide which machine owns `server_ed25519.key`.**
  `firmware/openrecall_sensor/main/config.h` currently has
  `SERVER_ED25519_PUBKEY[32] = {0}` — unprovisioned — so no device is pinned to
  a key yet. Pin the device to whichever key the box ends up with.

The `memory_index` `occurred_at` migration runs on first boot there; it is
idempotent and has been verified against a copy of this exact data.

## Troubleshooting

The entrypoint fails fast, with the cause, on the four misconfigurations that
are otherwise silent or confusing:

| Message | Cause |
|---|---|
| `FATAL: /data is not writable by uid N` | Volume ownership. Rebuild with your UID. |
| `FATAL: OPENRECALL_INFERENCE_URL is not set` | This image has no local ASR. |
| `FATAL: required model name(s) unset` | `OPENRECALL_EMBED_MODEL` / `OPENRECALL_LLM_MODEL`. |
| `Library libcublas.so.12 is not found` | CUDA-major mismatch: CTranslate2 wants CUDA 12, your toolkit is 13. Install `.[cuda]` and set `LD_LIBRARY_PATH` as in the nvidia section. **Do not downgrade the toolkit.** Note the model logs `ready` *before* this — CTranslate2 loads cuBLAS lazily at first compute, so a clean load proves nothing. |
| `No module named 'pkg_resources'` | webrtcvad (via Resemblyzer) imports it at module scope and Python 3.12+ venvs ship no setuptools. Reinstall the extra — `pip install -e '.[speaker]'` now pulls it in. |
| `WARNING: ...BASE_URL is ...localhost...` | Points at the container, not the host. |
| `cannot reach the LLM at ...` / `[Errno 111] Connection refused` | Either nothing is listening, or — the subtle one — the server is up but bound to loopback. **Ollama binds 127.0.0.1 by default**, which a container cannot reach even when `host.docker.internal` resolves. Start it with `OLLAMA_HOST=0.0.0.0:11434`, the same reason the inference sidecar needs `--host 0.0.0.0`. Capture and transcription are unaffected; only extraction and titles stop. |
| `426` + `websocket_sent_to_http_api`, or the relay reporting "Expected HTTP 101 response" | The tunnel is sending the WebSocket to the HTTP API. Only port 8766 is routed; add the `path: ^/$` rule below so the root path reaches the gateway. |

Two more worth knowing:

- **`--db` must be named `events.db`.** The seven sibling database paths are
  derived from it by substring replacement, so any other name used to collapse
  all eight stores onto one file. The gateway now refuses to start instead.
- **Container logs contain both bearer tokens**, printed at boot. Treat them as
  sensitive wherever logs are shipped.

## Remote access — Cloudflare Tunnel

### What has to be exposed

| Port | What | Expose? |
|---|---|---|
| 8765 | WebSocket gateway — §E control + §C.6 audio | yes |
| 8766 | HTTP control API — `/health`, `/sessions`, `/segments`, `/speakers`, … | yes |
| 8767 | Inference service | **never** — it has no authentication and receives raw audio |

**One hostname, not two.** The relay derives its WebSocket URL from the HTTP
URL it was provisioned with — same host, `wss` if `https`
(`android/.../net/WsUrl.kt`). It cannot be pointed at a second hostname.

### The routing rule

The relay drops path and query when building that URL, so its WebSocket always
lands on the **root** path, while every HTTP API call has a real path. That one
fact is what lets a single hostname carry both:

```yaml
# ~/.cloudflared/config.yml
tunnel: <tunnel-id>
credentials-file: /root/.cloudflared/<tunnel-id>.json

ingress:
  # Root -> the WebSocket gateway. The relay connects to "/" with no path.
  - hostname: sense.example.com
    path: ^/$
    service: ws://localhost:8765

  # Everything else -> the HTTP control API.
  - hostname: sense.example.com
    service: http://localhost:8766

  - service: http_status:404
```

Verified with `cloudflared tunnel ingress rule`: `/` → 8765, and `/health`,
`/sessions`, `/segments/{id}/audio`, `/mcp` → 8766.

**Routing only 8766 does not work**, and it fails the same way whichever port
you advertise. Left at the default the relay dials `wss://<host>:8765`, which
Cloudflare does not serve. With `--advertised-gateway-port 0` it dials
`wss://<host>` on 443, reaches the HTTP API, and gets a **426** naming the
problem (without the rule below it would be a bare 404, which clients report as
"Expected HTTP 101 response" — a message that points nowhere useful). Both
listeners have to be reachable through the one hostname.

### The port the server advertises

`/health` reports `gatewayPort`, and the relay builds
`wss://<host>:<gatewayPort>`. Left at the default that is **8765** — a port
Cloudflare does not serve, so the phone would fail to connect. Start the
gateway with:

```bash
python scripts/run_gateway.py --advertised-gateway-port 0    # omit the field
```

The relay then falls back to the port already in its HTTPS URL (443).
`--advertised-gateway-port 443` is equivalent and more explicit. This does not
change what the gateway binds — only what it advertises.

### Authentication

**Every HTTP route requires the bearer token, including `/health`** — there is
no public endpoint, so a tunnel health check must send the header. Two
consequences worth stating plainly:

1. **TLS terminates at Cloudflare's edge**, so transcripts and audio traverse
   their infrastructure in the clear. For a device that records bystanders this
   is a distinct trust decision from "no open ports".
2. **Put Cloudflare Access in front of it.** Otherwise one bearer token — which
   is printed to stdout at boot, so it is in `docker logs` — is all that
   separates the public internet from everything the wearer has said.

Cloudflare closes idle WebSockets at ~100 s; the server pings every 20 s, so the
tunnel survives silence.

*Risk:* continuous audio over a tunnel may brush Cloudflare's free-plan terms
on non-HTML content. Not legal advice; worth checking before relying on it.
