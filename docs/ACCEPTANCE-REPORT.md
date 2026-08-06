# Life Context MVP acceptance report

Date: 2026-08-05  
Scope: current checkout, acceptance/reporting artifacts, and measured local
verification. The targeted dashboard timeout recheck updated this report and
the companion dashboard/bootstrap notes; no application source or browser test
files were changed.

## Decision

The checkout is **not release-accepted** against the MVP gate. Dashboard
typecheck/tests/build, the local same-origin browser smokes, the final
event/auth lane, dependency checks, and the Windows/Tauri toolchain passed. The
expanded dashboard security smoke now passes its isolated remote HTTPS/bearer
artifact-download assertion; the earlier timeout was not reproducible. The
release gate remains open because the required Windows collector matrix, real
model execution, remote deployment, and managed-cloud operation were not run.

The measured component-test snapshot is **Python: 313 collected, 310 passed, 1
skipped, 2 expected xfails, and 1 warning; dashboard: 33 Vitest tests in 9 files; focused
CLI: 21; Cargo: 7**. These are component/test results only and do not establish release
acceptance.

## Measured verification

All commands below were run against this checkout on the date above.

| Area                                 | Command or evidence                                                                                                                                                                                                                                                            | Result                                                                                                                                                                                  |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Windows preflight                    | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\check-runtime.ps1 -RequireDesktop -Json`                                                                                                                                                                    | Pass: `failedRequired=0`, `warnings=0`                                                                                                                                                  |
| Locked Python metadata               | `uv lock --check`                                                                                                                                                                                                                                                              | Pass: lock matches `pyproject.toml`; the locked export resolves 28 packages and includes `websockets==15.0.1`                                                                           |
| Locked dependency/runtime checks     | `uv sync --locked --extra dev`; `uv pip check --python .venv\Scripts\python.exe`; locked import probe; `pnpm install --frozen-lockfile`                                                                                                                                        | Pass: 28 packages resolved, 27 checked; all installed packages compatible; imports pass with `websockets==15.0.1`; all 4 pnpm workspaces already up to date                             |
| Clean locked environment             | `uv venv`; `uv export --locked --extra dev`; `uv pip install`; `uv pip check`                                                                                                                                                                                                  | Pass: 27 packages installed from the hash-pinned export; imports include Uvicorn and `websockets==15.0.1`                                                                               |
| Final event/auth lane                | `uv run --locked --extra dev python -m pytest -q -rA tests\acceptance\test_api_capability_health_process.py tests\acceptance\test_event_stream_process.py tests\acceptance\test_remote_self_hosted_transport.py services\context_server\tests\integration\test_runtime_app.py` | **12 passed, 1 warning**; real-process SSE/WebSocket, model diagnostics, HTTP bearer, HTTPS policy, remote transport, and artifact/query checks passed                                  |
| Real Uvicorn SSE/WebSocket process   | `tests/acceptance/test_event_stream_process.py`; `tests/acceptance/test_server_process_lifecycle.py`                                                                                                                                                                           | **Pass:** 1 + 1 tests; SSE, authenticated/filtered WebSocket delivery, disconnect cleanup, and clean process exit                                                                       |
| Real Uvicorn API capability/health   | `uv run --locked --extra dev python -m pytest -q tests\acceptance\test_api_capability_health_process.py -s`                                                                                                                                                                    | **Pass:** 3 tests; capability/storage fields, queue staleness, artifact count, model states, and protected-route auth at the HTTP socket                                                |
| Python compilation                   | `python -m compileall -q apps packages services tests`                                                                                                                                                                                                                         | Pass                                                                                                                                                                                    |
| Focused bootstrap/full-story harness | `uv run --locked --no-project python -m pytest -q tests\test_bootstrap_smoke.py tests\acceptance\test_full_story.py`                                                                                                                                                           | **6 passed**; includes the deterministic cross-service fake-VLM capture-to-query harness                                                                                                |
| Full Python suite                    | `uv run --locked --no-project python -m pytest -q`                                                                                                                                                                                                                             | **313 collected: 310 passed, 1 skipped, 2 xfailed, 1 warning**                                                                                                                          |
| Dashboard typecheck                  | `pnpm --dir apps/dashboard run typecheck`                                                                                                                                                                                                                                      | Pass                                                                                                                                                                                    |
| Dashboard unit/component tests       | `pnpm --dir apps/dashboard run test`                                                                                                                                                                                                                                           | **33 passed in 9 files**                                                                                                                                                                |
| Dashboard production build           | `pnpm --dir apps/dashboard run build`                                                                                                                                                                                                                                          | Pass                                                                                                                                                                                    |
| Live dashboard browser smoke         | `python tests/browser/dashboard_smoke.py --api-url http://127.0.0.1:8000 --dashboard-url http://127.0.0.1:5173/`                                                                                                                                                               | **Pass**: 3 synthetic events seeded; status, timeline, query, evidence link, and download checks passed through the Vite same-origin `/api` proxy                                       |
| Live dashboard settings/privacy      | `python tests/browser/dashboard_settings_smoke.py --dashboard-url http://127.0.0.1:5173/`                                                                                                                                                                                      | **Pass**: plaintext warning, local-only clipboard routing, retention, worker/model settings, and same-origin API checks passed                                                          |
| Expanded dashboard security smoke    | `python tests/browser/dashboard_security_smoke.py`                                                                                                                                                                                                                             | **Pass:** 3 consecutive runs; isolated HTTPS Uvicorn API, bearer-authenticated Vite proxy, query/timeline/evidence, artifact download, settings, and unsafe-target rejection all passed |
| CLI focused tests                    | `uv run --locked --extra dev python -m pytest -q apps\cli\tests`                                                                                                                                                                                                               | **21 passed**; focused CLI tests                                                                                                                                                        |
| CLI help and live status             | `node scripts/run-task.mjs cli --help`; live `health`/`status` through `python -m apps.cli` and the pnpm wrapper                                                                                                                                                               | Pass: server reported `ok`, `running`, and `ready=true`                                                                                                                                 |
| Tauri frontend                       | `pnpm --dir apps/desktop run typecheck`; `pnpm --dir apps/desktop run build`                                                                                                                                                                                                   | Pass                                                                                                                                                                                    |
| Tauri/Rust shell                     | `.\apps\desktop\scripts\tauri-smoke.ps1`; `cargo check --locked --manifest-path apps/desktop/src-tauri/Cargo.toml`                                                                                                                                                             | Pass: manifest/lock, Rustup, Cargo, rustc, MSVC, SDK, and WebView2 detected; Cargo check passed                                                                                         |
| Cargo tests                          | `cargo test --locked --manifest-path apps/desktop/src-tauri/Cargo.toml`                                                                                                                                                                                                        | **7 passed**; native shell unit/integration tests                                                                                                                                       |
| Ruff advisory checks                 | `uv run --locked --extra dev ruff check .`; `uv run --locked --extra dev ruff format --check .`                                                                                                                                                                                | Not clean: 277 lint errors; 50 files would be reformatted and 116 are already formatted                                                                                                 |

