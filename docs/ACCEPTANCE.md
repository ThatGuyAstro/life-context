# Life Context MVP Acceptance Criteria

Status: approved MVP baseline criteria; current execution status is tracked separately

See the [current acceptance review](ACCEPTANCE-REPORT.md) for measured
evidence from this checkout. This document defines the release gate; it does
not by itself certify that the current implementation has passed it.

Current evidence snapshot (2026-08-05): the Python suite collected 313 tests
and reported 310 passed, 1 skipped, 2 expected xfails, and 1 warning; the
latest dashboard rerun passed typecheck and production build with 33 Vitest
tests in 9 files, the focused CLI lane has 21 passing tests, and Cargo has 7
passing tests. These counts are informational and do not satisfy the release
gate; the deferred acceptance scenarios and live API diagnostics are recorded
in the report.

## 1. Purpose

These scenarios define a measurable release gate for the Windows MVP. They cover the approved scope, including five-second sampling for every monitor, raw unredacted clipboard capture, no audio/video, the no-keylogging boundary, local/self-hosted operation, configurable routing/retention/queue/workers, and the complete capture-to-query path.

An acceptance run must record the implementation version, Windows version, effective configuration, monitor registry, server bind, worker count, queue settings, retention settings, test start/end times, and the evidence named by each scenario.

## 2. Test vocabulary

- A tick is one scheduled five-second sampling opportunity.
- A capture is accepted when the ingest acknowledgment rule says it entered the configured queue.
- A persisted capture is queryable only after the worker has written the archive/index record.
- A logical duplicate is more than one query result representing the same capture identifier.
- A healthy local run means the configured server, archive, queue, and worker process are available and the test host is not intentionally resource-starved.

Unless a scenario says otherwise, use synthetic records and clipboard markers; do not use real passwords, tokens, or personal data in acceptance evidence.

## 3. Acceptance scenarios

### A-01 — Windows startup and local health

**Given** a supported Windows host and a valid local deployment configuration.

**When** the collector and local/self-hosted server are started, health is checked, and one five-second tick is allowed to complete.

**Then**:

- the collector and server start without an external cloud dependency;
- health reports the effective server bind, monitor count, queue state, retention policy, and worker count;
- every enabled monitor reports either a successful sample or an explicit sample error; and
- the test run can be stopped cleanly without leaving the server process or configured port unexpectedly active.

**Pass measure:** 1 of 1 startup, health, and clean-stop attempts succeeds; the health output identifies every enabled monitor.

### A-02 — Every monitor is sampled every five seconds

**Given** N registered and enabled deterministic test monitors, where N is at least 3.

**When** the system runs for ten minutes under healthy local conditions.

**Then** every monitor receives a scheduled sample on each expected tick, independent of the success or failure of another monitor.

**Pass measure for each monitor:** at least 118 of 120 expected samples are observed; at least 95% of inter-sample intervals are between 4.5 and 5.5 seconds; no interval exceeds 10 seconds except the explicitly recorded startup and shutdown boundaries. Any missed sample is represented by an observable scheduler or monitor error.

### A-03 — Raw unredacted clipboard fidelity

**Given** the clipboard contains a synthetic marker with mixed case, leading and trailing spaces, line breaks, Unicode, punctuation, and a password-like substring.

**When** the clipboard monitor samples it and the record is persisted.

**Then** a query for that capture returns the exact original clipboard value, with no masking, hashing, tokenization, normalization, or silent truncation.

**Pass measure:** 10 of 10 distinct markers round-trip byte-for-byte or, for Unicode text, code-point-for-code-point; the returned capture identifier and capture time are present.

### A-04 — No audio or video capture

**Given** the Windows host has an available microphone and camera, and the test harness can observe device-open calls and stored record types.

**When** the collector runs through at least 20 five-second ticks.

**Then** it does not open or stream from a microphone or camera, does not create audio/video files, and does not place audio/video bytes or media record types in the queue, archive, or query response.

**Pass measure:** zero microphone opens, zero camera opens, zero media files, zero media record types, and zero media payload bytes attributable to the product during the run.

### A-05 — No-keylogging boundary

**Given** a focused text field and a keyboard-hook/raw-input audit suitable for the Windows implementation.

**When** a synthetic marker is typed but never copied, while the collector runs through at least 20 ticks.

**Then** the product registers no global keyboard hook, low-level keyboard callback, raw keyboard input device, or key-state polling used to reconstruct text; no key-down or key-up records are produced; and the typed marker does not appear in a product capture solely because it was typed.

**Pass measure:** zero keyboard-hook/raw-input registrations attributable to the product, zero key-event records, and zero occurrences of the uncopied marker in capture payloads or query results. A separately copied marker is tested under A-03 and may appear as clipboard data.

### A-06 — Local/self-hosted operation without cloud dependency

**Given** a local server deployment with external Internet access blocked and no third-party endpoint configured.

**When** the collector captures at least 10 deterministic records, workers process them, and a local query is issued.

**Then** all 10 records can be accepted, persisted, and queried while outbound Internet access remains unavailable.

**Pass measure:** 10 of 10 capture identifiers are returned by the local query; network evidence shows no required connection outside the configured local/self-hosted server.

### A-07 — End-to-end capture-to-query traceability

**Given** a deterministic monitor emits 10 uniquely identified payloads over at least two ticks.

**When** the records pass through scheduling, envelope validation, ingest, queue, routing, worker processing, archive/indexing, and query.

