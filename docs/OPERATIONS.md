# Life Context operations runbook

This runbook is for the local Windows deployment and a self-hosted HTTPS
server. The default local services bind to loopback and are intentionally not
publicly reachable.

## Service topology

| Service     | Default endpoint/process       | Purpose                               |
| ----------- | ------------------------------ | ------------------------------------- |
| Dashboard   | `http://127.0.0.1:5173/`       | React/Vite UI                         |
| Context API | `http://127.0.0.1:8000/v1`     | Ingest, storage, query, lifecycle     |
| Model       | `http://127.0.0.1:8080/v1`     | Local llama.cpp OpenAI-compatible VLM |
| Collector   | `python -m services.collector` | Windows capture and source adapters   |

The SQLite database and content-addressed artifacts default to
`E:\LifeContext\data`; the collector spool defaults to `E:\LifeContext\spool`.
These are runtime data, not repository files.

## Start

For a browser/API development session:

```powershell
pnpm start
```

This starts the local server and dashboard. Start the collector separately:

```powershell
.\scripts\start-collector.ps1
```

Start the Gemma server with the checked-in launcher when a local model is
available:

```powershell
.\scripts\start-gemma-26b.ps1
```

Verify each boundary:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/health
Invoke-RestMethod http://127.0.0.1:8000/v1/status
Invoke-WebRequest http://127.0.0.1:5173/ | Select-Object StatusCode
Invoke-RestMethod http://127.0.0.1:8080/health
```

The model may be intentionally stopped while capture and query tests run.
The API reports `model_status`, `worker_status`, and queue metrics separately.

## Stop safely

Stop only processes whose command lines belong to this checkout or the
configured Gemma model. Do not stop every `python.exe`, `node.exe`, or
`llama-server.exe` on a development machine.

```powershell
Get-NetTCPConnection -State Listen -LocalPort 5173,8000,8080 |
  Select-Object LocalPort,OwningProcess
Get-CimInstance Win32_Process -Filter "ProcessId = <PID>" |
  Select-Object ProcessId,ParentProcessId,Name,CommandLine
```

The local launcher can be restarted with:

```powershell
.\scripts\restart-local.ps1
```

This script intentionally does not manage the model server. The Tauri shell
owns the full server/collector lifecycle when running as a desktop app.

## Queue and backlog interpretation

`pending` means accepted inference work waiting for semantic analysis. It does
not mean that capture failed or that raw context was discarded. A healthy
backlog view should be read as:

- capture/session state: whether new events are arriving;
- pending/oldest age: semantic-index freshness;
- processing/processed rate: worker progress;
- failed/dead-lettered: historical model or validation failures;
- model status: endpoint availability and selected revision.

The queue is bounded by backpressure. Newest pristine visual/metadata evidence
is retained; stale redundant inference jobs are recorded as superseded no-ops.
Raw captures and source events remain durable. Do not delete SQLite job rows to
clear a queue because that destroys operational history; use an operator-clear
operation that records a reason if a future CLI/API command is added.

## Query troubleshooting

If a query appears to do nothing:

1. Confirm the browser page is `http://127.0.0.1:5173/` and reload it.
2. Check that the dashboard proxy and API health both return 200.
3. Submit and wait for the visible `Reviewing your recent activity` state.
4. Inspect `/v1/status` for model and queue state.
5. Check the server access log for `POST /v1/query` and its status code.

Relative-time requests such as `review the last 20 minutes of activity` are
translated into an observation window. A successful answer may explicitly say
that the archive has only one or a few timestamps; it must not invent a
continuous timeline when the collector did not record one.

If `sqlite_vec` is unavailable, lexical/recent-context retrieval still works.
The API should report semantic retrieval as unavailable rather than claiming
vector search ran.

## Model operations

The target local model is Gemma 4 26B A4B Q4 GGUF served by llama.cpp. The
configured endpoint must be loopback HTTP or remote HTTPS. Model files are
operator-managed; this project never downloads or silently replaces them.

Gemma can emit a private reasoning trace that consumes the output budget before
user-facing content. The client detects empty content, retries once with
reasoning disabled when supported, and never exposes `reasoning_content` as the
answer. Query synthesis uses bounded evidence excerpts and stores provenance.

Do not increase worker count on a 24 GB GPU without measuring VRAM, latency,
throughput, queue growth, and failure rate. One worker is the safe default.

## Privacy and retention

The local archive intentionally stores raw screenshots and clipboard text in
plaintext according to the selected MVP requirement. There is no audio, video,
or raw keystroke capture. Clipboard routing is local-only by default.

Before enabling a remote server:

- use HTTPS and bearer authentication;
- verify the remote host and certificate policy;
- route clipboard explicitly, or keep it local-only;
- review source routing and retention settings;
- never commit tokens, private keys, screenshots, databases, or spool files.

Default retention is raw screenshots 24 hours, keyframes/evidence 30 days, and
derived observations/source metadata/embeddings indefinitely. Purge operations
must be explicit and should be previewed with a dry run.

## Recovery

The server can restart without losing committed captures, artifacts, or job
rows. The collector spool replays idempotently after an offline period. If a
job lease expires, the worker recovery path returns it to retryable state. Keep
the data directory backed up before migrations or retention changes.
