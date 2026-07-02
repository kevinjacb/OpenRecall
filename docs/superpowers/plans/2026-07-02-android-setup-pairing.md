# Android Setup & Pairing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Android app the setup interface: pair a Sense wearable over BLE, provision it at runtime with the server's Ed25519 public key, and start the relay — end-to-end across server, firmware, and app.

**Architecture:** Server gets a second surface — a small aiohttp HTTP control API (bearer-token + TLS) alongside the existing audio-only WebSocket (which now also requires the bearer token). Firmware gains a provisioning GATT service + NVS-backed state machine that writes the server key at runtime (no reflash). App gains a Compose setup-wizard launcher Activity + persisted config, and the relay service sends the bearer token over wss.

**Tech Stack:** Python 3.14 / aiohttp / websockets / pytest (server); ESP-IDF v6.0.1 / NimBLE / libsodium / host cc tests (firmware); Kotlin / Jetpack Compose / OkHttp / DataStore / Gradle (Android).

## Global Constraints

- Server: Python 3.14, venv at `server/.venv`, `pytest` from `server/` with `pythonpath=["src"]`, `asyncio_mode="auto"`. TDD throughout. All models pluggable per `SENSE_LLM_*/SENSE_EMBED_*/SENSE_VLM_*` (OpenAI-compatible) — do not hardcode a model. New dep: `aiohttp>=3.10` added to `pyproject.toml` `[project] dependencies`.
- Firmware: ESP-IDF v6.0.1 at `~/.espressif/v6.0.1/esp-idf`; activate with `export IDF_PATH=~/.espressif/v6.0.1/esp-idf && source $IDF_PATH/export.sh`. Build for `esp32s3`. Host tests run with `make` (or `make test`) from `firmware/sense_sensor/test/` using `cc -std=c11 -Wall -Wextra -I../main`. Host-testable logic must be dependency-free (no ESP-IDF calls in the unit under test). Per `[[firmware-esp-idf]]`.
- Android: `compileSdk=34`, `minSdk=26`, Kotlin 17. JVM tests via `./gradlew test` from `android/sense-relay/` using `kotlin.test`. Cannot build/run instrumented tests in this env (no SDK/Gradle) — that's a bench-side step.
- Auth: single shared bearer token (32 random bytes, hex), generated server-side on first run into `data/server_token`, sent as `Authorization: Bearer <token>` on every HTTP and WS call. No "trust all" TLS toggle — pin a CA.
- UX: Compose, monochrome (black/white + at most one accent for state), generous spacing, large touch targets, fluid `AnimatedContent` transitions, minimal-step flows.
- Project is NOT a git repo today; `git init` is out of scope unless the user asks. Tasks still end with a `git add`/`git commit` step for when a repo exists — run them only if `.git` is present, otherwise treat the commit step as a checkpoint marker.

---

## File Structure

**Server** (`server/`)
- Create `src/sense_server/http/__init__.py`
- Create `src/sense_server/http/auth.py` — bearer-token middleware + a `BearerAuth` helper used by both HTTP and WS.
- Create `src/sense_server/http/app.py` — aiohttp app factory wiring auth + routes.
- Create `src/sense_server/http/routes/__init__.py`
- Create `src/sense_server/http/routes/provisioning.py` — `/health`, `/provisioning/pubkey`.
- Create `src/sense_server/auth.py` — `load_or_create_token(path)` (mirrors `load_or_create_signer`).
- Create `src/sense_server/http/token.py` — constant-time token compare helper (shared by HTTP middleware + WS).
- Create `tests/http/test_auth.py`, `tests/http/test_provisioning.py`, `tests/test_ws_auth.py`
- Modify `src/sense_server/gateway/adapter.py` — bearer check at top of `handler`; `serve()` gains `token` param.
- Modify `scripts/run_gateway.py` — load/generate token, start HTTP app, print URL+token+pubkey.
- Modify `pyproject.toml` — add `aiohttp>=3.10`.
- Modify `src/sense_server/sim/device_client.py` — send `Authorization: Bearer` on WS.

**Firmware** (`firmware/sense_sensor/`)
- Create `main/provisioning_core.c` + `main/provisioning_core.h` — pure state-machine logic, no ESP-IDF deps (host-testable).
- Create `main/provisioning.c` + `main/provisioning.h` — ESP-IDF NVS-backed `prov_store_t` + GATT access callbacks + `provisioning_init`.
- Modify `main/commands.c` / `commands.h` — add `commands_set_pubkey()`.
- Modify `main/ble_link.c` — register provisioning service + 3 characteristics.
- Modify `main/config.h` — provisioning service/char UUIDs, NVS namespace, FACTORY_RESET magic.
- Modify `main/sense_sensor.c` — `provisioning_init()` then seed commands from provisioning.
- Modify `main/CMakeLists.txt` — add `provisioning.c`, `provisioning_core.c`.
- Create `test/test_provisioning.c` — host test; modify `test/Makefile`.

**Android** (`android/sense-relay/app/`)
- Modify `build.gradle.kts` — Compose BOM + deps, DataStore, lifecycle-viewmodel-compose, buildFeatures.
- Create `src/main/kotlin/com/sense/relay/store/ServerConfig.kt` — DataStore persistence.
- Create `src/main/kotlin/com/sense/relay/http/SenseHttpClient.kt` — OkHttp, bearer, TLS pinning.
- Create `src/main/kotlin/com/sense/relay/setup/ProvisioningClient.kt` — BLE STATE/SERVER_KEY/FACTORY_RESET ops.
- Create `src/main/kotlin/com/sense/relay/setup/SetupViewModel.kt` — wizard state machine.
- Create `src/main/kotlin/com/sense/relay/ui/SetupActivity.kt` — launcher Activity + Compose nav.
- Create `src/main/kotlin/com/sense/relay/ui/SetupScreen.kt` + `ui/StatusScreen.kt` + `ui/Theme.kt` — Compose UI, monochrome.
- Modify `net/ServerSocket.kt` — accept `token`, add `Authorization` header.
- Modify `RelayService.kt` — read token (intent/DataStore), pass to `ServerSocket`, use `wss://`.
- Modify `src/main/AndroidManifest.xml` — add launcher Activity, `ACCESS_NETWORK_STATE`.
- Create `src/test/kotlin/com/sense/relay/ProvisioningClientTest.kt`, `SetupViewModelTest.kt`.

---

## Phase 1 — Server: bearer token, HTTP control API, WS auth

### Task 1: Token management (`load_or_create_token`)

**Files:**
- Create: `server/src/sense_server/auth.py`
- Test: `server/tests/test_token.py`

**Interfaces:**
- Produces: `load_or_create_token(path: str | Path) -> str` — returns a 64-char hex token; persists to `path` (0600) on first creation, loads thereafter.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_token.py
import os
from pathlib import Path
from sense_server.auth import load_or_create_token


def test_load_or_create_token_generates_and_persists(tmp_path: Path):
    p = tmp_path / "tok"
    t1 = load_or_create_token(p)
    assert len(t1) == 64
    assert all(c in "0123456789abcdef" for c in t1)
    assert p.read_text().strip() == t1
    assert (os.stat(p).st_mode & 0o777) == 0o600


def test_load_or_create_token_is_stable(tmp_path: Path):
    p = tmp_path / "tok"
    t1 = load_or_create_token(p)
    t2 = load_or_create_token(p)
    assert t1 == t2


def test_load_or_create_token_is_random_across_paths(tmp_path: Path):
    a = load_or_create_token(tmp_path / "a")
    b = load_or_create_token(tmp_path / "b")
    assert a != b
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_token.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sense_server.auth'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/src/sense_server/auth.py
from __future__ import annotations
import os
import secrets
from pathlib import Path


def load_or_create_token(path: str | Path) -> str:
    """Return a 32-byte (64 hex char) bearer token, generating + persisting on first use."""
    token_path = Path(path)
    if token_path.exists():
        return token_path.read_text().strip()
    token = secrets.token_hex(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token)
    try:
        token_path.chmod(0o600)
    except OSError:  # pragma: no cover - non-POSIX
        pass
    return token
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_token.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add server/src/sense_server/auth.py server/tests/test_token.py
git commit -m "feat(server): add load_or_create_token bearer-token helper"
```

---

### Task 2: Constant-time token compare + WS bearer check

**Files:**
- Create: `server/src/sense_server/http/token.py`
- Modify: `server/src/sense_server/gateway/adapter.py` (the `handler` at ~line 79 + `serve` signature)
- Test: `server/tests/test_ws_auth.py`

**Interfaces:**
- Produces: `constant_time_eq(a: str, b: str) -> bool` (in `sense_server.http.token`).
- Produces: `serve(..., token: str | None = None)` now rejects WS connections lacking a valid `Authorization: Bearer <token>` header with a 401 close. When `token is None`, auth is disabled (preserves local dev + existing tests until they opt in).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_ws_auth.py
import asyncio
import pytest
from websockets.asyncio.client import connect
from sense_server.gateway.adapter import serve
from sense_server.auth import load_or_create_token


@pytest.mark.asyncio
async def test_ws_rejects_missing_token(unused_tcp_port, tmp_path):
    token = load_or_create_token(tmp_path / "tok")
    server_task = asyncio.create_task(
        serve(lambda: None, host="127.0.0.1", port=unused_tcp_port,
              event_store=None, dispatcher=None, token=token)
    )
    await asyncio.sleep(0.1)
    try:
        async with connect(f"ws://127.0.0.1:{unused_tcp_port}") as ws:
            await ws.send('{"type":"hello","session_id":"s","start_seq":0}')
            # server should close with a policy error before processing
            with pytest.raises(Exception):
                await asyncio.wait_for(ws.recv(), timeout=1.0)
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task


@pytest.mark.asyncio
async def test_ws_accepts_valid_token(unused_tcp_port, tmp_path):
    token = load_or_create_token(tmp_path / "tok")
    server_task = asyncio.create_task(
        serve(lambda: None, host="127.0.0.1", port=unused_tcp_port,
              event_store=None, dispatcher=None, token=token)
    )
    await asyncio.sleep(0.1)
    try:
        async with connect(
            f"ws://127.0.0.1:{unused_tcp_port}",
            additional_headers={"Authorization": f"Bearer {token}"},
        ) as ws:
            # An invalid §E message yields a protocol-error close, NOT an auth close.
            # Receiving any close (not an immediate auth rejection) means auth passed.
            await ws.send("not json")
            with pytest.raises(Exception):
                await asyncio.wait_for(ws.recv(), timeout=1.0)
    finally:
        server_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server_task
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_ws_auth.py -v`
Expected: FAIL — `serve()` does not accept a `token` argument.