The browser smokes were run with the host Python installation that has
Playwright, Chromium, and the security-smoke TLS helpers. These tools are not
part of the locked project `dev` extra, so the Python-suite run does not imply
that browser tooling is present in `.venv`. The browser processes used GPU
disabled flags; the live checks used temporary state and left ports 8000 and
5173 clear afterward.

## Windows capture acceptance boundary

This refresh includes the safe live Windows capture boundary, with no GPU
execution. The live topology was enumerated by the collector's Windows
`EnumDisplayMonitors` adapter and the same backend was used for the capture
smoke.

| Evidence                       | Exact result                                                                                                                                                                                                                                                                                                                                           |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Live host and observation      | Windows `10.0.22631`, Python `3.11.14`; in-memory topology/capture probe observed at `2026-08-05T22:43:42.911089+00:00`; capture metadata timestamp was `2026-08-05T22:43:42.911089+00:00`                                                                                                                                                             |
| Live topology                  | One physical monitor: `\\.\DISPLAY1`, primary, bounds `(0, 0, 2560, 1440)`, size `2560x1440`; backend `windows-imagegrab`                                                                                                                                                                                                                              |
| Direct real capture            | `uv run --locked --extra dev python -m pytest -q services\\collector\\tests\\capture_smoke\\test_windows_capture_smoke.py -s` -> **1 passed in 0.28s**; the separate in-memory capture probe discovered and captured the same live monitor ID; contact sheet size `664x408`                                                                            |
| Direct capture probe artifacts | Probe sequence `capture-ac7c38366cc241d3aa82541ee2578b80`; monitor JPEG SHA-256 `3a5d4d59e68db37af73f0cb4de3dc469548b7e963020d8eb2736c867be6e44c9`; contact sheet SHA-256 `f7e9e46d2a9e3b11dbfe9768590869a9e744f55ba129804789eda4728cf39c3e`; sequence SHA-256 `0cf0fbc8e577e8acc53988ab36d495d8f296a81f0abed28a5141949cf8c82efe`                      |
| One-tick real collector        | `uv run --locked --extra dev python -m pytest -q services\\collector\\tests\\capture_smoke\\test_windows_collector_smoke.py -s` -> **1 passed in 1.33s**; `monitor_count=1`, `artifact_count=2`, `uploaded_artifacts=2`, `pending_artifacts=0`, `pending_batches=0`, `owned_processes_clean=true`, `owned_port_clean=true`, inference `not_configured` |

