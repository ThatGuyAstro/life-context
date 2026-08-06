# Life Context developer reference

This is the implementation guide for contributors working on the local-first
Windows MVP. It describes the seams that should remain stable, where features
belong, how to run focused checks, and how data moves through the system.

## Repository map

| Area                | Location                    | Responsibility                                               |
| ------------------- | --------------------------- | ------------------------------------------------------------ |
| Shared contracts    | `packages/contracts`        | Pydantic models, JSON Schema, TypeScript declarations        |
| Windows collector   | `services/collector`        | Screens, source adapters, spool, upload/retry                |
| Context server      | `services/context_server`   | FastAPI, SQLite, FTS5, artifacts, jobs, inference, retrieval |
| Browser dashboard   | `apps/dashboard`            | React/Vite UI, same-origin API proxy, live status            |
| Desktop shell       | `apps/desktop`              | Tauri tray, lifecycle supervision, embedded dashboard        |
| CLI                 | `apps/cli`                  | `contextctl` HTTP client and operator commands               |
| Acceptance tests    | `tests`, `services/*/tests` | Unit, integration, process, browser, and full-story checks   |
| Operational scripts | `scripts`                   | Runtime preflight, local launcher, model/collector helpers   |

## Runtime data flow

```text
Windows capture tick
  -> CaptureEnvelope + SourceEvent
  -> collector spool (when delivery is unavailable)
  -> POST /v1/ingest/batch and PUT /v1/artifacts/{sha256}
  -> SQLite sources/captures/observations/artifacts/jobs
  -> durable inference worker and OpenAI-compatible VLM
  -> validated ObservationV1 and provenance
  -> FTS5 / optional sqlite-vec retrieval
  -> evidence-grounded QueryResponse
  -> dashboard, CLI, REST, SSE/WebSocket
```

Capture records are durable independently of inference. A slow or unavailable
model must not remove screenshots, source events, clipboard records, or their
artifact references.

## Stable boundaries

### Contracts

Add or change shared request/response fields in
`packages/contracts/python/contracts/models.py` first. Update the JSON Schema
and TypeScript declaration in the same change. Keep compatibility aliases in
`services/context_server/api/schemas.py` when an existing client uses a
different field spelling.

Every model-derived observation must retain its model name, model revision,
prompt revision/hash, raw response, validation status, confidence, and request
provenance. Never make the dashboard depend on an unvalidated model payload.

### Collector

Collector adapters emit normalized events; they do not write directly to the
server database. Source adapters must respect the privacy boundary:

- no audio, video, or raw keystroke logging;
- clipboard text is intentionally plaintext and local-only by default;
- screenshots are individual monitor artifacts plus a contact sheet;
- retries use stable event/artifact identities so replay is idempotent.

Add a new source under `services/collector/sources`, cover normalization with
unit tests, then add a live/acceptance test only when the Windows dependency is
available.

### Server and storage

`services/context_server/runtime.py` is the composition seam, not the place to
put a second storage implementation. Storage schema and migrations belong in
`services/context_server/storage`; inference job transitions belong in
`services/context_server/inference`; query ranking and synthesis belong in
`services/context_server/query` or the runtime adapter that supplies evidence.

Migrations are forward-only and recorded in `schema_migrations`. Do not edit an
existing migration after it has shipped. Add a migration and recovery test.
Artifact bytes are immutable SHA-256 files; never overwrite an existing digest.

### Dashboard

The dashboard uses the typed client in `apps/dashboard/src/api.ts` and the
types in `apps/dashboard/src/types.ts`. Keep loading, error, stale-index, and
offline states visible. A query can take 20–30 seconds on a local reasoning
model; the UI must show progress and must not look inert.

The dashboard should use same-origin `/api` in local development. Direct
cross-origin mode requires explicit CORS and must not embed a production bearer
token in a public browser build.

## Query behavior

The query path canonicalizes natural-language relative windows such as:

- `past 20 minutes`
- `last 2 hours`
- `past 10 days`

When a relative window is present, conversational retrieval is enabled even if
the wording is imperative (`review`, `show`, or `tell me`) rather than a `what`
question. Retrieval returns evidence first; the model receives bounded,
human-readable excerpts and must answer only from those excerpts. If model
synthesis fails, the deterministic fallback must remain readable and must not
dump raw terminal JSON into `answer_text`.

Evidence quality matters more than a large result count. Prefer derived
observations, changed application/window state, browser context, meaningful
Git changes, and visual evidence. Limit repeated process metadata and
cumulative shell history. Raw screenshots are evidence artifacts; metadata
alone is not a claim that the model saw the pixels.

## Queue semantics

Inference jobs are durable SQLite rows with explicit pending, running,
succeeded, retry, and dead-letter states. The worker owns leases and retries;
do not update a running job without verifying its lease.

Backpressure currently keeps a rolling visual window and one newest pristine
metadata job per stable source/event kind. Superseded inference jobs are marked
as successful no-ops with a reason; the original capture, observation, and
artifact remain intact. Retried, failed, or running work is not silently
superseded. This prevents repeated five-second snapshots from growing without
bound while keeping the archive queryable.

The queue's pending count measures semantic analysis work, not capture loss.
Always expose pending count, oldest pending age, processed rate, failed count,
worker state, and model state together.

## Development commands

Run from the repository root:

```powershell
uv lock --check
uv sync --locked --extra dev
pnpm install --frozen-lockfile

uv run --locked pytest -q
pnpm --dir apps/dashboard run test
pnpm --dir apps/dashboard run typecheck
pnpm --dir apps/dashboard run build
pnpm --dir apps/desktop run typecheck
pnpm --dir apps/desktop run build
cargo check --manifest-path apps/desktop/src-tauri/Cargo.toml --locked
```

Focused checks are preferable while iterating:

```powershell
uv run pytest services/context_server/tests/query -q
uv run pytest services/context_server/tests/inference -q
uv run pytest services/collector/tests -q
pnpm --dir apps/dashboard exec vitest run src/App.test.tsx
```

Use `scripts/check-runtime.ps1` before Tauri work. GPU/model benchmarks are
serialized: do not launch a second llama.cpp/Gemma workload while the measured
model server is running.

## Adding a feature

1. Define the contract and privacy/data-retention behavior.
2. Add the smallest implementation in its owning subsystem.
3. Add deterministic tests with a fake model or in-memory SQLite.
4. Add process/browser coverage only for the cross-boundary behavior.
5. Update the relevant reference document and acceptance status.
6. Run the focused suite, then the affected package suite.

Keep changes reversible and avoid committing local databases, model files,
spools, credentials, screenshots, or runtime logs.