- [ ] **Step 3: Write minimal implementation**

```python
# server/src/sense_server/http/token.py
import hmac


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
```

Edit `server/src/sense_server/gateway/adapter.py`. Add import and modify `handler` + `serve`. The current `handler` (around line 79) becomes:

```python
from sense_server.http.token import constant_time_eq


async def serve(pipeline_factory, *, host, port, event_store, dispatcher, token=None):
    async def handler(ws):
        # --- bearer-token check (skip when token is None: local dev) ---
        if token is not None:
            auth = ws.request.headers.get("Authorization", "")
            if not auth.startswith("Bearer ") or not constant_time_eq(auth[7:], token):
                await ws.close(code=1008, reason="unauthorized")
                return
        core = GatewayCore(
            pipeline_factory=pipeline_factory,
            event_store=event_store,
            dispatcher=dispatcher,
        )
        try:
            async for message in ws:
                replies = await asyncio.to_thread(handle_message, core, message)
                for reply in replies:
                    await ws.send(reply)
        except GatewayError:
            await ws.close(code=1002, reason="protocol error")
        except ConnectionClosed:
            pass

    async with websockets.serve(handler, host, port):
        await asyncio.Future()  # run forever
```

Keep all existing imports/other code in `adapter.py` unchanged; only add the `token` param, the import, and the guard block.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_ws_auth.py -v`
Expected: PASS (2 tests). Also run the full suite to confirm no regression:
Run: `cd server && python -m pytest -q`
Expected: PASS (132 + new tests).

- [ ] **Step 5: Commit**

```bash
git add server/src/sense_server/http/token.py server/src/sense_server/gateway/adapter.py server/tests/test_ws_auth.py
git commit -m "feat(server): require bearer token on WS handshake"
```

---

### Task 3: Bearer auth middleware (aiohttp)

**Files:**
- Create: `server/src/sense_server/http/__init__.py` (empty)
- Create: `server/src/sense_server/http/auth.py`
- Test: `server/tests/http/__init__.py` (empty), `server/tests/http/test_auth.py`

**Interfaces:**
- Produces: `@web.middleware` `bearer_auth_middleware` reading the token from `app["sense_token"]`; sets `request["authorized"]=True` or returns `401`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/http/test_auth.py
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sense_server.http.app import build_app


@pytest.fixture
async def client(tmp_path):
    from sense_server.auth import load_or_create_token
    token = load_or_create_token(tmp_path / "tok")
    app = build_app(token=token, get_pubkey=lambda: bytes(32))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client, token
    await client.close()


async def test_health_requires_token(client):
    cli, token = client
    resp = await cli.get("/health")
    assert resp.status == 401


async def test_health_ok_with_token(client):
    cli, token = client
    resp = await cli.get("/health", headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 200
    assert (await resp.json())["status"] == "ok"


async def test_rejects_wrong_token(client):
    cli, _ = client
    resp = await cli.get("/health", headers={"Authorization": "Bearer deadbeef"})
    assert resp.status == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/http/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sense_server.http.app'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/src/sense_server/http/__init__.py
```
(empty file)

```python
# server/src/sense_server/http/auth.py
from aiohttp import web
from sense_server.http.token import constant_time_eq


@web.middleware
async def bearer_auth_middleware(request, handler):
    token = request.app["sense_token"]
    if token is None:
        request["authorized"] = True
        return await handler(request)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and constant_time_eq(auth[7:], token):
        request["authorized"] = True
        return await handler(request)
    return web.json_response({"error": "unauthorized"}, status=401)
```

`build_app` is created in Task 4; to make this test pass we need a minimal `build_app` now. Create a stub that will be expanded in Task 4:

```python
# server/src/sense_server/http/app.py
from aiohttp import web
from sense_server.http.auth import bearer_auth_middleware


def build_app(*, token, get_pubkey):
    app = web.Application(middlewares=[bearer_auth_middleware])
    app["sense_token"] = token
    app["sense_get_pubkey"] = get_pubkey
    from sense_server.http.routes.provisioning import add_routes
    add_routes(app)
    return app
```

And the routes file (also expanded in Task 4):

```python
# server/src/sense_server/http/routes/__init__.py
```
(empty)

```python
# server/src/sense_server/http/routes/provisioning.py
from aiohttp import web


def add_routes(app):
    app.router.add_get("/health", health)


async def health(request):
    if not request.get("authorized"):
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({"status": "ok"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/http/test_auth.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add server/src/sense_server/http server/tests/http
git commit -m "feat(server): aiohttp bearer-auth middleware + health route"
```

---

### Task 4: Provisioning pubkey route

**Files:**
- Modify: `server/src/sense_server/http/routes/provisioning.py` (add `/provisioning/pubkey`)
- Test: `server/tests/http/test_provisioning.py`

**Interfaces:**
- Produces: `GET /provisioning/pubkey` → 200 `{"pubkey": "<64 hex>", "key_id": "default", "created_at": null}`; 503 `{"error":"no_key"}` if `get_pubkey()` returns `None`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/http/test_provisioning.py
import pytest
from aiohttp.test_utils import TestClient, TestServer
from sense_server.http.app import build_app
from sense_server.auth import load_or_create_token


@pytest.fixture
async def client_factory(tmp_path):
    token = load_or_create_token(tmp_path / "tok")
    async def make(get_pubkey):
        app = build_app(token=token, get_pubkey=get_pubkey)
        server = TestServer(app)
        cli = TestClient(server)
        await cli.start_server()
        return cli
    yield make


async def test_pubkey_returns_hex(client_factory):
    cli = await client_factory(lambda: bytes(range(32)))
    resp = await cli.get("/provisioning/pubkey",
                        headers={"Authorization": f"Bearer " + ""})  # replaced below
    # use the real token:
    ...
```

Replace that with a clean version:

```python
# server/tests/http/test_provisioning.py
import pytest
from aiohttp.test_utils import TestClient, TestServer
from sense_server.http.app import build_app
from sense_server.auth import load_or_create_token


async def _client(tmp_path, get_pubkey):
    token = load_or_create_token(tmp_path / "tok")
    app = build_app(token=token, get_pubkey=get_pubkey)
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    return cli, token