The live result is therefore a **one-monitor pass** for monitor discovery,
per-monitor JPEG encoding/hash validation, contact-sheet generation, local
transport/spool delivery, and cleanup. It is not evidence that a real Windows
multi-monitor topology was present during this run.

The deterministic multi-monitor comparison also passed:

| Deterministic coverage             | Exact result                                                                                                                                                                                                                                                  |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Capture and artifact suite         | `uv run --locked --extra dev python -m pytest -q services\\collector\\capture\\tests\\test_capture.py services\\collector\\capture\\tests\\test_capture_artifacts_acceptance.py -s` -> **9 passed in 0.23s**                                                  |
| Two-monitor service fixture        | Monitors `one` and `two` are captured in order; each JPEG carries the sequence/monitor metadata and a matching SHA-256; the contact sheet is generated                                                                                                        |
| Virtual-coordinate adapter fixture | `DISPLAY1` is `(0, 0, 2, 1)` and `DISPLAY2` is `(-2, 0, 0, 1)`; the negative origin is preserved in the `mss` request                                                                                                                                         |
| Three-monitor artifact fixture     | `display-primary` `(0, 0, 4, 3)`, `display-left` `(-4, 0, 0, 3)`, and `display-above` `(0, -3, 4, 0)` are emitted in order; two sequences deduplicate to four stored JPEG blobs (three monitor images plus one contact sheet), whose contact sheet is `11x13` |
| Deterministic cadence              | Three multi-monitor sequences capture immediately, then after exactly two `5.0`-second sleeps; each sequence visits all three monitors in order                                                                                                               |

The deferred limitation is the live multi-monitor Windows matrix. A host with
at least two physical displays still needs the direct and collector smoke
repeated against the real topology, including a negative virtual-desktop
coordinate if that topology provides one, before multi-monitor live capture
can be called accepted. The deterministic injected tests prove the portable
ordering, geometry, artifact, and cadence contracts, but do not substitute for
that missing live multi-monitor evidence.

## Deterministic fake-VLM integration evidence

This refresh adds measured inference-path evidence. It is **deterministic
fake-model integration**, not a real Gemma run, GPU benchmark, model-quality
evaluation, or latency measurement. The lane uses scripted fake clients and a
loopback OpenAI-compatible HTTP stub. Its server database, artifact store, and
spool are isolated below temporary directories; it does not download weights or
write to the default `E:\LifeContext\data` archive.

The focused lane was run with:

```powershell
uv run --locked --extra dev python -m pytest -q `
  tests/acceptance/test_event_stream_process.py `
  services/context_server/tests/inference/test_client.py `
  services/context_server/tests/inference/test_contracts.py `
  services/context_server/tests/inference/test_fake.py `
  services/context_server/tests/inference/test_http_integration.py `
  services/context_server/tests/inference/test_queue.py `
  services/context_server/tests/inference/test_runtime_worker.py `
  services/context_server/tests/inference/test_sqlite_job_adapter.py `
  services/context_server/tests/inference/test_storage_adapter.py `
  services/context_server/tests/query/test_query_ingestion_integration.py `
  tests/acceptance/test_full_story.py
