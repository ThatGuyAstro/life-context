# Life Context

Life Context is a local-first Windows context archive: a five-second desktop
collector sends monitor images and source metadata to a FastAPI/SQLite server,
which stores content-addressed artifacts, durable inference jobs, searchable
observations, and evidence-backed queries.

The MVP intentionally captures no audio, video, or raw keystrokes. Raw
screenshots and clipboard text are plaintext in the configured local archive;
clipboard is local-only when routing to a remote server unless explicitly
enabled.

## Quick start

Requirements:

- Windows 10/11, Python 3.11+, Node.js 18+, and pnpm 9+
- uv 0.10.7 for the locked Python environment
- Rust/MSVC Build Tools/WebView2 for the Tauri desktop shell

From this directory:

```powershell
uv lock --check
uv sync --locked --extra dev
. .\.venv\Scripts\Activate.ps1
pnpm install --frozen-lockfile
python -m pytest -q
pnpm --dir apps/dashboard run typecheck
pnpm --dir apps/dashboard run test
pnpm --dir apps/dashboard run build
pnpm --dir apps/desktop run typecheck
pnpm --dir apps/desktop run build
cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml --locked
```

Start the local services in separate terminals:

```powershell
pnpm server       # http://127.0.0.1:8000
pnpm collector    # all monitors every five seconds
pnpm dashboard    # http://127.0.0.1:5173/; Vite proxies /api to the local server
pnpm cli status
```

### Local troubleshooting and safe restart

The local API listens on `http://127.0.0.1:8000`; the dashboard listens on
`http://127.0.0.1:5173/`. The dashboard's same-origin `/api` proxy should
forward `/api/v1/health` to the API:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/health
Invoke-RestMethod http://127.0.0.1:5173/api/v1/health
Invoke-WebRequest http://127.0.0.1:5173/ | Select-Object StatusCode
```

Run each service in its own terminal so its stdout/stderr is the first place
to look for a failure. To keep a persistent local log instead, use the ignored
artifact directory:

```powershell
New-Item -ItemType Directory -Force .\.context-artifacts\local-site | Out-Null
pnpm server *> .\.context-artifacts\local-site\server.log
pnpm dashboard *> .\.context-artifacts\local-site\dashboard.log
```

To restart, press `Ctrl+C` in the terminal that owns the server or dashboard,
then start that one command again. If a terminal was closed and a port is
still occupied, inspect the listener and command line before stopping anything:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8000,5173 |
  Select-Object LocalAddress,LocalPort,OwningProcess
Get-CimInstance Win32_Process -Filter "ProcessId = <PID>" |
  Select-Object ProcessId,Name,CommandLine
```

Stop a process only after confirming its command line belongs to this checkout;
do not use a broad `Stop-Process` by name because unrelated Python or Node
services may be running. After a restart, repeat the health and proxy checks
above. A healthy API with a stale browser tab usually needs a hard reload or a
new tab, not a machine-wide process cleanup.

The Tauri shell can supervise the Python server and collector after the
[Windows preflight](scripts/check-runtime.ps1) passes:

```powershell
.\scripts\check-runtime.ps1 -RequireDesktop
pnpm --dir apps/desktop tauri dev
```

## Interfaces

The server exposes the versioned API under `/v1` and keeps localhost aliases
for development: health/capabilities/status, capture-envelope ingestion,
content-addressed artifact upload/download, timeline/observations, query,
SSE/WebSocket events, configuration, purge, and lifecycle controls.

The CLI supports:

```text
contextctl start | pause | status | query | timeline | purge | config
```

## Documentation map

- [Project context](docs/PROJECT-CONTEXT.md): product boundaries, current state,
  lessons from live validation, limitations, and next work.
- [Developer reference](docs/DEVELOPER_REFERENCE.md): repository seams, data
  flow, contracts, queue behavior, extension points, and focused checks.
- [Operations runbook](docs/OPERATIONS.md): Windows service lifecycle, model,
  queue, troubleshooting, privacy, retention, and recovery.
- [API and query reference](docs/API-AND-QUERY.md): endpoint behavior, query
  windows, response/citation shape, events, and configuration.
- [Bootstrap](docs/BOOTSTRAP.md), [model runtime](docs/MODEL-RUNTIME.md),
  [threat model](docs/THREAT_MODEL.md), and [acceptance report](docs/ACCEPTANCE-REPORT.md)
  contain the deeper environment, security, model, and test evidence.