async def test_pubkey_returns_hex(tmp_path):
    cli, token = await _client(tmp_path, lambda: bytes(range(32)))
    resp = await cli.get("/provisioning/pubkey",
                        headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 200
    body = await resp.json()
    assert body["pubkey"] == bytes(range(32)).hex()
    assert body["key_id"] == "default"
    await cli.close()


async def test_pubkey_503_when_no_key(tmp_path):
    cli, token = await _client(tmp_path, lambda: None)
    resp = await cli.get("/provisioning/pubkey",
                        headers={"Authorization": f"Bearer {token}"})
    assert resp.status == 503
    assert (await resp.json())["error"] == "no_key"
    await cli.close()


async def test_pubkey_requires_token(tmp_path):
    cli, _ = await _client(tmp_path, lambda: bytes(32))
    resp = await cli.get("/provisioning/pubkey")
    assert resp.status == 401
    await cli.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/http/test_provisioning.py -v`
Expected: FAIL — 404 (route not registered).

- [ ] **Step 3: Write minimal implementation**

```python
# server/src/sense_server/http/routes/provisioning.py
from aiohttp import web


def add_routes(app):
    app.router.add_get("/health", health)
    app.router.add_get("/provisioning/pubkey", provisioning_pubkey)


async def health(request):
    return web.json_response({"status": "ok"})


async def provisioning_pubkey(request):
    get_pubkey = request.app["sense_get_pubkey"]
    pk = get_pubkey()
    if pk is None:
        return web.json_response({"error": "no_key"}, status=503)
    return web.json_response({"pubkey": pk.hex(), "key_id": "default", "created_at": None})
```

(`health` no longer needs its own authorized-check — the middleware already 401s unauthorized requests before handlers run.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/http/test_provisioning.py tests/http/test_auth.py -v`
Expected: PASS (all). Full suite:
Run: `cd server && python -m pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/src/sense_server/http/routes/provisioning.py server/tests/http/test_provisioning.py
git commit -m "feat(server): GET /provisioning/pubkey route"
```

---

### Task 5: Wire HTTP app + token into run_gateway; update DeviceClient sim

**Files:**
- Modify: `server/scripts/run_gateway.py`
- Modify: `server/src/sense_server/sim/device_client.py`
- Modify: `server/pyproject.toml` (add aiohttp)

**Interfaces:**
- `run_gateway` now: loads token via `load_or_create_token(args.token_file)`, starts the aiohttp app on `--http-port` (default 8766) in the same event loop as the WS `serve(..., token=token)`, and prints the three lines the operator copies to the phone.
- `DeviceClient` gains a `token` param and sends the `Authorization: Bearer` header on its WS connect (so the existing e2e keeps passing once WS auth is on).

- [ ] **Step 1: Add the aiohttp dependency**

Edit `server/pyproject.toml` `[project] dependencies` to add `"aiohttp>=3.10",` after the `websockets` line.

- [ ] **Step 2: Update DeviceClient to send the token**

Open `server/src/sense_server/sim/device_client.py`. Find where it constructs the WS connection (the `websockets.connect(...)` call). Add an `additional_headers` kwarg for the token. The constructor and connect site change to:

```python
class DeviceClient:
    def __init__(self, *, url, server_pubkey, token=None):
        self.url = url
        self.server_pubkey = server_pubkey
        self.token = token
        ...

    async def run(self):
        headers = {}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        async with websockets.connect(self.url, additional_headers=headers) as ws:
            ...
```

(Adjust to the existing variable names in the file; the key change is threading `token` through `__init__` and emitting the header.)

- [ ] **Step 3: Update the existing e2e test to pass the token**

Find the e2e test that starts `serve(...)` + `DeviceClient` (in `server/tests/`, the "real socket + real crypto" test). Pass the same `token` to both `serve(..., token=token)` and `DeviceClient(..., token=token)`. Use `load_or_create_token(tmp_path/"tok")` for the token.

- [ ] **Step 4: Wire run_gateway**

Edit `server/scripts/run_gateway.py`. Add args `--token-file` (default `data/server_token`) and `--http-port` (default `8766`). Replace the `asyncio.run(serve(...))` block (around lines 48-54) with a single event loop running both servers:

```python
import aiohttp.web
from sense_server.auth import load_or_create_token
from sense_server.http.app import build_app
from sense_server.gateway.adapter import serve

token = load_or_create_token(args.token_file)

app = build_app(token=token, get_pubkey=signer.public_key_bytes)

async def main():
    http_runner = aiohttp.web.AppRunner(app)
    await http_runner.setup()
    site = aiohttp.web.TCPSite(http_runner, args.host, args.http_port)
    await site.start()
    print(f"http control API on http://{args.host}:{args.http_port}")
    print(f"gateway listening on ws://{args.host}:{args.port}")
    print(f"bearer token (copy to phone): {token}")
    print(f"server command public key (provision on device): {signer.public_key_bytes.hex()}")
    await serve(factory, host=args.host, port=args.port,
                event_store=store, dispatcher=dispatcher, token=token)

asyncio.run(main())
```

Add the two argparse arguments in the `argparse` block alongside the existing `--host/--port/--db/--key-file`:

```python
p.add_argument("--token-file", default="data/server_token")
p.add_argument("--http-port", type=int, default=8766)
```

- [ ] **Step 5: Run the full suite + smoke the script**

Run: `cd server && python -m pip install -e '.[dev]' -q && python -m pytest -q`
Expected: PASS (including the updated e2e).

Smoke (manual, optional): `cd server && python scripts/run_gateway.py --host 127.0.0.1 --port 8765 --http-port 8766` should print the four lines and stay up; `curl -s http://127.0.0.1:8766/health` → 401, `curl -s -H "Authorization: Bearer <token>" http://127.0.0.1:8766/provisioning/pubkey` → JSON. Kill with Ctrl-C.

- [ ] **Step 6: Commit**

```bash
git add server/pyproject.toml server/scripts/run_gateway.py server/src/sense_server/sim/device_client.py server/tests
git commit -m "feat(server): run_gateway starts HTTP control API + prints bearer token"
```

---

## Phase 2 — Firmware: provisioning core, NVS store, GATT service

### Task 6: Provisioning core state machine (host-tested, no ESP-IDF deps)

**Files:**
- Create: `firmware/sense_sensor/main/provisioning_core.h`
- Create: `firmware/sense_sensor/main/provisioning_core.c`
- Create: `firmware/sense_sensor/test/test_provisioning.c`
- Modify: `firmware/sense_sensor/test/Makefile`

**Interfaces:**
- Produces a pure, host-testable core:
```c
typedef enum { PROV_UNPROVISIONED = 0, PROV_PROVISIONED = 1 } prov_state_t;

typedef struct {
    int (*get)(uint8_t out_key[32], uint8_t *out_provisioned); /* 0 ok, 1 empty */
    int (*set)(const uint8_t key[32]);                          /* 0 ok */
    int (*clear)(void);                                          /* 0 ok */
} prov_store_t;

int  provisioning_core_init(const prov_store_t *store);
prov_state_t provisioning_core_state(void);
int  provisioning_core_apply_key(const uint8_t key[32]); /* 0 accepted, -1 rejected (already provisioned) */
int  provisioning_core_factory_reset(void);
const uint8_t *provisioning_core_server_key(void);       /* valid only when PROVISIONED */
```

- [ ] **Step 1: Write the failing test**

```c
/* firmware/sense_sensor/test/test_provisioning.c */
#include <string.h>
#include <assert.h>
#include "../main/provisioning_core.h"

static uint8_t s_key[32];
static uint8_t s_prov;

static int stub_get(uint8_t out_key[32], uint8_t *out_provisioned) {
    if (!s_prov) return 1;
    memcpy(out_key, s_key, 32);
    *out_provisioned = 1;
    return 0;
}
static int stub_set(const uint8_t key[32]) { memcpy(s_key, key, 32); s_prov = 1; return 0; }
static int stub_clear(void) { memset(s_key, 0, 32); s_prov = 0; return 0; }

static const prov_store_t stub_store = { stub_get, stub_set, stub_clear };

static void reset_store(void) { memset(s_key, 0, 32); s_prov = 0; }

int main(void) {
    /* fresh boot: unprovisioned */
    reset_store();
    assert(provisioning_core_init(&stub_store) == 0);
    assert(provisioning_core_state() == PROV_UNPROVISIONED);

    /* applying a key when unprovisioned succeeds and flips state */
    uint8_t k[32]; for (int i = 0; i < 32; i++) k[i] = (uint8_t)(i + 1);
    assert(provisioning_core_apply_key(k) == 0);
    assert(provisioning_core_state() == PROV_PROVISIONED);
    assert(memcmp(provisioning_core_server_key(), k, 32) == 0);

    /* applying again while provisioned is rejected */
    uint8_t k2[32]; memset(k2, 9, 32);
    assert(provisioning_core_apply_key(k2) == -1);
    assert(provisioning_core_state() == PROV_PROVISIONED);
    assert(memcmp(provisioning_core_server_key(), k, 32) == 0); /* unchanged */

    /* factory reset returns to unprovisioned */
    assert(provisioning_core_factory_reset() == 0);
    assert(provisioning_core_state() == PROV_UNPROVISIONED);

    /* re-provision after reset works */
    assert(provisioning_core_apply_key(k2) == 0);
    assert(provisioning_core_state() == PROV_PROVISIONED);
    assert(memcmp(provisioning_core_server_key(), k2, 32) == 0);

    /* reboot with persisted key boots straight to provisioned */
    reset_store(); memcpy(s_key, k, 32); s_prov = 1;
    assert(provisioning_core_init(&stub_store) == 0);
    assert(provisioning_core_state() == PROV_PROVISIONED);
    assert(memcmp(provisioning_core_server_key(), k, 32) == 0);

    printf("test_provisioning: OK\n");
    return 0;
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd firmware/sense_sensor/test && make provisioning`
Expected: FAIL — no rule `provisioning` yet (and no source files).

- [ ] **Step 3: Write minimal implementation**

```c
/* firmware/sense_sensor/main/provisioning_core.h */
#ifndef PROVISIONING_CORE_H
#define PROVISIONING_CORE_H
#include <stdint.h>

typedef enum { PROV_UNPROVISIONED = 0, PROV_PROVISIONED = 1 } prov_state_t;

typedef struct {
    int (*get)(uint8_t out_key[32], uint8_t *out_provisioned);
    int (*set)(const uint8_t key[32]);
    int (*clear)(void);
} prov_store_t;

int provisioning_core_init(const prov_store_t *store);
prov_state_t provisioning_core_state(void);
int provisioning_core_apply_key(const uint8_t key[32]);
int provisioning_core_factory_reset(void);
const uint8_t *provisioning_core_server_key(void);

#endif
```

```c
/* firmware/sense_sensor/main/provisioning_core.c */
#include "provisioning_core.h"
#include <string.h>

static const prov_store_t *s_store;
static prov_state_t s_state;
static uint8_t s_key[32];

int provisioning_core_init(const prov_store_t *store) {
    s_store = store;
    uint8_t provisioned = 0;
    if (store->get(s_key, &provisioned) == 0 && provisioned) {
        s_state = PROV_PROVISIONED;
    } else {
        s_state = PROV_UNPROVISIONED;
        memset(s_key, 0, sizeof s_key);
    }
    return 0;
}

prov_state_t provisioning_core_state(void) { return s_state; }

int provisioning_core_apply_key(const uint8_t key[32]) {
    if (s_state == PROV_PROVISIONED) return -1;
    if (s_store->set(key) != 0) return -1;       /* persist first */
    memcpy(s_key, key, 32);
    s_state = PROV_PROVISIONED;
    return 0;
}

int provisioning_core_factory_reset(void) {
    if (s_store->clear() != 0) return -1;
    memset(s_key, 0, sizeof s_key);
    s_state = PROV_UNPROVISIONED;
    return 0;
}

const uint8_t *provisioning_core_server_key(void) { return s_key; }
```

Update the Makefile (`firmware/sense_sensor/test/Makefile`):

```make
CC ?= cc
CFLAGS ?= -std=c11 -Wall -Wextra -I../main

test: c6 vad provisioning

c6: /tmp/c6test
	/tmp/c6test

vad: /tmp/vadtest
	/tmp/vadtest

provisioning: /tmp/provtest
	/tmp/provtest

/tmp/c6test: test_c6_packet.c ../main/c6_packet.c ../main/c6_packet.h ../main/config.h
	$(CC) $(CFLAGS) ../main/c6_packet.c test_c6_packet.c -o $@

/tmp/vadtest: test_vad.c ../main/vad.c ../main/vad.h ../main/config.h
	$(CC) $(CFLAGS) ../main/vad.c test_vad.c -o $@

/tmp/provtest: test_provisioning.c ../main/provisioning_core.c ../main/provisioning_core.h
	$(CC) $(CFLAGS) ../main/provisioning_core.c test_provisioning.c -o $@

clean:
	rm -f /tmp/c6test /tmp/vadtest /tmp/provtest

.PHONY: test c6 vad provisioning clean
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd firmware/sense_sensor/test && make provisioning`
Expected: prints `test_provisioning: OK` and exits 0. Then `make test` runs all three (c6, vad, provisioning) green.

- [ ] **Step 5: Commit**

```bash
git add firmware/sense_sensor/main/provisioning_core.c firmware/sense_sensor/main/provisioning_core.h firmware/sense_sensor/test/test_provisioning.c firmware/sense_sensor/test/Makefile
git commit -m "feat(firmware): host-tested provisioning core state machine"
```

---

### Task 7: `commands_set_pubkey` setter

**Files:**
- Modify: `firmware/sense_sensor/main/commands.c` (around line 16-31)
- Modify: `firmware/sense_sensor/main/commands.h` (around line 23-31)

**Interfaces:**
- Produces: `void commands_set_pubkey(const uint8_t server_pubkey[32]);` — updates `s_pubkey` at runtime after a successful provisioning apply, so commands verify against the live key without a reboot.

- [ ] **Step 1: Write the failing test**

Add a tiny host test by extending the existing `test/Makefile` pattern is not feasible (commands.c pulls libsodium/ESP logging). Instead, verify the setter via the firmware build + an assertion-style call from `provisioning` wiring in Task 9. For this task, the "test" is compile-time: add the declaration and a no-op-safe definition, then confirm the firmware still links.

Run: `cd firmware/sense_sensor && idf.py build 2>&1 | tail -5`
Expected: builds clean (no new warnings). (No source change yet → still passes; this establishes the baseline.)

- [ ] **Step 2: Add the setter (minimal implementation)**

In `commands.h`, after the `commands_init` declaration:

```c
/* Update the trusted server public key at runtime (after provisioning). */
void commands_set_pubkey(const uint8_t server_pubkey[32]);
```

In `commands.c`, after `commands_init`:

```c
void commands_set_pubkey(const uint8_t server_pubkey[32]) {
  memcpy(s_pubkey, server_pubkey, sizeof s_pubkey);
}
```

- [ ] **Step 3: Verify it builds**

Run: `cd firmware/sense_sensor && idf.py build 2>&1 | tail -5`
Expected: `Project build complete. ...` with ~625 KB image, no new warnings.

- [ ] **Step 4: Commit**

```bash
git add firmware/sense_sensor/main/commands.c firmware/sense_sensor/main/commands.h
git commit -m "feat(firmware): commands_set_pubkey runtime setter"
```

---

### Task 8: NVS-backed provisioning store + GATT service

**Files:**
- Create: `firmware/sense_sensor/main/provisioning.h`
- Create: `firmware/sense_sensor/main/provisioning.c`
- Modify: `firmware/sense_sensor/main/config.h` (add UUIDs, NVS namespace, reset magic)
- Modify: `firmware/sense_sensor/main/ble_link.c` (register service + characteristics)
- Modify: `firmware/sense_sensor/main/CMakeLists.txt` (add provisioning.c; provisioning_core.c)

**Interfaces:**
- Produces: `int provisioning_init(void);` (ESP-IDF: opens NVS `sense_prov`, wires a static `prov_store_t` with NVS get/set/clear, calls `provisioning_core_init`, and if provisioned calls `commands_set_pubkey`). GATT access callbacks `prov_state_access`, `prov_key_access`, `prov_reset_access` declared in `provisioning.h` for `ble_link.c` to reference.
- `config.h` adds:
```c
#define PROV_SERVICE_UUID      "6e9d0010-b5a3-4f6e-9b1a-7c2d5e8f0a10"
#define PROV_STATE_CHAR_UUID  "6e9d0011-b5a3-4f6e-9b1a-7c2d5e8f0a10"
#define PROV_KEY_CHAR_UUID     "6e9d0012-b5a3-4f6e-9b1a-7c2d5e8f0a10"
#define PROV_RESET_CHAR_UUID  "6e9d0013-b5a3-4f6e-9b1a-7c2d5e8f0a10"
#define PROV_NVS_NAMESPACE    "sense_prov"
#define PROV_NVS_KEY          "srvkey"
#define PROV_NVS_PROVISIONED  "provd"
#define PROV_FACTORY_RESET_MAGIC 0xA5A5A5A5u
```

- [ ] **Step 1: Add config.h constants**

Append the block above to `firmware/sense_sensor/main/config.h` (near the existing UUID defines, ~line 51-54).

- [ ] **Step 2: Write provisioning.h**

```c
/* firmware/sense_sensor/main/provisioning.h */
#ifndef PROVISIONING_H
#define PROVISIONING_H
#include <stdint.h>

int provisioning_init(void);              /* call once on boot, after nvs_flash_init */
uint8_t provisioning_state_byte(void);    /* 0 unprovisioned, 1 provisioned */

/* GATT access callbacks (referenced by ble_link.c) */
int prov_state_access(struct ble_gatt_access_ctxt *ctxt, void *arg);
int prov_key_access(struct ble_gatt_access_ctxt *ctxt, void *arg);
int prov_reset_access(struct ble_gatt_access_ctxt *ctxt, void *arg);

#endif
```

- [ ] **Step 3: Write provisioning.c**

```c
/* firmware/sense_sensor/main/provisioning.c */
#include "provisioning.h"
#include "provisioning_core.h"
#include "commands.h"
#include "config.h"
#include <string.h>
#include "nvs_flash.h"
#include "esp_log.h"
#include "host/ble_hs.h"
#include "host/ble_gatt.h"

static const char *TAG = "provisioning";

static int nvs_get(uint8_t out_key[32], uint8_t *out_provisioned) {
  nvs_handle_t h;
  if (nvs_open(PROV_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return 1;
  uint8_t prov = 0;
  size_t len = 32;
  esp_err_t e = nvs_get_blob(h, PROV_NVS_KEY, out_key, &len);
  if (e != ESP_OK || len != 32) { nvs_close(h); return 1; }
  nvs_get_u8(h, PROV_NVS_PROVISIONED, &prov);
  nvs_close(h);
  *out_provisioned = prov;
  return 0;
}

static int nvs_set(const uint8_t key[32]) {
  nvs_handle_t h;
  if (nvs_open(PROV_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return -1;
  if (nvs_set_blob(h, PROV_NVS_KEY, key, 32) != ESP_OK) { nvs_close(h); return -1; }
  nvs_set_u8(h, PROV_NVS_PROVISIONED, 1);
  nvs_commit(h);
  nvs_close(h);
  return 0;
}

static int nvs_clear(void) {
  nvs_handle_t h;
  if (nvs_open(PROV_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return -1;
  nvs_erase_key(h, PROV_NVS_KEY);
  nvs_set_u8(h, PROV_NVS_PROVISIONED, 0);
  nvs_commit(h);
  nvs_close(h);
  return 0;
}

static const prov_store_t s_store = { nvs_get, nvs_set, nvs_clear };

int provisioning_init(void) {
  if (provisioning_core_init(&s_store) != 0) {
    ESP_LOGE(TAG, "core init failed");
    return -1;
  }
  if (provisioning_core_state() == PROV_PROVISIONED) {
    commands_set_pubkey(provisioning_core_server_key());
    ESP_LOGI(TAG, "booted provisioned");
  } else {
    ESP_LOGI(TAG, "booted unprovisioned");
  }
  return 0;
}

uint8_t provisioning_state_byte(void) {
  return (uint8_t)provisioning_core_state();
}

/* --- GATT access callbacks --- */

/* Use the proven patterns from ble_link.c: command_write_access (mbuf->flat copy,
 * lines 44-64) for writes, and the AUDIO/ACK notify pattern (~line 120) for STATE
 * notify. s_state_handle is wired in ble_link.c's GATT table (val_handle). */
extern uint16_t s_prov_state_handle;  /* defined in ble_link.c */

static int notify_state(void) {
  uint8_t v = provisioning_state_byte();
  /* Mirror ble_link.c's notify call (e.g. ble_gatts_notify or nimble's
   * ble_gatts_chr_updated + ble_gap_notify). Use exactly what ble_link.c uses. */
  return 0;  /* placeholder return; see implementer note below */
}

int prov_state_access(struct ble_gatt_access_ctxt *ctxt, void *arg) {
  (void)arg;
  if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
    uint8_t v = provisioning_state_byte();
    os_mbuf_append(ctxt->om, &v, 1);  /* matches ble_link.c chr read style */
    return 0;
  }
  return 0;  /* writes not supported on STATE */
}

int prov_key_access(struct ble_gatt_access_ctxt *ctxt, void *arg) {
  (void)arg;
  if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return 0;
  uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
  if (len != 32) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
  uint8_t key[32];
  if (ble_hs_mbuf_to_flat(ctxt->om, key, sizeof key, NULL) != 0) return BLE_ATT_ERR_UNLIKELY;
  if (provisioning_core_apply_key(key) != 0) {
    ESP_LOGW(TAG, "key write rejected (already provisioned)");
    return BLE_ATT_ERR_INSUFFICIENT_AUTH;
  }
  commands_set_pubkey(provisioning_core_server_key());
  notify_state();
  return 0;
}

int prov_reset_access(struct ble_gatt_access_ctxt *ctxt, void *arg) {
  (void)arg;
  if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return 0;
  uint32_t magic = 0;
  uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
  if (len != sizeof magic) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
  ble_hs_mbuf_to_flat(ctxt->om, &magic, sizeof magic, NULL);
  if (magic != PROV_FACTORY_RESET_MAGIC) return BLE_ATT_ERR_UNLIKELY;
  provisioning_core_factory_reset();
  uint8_t zero[32] = {0};
  commands_set_pubkey(zero);
  notify_state();
  return 0;
}
```

**Note for the implementer:** the exact NimBLE mbuf/notify helpers (`ble_hs_mbuf_to_flat`, `OS_MBUF_PKTLEN`, `os_mbuf_append`, and the notify routine) and include paths vary by IDF v6.0.1's bundled NimBLE. Mirror what `ble_link.c` already uses: copy `command_write_access`'s mbuf→flat pattern (lines 44-64) for the write handlers, and copy the AUDIO/ACK notify pattern (~line 120) into `notify_state()` (referencing the `s_prov_state_handle` that `ble_link.c` sets via `.val_handle`). If a symbol name differs from what's written above, use the name `ble_link.c` uses — keep it consistent. The `extern uint16_t s_prov_state_handle;` resolves to the `static uint16_t s_prov_state_handle;` you add in `ble_link.c` (Task 8 Step 4); if making it `static` there breaks linkage, instead define the handle as a non-static global in `ble_link.c` and declare it `extern` here.

- [ ] **Step 4: Register the provisioning service in ble_link.c**

In `firmware/sense_sensor/main/ble_link.c`:

Add UUIDs near the existing ones (~line 22-25):
```c
static const ble_uuid128_t s_prov_svc_uuid   = SENSE_UUID128(0x10);
static const ble_uuid128_t s_prov_state_uuid  = SENSE_UUID128(0x11);
static const ble_uuid128_t s_prov_key_uuid    = SENSE_UUID128(0x12);
static const ble_uuid128_t s_prov_reset_uuid  = SENSE_UUID128(0x13);
```
(`SENSE_UUID128(n)` sets byte 12 to `n` — confirm against the macro definition; if it uses a different byte, keep `0x10..0x13` consistent with how `0x01..0x04` map to `6e9d0001..0004`.)

Add `#include "provisioning.h"` at the top.

Add a second service entry in `s_gatt_svcs[]` (after the audio service's `{0}` terminator entry, before the final `{0}`):
```c
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &s_prov_svc_uuid.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = &s_prov_state_uuid.u,
                .access_cb = prov_state_access,
                .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &s_prov_state_handle,
            },
            {
                .uuid = &s_prov_key_uuid.u,
                .access_cb = prov_key_access,
                .flags = BLE_GATT_CHR_F_WRITE,
            },
            {
                .uuid = &s_prov_reset_uuid.u,
                .access_cb = prov_reset_access,
                .flags = BLE_GATT_CHR_F_WRITE,
            },
            {0},
        },
    },
```
Add `uint16_t s_prov_state_handle;` (non-`static` — `provisioning.c` references it as `extern` for its notify callback) near the existing `s_audio_handle`/`s_ack_handle`.

- [ ] **Step 5: Add to CMakeLists.txt**

In `firmware/sense_sensor/main/CMakeLists.txt`, add to the source list:
```cmake
"provisioning.c"
"provisioning_core.c"
```

- [ ] **Step 6: Build + host-test**

Run: `cd firmware/sense_sensor/test && make test`
Expected: c6, vad, provisioning all green (core unchanged).

Run: `cd firmware/sense_sensor && idf.py build 2>&1 | tail -8`
Expected: `Project build complete.` If a NimBLE helper name is wrong, the error will name it — fix by mirroring `ble_link.c`'s existing usage, then rebuild.

- [ ] **Step 7: Commit**

```bash
git add firmware/sense_sensor/main/provisioning.c firmware/sense_sensor/main/provisioning.h firmware/sense_sensor/main/config.h firmware/sense_sensor/main/ble_link.c firmware/sense_sensor/main/CMakeLists.txt
git commit -m "feat(firmware): NVS-backed provisioning + provisioning GATT service"
```

---

### Task 9: Wire provisioning_init into app_main

**Files:**
- Modify: `firmware/sense_sensor/main/sense_sensor.c` (lines ~63-89)

**Interfaces:**
- After `nvs_flash_init` (line ~74) and before `commands_init` (line ~80): call `provisioning_init()` which loads the key from NVS (if present) and calls `commands_set_pubkey` itself. Then change `commands_init(SERVER_ED25519_PUBKEY, ...)` to seed with the all-zero fallback (it will be overwritten by `commands_set_pubkey` if provisioned).

- [ ] **Step 1: Edit app_main**

Add `#include "provisioning.h"` at the top. Replace the `commands_init` block (line ~78-81) with:

```c
  if (provisioning_init() != 0) {
    ESP_LOGE(TAG, "provisioning_init failed");
  }
  /* commands_init seeds the verifier; if provisioned, provisioning_init already
   * installed the real key via commands_set_pubkey. The fallback is all-zeros. */
  if (commands_init(SERVER_ED25519_PUBKEY, ble_link_notify_ack) != 0) {
    ESP_LOGE(TAG, "commands_init failed");
  }
  if (provisioning_state_byte() == 1) {
    commands_set_pubkey(provisioning_core_server_key());  /* belt-and-suspenders: ensure live key */
  }
```
(Requires `#include "provisioning_core.h"` too, for `provisioning_core_server_key`.)

- [ ] **Step 2: Build**

Run: `cd firmware/sense_sensor && idf.py build 2>&1 | tail -8`
Expected: `Project build complete.` image ~625 KB.

- [ ] **Step 3: Commit**

```bash
git add firmware/sense_sensor/main/sense_sensor.c
git commit -m "feat(firmware): wire provisioning_init into app_main boot"
```

---

## Phase 3 — Android: Compose setup wizard, provisioning client, relay auth

### Task 10: Gradle dependencies for Compose + DataStore + ViewModel

**Files:**
- Modify: `android/sense-relay/app/build.gradle.kts`

- [ ] **Step 1: Enable Compose + add deps**

Add the Compose compiler plugin and BOM. In the `plugins {}` block add:
```kotlin
id("org.jetbrains.kotlin.plugin.compose") version "1.9.0"
```
In the `android {}` block add:
```kotlin
buildFeatures { compose = true }
```
In `dependencies {}` add:
```kotlin
val composeBom = platform("androidx.compose:compose-bom:2024.10.02")
implementation(composeBom)
implementation("androidx.compose.ui:ui")
implementation("androidx.compose.ui:ui-tooling-preview")
implementation("androidx.compose.material3:material3")
implementation("androidx.activity:activity-compose:1.9.3")
implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
implementation("androidx.datastore:datastore-preferences:1.1.1")
debugImplementation("androidx.compose.ui:ui-tooling")
```

- [ ] **Step 2: Verify it builds (bench step)**

Run: `cd android/sense-relay && ./gradlew :app:assembleDebug` (requires Android SDK — bench-side).
Expected: `BUILD SUCCESSFUL`. JVM test target still works:
Run: `cd android/sense-relay && ./gradlew test`
Expected: `RelaySessionTest` passes.

- [ ] **Step 3: Commit**

```bash
git add android/sense-relay/app/build.gradle.kts android/sense-relay/build.gradle.kts android/sense-relay/settings.gradle.kts
git commit -m "build(android): add Compose, DataStore, viewmodel-compose deps"
```

---

### Task 11: ServerConfig (DataStore persistence)

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/store/ServerConfig.kt`
- Test: `android/sense-relay/app/src/test/kotlin/com/sense/relay/ServerConfigTest.kt`

**Interfaces:**
- Produces `class ServerConfig(context: Context)` with `suspend fun read(): Config`, `suspend fun write(c: Config)`, `suspend fun clear()`, where:
```kotlin
data class Config(val serverUrl: String, val token: String, val deviceAddress: String?, val provisioned: Boolean)
```

- [ ] **Step 1: Write the failing test**

```kotlin
// android/sense-relay/app/src/test/kotlin/com/sense/relay/ServerConfigTest.kt
package com.sense.relay
import com.sense.relay.store.ServerConfig
import com.sense.relay.store.Config
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class ServerConfigTest {
    @Test
    fun roundTripsConfig() = runTest {
        val tmp = File(System.getProperty("java.io.tmpdir"), "sc_${System.nanoTime()}")
        val cfg = ServerConfig(tmp)  // ctor takes a File dir for testability
        cfg.write(Config("wss://x:8765", "tok", "AA:BB", true))
        val read = cfg.read()
        assertEquals("wss://x:8765", read.serverUrl)
        assertEquals("tok", read.token)
        assertEquals("AA:BB", read.deviceAddress)
        assertTrue(read.provisioned)
        cfg.clear()
        assertEquals("", cfg.read().serverUrl)
    }
}
```
(Design `ServerConfig` to take a `File` dir for the DataStore so it's JVM-testable without a `Context`. The Android-side wrapper passes `context.filesDir`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd android/sense-relay && ./gradlew test` (bench) — or note: expected FAIL (class missing).

- [ ] **Step 3: Write minimal implementation**

```kotlin
// android/sense-relay/app/src/main/kotlin/com/sense/relay/store/ServerConfig.kt
package com.sense.relay.store
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import androidx.datastore.preferences.core.*
import androidx.datastore.preferences.core.edit
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.emptyPreferences
import androidx.datastore.preferences.preferencesDataStore
import java.io.File

data class Config(
    val serverUrl: String = "",
    val token: String = "",
    val deviceAddress: String? = null,
    val provisioned: Boolean = false,
)

private val KEY_URL = stringPreferencesKey("url")
private val KEY_TOKEN = stringPreferencesKey("token")
private val KEY_DEV = stringPreferencesKey("device")
private val KEY_PROV = booleanPreferencesKey("prov")

class ServerConfig(dir: File) {
    private val Context.ds: DataStore<Preferences> by preferencesDataStore("sense_config")
    // For JVM testability we construct the DataStore directly from a file.
    private val store: DataStore<Preferences> =
        androidx.datastore.preferences.core.preferencesDataStoreFile(dir, "sense_config")
            .let { androidx.datastore.core.DataStoreFactory.create({ it }) } as DataStore<Preferences>
    // NOTE: the simplest portable approach — use a singleton-by-dir DataStore:
    init {}
    suspend fun read(): Config = store.data.map { p ->
        Config(
            serverUrl = p[KEY_URL] ?: "",
            token = p[KEY_TOKEN] ?: "",
            deviceAddress = p[KEY_DEV],
            provisioned = p[KEY_PROV] ?: false,
        )
    }.first()
    suspend fun write(c: Config) { store.edit { p ->
        p[KEY_URL] = c.serverUrl
        p[KEY_TOKEN] = c.token
        c.deviceAddress?.let { p[KEY_DEV] = it } ?: run { p.remove(KEY_DEV) }
        p[KEY_PROV] = c.provisioned
    }}
    suspend fun clear() { store.edit { it.clear() } }
}
```
**Implementer note:** the inline DataStoreFactory call above is schematic; the clean portable form is a top-level `Context.dataStore by preferencesDataStore("sense_config")` for the Android path, and a `DataStoreFactory.create(producer = { preferencesDataStoreFile(dir, "sense_config") })` for the JVM-test path. Implement ONE `ServerConfig` that takes `File`, using `DataStoreFactory.create` with `preferencesDataStoreFile(dir, name)`. Remove the dead `Context.ds` line before building.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd android/sense-relay && ./gradlew test`
Expected: `ServerConfigTest` + `RelaySessionTest` PASS.

- [ ] **Step 5: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/store/ServerConfig.kt android/sense-relay/app/src/test/kotlin/com/sense/relay/ServerConfigTest.kt
git commit -m "feat(android): ServerConfig DataStore persistence"
```

---

### Task 12: SenseHttpClient (bearer + TLS pinning)

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/http/SenseHttpClient.kt`

**Interfaces:**
- Produces:
```kotlin
class SenseHttpClient(
    private val baseUrl: String,
    private val token: String,
    caPem: String? = null,   // null = use system trust store (for Let's Encrypt certs)
)
suspend fun health(): Boolean                 // GET /health
suspend fun serverPubkey(): ByteArray          // GET /provisioning/pubkey -> 32 bytes
```
Throws on 401 / non-2xx.

- [ ] **Step 1: Write implementation (no JVM test — uses OkHttp/Android; verified at build + bench)**

```kotlin
// android/sense-relay/app/src/main/kotlin/com/sense/relay/http/SenseHttpClient.kt
package com.sense.relay.http
import okhttp3.OkHttpClient
import okhttp3.Request
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class SenseHttpClient(
    private val baseUrl: String,
    private val token: String,
    caPem: String? = null,
) {
    private val client: OkHttpClient = buildClient(caPem)

    private fun buildClient(caPem: String?): OkHttpClient {
        val builder = OkHttpClient.Builder()
        if (caPem != null) {
            val cf = javax.net.ssl.SSLContext.getDefault().socketFactory
            // Pin a single CA: parse the PEM into an X509Certificate and build a
            // TrustManager that trusts only that CA. (See implementer note.)
            builder.sslSocketFactory(buildPinnedSslContext(caPem).socketFactory,
                                     pinnedTrustManager(caPem))
        }
        return builder.build()
    }

    private fun req(path: String) = Request.Builder()
        .url(baseUrl.trimEnd('/') + path)
        .addHeader("Authorization", "Bearer $token")
        .build()

    suspend fun health(): Boolean = withContext(Dispatchers.IO) {
        client.newCall(req("/health")).execute().use { it.code in 200..299 }
    }

    suspend fun serverPubkey(): ByteArray = withContext(Dispatchers.IO) {
        client.newCall(req("/provisioning/pubkey")).execute().use { resp ->
            if (resp.code == 401) throw SecurityException("unauthorized")
            if (resp.code !in 200..299) throw IOException("pubkey http ${resp.code}")
            val body = resp.body?.string().orEmpty()
            // body is {"pubkey":"<hex>",...}; parse hex. No JSON dep yet — minimal parse:
            val hex = Regex("\"pubkey\"\\s*:\\s*\"([0-9a-fA-F]+)\"").find(body)?.groupValues?.get(1)
                ?: throw IOException("no pubkey in response")
            hex.chunked(2).map { it.toInt(16).toByte() }.toByteArray().also {
                require(it.size == 32) { "pubkey not 32 bytes" }
            }
        }
    }
}
```
**Implementer note:** `buildPinnedSslContext` / `pinnedTrustManager` parse `caPem` (a PEM string) into an `X509Certificate` via a `CertificateFactory.getInstance("X.509")`, then return an `X509TrustManager` whose `checkServerTrusted` accepts chains anchoring on that cert, and an `SSLContext` initialised with it. This is standard ~20-line boilerplate; keep it in this file. When `caPem == null`, leave the default trust manager (Let's Encrypt etc.). Never implement a no-op trust-all.

- [ ] **Step 2: Build**

Run: `cd android/sense-relay && ./gradlew :app:assembleDebug`
Expected: `BUILD SUCCESSFUL`.

- [ ] **Step 3: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/http/SenseHttpClient.kt
git commit -m "feat(android): SenseHttpClient with bearer auth + CA pinning"
```

---

### Task 13: ProvisioningClient (BLE byte framing, host-tested)

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/setup/ProvisioningClient.kt`
- Test: `android/sense-relay/app/src/test/kotlin/com/sense/relay/ProvisioningClientTest.kt`

**Interfaces:**
- Produces pure byte-framing helpers (the BLE-call layer is a thin interface `BleProvisioning`, implemented by SensorLink on-device; tests use a fake):
```kotlin
interface BleProvisioning {
    suspend fun readState(): Int                  // 0 or 1
    suspend fun writeServerKey(key: ByteArray)    // throws if rejected
    suspend fun factoryReset()
}
class ProvisioningClient(private val ble: BleProvisioning) {
    suspend fun ensureProvisioned(serverPubkey: ByteArray): Int  // returns final state (1)
}
```
- Byte framing constants (tested):
```kotlin
object ProvFrames {
    const val STATE_UNPROVISIONED = 0
    const val STATE_PROVISIONED = 1
    val FACTORY_RESET_MAGIC: ByteArray = byteArrayOf(0xA5.toByte(),0xA5,0xA5,0xA5)  // little-endian u32 0xA5A5A5A5
    const val KEY_LEN = 32
}
```

- [ ] **Step 1: Write the failing test**

```kotlin
// android/sense-relay/app/src/test/kotlin/com/sense/relay/ProvisioningClientTest.kt
package com.sense.relay
import com.sense.relay.setup.BleProvisioning
import com.sense.relay.setup.ProvisioningClient
import com.sense.relay.setup.ProvFrames
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ProvisioningClientTest {
    private class FakeBle(var state: Int = 0) : BleProvisioning {
        var writtenKey: ByteArray? = null
        var resetCount = 0
        override suspend fun readState(): Int = state
        override suspend fun writeServerKey(key: ByteArray) {
            require(key.size == 32)
            require(state == 0) { "rejected: already provisioned" }
            writtenKey = key; state = 1
        }
        override suspend fun factoryReset() { resetCount++; state = 0 }
    }

    @Test fun magicIsFourA5s() {
        assertArrayEquals(byteArrayOf(0xA5.toByte(),0xA5,0xA5,0xA5), ProvFrames.FACTORY_RESET_MAGIC)
    }

    @Test fun provisionsWhenUnprovisioned() = runTest {
        val ble = FakeBle(0)
        val key = ByteArray(32) { (it + 1).toByte() }
        val final = ProvisioningClient(ble).ensureProvisioned(key)
        assertEquals(1, final)
        assertArrayEquals(key, ble.writtenKey)
    }

    @Test fun noopWhenAlreadyProvisioned() = runTest {
        val ble = FakeBle(1)
        val final = ProvisioningClient(ble).ensureProvisioned(ByteArray(32))
        assertEquals(1, final)
        assertEquals(null, ble.writtenKey)  // did not re-write
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd android/sense-relay && ./gradlew test`
Expected: FAIL — classes missing.

- [ ] **Step 3: Write minimal implementation**

```kotlin
// android/sense-relay/app/src/main/kotlin/com/sense/relay/setup/ProvisioningClient.kt
package com.sense.relay.setup
import kotlinx.coroutines.test.runTest

object ProvFrames {
    const val STATE_UNPROVISIONED = 0
    const val STATE_PROVISIONED = 1
    val FACTORY_RESET_MAGIC: ByteArray = byteArrayOf(0xA5.toByte(), 0xA5, 0xA5, 0xA5)
    const val KEY_LEN = 32
}

interface BleProvisioning {
    suspend fun readState(): Int
    suspend fun writeServerKey(key: ByteArray)
    suspend fun factoryReset()
}

class ProvisioningClient(private val ble: BleProvisioning) {
    suspend fun ensureProvisioned(serverPubkey: ByteArray): Int {
        require(serverPubkey.size == ProvFrames.KEY_LEN) { "server pubkey must be 32 bytes" }
        val s = ble.readState()
        if (s == ProvFrames.STATE_UNPROVISIONED) {
            ble.writeServerKey(serverPubkey)
        }
        return ble.readState()
    }
}
```
(Remove the stray `import kotlinx.coroutines.test.runTest` in the main file before building — it's only for tests.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd android/sense-relay && ./gradlew test`
Expected: PASS (ProvisioningClientTest + ServerConfigTest + RelaySessionTest).

- [ ] **Step 5: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/setup/ProvisioningClient.kt android/sense-relay/app/src/test/kotlin/com/sense/relay/ProvisioningClientTest.kt
git commit -m "feat(android): ProvisioningClient byte framing + state flow (host-tested)"
```

---

### Task 14: SetupViewModel state machine (host-tested)

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/setup/SetupViewModel.kt`
- Test: `android/sense-relay/app/src/test/kotlin/com/sense/relay/SetupViewModelTest.kt`

**Interfaces:**
- A pure-ish state machine behind interfaces (`ServerApi`, `BleProvisioning`, `DeviceScanner`) so the VM is JVM-testable:
```kotlin
sealed interface SetupStep {
    data class EnterServer(val url: String = "", val token: String = "", val error: String? = null) : SetupStep
    data class Connecting(val msg: String) : SetupStep
    data class Scanning(val devices: List<String> = emptyList(), val error: String? = null) : SetupStep
    data class Provisioning(val msg: String) : SetupStep
    data class Done(val relayRunning: Boolean) : SetupStep
}
interface ServerApi { suspend fun health(urlBase: String, token: String): Boolean; suspend fun pubkey(urlBase: String, token: String): ByteArray }
interface DeviceScanner { suspend fun scan(): List<String>; suspend fun connect(address: String): BleProvisioning }
class SetupViewModel(
    private val serverApi: ServerApi,
    private val scanner: DeviceScanner,
    private val onDone: suspend (Config) -> Unit,   // persists config + starts relay
)
```
`step` is exposed as a `StateFlow<SetupStep>` (see implementation) so Compose can `collectAsState()`; tests read `.step.value`.

- [ ] **Step 1: Write the failing test** (the executable spec — drive the happy path + one error)

```kotlin
// android/sense-relay/app/src/test/kotlin/com/sense/relay/SetupViewModelTest.kt
package com.sense.relay
import com.sense.relay.setup.*
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.flow.first
import org.junit.Assert.*
import org.junit.Test

class SetupViewModelTest {
    private class FakeServer(val ok: Boolean = true) : ServerApi {
        override suspend fun health(urlBase: String, token: String) = ok
        override suspend fun pubkey(urlBase: String, token: String) =
            if (ok) ByteArray(32) { (it + 1).toByte() } else throw SecurityException("401")
    }
    private class FakeScanner(val addrs: List<String> = listOf("AA")) : DeviceScanner {
        var connected: String? = null
        override suspend fun scan() = addrs
        override suspend fun connect(address: String): BleProvisioning {
            connected = address
            return object : BleProvisioning {
                var st = 0
                override suspend fun readState() = st
                override suspend fun writeServerKey(key: ByteArray) { st = 1 }
                override suspend fun factoryReset() { st = 0 }
            }
        }
    }

    @Test fun happyPathGoesEnterServerToDone() = runTest {
        var saved: Config? = null
        val vm = SetupViewModel(FakeServer(), FakeScanner()) { saved = it }
        assertEquals(SetupStep.EnterServer::class, vm.step.value::class)
        vm.submitServer("wss://x:8766", "tok")
        assertTrue(vm.step.value is SetupStep.Done)
        assertNotNull(saved)
        assertEquals("wss://x:8766", saved!!.serverUrl)
        assertTrue(saved!!.provisioned)
    }

    @Test fun badTokenStaysOnEnterServerWithError() = runTest {
        val vm = SetupViewModel(FakeServer(ok = false), FakeScanner()) {}
        vm.submitServer("wss://x:8766", "wrong")
        val step = vm.step.value as SetupStep.EnterServer
        assertNotNull(step.error)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd android/sense-relay && ./gradlew test`
Expected: FAIL — `SetupViewModel` missing.

- [ ] **Step 3: Write minimal implementation**

```kotlin
// android/sense-relay/app/src/main/kotlin/com/sense/relay/setup/SetupViewModel.kt
package com.sense.relay.setup
import com.sense.relay.store.Config
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.runBlocking

sealed interface SetupStep {
    data class EnterServer(val url: String = "", val token: String = "", val error: String? = null) : SetupStep
    data class Connecting(val msg: String) : SetupStep
    data class Scanning(val devices: List<String> = emptyList(), val error: String? = null) : SetupStep
    data class Provisioning(val msg: String) : SetupStep
    data class Done(val relayRunning: Boolean) : SetupStep
}

interface ServerApi {
    suspend fun health(urlBase: String, token: String): Boolean
    suspend fun pubkey(urlBase: String, token: String): ByteArray
}
interface DeviceScanner {
    suspend fun scan(): List<String>
    suspend fun connect(address: String): BleProvisioning
}

class SetupViewModel(
    private val serverApi: ServerApi,
    private val scanner: DeviceScanner,
    private val onDone: suspend (Config) -> Unit,
) {
    private val _step = MutableStateFlow<SetupStep>(SetupStep.EnterServer())
    val step: StateFlow<SetupStep> = _step.asStateFlow()

    fun submitServer(url: String, token: String) {
        // Synchronous test harness expects blocking; real VM uses viewModelScope. See note.
        runBlocking {
            _step.value = SetupStep.Connecting("testing connection")
            try {
                if (!serverApi.health(url, token)) {
                    step.value = SetupStep.EnterServer(url, token, "server unreachable"); return@runBlocking
                }
                val pubkey = serverApi.pubkey(url, token)
                step.value = SetupStep.Scanning()
                val devices = scanner.scan()
                if (devices.isEmpty()) {
                    step.value = SetupStep.Scanning(emptyList(), "no Sense device found"); return@runBlocking
                }
                val ble = scanner.connect(devices.first())
                step.value = SetupStep.Provisioning("provisioning device")
                ProvisioningClient(ble).ensureProvisioned(pubkey)
                onDone(Config(url, token, devices.first(), true))
                step.value = SetupStep.Done(true)
            } catch (e: SecurityException) {
                step.value = SetupStep.EnterServer(url, token, "bad token or unauthorized")
            } catch (e: Exception) {
                step.value = SetupStep.EnterServer(url, token, e.message ?: "error")
            }
        }
    }
}
```
**Implementer note:** `step` is a `StateFlow` so Compose can observe it; tests read `step.value` synchronously. `runBlocking` inside `submitServer` makes the synchronous JUnit harness work. For the real `SetupActivity`, call `submitServer` from `lifecycleScope.launch` (it's safe — `runBlocking` blocks that launched coroutine only) and collect `vm.step` via `collectAsState()`. If you'd rather not nest `runBlocking` in a real coroutine, refactor `submitServer` to a `suspend fun` and have both the Activity (`lifecycleScope.launch { vm.submitServer(...) }`) and the tests (`runTest { vm.submitServer(...) }`) drive it — the logic body is identical either way.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd android/sense-relay && ./gradlew test`
Expected: PASS (SetupViewModelTest + ProvisioningClientTest + ServerConfigTest + RelaySessionTest).

- [ ] **Step 5: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/setup/SetupViewModel.kt android/sense-relay/app/src/test/kotlin/com/sense/relay/SetupViewModelTest.kt
git commit -m "feat(android): SetupViewModel wizard state machine (host-tested)"
```

---

### Task 15: Compose UI — theme, setup screen, status screen, launcher Activity

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/Theme.kt`
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/SetupScreen.kt`
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/SetupActivity.kt`
- Modify: `android/sense-relay/app/src/main/AndroidManifest.xml`

**Interfaces:**
- `SetupActivity : ComponentActivity` — the launcher; hosts the wizard `SetupScreen` then the `StatusScreen`. Wires real `ServerApi` (SenseHttpClient), real `DeviceScanner` (SensorLink-backed), and the `onDone` that writes `ServerConfig` + starts `RelayService`.

- [ ] **Step 1: Monochrome theme**

```kotlin
// android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/Theme.kt
package com.sense.relay.ui
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.foundation.isSystemInDarkTheme

private val LightColors = lightColorScheme()   // default = monochrome neutrals
private val DarkColors = darkColorScheme()

@Composable
fun SenseTheme(content: @Composable () -> Unit) {
    val colors = if (isSystemInDarkTheme()) DarkColors else LightColors
    MaterialTheme(colorScheme = colors, content = content)
}
```

- [ ] **Step 2: Setup + Status composables**

```kotlin
// android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/SetupScreen.kt
package com.sense.relay.ui
import androidx.compose.animation.AnimatedContent
import androidx.compose.animation.togetherWith
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.sense.relay.setup.SetupStep

@Composable
fun SetupScreen(step: SetupStep, onServer: (String, String) -> Unit) {
    Surface(Modifier.fillMaxSize()) {
        Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Text("Set up your Sense", style = MaterialTheme.typography.headlineMedium)
            AnimatedContent(step, transitionSpec = { togetherWith() }) { s ->
                when (s) {
                    is SetupStep.EnterServer -> ServerForm(s, onServer)
                    is SetupStep.Connecting -> StatusText(s.msg)
                    is SetupStep.Scanning -> ScanView(s)
                    is SetupStep.Provisioning -> StatusText(s.msg)
                    is SetupStep.Done -> StatusText("Relay running")
                }
            }
        }
    }
}

@Composable private fun ServerForm(s: SetupStep.EnterServer, onServer: (String, String) -> Unit) {
    var url by remember { mutableStateOf(s.url.ifEmpty { "https://" }) }
    var token by remember { mutableStateOf(s.token) }
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        OutlinedTextField(url, { url = it }, label = { Text("Server URL") }, singleLine = true)
        OutlinedTextField(token, { token = it }, label = { Text("Bearer token") }, singleLine = true)
        s.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        Button({ onServer(url.trim(), token.trim()) }) { Text("Connect") }
    }
}

@Composable private fun ScanView(s: SetupStep.Scanning) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (s.devices.isNotEmpty()) Text("Found device: ${s.devices.first()}") else Text("Scanning…")
        s.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
    }
}

@Composable private fun StatusText(msg: String) { Text(msg) }
```

- [ ] **Step 3: SetupActivity wiring**

```kotlin
// android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/SetupActivity.kt
package com.sense.relay.ui
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.runtime.collectAsState
import androidx.lifecycle.lifecycleScope
import com.sense.relay.RelayService
import com.sense.relay.setup.SetupViewModel
import com.sense.relay.store.Config
import com.sense.relay.store.ServerConfig
import kotlinx.coroutines.launch

class SetupActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val vm = SetupViewModel(
            serverApi = RealServerApi,                // see Task 12 SenseHttpClient adapter
            scanner = RealDeviceScanner(this),        // SensorLink-backed; defined here
            onDone = { c ->
                ServerConfig(filesDir).write(c)
                val i = Intent(this, RelayService::class.java)
                    .putExtra("server_url", c.serverUrl.replaceFirst("https", "wss"))
                    .putExtra("token", c.token)
                startForegroundService(i)
            },
        )
        setContent {
            SenseTheme {
                val step by vm.step.collectAsState()
                SetupScreen(step) { u, t -> lifecycleScope.launch { vm.submitServer(u, t) } }
            }
        }
    }
}
```
`RealServerApi` is a thin adapter over `SenseHttpClient`:

```kotlin
object RealServerApi : ServerApi {
    override suspend fun health(urlBase: String, token: String): Boolean =
        SenseHttpClient(urlBase, token).health()
    override suspend fun pubkey(urlBase: String, token: String): ByteArray =
        SenseHttpClient(urlBase, token).serverPubkey()
}
```

`RealDeviceScanner(context)` wraps `SensorLink`'s scan+connect and returns a `BleProvisioning` that reads the STATE characteristic and writes SERVER_KEY/FACTORY_RESET via the new provisioning service UUIDs. Implement it as a class in `SetupActivity.kt` (or a `RealBindings.kt`); it reuses `SensorLink`'s existing scan/GATT-queue plumbing and adds reads/writes on the `6e9d0011/0012/0013` characteristics. The on-device BLE ops are a **bench-side validation gap** per the spec — the emulator has no BLE radio, so this class is built and type-checked here but exercised only on real hardware.

- [ ] **Step 4: Manifest — make SetupActivity the launcher**

Edit `android/sense-relay/app/src/main/AndroidManifest.xml`. Add inside `<application>` (before the `<service>` entry):

```xml
<activity
    android:name=".ui.SetupActivity"
    android:exported="true">
    <intent-filter>
        <action android:name="android.intent.action.MAIN" />
        <category android:name="android.intent.category.LAUNCHER" />
    </intent-filter>
