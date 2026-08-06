/** Transport-neutral JSON values accepted by the Python contracts. */
export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | { [key: string]: JsonValue };

export type JsonObject = { [key: string]: JsonValue };

export type ArtifactKind =
  | "text"
  | "image"
  | "audio"
  | "video"
  | "document"
  | "archive"
  | "structured"
  | "other";

export type SourceKind =
  | "file"
  | "email"
  | "calendar"
  | "message"
  | "note"
  | "web"
  | "application"
  | "sensor"
  | "manual"
  | "other";

export type SourceEventType = "created" | "updated" | "deleted" | "observed";

export type ObservationKind =
  | "fact"
  | "measurement"
  | "classification"
  | "relation"
  | "summary";

export type ObservationStatus = "candidate" | "confirmed" | "retracted";

export type QuerySortOrder = "ascending" | "descending";

export type HealthState = "healthy" | "degraded" | "unhealthy" | "unknown";

/** Alias names retained for callers using the shorter vocabulary. */
export type SourceType = SourceKind;
export type EventType = SourceEventType;
export type ObservationType = ObservationKind;
export type HealthStatus = HealthState;

export interface ArtifactRef {
  artifact_id: string;
  kind: ArtifactKind;
  uri?: string | null;
  name?: string | null;
  media_type?: string | null;
  size_bytes?: number | null;
  sha256?: string | null;
  metadata?: JsonObject;
}

export interface SourceEvent {
  event_id: string;
  source_id: string;
  source_kind: SourceKind;
  event_type: SourceEventType;
  occurred_at: string;
  captured_at?: string | null;
  payload?: JsonValue;
  artifacts?: ArtifactRef[];
  metadata?: JsonObject;
}

export interface CaptureEnvelope {
  envelope_id: string;
  schema_version?: "1";
  captured_at: string;
  source_kind: SourceKind;
  events: SourceEvent[];
  artifacts?: ArtifactRef[];
  producer?: string | null;
  metadata?: JsonObject;
}

export interface ObservationV1 {
  schema_version?: "1";
  observation_id: string;
  subject: string;
  predicate: string;
  value: JsonValue;
  kind?: ObservationKind;
  status?: ObservationStatus;
  confidence?: number;
  observed_at: string;
  source_event_id: string;
  evidence?: ArtifactRef[];
  metadata?: JsonObject;
}

export interface QueryRequest {
  query?: string | null;
  subject?: string | null;
  predicate?: string | null;
  source_ids?: string[];
  kinds?: ObservationKind[];
  statuses?: ObservationStatus[];
  observed_after?: string | null;
  observed_before?: string | null;
  sort_order?: QuerySortOrder;
  limit?: number;
  cursor?: string | null;
  include_evidence?: boolean;
}

export interface QueryResponse {
  request_id?: string | null;
  observations?: ObservationV1[];
  total_count?: number;
  next_cursor?: string | null;
}

export interface BacklogStatus {
  pending_count: number;
  processing_count: number;
  completed_count: number;
  failed_count: number;
  oldest_pending_at?: string | null;
  updated_at: string;
}

export interface HealthSnapshot {
  service: string;
  status: HealthState;
  checked_at: string;
  version?: string | null;
  components?: { [component: string]: HealthState };
  backlog?: BacklogStatus | null;
  message?: string | null;
  details?: JsonObject;
}