Set `CONTEXT_SERVER_URL` and `CONTEXT_SERVER_TOKEN` for a remote authenticated
CLI client. Remote server mode requires HTTPS and a bearer token; local mode
binds to `127.0.0.1` by default. The complete proxy/TLS/auth runbook, including
the public diagnostics and docs caveat, is in
[docs/BOOTSTRAP.md](docs/BOOTSTRAP.md#remoteself-hosted-runbook).

## Configuration defaults

- Server data: `E:\LifeContext\data`
- Collector spool: `E:\LifeContext\spool`
- Capture interval: 5 seconds
- Inference workers: 1
- Queue size: 1000
- Raw screenshots: 24 hours
- Keyframes/evidence: 30 days
- Derived observations, metadata, and embeddings: indefinite

Configure the model with `CONTEXT_SERVER_MODEL_ENDPOINT` and
`CONTEXT_SERVER_MODEL_NAME`, or update it through `/v1/config`. The server
accepts a local llama.cpp/OpenAI-compatible endpoint over loopback HTTP or a
remote HTTPS endpoint. Non-loopback HTTP and embedded URL credentials are
rejected. It does not download or replace model files. A configured endpoint
processes the durable queue with strict JSON validation and stores model/prompt
revision, raw response, validation, confidence, and evidence provenance. The
measured Gemma 4 26B launcher and `auto` -> `json_object` compatibility path
are documented in [docs/MODEL-RUNTIME.md](docs/MODEL-RUNTIME.md).

Without a configured model endpoint, the server reports inference as
`not_configured` and accepted inference jobs remain in the durable queue; this
is an intentional local-runtime state, not evidence of model execution.

## Layout

- `apps/desktop`: Tauri v2 Windows shell and tray supervisor
- `apps/dashboard`: React/Vite browser and embedded-dashboard frontend
- `apps/cli`: `contextctl` CLI and HTTP/service client
- `services/collector`: Windows capture, source adapters, spool, and upload
- `services/context_server`: FastAPI lifecycle, SQLite/FTS5, artifacts, queue,
  inference, retrieval, and API routes
- `packages/contracts`: shared Pydantic contracts, schemas, and TypeScript types
- `docs`: product, threat-model, acceptance, and Windows bootstrap notes

Run the full Python suite with `uv run --locked pytest -q`. Test counts are
time-sensitive; rerun the suite before publishing a new verification report.

The current measured MVP gate status is recorded in
[docs/ACCEPTANCE-REPORT.md](docs/ACCEPTANCE-REPORT.md).

## Current verification snapshot

Measured on 2026-08-05 in this checkout:

- Python: **313 collected; 310 passed, 1 skipped, 2 expected xfails, 1
  warning**. The real-process SSE/WebSocket acceptance test passes with the
  locked `websockets==15.0.1` dependency; both xfails are documented
  contract-drift cases.
- Dashboard: the latest local rerun passed typecheck, production build, and
  all **33 Vitest tests in 9 files**. The recorded live browser smoke passes with three synthetic
  events through the local Vite `/api` proxy. The companion
  `tests/browser/dashboard_settings_smoke.py` script is a separate live
  settings/privacy check and is not included in the Vitest count. A direct
  cross-origin `?api=http://127.0.0.1:8000` URL fails without CORS.
- CLI: **21 focused tests pass**, and live CLI health/status reports `ok`,
  `running`, and `ready=true`.
- Tauri/Cargo: **7 Cargo tests pass**; desktop typecheck/build, locked Cargo
  check, and the Windows preflight/Tauri smoke pass.
- Windows capture: the live one-tick smoke observed **one physical monitor**;
  the required live multi-monitor acceptance matrix was not run.
- Runtime: one inference worker and queue capacity 1000; no model endpoint is
  configured. After the smoke seed, the measured queue had 3 pending, 0
  completed, and 0 failed jobs; inference reported `not_configured`.

This is not release acceptance: the full Windows collector matrix, a sustained
Gemma quality benchmark, remote network/TLS deployment, and managed-cloud
operation remain unverified. The short measured Gemma 4 26B direct and queue
smoke is recorded separately in
[docs/GPU-BENCHMARK-REPORT.md](docs/GPU-BENCHMARK-REPORT.md). The isolated
remote HTTPS/bearer browser smoke passes query, timeline, evidence download,
settings, and unsafe-target checks; it is security/proxy evidence, not a real
second-host deployment.