**Then** each query result contains the same capture identifier, monitor identity, capture time, and original payload that were emitted by the monitor; processing status and route are available where the query contract exposes them.

**Pass measure:** 10 of 10 records are queryable within 30 seconds of successful ingest acknowledgment in a healthy local run, with zero logical duplicates and zero identifier changes.

### A-08 — Deterministic configurable routing

**Given** two configured routes and a default or explicit no-match policy, with test monitors whose records match each route.

**When** 20 records are captured, including records that match each rule and at least one no-match record.

**Then** each matching record is written only to its selected route; the route is visible in stored metadata or diagnostics; and the no-match record follows the configured reject or default behavior.

**Pass measure:** route counts and capture identifiers match the expected mapping exactly; invalid or ambiguous routing configuration is rejected before the run begins or is reported as an explicit reload failure; no capture is silently dropped.

### A-09 — Configurable retention

**Given** an archive with a retention policy of two minutes, one record older than the policy, and one record inside the policy window.

**When** the retention job runs and the archive is queried before and after the job.

**Then** the old record is removed from the queryable archive, the new record remains, and the retention action reports its eligible, removed, and failed counts.

**Pass measure:** 1 of 1 expired record is absent after successful purge; 1 of 1 in-window record remains; the purge result is observable; no unrelated record is removed.

### A-10 — Queue capacity, backpressure, retry, and acknowledgment

**Given** a queue with a deliberately small configured capacity, workers paused, and a known set of captures larger than the capacity.

**When** the collector produces the set, the queue reaches pressure, workers resume, and retries are allowed to complete.

**Then** the configured overflow behavior is visible as backpressure, retry, or explicit rejection; an accepted capture is not silently lost; and a rejected capture is reported as rejected rather than acknowledged.

**Pass measure:** every accepted capture identifier is eventually persisted or has an explicit terminal failure; every rejected identifier has an explicit rejection result; zero accepted identifiers disappear without a state transition; and retries produce at most one logical archive record per identifier.

### A-11 — Configurable worker count

**Given** the same deterministic input set and two otherwise identical runs, one with one worker and one with at least four workers.

**When** all records are processed and queried.

**Then** both runs preserve the same payloads, identifiers, routing, retention semantics, and query results; only concurrency and throughput may differ.

**Pass measure:** 100% of input identifiers are queryable in each run, with zero logical duplicates and no worker-count-dependent data changes. Health or diagnostics report the effective worker count in both runs.

### A-12 — Query filters and original values

**Given** persisted records from at least two monitors, two time windows, and three distinct text terms, including a raw clipboard record.

**When** queries are issued by time range, monitor, and text term in isolation and in combination.

**Then** each response contains only records matching all supplied filters, returns stable identifiers and timestamps, and returns the original raw clipboard value when that record matches.

**Pass measure:** expected positive and negative cases are all correct; no record outside the requested time range or monitor is returned; the raw clipboard marker is unchanged.

### A-13 — Monitor failure remains observable

**Given** at least three enabled monitors and one adapter forced to return an error for three consecutive ticks.

**When** the other monitors continue running for at least five ticks.

**Then** the healthy monitors continue to be scheduled, the failing monitor reports one explicit error per failed opportunity or an equivalent counted state, and system health distinguishes monitor failure from collector-wide failure.

**Pass measure:** 100% of healthy-monitor ticks meet A-02’s cadence criteria for the shortened run; the failing monitor exposes three or more counted errors; no failure is represented only by a silent gap.

### A-14 — Plaintext archive risk is disclosed and bounded by retention

**Given** a test clipboard marker has been persisted and the deployment is inspected by an authorized operator.

**When** the archive and deployment documentation are reviewed and the configured retention job is run.

**Then** the marker is demonstrably readable by an authorized archive reader in the MVP plaintext archive, the documentation explicitly warns that raw clipboard data may contain secrets, and the marker is removed after its retention window expires.

**Pass measure:** 1 of 1 authorized archive inspections confirms the intended plaintext behavior; the warning appears in the product/threat documentation; the expired marker is absent from query results after purge. This scenario is a risk acknowledgment, not a claim that plaintext storage is secure.

## 4. Release gates

The MVP is accepted only when:

1. A-01, A-02, A-03, A-04, A-05, A-06, and A-07 pass.
2. A-08 through A-13 pass for every supported configuration option they exercise.
3. A-14 is completed and its plaintext archive risk is acknowledged by the deployment owner.
4. No test produces an unexplained audio/video record, keyboard event, silently missing accepted capture, or changed raw clipboard value.
5. The evidence bundle contains logs or metrics for scheduling, ingest acknowledgment, queue transitions, worker processing, routing, archive/indexing, query results, and retention.

Any failure in the no-audio/video boundary, no-keylogging boundary, raw clipboard fidelity, or capture-to-query traceability is a release blocker.

## 5. Evidence record

For each scenario, retain:

- scenario identifier and pass/fail result;
- implementation and configuration versions;
- Windows host and server details;
- exact start/end timestamps;
- expected and observed counts;
- capture identifiers used for correlation;
- query requests and sanitized query responses;
- queue, worker, route, and retention diagnostics; and
- the negative-test evidence for media and keyboard boundaries.

Do not place real secrets or personal clipboard contents in the evidence bundle. Synthetic markers are sufficient to prove raw-fidelity behavior.
