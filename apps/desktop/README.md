# Life Context Desktop

This directory contains the Windows Tauri v2 desktop shell for Life Context.
The React dashboard is embedded in the Tauri window, while the native shell
owns tray actions and supervises the local Python server and collector.

## Prerequisites

- Node.js 18+ and npm or pnpm. The repository preflight enforces Node.js 18+.
- Rustup with the stable `x86_64-pc-windows-msvc` toolchain and Cargo.
- Microsoft C++ Build Tools, including the MSVC x64/x86 workload and a
  Windows 10/11 SDK. A regular PowerShell window is usually sufficient for
  `cargo check`; a Visual Studio Developer PowerShell is the fallback when
  `link.exe` or SDK tools are not discovered.
- The Evergreen WebView2 Runtime. It is used at runtime by Tauri and is not
  supplied by this repository.

From `apps/desktop`, the scoped preflight helper checks these prerequisites and
runs Cargo when it is available:

```powershell
.\scripts\tauri-smoke.ps1
```

To inspect prerequisites without compiling:

```powershell
.\scripts\tauri-smoke.ps1 -SkipCargoCheck
```

## Run

```powershell
pnpm install --frozen-lockfile
pnpm tauri dev
```

For a browser-only UI preview, use `pnpm dev`. In that mode the service
controls use an in-memory browser fallback so the layout and interaction can
be checked without Tauri or Rust. The Tauri dev server is pinned to
`127.0.0.1:1420` by `vite.config.ts`.

## Verify

From `apps/desktop`, run the frontend checks and the Windows shell smoke:

```powershell
pnpm run typecheck
pnpm run build
.\scripts\tauri-smoke.ps1
```

The current local result is a passing frontend typecheck/build and Cargo smoke;
the native shell reports **7 passing Cargo tests**. This package has no
JavaScript `test` script, so the final rerun recorded **313 Python tests
collected (310 passed, 1 skipped, 2 expected xfails, 1 warning)**, **33
dashboard tests in 9 files**, and **21 focused CLI tests**; repository-wide
evidence and deferred claims are recorded in the [acceptance report](../../docs/ACCEPTANCE-REPORT.md).

## Boundary and intent flow

```text
React controls / tray menu
          |
          v
thin Tauri commands (`src-tauri/src/lib.rs`)
          |
          v
`ServiceSupervisor` boundary (`src-tauri/src/supervisor.rs`)
          |
          v
Python child processes (`supervisor.rs`)
```

The checked-in `SupervisorBridge` is the native process boundary. `start`
spawns `python -m services.context_server` and `python -m services.collector`
from the configured repository root. `pause` stops only the collector, leaving
the server available; `stop` terminates both children. The bridge reaps exited
children when status is requested. Keep service lifecycle changes behind the
`ServiceSupervisor` trait so the tray and Tauri commands remain thin routes.

The dashboard target is an iframe webview in the main window. Its URL is kept
in local storage and is constrained to HTTPS for remote hosts; HTTP is allowed
only for loopback development targets. The Tauri CSP applies the same frame
boundary, and the embedded dashboard is not granted clipboard permissions.

## Service environment

The child processes inherit the Tauri process environment. The supervisor also
sets `PYTHONUNBUFFERED=1` and prepends the resolved repository/resource root to
`PYTHONPATH` while preserving inherited entries.

Supervisor-specific variables:

| Variable              | Default                                                                                  | Purpose                                                              |
| --------------------- | ---------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| `LIFE_CONTEXT_ROOT`   | inferred from the Tauri manifest, packaged resources, or the checkout during development | Repository/resource root used as the Python child working directory. |
| `LIFE_CONTEXT_PYTHON` | `python`                                                                                 | Python executable used for both child processes.                     |

The server reads `CONTEXT_SERVER_*` variables. Common desktop settings are:

| Variable                                  | Default               | Purpose                                                             |
| ----------------------------------------- | --------------------- | ------------------------------------------------------------------- |
| `CONTEXT_SERVER_HOST`                     | `127.0.0.1`           | Bind address; keep localhost for a local-only server.               |
| `CONTEXT_SERVER_PORT`                     | `8000`                | Server port.                                                        |
| `CONTEXT_SERVER_DATA_DIR`                 | `E:\LifeContext\data` | SQLite database and content-addressed artifacts.                    |
| `CONTEXT_SERVER_CAPTURE_INTERVAL_SECONDS` | `5`                   | Capture cadence reported by the server.                             |
| `CONTEXT_SERVER_WORKER_COUNT`             | `1`                   | Inference workers.                                                  |
| `CONTEXT_SERVER_QUEUE_SIZE`               | `1000`                | Persistent inference queue limit.                                   |
| `CONTEXT_SERVER_MODEL_ENDPOINT`           | empty                 | OpenAI-compatible VLM endpoint; loopback HTTP or remote HTTPS only. |
| `CONTEXT_SERVER_MODEL_NAME`               | empty                 | Configured model identifier.                                        |
| `CONTEXT_SERVER_MODEL_REVISION`           | empty                 | Operator-supplied model revision.                                   |
| `CONTEXT_SERVER_AUTH_TOKEN`               | empty                 | Bearer token; required for any non-loopback server bind.            |
| `CONTEXT_SERVER_REQUIRE_HTTPS_REMOTE`     | `true`                | Require HTTPS when the server is used remotely.                     |
| `CONTEXT_SERVER_CONFIG_FILE`              | empty                 | Optional JSON/TOML/YAML settings file.                              |

