# Deploying OpenRecall

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
| `apple` | container | **native host process** | working |
| `cpu` | container | container (faster-whisper) | working |
| `nvidia` | container | CUDA container | **not built** — needs the GPU box |
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
| `WARNING: ...BASE_URL is ...localhost...` | Points at the container, not the host. |

Two more worth knowing:

- **`--db` must be named `events.db`.** The seven sibling database paths are
  derived from it by substring replacement, so any other name used to collapse
  all eight stores onto one file. The gateway now refuses to start instead.
- **Container logs contain both bearer tokens**, printed at boot. Treat them as
  sensitive wherever logs are shipped.

## Remote access

Cloudflare Tunnel gives outbound-only reach with no open ports. Two caveats,
recorded deliberately:

1. **TLS terminates at Cloudflare's edge**, so transcripts and audio traverse
   their infrastructure in the clear. For a device that records bystanders this
   is a distinct trust decision from "no open ports".
2. **Put Cloudflare Access in front of it.** Otherwise one bearer token — which,
   per above, is sitting in the container logs — is all that separates the
   public internet from everything the wearer has said.

Cloudflare closes idle WebSockets at ~100 s; the server pings every 20 s, so the
tunnel survives silence.