</activity>
```

Add to the `<manifest>` permissions block:
```xml
<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
```

- [ ] **Step 5: Build + (bench) instrumented smoke**

Run: `cd android/sense-relay && ./gradlew :app:assembleDebug`
Expected: `BUILD SUCCESSFUL`. JVM tests:
Run: `cd android/sense-relay && ./gradlew test`
Expected: all green. The existing `RelayServiceStartTest` instrumentation now starts from a launcher Activity; keep it targeting `RelayService` directly (it already does) — no change needed.

- [ ] **Step 6: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui android/sense-relay/app/src/main/AndroidManifest.xml
git commit -m "feat(android): Compose setup wizard + launcher SetupActivity"
```

---

### Task 16: Relay auth — token on WS + wss

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/net/ServerSocket.kt`
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/RelayService.kt`

- [ ] **Step 1: Add token to ServerSocket**

Change the constructor to take `token: String` and add the header on the request (current `Request.Builder().url(url).build()` at ~line 33-46):

```kotlin
class ServerSocket(private val url: String, private val token: String, private val listener: Listener) {
    ...
    fun connect() {
        socket = client.newWebSocket(
            Request.Builder().url(url)
                .addHeader("Authorization", "Bearer $token")
                .build(),
            object : WebSocketListener() { /* unchanged */ },
        )
    }
}
```