The collector reads `COLLECTOR_*` variables. The most useful settings are:

| Variable                                         | Default                  | Purpose                                                                                                         |
| ------------------------------------------------ | ------------------------ | --------------------------------------------------------------------------------------------------------------- |
| `COLLECTOR_SERVER_URL`                           | `http://127.0.0.1:8000`  | Ingest server URL; HTTP is limited to loopback and remote targets must use HTTPS (`COLLECTOR_URL` is an alias). |
| `COLLECTOR_SPOOL_DIR`                            | `E:\LifeContext\spool`   | Durable offline spool.                                                                                          |
| `COLLECTOR_ROOTS`                                | current directory        | Semicolon-separated filesystem roots.                                                                           |
| `COLLECTOR_CAPTURE_INTERVAL_SECONDS`             | `5`                      | Screen capture cadence.                                                                                         |
| `COLLECTOR_JPEG_QUALITY`                         | `85`                     | Monitor/contact-sheet JPEG quality.                                                                             |
| `COLLECTOR_CLIPBOARD_ENABLED`                    | `true`                   | Capture raw clipboard text locally.                                                                             |
| `COLLECTOR_ALLOW_REMOTE_CLIPBOARD`               | `false`                  | Explicitly permit clipboard delivery to a remote server.                                                        |
| `COLLECTOR_BROWSER_ENABLED`                      | `true`                   | Enable browser metadata collection.                                                                             |
| `COLLECTOR_INCLUDE_POWERSHELL_HISTORY`           | `true`                   | Include bounded PowerShell history metadata.                                                                    |
| `COLLECTOR_INCLUDE_PROCESSES`                    | `true`                   | Include foreground/process metadata.                                                                            |
| `COLLECTOR_MAX_PROCESSES`                        | `64`                     | Maximum process records per collection tick.                                                                    |
| `COLLECTOR_BEARER_TOKEN`                         | empty                    | Bearer token for the ingest server.                                                                             |
| `COLLECTOR_INGEST_PATH`                          | `/v1/ingest/batch`       | Ingest endpoint path; override if the server exposes `/v1/ingest/batches`.                                      |
| `COLLECTOR_ARTIFACT_PATH`                        | `/v1/artifacts/{sha256}` | Content-addressed artifact endpoint.                                                                            |
| `COLLECTOR_CA_FILE`                              | empty                    | Additional CA bundle for HTTPS.                                                                                 |
| `COLLECTOR_CLIENT_CERT` / `COLLECTOR_CLIENT_KEY` | empty                    | Optional mTLS client certificate/key pair.                                                                      |
| `COLLECTOR_INSECURE_TLS`                         | `false`                  | Development-only TLS verification bypass.                                                                       |

Example local launch configuration. Run this block from the repository root;
the disposable runtime state is kept beside the checkout rather than in the
source tree:

```powershell
$repo = (Get-Location).Path
$localState = Join-Path (Split-Path -Parent $repo) 'life-context-local-state'
$env:LIFE_CONTEXT_ROOT = $repo
$env:LIFE_CONTEXT_PYTHON = Join-Path $repo '.venv\Scripts\python.exe'
$env:CONTEXT_SERVER_DATA_DIR = Join-Path $localState 'data'
$env:COLLECTOR_SPOOL_DIR = Join-Path $localState 'spool'
$env:COLLECTOR_ROOTS = $repo
$env:COLLECTOR_SERVER_URL = 'http://127.0.0.1:8000'
pnpm --dir apps/desktop tauri dev
```

Do not put bearer tokens or private certificate material in checked-in files.
The collector intentionally treats clipboard text as plaintext locally; use
the dashboard warning and source-routing settings before enabling remote
delivery. The launch example above is local-only. Remote HTTPS dashboard and
collector targets are operator-configured and are not evidence of a tested
remote deployment.

## Windows packaging

The checked-in `src-tauri/icons/icon.ico` is used for the Windows tray and
installer. `src-tauri/tauri.windows.conf.json` enables MSI and NSIS release
bundles and includes the Python service source plus shared contracts under the
installer resource directory. The release remains dependent on a compatible
Python environment; set `LIFE_CONTEXT_PYTHON` when the installed `python`
command is not the environment containing the project dependencies.

Build the Windows release from this directory with:

```powershell
pnpm tauri build
```

The dashboard is still an operator-configured HTTP(S) iframe target. The
desktop shell does not silently bundle or replace that target; set the target
in the shell before shipping a deployment-specific dashboard URL.
