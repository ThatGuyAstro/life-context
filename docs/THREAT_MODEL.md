# Life Context MVP Threat Model

Status: approved MVP baseline

## 1. Security and privacy objective

The MVP is a user-controlled, local-first capture and query system. Its primary security objective is to make the capture path explicit and controllable while keeping the data within a local or self-hosted deployment. Local hosting is a deployment choice, not a promise that sensitive data is encrypted, inaccessible to other local principals, or safe after host compromise.

The system intentionally captures raw, unredacted clipboard text. That decision creates a material plaintext archive risk and must remain visible in product, deployment, and acceptance documentation.

## 2. Scope and trust boundaries

### Components

1. Windows monitor collector and five-second scheduler.
2. Monitor adapters, including the clipboard adapter.
3. Portable capture envelope, routing, queue, retention, and query contracts.
4. Local or self-hosted ingest service.
5. Configured queue and processing workers.
6. Plaintext archive and query index.
7. Query clients and administrative configuration.

### Trust boundaries

| Boundary                                 | Data crossing it                                     | Main concern                                                     |
| ---------------------------------------- | ---------------------------------------------------- | ---------------------------------------------------------------- |
| Windows applications to monitor adapters | Context and clipboard contents                       | A user may copy credentials, tokens, or private text.            |
| Collector to server                      | Capture envelopes and errors                         | Interception, replay, loss, or unauthorized server exposure.     |
| Server to queue/workers                  | Accepted captures and processing metadata            | Backlog, duplication, misrouting, and tampering.                 |
| Workers to archive/index                 | Raw payloads and metadata                            | Persistent disclosure and retention failures.                    |
| Query client to query service            | Filters and returned records                         | Unauthorized read access and query injection or abuse.           |
| Configuration to all services            | Routing, retention, queue, worker, and bind settings | A bad or malicious policy can redirect or retain sensitive data. |

The default deployment should be local to the user-controlled host. A self-hosted deployment on another host adds the network and host trust boundaries of that deployment.

## 3. Assets

| Asset                                              | Sensitivity                                                             | Required property                                                               |
| -------------------------------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| Raw clipboard payloads                             | High; may contain passwords, tokens, personal data, or proprietary text | Preserve exactly when captured, but clearly warn that the archive is sensitive. |
| Other monitor payloads                             | Depends on the monitor                                                  | Preserve capture identity and source context.                                   |
| Capture identifiers and timestamps                 | Medium                                                                  | Keep stable and queryable for traceability.                                     |
| Archive and query index                            | High                                                                    | Limit access, enforce retention, and make exposure explicit.                    |
| Queue contents and retry state                     | High                                                                    | Do not silently lose or duplicate accepted logical captures.                    |
| Routing and retention configuration                | High                                                                    | Validate changes and make effective policy observable.                          |
| Server credentials or access-control configuration | High                                                                    | Protect from unauthorized query and administrative access.                      |
| Health, error, and audit diagnostics               | Medium to high                                                          | Avoid leaking full raw payloads into logs unless explicitly configured.         |

## 4. Actors and assumptions

### Actors

- The authorized owner or administrator who configures and queries the system.
- Another local user with filesystem or service access.
- A malicious process running as the user or with elevated Windows privileges.
- A network attacker when a self-hosted server is bound beyond loopback.
- A compromised dependency, worker, or deployment host.
- An accidental operator who selects an unsafe route, retention period, or bind setting.

### Assumptions

- The user controls the Windows machine or the self-hosted server.
- The operating system and service account enforce the baseline filesystem and process permissions.
- A fully compromised host can read process memory, clipboard contents, configuration, and a plaintext archive; the MVP cannot defend against that condition.
- The MVP does not depend on an external cloud service for capture, storage, or query.
- The monitor registry defines the set of monitors that must be scheduled; disabled monitors are intentionally outside a run.

## 5. Explicit high-risk decisions

### Plaintext archive risk

The MVP archive is allowed to contain raw, unredacted clipboard text in plaintext. Anyone who can read the archive files, database, index, backups, queue spill files, or an equivalent self-hosted storage layer may be able to read the copied content. Clipboard contents may include credentials, session tokens, financial information, health information, or private communications.

PowerShell history is a separate, bounded source. It reads persisted PSReadLine
commands rather than keyboard events, but commands can still contain passwords,
tokens, or other sensitive text. It is enabled by default for the collector;
disable it with `COLLECTOR_INCLUDE_POWERSHELL_HISTORY=false` or route the
`powershell` source to the local spool before using a remote collector URL.

This is an accepted MVP risk, not a missing requirement. The MVP does not claim application-level encryption at rest or automatic secret redaction. Local/self-hosted placement limits third-party exposure but does not protect against:

- another local account with read access;
- an administrator or backup operator;
- malware running with sufficient privileges;
- a stolen disk or backup when host/disk encryption is absent; or
- an intentionally exposed server or query credential.

Minimum controls for an MVP deployment are least-privilege filesystem and service permissions, a documented local bind default, explicit warnings for non-local exposure, configurable retention, and avoidance of raw payloads in routine logs. Host or disk encryption, secret redaction, and stronger per-record authorization are follow-on controls unless separately implemented and verified.

### No-keylogging boundary

