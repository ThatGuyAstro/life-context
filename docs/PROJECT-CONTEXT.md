# Life Context project context

## Purpose

Life Context is a local-first, universally queryable activity archive for a
Windows workstation. It captures a five-second, all-monitor visual stream plus
application and source metadata, then makes the resulting context searchable
with evidence-backed natural-language answers.

The product is designed for coding and knowledge work: recovering where a task
was left, identifying recent projects and tools, finding related screenshots or
Git changes, and answering questions about a bounded activity window.

## Deliberate boundaries

- Windows is the tested runtime; adapter interfaces leave room for macOS later.
- No audio, video, or raw keystroke logging.
- Raw screenshots and clipboard text are plaintext locally by explicit MVP
  choice; clipboard is local-only for remote routing by default.
- Local server mode binds to loopback. Remote self-hosting requires authenticated
  HTTPS; managed cloud deployment is out of scope.
- Model files are configured by the operator. There is no silent download or
  replacement.
- Continuous semantic analysis may lag capture. The UI must show that distinction
  instead of claiming a real-time SLA.

## Current implementation state

Implemented end-to-end:

- Tauri v2 Windows shell boundary with tray/service supervision.
- Python collector with all-monitor screenshots, contact sheets, foreground
  window/process data, filesystem/Git/PowerShell/browser/clipboard adapters,
  spool/retry, and source routing.
- FastAPI/SQLite/FTS5 server with content-addressed artifacts, durable jobs,
  retention and purge, health/status/capability routes, timeline, query, and
  event streaming.
- OpenAI-compatible llama.cpp model integration with Gemma 4 26B target,
  strict observation validation, provenance, reasoning-output protection, and
  deterministic fallback.
- Relative-time query windows, humanized terminal/Git evidence, evidence
  citations, and visible dashboard loading state.
- Queue backpressure that bounds repeated visual and metadata inference without
  deleting source context.

## Important lessons from live validation

1. A `200` query response is not enough: inspect answer status, evidence count,
   rendered answer, and citations.
2. Natural-language time windows must be recognized independently of grammar;
   imperative wording such as `review the last 20 minutes` is valid.
3. Terminal history snapshots are cumulative telemetry. A recap must label them
   as snapshots and must not claim every listed command ran in the requested
   interval.
4. Gemma 4 may spend its output budget on private reasoning. Empty content must
   trigger a bounded retry or readable fallback; reasoning text must never leak.
5. Capture and semantic indexing are separate clocks. A large inference queue
   does not imply lost captures, but it does imply stale semantic answers.
6. Evidence selection must favor diverse, recent, meaningful activity over
   repeated process metadata and unchanged Git snapshots.

## Known limitations

- sqlite-vec is optional and may be unavailable; semantic retrieval then reports
  its limitation and uses lexical/recent-context retrieval.
- A screenshot artifact is not automatically visual understanding. The query
  model sees derived observations or metadata unless multimodal evidence is
  explicitly wired into the query path.
- A single RTX 3090 and one default worker limit throughput. Benchmark queue
  growth, latency, and quality before raising worker count.
- Full multi-monitor Windows acceptance, sustained model-quality benchmarks,
  and a real second-host remote deployment remain separate acceptance work.
- Four historical failed/dead-lettered jobs may remain after the live queue is
  caught up; they require review rather than being silently hidden.

## Recommended next work

- Add a supported operator command/API for clearing pending inference jobs with
  an audit reason, rather than relying on manual maintenance.
- Improve activity-evidence ranking and visual observation freshness.
- Add sqlite-vec installation/migration tests for supported Windows builds.
- Run a sustained Gemma benchmark with queue growth, VRAM, latency, throughput,
  failure rate, and answer-quality measurements.
- Complete Tauri packaging and multi-monitor capture acceptance on the target
  Windows machine.

## Verification baseline

The authoritative current verification is generated at development time. Run:

```powershell
uv run --locked pytest -q
pnpm --dir apps/dashboard run test
pnpm --dir apps/dashboard run typecheck
pnpm --dir apps/dashboard run build
```

Do not copy old test counts into new status reports without rerunning them.
Record date, model revision, endpoint, worker count, queue state, and whether
the test used a fake model or live GPU inference.
