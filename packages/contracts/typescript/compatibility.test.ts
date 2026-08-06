import type {
  ArtifactKind,
  ArtifactRef,
  BacklogStatus,
  CaptureEnvelope,
  HealthSnapshot,
  HealthState,
  JsonValue,
  ObservationKind,
  ObservationStatus,
  ObservationV1,
  QueryRequest,
  QueryResponse,
  QuerySortOrder,
  SourceEvent,
  SourceEventType,
  SourceKind,
} from "./index";

const rawClipboard = "  λ\u00a0🧠\r\n\tindented line  \ntrailing-space:   ";
const timestamp = "2026-08-05T16:00:00Z";
const rawValue: JsonValue = { raw_text: rawClipboard };

const artifact: ArtifactRef = {
  artifact_id: "artifact-1",
  kind: "text",
  uri: "memory://artifact-1",
  media_type: "text/plain",
  size_bytes: 12,
  sha256: "a".repeat(64),
  metadata: { source: "clipboard" },
};

const sourceEvent: SourceEvent = {
  event_id: "event-1",
  source_id: "clipboard:local",
  source_kind: "manual",
  event_type: "observed",
  occurred_at: timestamp,
  captured_at: timestamp,
  payload: {
    raw_text: rawClipboard,
    nested: [rawClipboard],
  },
  artifacts: [artifact],
  metadata: { raw_text: rawClipboard },
};

const envelope: CaptureEnvelope = {
  envelope_id: "capture-1",
  schema_version: "1",
  captured_at: timestamp,
  source_kind: "manual",
  events: [sourceEvent],
  artifacts: [artifact],
  producer: "capture-service",
  metadata: { raw_text: rawClipboard },
};

const observation: ObservationV1 = {
  schema_version: "1",
  observation_id: "observation-1",
  subject: "person-1",
  predicate: "clipboard.text",
  value: rawValue,
  kind: "fact",
  status: "candidate",
  confidence: 0.9,
  observed_at: timestamp,
  source_event_id: sourceEvent.event_id,
  evidence: [artifact],
  metadata: { source_event_id: sourceEvent.event_id },
};

const request: QueryRequest = {
  query: "clipboard",
  subject: "person-1",
  predicate: "clipboard.text",
  source_ids: [sourceEvent.source_id],
  kinds: ["fact"],
  statuses: ["candidate"],
  observed_after: "2026-08-05T15:00:00Z",
  observed_before: timestamp,
  sort_order: "descending",
  limit: 25,
  cursor: "cursor-1",
  include_evidence: true,
};

const response: QueryResponse = {
  request_id: "request-1",
  observations: [observation],
  total_count: 1,
  next_cursor: "cursor-2",
};

const backlog: BacklogStatus = {
  pending_count: 1,
  processing_count: 2,
  completed_count: 3,
  failed_count: 0,
  oldest_pending_at: timestamp,
  updated_at: timestamp,
};

const health: HealthSnapshot = {
  service: "query-service",
  status: "healthy",
  checked_at: timestamp,
  version: "0.1.0",
  components: { database: "healthy" },
  backlog,
  message: "ready",
  details: { raw_text: rawClipboard },
};

const allArtifactKinds: ArtifactKind[] = [
  "text",
  "image",
  "audio",
  "video",
  "document",
  "archive",
  "structured",
  "other",
];
const allSourceKinds: SourceKind[] = [
  "file",
  "email",
  "calendar",
  "message",
  "note",
  "web",
  "application",
  "sensor",
  "manual",
  "other",
];
const allEventTypes: SourceEventType[] = [
  "created",
  "updated",
  "deleted",
  "observed",
];
const allObservationKinds: ObservationKind[] = [
  "fact",
  "measurement",
  "classification",
  "relation",
  "summary",
];
const allObservationStatuses: ObservationStatus[] = [
  "candidate",
  "confirmed",
  "retracted",
];
const allSortOrders: QuerySortOrder[] = ["ascending", "descending"];
const allHealthStates: HealthState[] = [
  "healthy",
  "degraded",
  "unhealthy",
  "unknown",
];

const values: JsonValue[] = [
  rawValue,
  rawClipboard,
  ...allArtifactKinds,
  ...allSourceKinds,
  ...allEventTypes,
  ...allObservationKinds,
  ...allObservationStatuses,
  ...allSortOrders,
  ...allHealthStates,
];

void values;

// @ts-expect-error Source events require their canonical identifier.
const missingEventId: SourceEvent = {
  source_id: "clipboard:local",
  source_kind: "manual",
  event_type: "observed",
  occurred_at: timestamp,
};

const invalidSourceKind: SourceEvent = {
  event_id: "event-2",
  source_id: "clipboard:local",
  // @ts-expect-error Contract enums do not accept arbitrary source kinds.
  source_kind: "clipboard",
  event_type: "observed",
  occurred_at: timestamp,
};

void missingEventId;
void invalidSourceKind;