```

Result: **48 passed in 3.66s**. `websockets==15.0.1` is a direct runtime
dependency in `pyproject.toml` and `uv.lock`, so the process-boundary probe runs
from the locked environment without an ephemeral `--with` overlay.

| Boundary                        | Measured evidence                                                                                                                                                                                                                                                                                                                         |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Strict schema                   | The OpenAI-compatible request contains `response_format.type=json_schema`, `json_schema.strict=true`, and the exact contract schema. Deterministic prompt/extraction checks reject surrounding prose, code fences, duplicate keys, missing fields, and extra fields.                                                                      |
| Image data URL                  | Embedded JPEG bytes become a multimodal `data:image/jpeg;base64,...` URL; artifact SHA-256 and `source=embedded` provenance are retained. Invalid base64, prompt-hash tampering, and non-JSON values fail closed.                                                                                                                         |
| Queue processing/completion     | `DeterministicFakeModelClient` returns a scripted valid object; the worker reports success, the job reaches `SUCCEEDED`, the request ID is stable, and the backlog reaches zero. Retry and dead-letter transitions also pass.                                                                                                             |
| Persisted result and provenance | SQLite claim/complete persists the result and nested model/prompt provenance, including prompt/schema hashes. The fake HTTP sequence also verifies retry attempt 4, model revision, endpoint, response/request IDs, and usage metadata.                                                                                                   |
| Real process boundary           | The shipped Uvicorn process runs one worker against `event-stream-fake-model` / `event-stream-fake-revision`; authenticated SSE emits both capture and `context.inference` events, filtered WebSocket delivery works, duplicate ingest remains healthy, and the process exits cleanly.                                                    |
| Query evidence                  | The full-story harness creates ten deterministic two-monitor captures at five-second spacing, queries `activity-07`, matches the answer evidence ID to the result, downloads the linked artifact byte-for-byte, and verifies duplicate replay and purge behavior. Query integration tests also verify filters and artifact evidence URLs. |

This evidence demonstrates that the request, queue, persistence, event, and
query plumbing can carry a deterministic fake response end to end. It does
not demonstrate that Gemma is installed, that a real VLM accepts the schema or
image input, or that any GPU execution is available or performant.

### Cross-service acceptance boundary

The new test-only path is:

```powershell
uv run --locked --no-project python -m pytest -q tests\acceptance\test_full_story.py
```

Result: **2 passed**. The second test composes the deterministic
two-monitor capture fixture, collector spool/upload transport, real HTTP
ingest and content-addressed artifact store, persisted inference job, an
inspecting fake VLM, the artifact-to-`data:image/jpeg;base64,...` resolver,
the persisted validated observation/provenance, and the public query response.
It asserts both the fake-VLM observation citation and the source screenshot
citation, then downloads the cited source artifact byte-for-byte. All state is
under a temporary directory; no real model, GPU, desktop capture, or default
archive is used.

The harness also records a deliberate current API boundary: the derived
inference observation is queryable and cited, and its database row retains the
primary artifact digest, but the current public derived-observation metadata
does not expose `artifact_url`. The byte-download assertion therefore uses the
source-event citation while the derived row is verified through its persisted
digest. Exposing a direct artifact URL on derived inference evidence remains a
production follow-up and was intentionally outside this test-only slice.

## Full-suite skip and expected xfails

The full Python result is not a hidden failure count. Pytest collected 313 items
and reported 310 passes, 1 skip, 2 expected xfails, and 1 warning. The real
process API capability/health and SSE/WebSocket acceptance tests pass with the
direct locked runtime dependency.

- **Skip:** `services/collector/sources/tests/test_filesystem.py:41`,
  `symlink creation is unavailable` on this Windows environment.
- **Expected xfail:**
  `services/context_server/tests/integration/test_runtime_contracts.py::test_runtime_query_payload_is_directly_accepted_by_shared_query_response`.
  The current runtime query response emits `answer`/`evidence`/`results`,
  while `contracts.QueryResponse` only accepts the older
  `observations`/`pagination` shape.
- **Expected xfail:**
  `services/context_server/tests/integration/test_runtime_contracts.py::test_runtime_timeline_item_is_directly_accepted_by_observation_v1`.
  Runtime timeline items are source-event records and do not directly contain
  the `ObservationV1` subject/predicate/value fields.

The suite also emitted one Starlette deprecation warning about using httpx via
`fastapi.testclient`; it did not fail a test.

## Real API capability and health evidence

The following acceptance probe ran against a real Uvicorn process over a
loopback socket, using isolated temporary data directories:

```powershell
uv run --locked --extra dev python -m pytest -q `
  tests\acceptance\test_api_capability_health_process.py -s
```

Result: **3 passed in 4.33s**. The probe does not start a model process or
download weights; unavailable and degraded cases use deterministic HTTP
responders so the server's health/capability classification is still measured
at the API boundary.

