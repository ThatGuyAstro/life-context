# Life Context Windows bootstrap

This repository is currently a Windows-first local application. The bootstrap
lane provisions and checks the developer toolchain; it does not download a
VLM, start a capture session, or touch the configured Life Context data
directory.

## Prerequisites

Install these before using the desktop shell:

- Windows 10 or 11.
- Python 3.11 or newer.
- `uv` 0.10.7 for the current CI baseline.
- Node.js 18 or newer and pnpm 9 or newer. The CI baseline uses Node 24 and
  pnpm 11.9.
- Rustup with the stable `x86_64-pc-windows-msvc` toolchain and Cargo.
- Microsoft C++ Build Tools with the MSVC x64/x86 workload and a Windows SDK.
- The Evergreen WebView2 Runtime.

The Tauri-specific prerequisites are documented by the upstream project in
[Tauri's Windows prerequisites guide](https://v2.tauri.app/start/prerequisites/).

## Preflight

From the repository root, run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\check-runtime.ps1
```

The check is read-only. It reports required tools, Python imports, the Tauri
desktop prerequisites, missing root files, and dependency-lock/build-policy
status. The checked-in root project metadata and pnpm build policy are now
complete, so a clean preflight should have zero warnings.

For machine-readable output:

```powershell
New-Item -ItemType Directory -Force .\.context-artifacts | Out-Null
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\check-runtime.ps1 -Json | Tee-Object .\.context-artifacts\runtime-report.json
```

The report is written under the ignored `.context-artifacts` directory so a
local preflight does not create a root-level untracked file. Use
`-RequireDesktop` when the Tauri shell must be buildable. Without that
switch, Rust, MSVC, and WebView2 are warnings so server/collector work can be
performed on a machine that does not have the desktop toolchain.

If `cargo` is installed by Rustup but is not visible in the current PowerShell
session, the preflight discovers `%USERPROFILE%\.cargo\bin` automatically. A
new terminal can also be opened after installing Rustup.

## Local setup

The root project declares the server, collector, and test dependencies.
WebSocket runtime support is a direct locked dependency
(`websockets>=13,<16`), not a test-only overlay. Check the lock metadata before
installation, then use the normal setup:

```powershell
uv lock --check
uv sync --locked --extra dev
```

The locked runtime import probe used by CI can be reproduced locally:

```powershell
uv run --locked --extra dev python -c "import fastapi, httpx, mss, PIL, psutil, pydantic, uvicorn, websockets; print('locked runtime imports: ok'); print('websockets=' + websockets.__version__)"
```

The task dispatcher uses the active `python` on `PATH`. In PowerShell, activate
the environment before running Python-backed `pnpm` tasks:

```powershell
. .\.venv\Scripts\Activate.ps1
```

For an isolated disposable environment:

```powershell
$venv = Join-Path $env:TEMP 'life-context-venv'
uv venv --python 3.11 $venv
$python = Join-Path $venv 'Scripts\python.exe'
$requirements = Join-Path $venv 'requirements.lock.txt'
uv export --locked --extra dev --output-file $requirements
uv pip install --python $python --requirement $requirements
uv pip check --python $python
& $python -c "import uvicorn, websockets; print('locked runtime imports: ok'); print('websockets=' + websockets.__version__)"
```

Install JavaScript dependencies with the checked-in build policy:

```powershell
pnpm install --frozen-lockfile
```

The dashboard's supported local browser path uses the Vite same-origin `/api`
proxy. Start the server and dashboard in separate terminals, then run the
deterministic browser smoke with a host Python installation that has Playwright
and Chromium:

```powershell
pnpm server
pnpm dashboard
python tests/browser/dashboard_smoke.py `
  --api-url http://127.0.0.1:8000 `
  --dashboard-url http://127.0.0.1:5173/
```

The direct `?api=http://127.0.0.1:8000` form is cross-origin and requires the
server to allow the dashboard origin and headers through CORS. The current
local server does not enable that policy by default. The latest local dashboard
rerun passed typecheck and production build, with 33 Vitest tests in 9 files:

```powershell
pnpm --dir apps/dashboard run typecheck
pnpm --dir apps/dashboard run test
pnpm --dir apps/dashboard run build
```

### Local runbook: health, logs, and restart

The expected local ports are API `8000` and dashboard `5173`. Check the API,
the dashboard root, and the dashboard-to-API proxy independently:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/health
Invoke-WebRequest http://127.0.0.1:5173/ | Select-Object StatusCode
Invoke-RestMethod http://127.0.0.1:5173/api/v1/health
```

Foreground terminals contain the service logs. For a persistent diagnostic
copy, redirect each service separately into the ignored artifact directory:

```powershell
New-Item -ItemType Directory -Force .\.context-artifacts\local-site | Out-Null
pnpm server *> .\.context-artifacts\local-site\server.log
pnpm dashboard *> .\.context-artifacts\local-site\dashboard.log
```

Prefer `Ctrl+C` in the terminal that started the failing service, followed by
the same `pnpm server` or `pnpm dashboard` command. If the original terminal
is gone, identify the exact listener and process command line before stopping
it; never kill every `python`, `node`, or `pnpm` process on the machine:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8000,5173 |
  Select-Object LocalAddress,LocalPort,OwningProcess
Get-CimInstance Win32_Process -Filter "ProcessId = <PID>" |
  Select-Object ProcessId,Name,CommandLine
```

Only stop a PID after its command line confirms this checkout. Once restarted,
rerun both health URLs. If those checks pass but the page looks crashed, hard
reload the browser tab or open a new one; do not perform a broad process cleanup.

The companion settings/privacy smoke uses the same local server/dashboard pair
and is a separate live check, not another Vitest test. The local settings/privacy
smoke and the expanded security smoke both pass. The expanded check builds an
ephemeral HTTPS self-hosted API, configures the Vite proxy with the matching
short-lived CA and bearer token, and verifies the same-origin artifact download
without disabling TLS verification or adding CORS:

```powershell
python tests/browser/dashboard_settings_smoke.py `
  --dashboard-url http://127.0.0.1:5173/
```

```powershell
python tests/browser/dashboard_security_smoke.py
```

The Tauri frontend and Rust shell checks are:

```powershell
pnpm --dir apps/desktop run typecheck
pnpm --dir apps/desktop run build
.\apps\desktop\scripts\tauri-smoke.ps1
```

The smoke helper discovers Rustup/Cargo, MSVC, the Windows SDK, and WebView2,
then runs locked Cargo check. If Cargo is not on the current PATH, invoke the
discovered executable explicitly:

```powershell
& "$env:USERPROFILE\.cargo\bin\cargo.exe" check --locked `
  --manifest-path apps/desktop/src-tauri/Cargo.toml
```

## Remote/self-hosted runbook

The Python API is an HTTP upstream; this repository does not configure Uvicorn
with a public certificate. For a remote deployment, terminate HTTPS at a
trusted reverse proxy or load balancer and forward to the API over a private
or loopback connection. The proxy must set/validate `X-Forwarded-Proto:
https`, preserve the client's `Authorization` header, and support the
WebSocket upgrade used by `/v1/events` and `/v1/events/stream`.

Do not make the API believe a remote request is local. Start the server with a
non-loopback bind so its own HTTPS and bearer checks are active; use `pnpm
server`, not `pnpm start:local` (the latter intentionally forces loopback):

```powershell
$env:CONTEXT_SERVER_HOST = "0.0.0.0" # prefer a specific private interface
$env:CONTEXT_SERVER_PORT = "8000"
$env:CONTEXT_SERVER_AUTH_TOKEN = "<strong-random-token>"
$env:CONTEXT_SERVER_REQUIRE_HTTPS_REMOTE = "true"
$env:CONTEXT_SERVER_DOCS_ENABLED = "false"
pnpm server
```

For an Nginx-style terminator, the important boundary is equivalent to:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

Restrict the upstream so it is reachable only by the terminator/firewall,
obtain a real certificate for the public hostname, and configure the proxy to
strip and set forwarding headers rather than trusting values supplied by the
internet. If the API is intentionally bound to loopback, the proxy becomes
the sole authentication boundary; it must enforce the same bearer policy.

Bearer tokens are sent as `Authorization: Bearer <token>`. The server-side
token is `CONTEXT_SERVER_AUTH_TOKEN`; client-side names are different:

| Client or process                   | Environment variables                                                                                                        |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Life Context server model wiring    | `CONTEXT_SERVER_MODEL_ENDPOINT`, `CONTEXT_SERVER_MODEL_NAME`, `CONTEXT_SERVER_MODEL_REVISION`, `CONTEXT_SERVER_WORKER_COUNT` |
| Inference client inside that server | `CONTEXT_MODEL_API_KEY`, `CONTEXT_MODEL_STRUCTURED_OUTPUT_MODE`, `CONTEXT_MODEL_REQUIRES_MULTIMODAL`                         |
| `contextctl` CLI                    | `CONTEXT_SERVER_URL`, `CONTEXT_SERVER_TOKEN` (or the server-token fallback `CONTEXT_SERVER_AUTH_TOKEN`)                      |
| Dashboard Vite proxy                | `VITE_API_PROXY_TARGET`, `VITE_API_PROXY_BEARER_TOKEN`; trust a private CA with `NODE_EXTRA_CA_CERTS`                        |
| Collector                           | `COLLECTOR_SERVER_URL`, `COLLECTOR_BEARER_TOKEN`, and `COLLECTOR_CA_FILE` when needed                                        |

These names are not interchangeable. For example, `CONTEXT_SERVER_AUTH_TOKEN`
authenticates clients to Life Context, while `CONTEXT_MODEL_API_KEY`
authenticates Life Context to the model provider. The dashboard should use its
same-origin `/api` proxy for a remote target; a direct `?api=https://...`
connection is cross-origin and is not the supported default.

### Public diagnostics and documentation caveat

The current server intentionally leaves operational probes public, even when
remote feature routes require auth: `/v1/health`, `/v1/health/live`,
`/v1/health/ready`, `/v1/capabilities`, and `/v1/status` do not require a
bearer token. `/v1/capabilities` and `/v1/status` can reveal model, queue,
storage, and runtime metadata, so treat them as public information. FastAPI's
`/docs`, `/redoc`, and `/openapi.json` are also public while
`CONTEXT_SERVER_DOCS_ENABLED=true` (the default). Disable them for an internet-
reachable deployment, and do not put secrets in configuration values that may
be returned as diagnostics.

Verify the boundary through the public proxy, not only against the private
upstream:

```powershell
Invoke-RestMethod https://context.example/v1/health
Invoke-WebRequest https://context.example/docs -SkipHttpErrorCheck |
  Select-Object StatusCode
Invoke-WebRequest https://context.example/v1/timeline -SkipHttpErrorCheck |
  Select-Object StatusCode
Invoke-WebRequest https://context.example/v1/timeline `
  -Headers @{ Authorization = "Bearer <strong-random-token>" } |
  Select-Object StatusCode
```

The expected result is public health/diagnostic access, a disabled docs
response when configured, `401` for the protected route without a token, and
`200` with the correct token. A private CA must be trusted by each client;
never replace that trust setup with `secure: false`, an insecure TLS bypass, or
browser certificate ignoring.

## CI workflow

`.github/workflows/ci.yml` runs on `windows-latest` and follows the same
boundaries as local preflight:

1. Install pinned uv, Node, pnpm, and the stable MSVC Rust toolchain.
2. Verify `uv lock --check`.
3. Create an isolated temporary Python environment from the hash-pinned locked
   project export, including the dev dependencies, and probe the Uvicorn and
   WebSocket runtime imports.
4. Install JavaScript dependencies with the frozen lockfile and checked-in
   `allowBuilds` policy.
5. Run `check-runtime.ps1 -RequireDesktop -Json` and upload its report.
6. Run the Python tests and bytecode compilation probe, with Ruff kept advisory
   until the existing runtime formatting drift is cleaned up.
7. Typecheck, test, and build the dashboard.
8. Typecheck/build the Tauri frontend and run locked `cargo check` for the Rust
   shell.

The workflow never downloads model weights and does not require a GPU. Model
server capability checks belong to the inference lane and should remain
separate from host bootstrap.

## Observed local status

The following was measured on the Windows development host during this review
on 2026-08-05. These results describe the current checkout; they are not a
release-acceptance claim.

| Check                                                          | Result                                                                                  |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Python 3.11, uv 0.10.7, Node 24.18, pnpm 11.9                  | Available                                                                               |
| FastAPI, uvicorn, Pydantic, Pillow, mss, psutil, httpx imports | Available                                                                               |
| Locked Uvicorn/WebSocket import probe                          | Pass: `uvicorn` and `websockets==15.0.1` imported from the locked environment           |
| Rustup/Cargo/Rust compiler                                     | Available under `%USERPROFILE%\.cargo\bin`                                              |
| Visual Studio C++ Build Tools                                  | Visual Studio 2022 Community detected                                                   |
| WebView2                                                       | Evergreen runtime detected                                                              |
| `check-runtime.ps1 -Json`                                      | Pass: 0 required failures, 0 warnings                                                   |
| `uv lock --check` at the root                                  | Pass: lock matches `pyproject.toml`; locked graph includes `websockets==15.0.1`         |
| Real Uvicorn SSE/WebSocket process probe                       | Pass: SSE, authenticated WebSocket delivery, filtering, disconnect, and clean exit      |
| Python bytecode compilation                                    | Pass                                                                                    |
| Bootstrap smoke and full-story acceptance harness              | Pass: 6 passed                                                                          |
| Full Python suite                                              | **Pass:** 313 collected; 310 passed, 1 skipped, 2 xfailed, 1 warning                    |
| Ruff check / format check                                      | **Not clean:** 277 lint errors; 50 files would be reformatted; 116 already formatted    |
| Dashboard typecheck                                            | Pass                                                                                    |
| Dashboard tests                                                | **Pass:** 33 of 33 tests in 9 files                                                     |
| Dashboard build                                                | Pass                                                                                    |
| Dashboard browser smoke                                        | **Pass:** 3 synthetic events via the local `/api` proxy                                 |
| CLI focused tests                                              | **Pass:** 21 tests                                                                      |
| CLI live health/status                                         | **Pass:** server `ok`, `running`, `ready=true`                                          |
| Tauri typecheck/build                                          | Pass                                                                                    |
| Rust `cargo check --locked` / Tauri smoke                      | Pass                                                                                    |
| Cargo tests                                                    | **Pass:** 7 tests                                                                       |
| Live Windows capture                                           | **Pass:** one physical monitor in the one-tick smoke; live multi-monitor matrix not run |
| Live queue/inference snapshot                                  | 1 worker, capacity 1000; 3 pending, 0 completed/failed; model `not_configured`          |

The real-process event-stream acceptance test now passes with the direct locked
`websockets` runtime dependency. The browser settings/privacy smoke passed
through the same-origin proxy, and the expanded remote HTTPS/bearer security
phase passed its query, timeline, bearer, artifact-download, settings, and
unsafe-target checks. If the temporary CA is omitted from the Vite child,
Node's HTTPS proxy correctly rejects the self-signed fixture certificate; do
not replace that trust setup with `secure: false` or browser certificate
ignoring.

Do not treat the observed status table as a permanent release claim. Rerun
the preflight and the full workflow after changing dependencies, Rust, Visual
Studio, WebView2, or the root package metadata. These checks do not provide a
real Gemma run, GPU/model benchmark, or managed-cloud acceptance evidence;
inference remains `not_configured`, and the live capture result covers one
physical monitor only.