The system must not install or use global keyboard hooks, low-level keyboard callbacks, raw keyboard input, key-state polling for text reconstruction, or application text-field scraping intended to recover keystrokes. It must not record key-down or key-up events.

Clipboard capture remains allowed because it reads the clipboard value at a scheduled sample. If a user types a secret and then copies it, the copied value may enter the raw clipboard archive; that is clipboard capture, not keylogging. If a user types text without copying it, the system must not capture the keystroke sequence or reconstruct the text from keyboard events.

This boundary does not prevent a separate, explicitly configured non-keyboard monitor from recording its own metadata, such as an application or window identifier. It does prevent that monitor from becoming a covert keystroke channel.

## 6. Threats and controls

| ID   | Threat                                                                       | Impact                                                                   | MVP control or required behavior                                                                                                                                                                                 | Residual risk                                                                         |
| ---- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| T-01 | A local reader or malware reads the plaintext archive or queue.              | Disclosure of all retained sensitive context, especially clipboard data. | Local/self-hosted deployment, least-privilege permissions, explicit risk documentation, configurable retention, no default payload logging.                                                                      | High; host compromise and authorized archive access remain in scope.                  |
| T-02 | A server is exposed on a network without deliberate operator action.         | Remote query or ingest access.                                           | Local bind by default; non-local bind must be explicit, require a bearer token and HTTPS, and report effective bind settings. Collector HTTP delivery is limited to loopback; remote dashboard frames use HTTPS. | Depends on authentication, TLS, firewall, and host configuration.                     |
| T-03 | A route sends captures to an unintended collection or host.                  | Data disclosure or policy violation.                                     | Deterministic routing, explicit default/reject behavior, configuration validation, route identity in diagnostics and records.                                                                                    | An authorized operator can still configure an unsafe route.                           |
| T-04 | Retention is missing, misapplied, or fails silently.                         | Sensitive data remains longer than intended.                             | Per-policy retention configuration, observable purge results, tests proving old records disappear while new records remain.                                                                                      | Backup copies and filesystem recovery may outlive archive retention.                  |
| T-05 | Queue pressure drops captures without telling the operator.                  | Loss of timeline and false confidence in completeness.                   | Explicit acknowledgment boundary, visible accepted/rejected/pending/failed states, configured backpressure or overflow behavior.                                                                                 | Events not yet accepted before a process or host failure may be lost.                 |
| T-06 | Retries or multiple workers create duplicate logical records.                | Incorrect history and inflated query results.                            | Stable capture identifier, idempotent archive behavior, worker-count and restart tests.                                                                                                                          | A downstream store without idempotency can still duplicate physical copies.           |
| T-07 | A malicious or accidental change enables keylogging.                         | Privacy boundary violation and collection of keystrokes.                 | No keyboard-hook or raw-input code path, API/dependency review, negative typing test, explicit no-keylogging requirement.                                                                                        | A compromised host or separately installed software can keylog outside this product.  |
| T-08 | A media path is added or invoked accidentally.                               | Unapproved audio/video collection.                                       | No microphone/camera open calls, no media adapters or media envelope types, negative device-availability test.                                                                                                   | Other applications on the host may still use those devices.                           |
| T-09 | Clipboard content is redacted, hashed, truncated, or transformed.            | Loss of the approved raw-context behavior and query fidelity.            | Exact-value test using whitespace, Unicode, and sensitive-looking synthetic data; schema review.                                                                                                                 | Clipboard APIs or storage limits outside the product may impose platform constraints. |
| T-10 | An attacker injects malformed or query-like content into a captured payload. | Query corruption, service errors, or unsafe rendering.                   | Treat captured values as data, validate envelopes, parameterize query operations, bound resource use, and keep diagnostics separate from payload.                                                                | A future UI must still safely render untrusted text.                                  |
| T-11 | A server or worker is tampered with.                                         | Capture alteration, route changes, or archive compromise.                | Configuration and health visibility, stable identifiers, traceable status transitions, host hardening assumptions.                                                                                               | A privileged attacker can alter the system and its records.                           |
| T-12 | A monitor silently stops while the rest of the system appears healthy.       | False completeness of the archive.                                       | Per-monitor tick/error health, cadence metrics, and acceptance test covering every registered monitor.                                                                                                           | A host-wide failure still affects all monitors.                                       |

## 7. Security invariants for release

The following are release-blocking invariants:

1. No microphone, camera, audio, video, global keyboard-hook, or raw-keyboard capture path is present in the MVP.
2. Raw clipboard text is preserved without default redaction or silent transformation.
3. The archive risk is disclosed wherever a deployment is described.
4. Local/self-hosted operation works with external network access unavailable.
5. Non-local server exposure is explicit and does not bypass configured access control.
6. A capture can be traced by one stable identifier from scheduled sample through query result.
7. Accepted queue items are not silently lost, and retries do not create duplicate logical records.
8. Retention and routing behavior are observable and testable.

## 8. Out of scope for the MVP threat claim

The MVP does not claim protection from a fully compromised Windows host, a local administrator, a user who is authorized to read the archive, stolen unencrypted backups, malicious software outside the product, or a self-hosted server exposed without appropriate network security. Those conditions require host, storage, identity, and deployment controls beyond the core capture pipeline.
