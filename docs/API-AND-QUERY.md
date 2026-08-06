# API and query reference

The canonical local API base is `http://127.0.0.1:8000/v1`. The dashboard
normally reaches it through its same-origin `/api` proxy. Remote self-hosted
clients use the same paths over authenticated HTTPS.

## Health and lifecycle

| Method | Path            | Use                                                     |
| ------ | --------------- | ------------------------------------------------------- |
| `GET`  | `/health`       | Liveness/readiness probe                                |
| `GET`  | `/capabilities` | API, model, storage, and feature capabilities           |
| `GET`  | `/status`       | Capture, queue, model, and storage snapshot             |
| `POST` | `/start`        | Resume capture/inference lifecycle                      |
| `POST` | `/pause`        | Pause capture/inference while keeping the API available |

Example:

```powershell
$status = Invoke-RestMethod http://127.0.0.1:8000/v1/status
$status.queue
$status.model
```

## Ingestion and artifacts

| Method | Path                  | Use                                       |
| ------ | --------------------- | ----------------------------------------- |
| `POST` | `/ingest/batch`       | Idempotent capture/source-event ingestion |
| `PUT`  | `/artifacts/{sha256}` | Content-addressed artifact upload         |
| `GET`  | `/artifacts/{sha256}` | Evidence/artifact retrieval               |

The legacy/singular `/ingest/batch` route is the current implementation path;
clients should use the versioned API and shared contracts rather than building
payloads ad hoc.

## Timeline and observations

```text
GET /timeline?limit=50&start=<iso>&end=<iso>
GET /observations?limit=50&source_id=<source>
```

Timeline results are chronological context records. Query results are ranked
evidence and may include artifact URLs, source labels, timestamps, quality
metadata, and provenance.

## Query request

```json
{
  "query": "Review the last 20 minutes of activity",
  "mode": "hybrid",
  "limit": 8,
  "include_evidence": true,
  "evidence_limit": 8,
  "filters": {
    "source_ids": ["window:foreground"]
  }
}
```

Supported modes are `hybrid`, `fts`, and `semantic`. The semantic backend is
optional; when sqlite-vec is unavailable, the server reports that limitation
and uses the available lexical/recent-context path.

The query normalizer recognizes relative windows in natural language:
`past 20 minutes`, `last 2 hours`, and `past 10 days`. It applies the window
before retrieval and permits conversational or imperative wording, including
`what`, `summarize`, `recap`, `review`, `show`, and `tell` requests.

## Query response

The response contains both a structured answer and compatibility fields:

```json
{
  "query": "Review the last 20 minutes of activity",
  "answer": {
    "text": "... [1] ...",
    "confidence": 0.8,
    "status": "model_synthesized",
    "evidence_ids": ["observation:..."],
    "metadata": {
      "model_name": "gemma-4-26b-a4b-it"
    }
  },
  "answer_text": "... [1] ...",
  "results": [],
  "evidence": [],
  "meta": {},
  "warnings": []
}
```

Answer statuses include `model_synthesized`,
`fallback_evidence_summary`, and `no_results`. `answer_text` is prose and must
not be a raw JSON payload copied from terminal history or source metadata.
Evidence citations use bracketed numbers such as `[1]`; the corresponding
evidence item and `answer.evidence_ids` must remain aligned.

## Events

`GET /events/stream` provides an SSE stream; the WebSocket route is available
where enabled by the API router. Events include capture lifecycle changes,
ingestion, job progress, and status updates. Consumers must tolerate reconnect
and out-of-order source events.

## Configuration and retention

The dashboard and CLI share the server configuration contract. Important
settings include model endpoint/name/revision, worker count, queue size,
capture interval, source routing, raw/keyframe retention, remote URL, bearer
token, and HTTPS requirement.

Purge is explicit and scoped by date, source, project, or retention tier. Use a
dry run before a destructive purge. Purge is separate from queue backpressure:
queue compaction must never delete context artifacts or observations.