- [ ] **Step 2: Pass token + wss from RelayService**

In `RelayService.kt` (~line 32-49): read a `token` extra (fall back to `ServerConfig(filesDir).read().token` via a quick blocking read on first start), and when constructing `ServerSocket`, pass `token`. Convert `https://` server URLs to `wss://` (and `http://`→`ws://`) for the socket:

```kotlin
private var serverUrl: String = "ws://10.0.2.2:8765"
private var token: String = ""

override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    intent?.getStringExtra("server_url")?.let { serverUrl = it }
    intent?.getStringExtra("token")?.let { token = it }
    ...
}

override fun onConnected() {
    val wsUrl = serverUrl.replaceFirst(Regex("^https?://"), if (serverUrl.startsWith("https")) "wss://" else "ws://")
    Log.i(TAG, "device connected; opening socket to $wsUrl")
    socket = ServerSocket(wsUrl, token, socketListener).also { it.connect() }
}
```

- [ ] **Step 3: Build + JVM tests**

Run: `cd android/sense-relay && ./gradlew :app:assembleDebug && ./gradlew test`
Expected: `BUILD SUCCESSFUL`, JVM tests green.

- [ ] **Step 4: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/net/ServerSocket.kt android/sense-relay/app/src/main/kotlin/com/sense/relay/RelayService.kt
git commit -m "feat(android): send bearer token on WS, use wss for remote server"
```

---

## Verification (whole slice)

- Server: `cd server && python -m pytest -q` → all green; `python scripts/run_gateway.py --host 127.0.0.1` prints the 4 lines and serves HTTP + WS.
- Firmware: `cd firmware/sense_sensor/test && make test` (c6, vad, provisioning) + `idf.py build` clean ~625 KB.
- Android: `./gradlew test` (RelaySession, ProvisioningClient, ServerConfig, SetupViewModel) green; `./gradlew :app:assembleDebug` builds.

## Bench-side gaps (require real hardware / SDK — not automatable here)

- On-device validation of the new firmware provisioning GATT characteristics (STATE/SERVER_KEY/FACTORY_RESET) with a real esp32s3 + the app.
- On-device BLE provisioning flow in the app (`RealDeviceScanner` + `SensorLink` provisioning ops) — emulator has no BLE radio.
- TLS: operator obtains a cert (Let's Encrypt) or generates a self-signed CA and pins it in the app. No "trust all."
- Gradle/instrumented builds require Android Studio/SDK (not present in this env); JVM `./gradlew test` is the automatable gate.