| API surface                      | Measured evidence                                                                                                                                                                                                                                                                                                      |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Health                           | `GET /v1/health` returned HTTP 200: `status=ok`, `service=context-server`, `version=0.1.0`, `ready=true`.                                                                                                                                                                                                              |
| Capabilities                     | `GET /v1/capabilities` returned HTTP 200 with `status=ok` and `capture-envelopes`, `content-addressed-artifacts`, `sqlite-fts5`, `hybrid-query`, `sse-events`, `websocket-events`, and `plaintext-local-archive`.                                                                                                      |
| SQLite storage                   | `GET /v1/status` reported `storage.schema_version=3`, `storage.fts5=true`, and `storage.sqlite_vec.available=false`, `state=unavailable`, `reason="sqlite_vec package is not installed"`.                                                                                                                              |
| No model endpoint                | With the endpoint unset, `/v1/status` reported `model.configured=false`; `model.status=not_configured`, `model.worker_status=not_configured`, `model.model_status=not_configured`, and `model.availability=not_configured`, with `active_workers=0` and `processed=0`.                                                 |
| Artifact count                   | A content-addressed artifact `PUT` returned HTTP 201 with the expected SHA-256 and `size_bytes=41`; download returned the same bytes; subsequent `/v1/status` reported `artifact_count=1`.                                                                                                                             |
| Queue staleness                  | `POST /v1/ingest/batches` returned HTTP 202 with one accepted capture. The isolated probe then aged that pending row to about 120 seconds; `/v1/status` reported `pending=1`, `processing=0`, `completed=0`, `failed=0`, and `queue.oldest_pending_age_seconds` and `backlog.oldest_pending_age_seconds` at least 120. |
| Configured but unavailable model | An endpoint returning HTTP 404 for `/health` and `/v1/models` produced `status=unavailable`, `model_status=unavailable`, `availability=unavailable`, `worker_status=running`, `active_workers=1`, and nested health/capability HTTP status 404.                                                                        |
| Configured degraded model        | An endpoint returning `{"status":"degraded","ready":false}` and a listed model with `supports_json_schema=false` produced `status=degraded`, `model_status=degraded`, `availability=degraded`, `worker_status=running`, and `active_workers=1`; the capability detail identified missing JSON Schema output support.   |
| Protected route auth             | With a non-loopback host and configured bearer token, public `GET /v1/health`, `/v1/capabilities`, and `/v1/status` returned HTTP 200. Protected `GET /v1/timeline` returned HTTP 401 for missing and incorrect tokens, and HTTP 200 with the correct token.                                                           |

The queue-aging step changes only the isolated probe database to create a
known stale row; the staleness claim is the resulting public `/v1/status`
response. The unavailable/degraded rows prove server behavior and diagnostics,
not real model execution, model quality, GPU utilization, latency, or a GPU
benchmark.

## Browser/API boundary

The supported local browser path is the dashboard at `http://127.0.0.1:5173/`
with the Vite `/api` proxy targeting the local server. A direct
`?api=http://127.0.0.1:8000` smoke attempt failed with browser CORS errors;
the current local server does not enable CORS for a cross-origin dashboard by
default. This is recorded as a deployment/configuration limitation, not
converted into a browser-pass claim.
The companion `tests/browser/dashboard_settings_smoke.py` settings/privacy
smoke passed through the same-origin proxy as recorded above. The expanded
`tests/browser/dashboard_security_smoke.py` passes both the local phase and the
remote HTTPS/bearer artifact-download phase. The smoke creates a short-lived
loopback certificate, gives that certificate to the temporary Vite process via
`NODE_EXTRA_CA_CERTS`, and keeps the browser on same-origin `/api` links so the
proxy can attach the bearer token. A controlled run with that CA trust removed
produced Vite `self-signed certificate` proxy errors and HTTP 500 responses;
that is a missing test/proxy trust setup, not a reason to disable TLS
verification or add CORS.

The cross-service acceptance test intentionally stops at the public query and
artifact HTTP boundary: it does not start Vite or drive the dashboard. The
browser smoke remains a separate deterministic seed-data check for rendering,
timeline/query interaction, evidence links, and artifact download; no claim
is made that the exact fake-VLM bytes from the cross-service harness were
rendered by the dashboard in one single browser journey.

## Remote and authentication boundary

The real-process probe above measures bearer authorization at the HTTP socket:

- loopback diagnostics remain usable without a bearer token;
- a configured remote bearer protects feature routes while leaving health,
  capabilities, and status public;
