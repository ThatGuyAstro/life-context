# Life Context MVP Product Definition

Status: approved MVP baseline

## 1. Product intent

Life Context is a user-controlled, local-first system that samples configured context monitors on a Windows machine, archives the resulting records, and makes them searchable through a local or self-hosted server. The MVP is designed to preserve a traceable path from an individual capture to the record returned by a query.

The product is for the owner or administrator of the monitored machine. It is not a cloud-managed surveillance service, a keylogger, or a media recorder.

## 2. Approved MVP scope

| Area                  | MVP contract                                                                                                                                                                                             |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Implementation target | The running collector and server are implemented for Windows.                                                                                                                                            |
| Future portability    | The capture, envelope, routing, queue, archive, and query contracts are platform-neutral. Windows APIs belong behind replaceable adapters so a future port does not require changing the core data flow. |
| Sampling              | Every registered and enabled monitor is sampled on a fixed five-second cadence. A monitor must not be silently omitted because another monitor is unavailable.                                           |
| Media                 | No audio or video capture. The MVP does not open microphones or cameras and does not store audio samples, camera frames, video streams, or video files.                                                  |
| Clipboard             | Clipboard text is captured as raw, unredacted content at sampling time. The MVP does not mask, tokenize, hash, summarize, or silently truncate it.                                                       |
| Server                | The server is local or self-hosted and user-controlled. The core capture-to-query flow must work without a third-party cloud service.                                                                    |
| Operations            | Routing, retention, queue behavior, and worker count are configurable.                                                                                                                                   |
| Query                 | Persisted captures can be queried by time and monitor, with text matching where the stored record has text. Query results retain capture identity and capture time.                                      |

For this document, all monitors means every monitor present in the configured monitor registry and enabled for the test run. The monitor registry is the source of truth; adding a monitor adapter adds it to the same five-second scheduling contract.

## 3. Functional requirements

### P-01 — Windows implementation

The MVP collector and local/self-hosted server start, run, stop, and report health on the supported Windows environment. Windows-specific scheduling, clipboard access, process or window inspection, and transport integration are isolated behind core interfaces.

### P-02 — Future-portable core

The portable core owns monitor contracts, capture envelopes, identifiers, routing decisions, queue semantics, retention decisions, archive/query contracts, and error semantics. Operating-system code is limited to adapters for capture and platform services.

At minimum, the core must not require a Windows-only API to:

1. validate a capture envelope;
2. route a capture;
3. enqueue or retry a capture;
4. persist or expire a capture; or
5. execute a query.

### P-03 — Five-second sampling

The scheduler runs every registered and enabled monitor once per five-second tick. Each emitted record identifies the monitor and the tick that produced it. A failed sample is observable as a monitor error or health result; it is not silently treated as if the monitor had not been scheduled.

The cadence is fixed for the MVP. Routing, retention, queue, and worker settings are configurable; a configuration must not silently change the sampling interval.

### P-04 — Explicit media exclusion

The collector has no audio or video capture path. It must not request microphone or camera access, open a media stream, write media files, or place media bytes in a capture envelope. This boundary applies even when a microphone or camera is present and available on the Windows host.

### P-05 — Raw clipboard capture

When the clipboard monitor observes text, the stored clipboard value must equal the captured value exactly, including case, whitespace, line breaks, Unicode characters, and sensitive-looking strings. There is no default redaction or content classification in the MVP.

Clipboard capture is a read of the clipboard at a scheduled sample. It is not a read of keyboard input, a keyboard buffer, or an application’s internal text field.

### P-06 — Local/self-hosted service

The server can be run on the same Windows machine as the collector or on infrastructure controlled by the user. A default local deployment must continue to ingest and query with outbound Internet access unavailable. No third-party hosted service is required for capture, storage, or query.

If the server is exposed beyond the local machine, deployment documentation and configuration must make that exposure explicit and apply the server’s configured access control. Local hosting reduces network exposure; it does not make the archive safe from a compromised host or an authorized local user.

### P-07 — Configurable routing

Routing rules select the destination or collection for a capture using stable metadata such as monitor identity, record type, and time or policy attributes. The configuration must provide:

- an explicit default or rejected-route behavior;
- deterministic rule ordering;
- a way to identify the selected route in stored metadata and diagnostics; and
- startup or reload validation for invalid or ambiguous rules.

A capture that cannot be routed is reported as an error or rejected according to policy. It is never silently discarded.

### P-08 — Configurable retention

Retention policies are configurable per archive, route, or collection as supported by the implementation. A retention run identifies records eligible for removal, removes records older than the configured policy, and leaves records inside the retention window queryable.

The policy must be inspectable at runtime or in deployment configuration, and a retention action must be observable in logs or metrics. The MVP does not claim indefinite retention.

### P-09 — Configurable queue behavior

Queue capacity, retry behavior, backoff, and overflow or backpressure behavior are configuration concerns. The queue must expose enough state to distinguish accepted, pending, retried, rejected, and failed captures.

The ingest acknowledgment boundary must be explicit. A capture is acknowledged only after the configured queue has accepted it, or the client receives an explicit failure. Retries use the stable capture identity so a replay cannot create an accidental second logical record.

### P-10 — Configurable workers

The number of processing workers is configurable within implementation limits. Worker count changes concurrency and throughput but must not change record meaning, routing, retention policy, or query semantics. A worker restart or scale change must not create duplicate logical records for the same capture identity.

### P-11 — Capture envelope and traceability

Every capture carries, at minimum:

- a unique capture identifier;
- monitor identity and record type;
- capture time in UTC;
- schema or envelope version;
- the captured payload or an explicit capture error; and
- route and processing status once those decisions exist.

The identifier remains stable across retries and is available in archive records, diagnostics, and query results.

### P-12 — Queryable archive

The query interface returns only records that have reached the configured archive/index stage. It supports:

- a time range;
- monitor or record-type filtering; and
- text matching for records containing text.

Results include capture identifier, capture time, monitor identity, route or collection when available, and the original stored payload subject to the query contract. A query must not silently redact clipboard content that was intentionally archived raw.

## 4. Full capture-to-query flow

The MVP flow is:

1. The Windows scheduler reaches a five-second tick.
2. Every registered and enabled monitor is invoked for that tick.
3. Each adapter emits a capture or an explicit capture error. The clipboard adapter includes the raw, unredacted value when text is present; no audio or video adapter exists.
4. The portable core validates the capture envelope and assigns or preserves its stable capture identifier.
5. The collector sends the envelope to the local or self-hosted ingest endpoint.
6. Ingest applies the acknowledgment rule and places accepted captures into the configured queue.
7. A configurable worker takes a queued capture, applies the configured routing rule, and writes the archive/index record.
8. The archive/index exposes the persisted record and its processing status.
9. A query client submits time, monitor, and optional text filters to the local/self-hosted query interface.
10. The query interface returns the matching persisted records, including the same capture identifier and capture time that originated at step 3.

The flow must be diagnosable at each boundary. A capture that is rejected, retried, expired, or failed must be distinguishable from a capture that was never scheduled.

## 5. Configuration surface

The implementation may choose the file or API format, but the deployment must expose these concepts:

| Configuration    | Required effect                                                                                                 |
| ---------------- | --------------------------------------------------------------------------------------------------------------- |
| Monitor registry | Identifies all available monitors and which are enabled for a run.                                              |
| Routing          | Maps accepted captures to destinations, routes, or collections and defines the no-match behavior.               |
| Retention        | Defines the retention window and the archive/collection to which it applies.                                    |
| Queue            | Defines capacity, retry count or duration, backoff, and overflow/backpressure behavior.                         |
| Workers          | Defines processing worker count and reports the effective count.                                                |
| Server           | Defines local/self-hosted bind and connection settings plus any required access control for non-local exposure. |

The five-second cadence is an MVP invariant, not a hidden side effect of routing or worker configuration.

## 6. Deliberate non-goals

The following are outside the approved MVP:

- a mandatory third-party cloud backend;
- audio recording, microphone monitoring, camera capture, or video recording;
- global keyboard hooks, raw key-event capture, or reconstruction of keystrokes;
- default clipboard redaction or secret detection;
- a cross-platform collector implementation in the first release;
- a claim that local hosting protects data from host compromise, local administrators, or users who can read the archive; and
- indefinite storage without a configured retention policy.