- the remote/self-hosted security suite also covers `require_https_remote=true`
  rejecting HTTP with 400 and requiring WSS for remote WebSocket streams; and
- collector remote routing keeps clipboard events local unless explicit remote
  clipboard opt-in is enabled.

This is not evidence of a real network deployment. No public DNS, certificate,
Internet-blocked ten-record run, hosted identity provider, CORS deployment,
managed cloud service, or production remote archive was exercised.
The real-process WebSocket authorization check passes in the deterministic
loopback probe; this is not evidence of a real network deployment.

## MVP scenario checklist

| Scenario                           | Current evidence                                                                                                                                                                                                        | Status  |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| A-01 startup and health            | Live local server health/CLI status and bootstrap checks pass; the one-tick Windows server-plus-collector smoke passed with owned process/port cleanup, but the full acceptance run was not performed.                  | Partial |
| A-02 five-second monitor cadence   | Deterministic interval tests and the ten-tick synthetic harness pass; one-monitor live capture passed, but the required multi-monitor, ten-minute Windows run was not performed.                                        | Partial |
| A-03 raw clipboard fidelity        | Exact-value/privacy tests pass; the required ten-marker collector acceptance run was not performed.                                                                                                                     | Partial |
| A-04 no audio/video                | Privacy and full-story forbidden-field checks pass; no twenty-tick device-open audit was performed.                                                                                                                     | Partial |
| A-05 no keylogging                 | Wire-level privacy tests pass; no Windows hook/raw-input audit was performed.                                                                                                                                           | Partial |
| A-06 local/self-hosted operation   | Live local run and targeted remote HTTP/security tests pass, including the real-process WebSocket authorization check; no Internet-blocked ten-record deployment run was performed.                                     | Partial |
| A-07 capture-to-query traceability | Focused harness passes ten synthetic events, query/evidence lookup, duplicate replay, and purge; the one-tick Windows collector smoke passed transport/artifact integrity, but no full Windows query run was performed. | Partial |
| A-08 deterministic routing         | Configuration and source-routing privacy tests pass; no complete deployed route selection/rejection matrix was performed.                                                                                               | Partial |
| A-09 retention                     | Storage/purge tests pass; the exact two-minute acceptance procedure was not performed.                                                                                                                                  | Partial |
| A-10 queue/backpressure/retry      | Queue metrics, retry, and transport replay tests pass; no small-capacity live acceptance run was performed.                                                                                                             | Partial |
| A-11 worker count                  | Worker configuration is tested and one worker was observed live; matched one-worker/four-worker runs were not performed.                                                                                                | Not run |
| A-12 query filters/original values | Query-filter and clipboard tests pass; the complete multi-window/multi-monitor matrix was not performed.                                                                                                                | Partial |
| A-13 monitor failure observability | No dedicated Windows cadence/error acceptance run was performed.                                                                                                                                                        | Not run |
| A-14 plaintext risk and retention  | Product/threat documentation and purge tests cover the contract; no authorized archive inspection was performed.                                                                                                        | Partial |

## Claims still intentionally not made

This refresh does not claim:

- **Live multi-monitor capture:** only one physical monitor was present in the
  live Windows run. The deterministic two-/three-monitor fixtures prove
  portable ordering, geometry, artifact, and cadence contracts, but live
  multi-monitor capture, including a negative virtual-desktop origin where
  available, remains deferred.
- **Model/GPU execution:** the fake-VLM lane uses scripted clients and no
  weights; the live endpoint is unset and inference is `not_configured`. No
  downloadable/runnable model provenance, real VLM execution, GPU utilization,
  VRAM use, latency, quality, or GPU benchmark was measured.
- **Remote deployment:** in-process loopback security tests are not a real
  second-host deployment. No public DNS, certificate/TLS deployment,
  Internet-blocked ten-record run, hosted identity provider, CORS deployment,
  or production remote archive was exercised.
- **Managed cloud:** no managed-cloud hosting, managed identity, managed
  storage, production deployment, or credentialed hosted endpoint was
  exercised.
- **Full Windows acceptance:** the complete collector matrix, clipboard
  round-trip markers, cadence duration, and full acceptance procedure were not
  run; the one-monitor one-tick smoke is recorded above.
- **Release readiness:** green unit/in-process tests, the dashboard smoke, CLI
  status, or Tauri compilation alone do not establish release acceptance.

The detailed acceptance criteria remain in [docs/ACCEPTANCE.md](ACCEPTANCE.md).
