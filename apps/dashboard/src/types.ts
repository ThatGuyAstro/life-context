export type SessionState = "live" | "paused" | "idle" | "reconnecting";

export type SourceKind = "messages" | "documents" | "calendar" | "tasks";

export type DateRange = "all" | "today" | "7d" | "30d";

export type SourceRoute =
  | "local"
  | "local-only"
  | "remote"
  | "remote-only"
  | "cloud"
  | "excluded"
  | (string & {});

export type Confidence = "high" | "medium" | "low";

export type QueueState = "fresh" | "stale" | "offline";

export interface SessionStatus {
  id: string;
  title: string;
  state: SessionState;
  startedAt: string;
  lastEventAt: string;
  eventCount: number;
  participantCount: number;
  privacyMode: "private" | "shared";
  transport: "connected" | "degraded" | "disconnected";
}

export interface TimelineEvent {
  id: string;
  occurredAt: string;
  relativeLabel: string;
  source: SourceKind;
  sourceLabel: string;
  title: string;
  summary: string;
  tags: string[];
  participant?: string;
  important?: boolean;
  evidenceUrl?: string;
}

export interface TimelineFilters {
  source: SourceKind | "all";
  dateRange: DateRange;
  importantOnly: boolean;
}

export interface SearchParams {
  query: string;
  filters?: TimelineFilters;
}

export interface RankedResult {
  id: string;
  rank: number;
  title: string;
  snippet: string;
  source: SourceKind;
  sourceLabel: string;
  occurredAt: string;
  score: number;
  reason: string;
  tags: string[];
  evidenceUrl?: string;
}

export interface EvidenceItem {
  id: string;
  source: SourceKind;
  sourceLabel: string;
  title: string;
  detail: string;
  occurredAt: string;
  confidence: Confidence;
  url?: string;
}

export interface AnswerCard {
  query: string;
  answer: string;
  confidence: Confidence;
  generatedAt: string;
  caveat?: string;
  evidence: EvidenceItem[];
}

export interface QueueStatus {
  state: QueueState;
  lastIndexedAt: string;
  lagSeconds: number;
  pending: number;
  failed: number;
  deadLettered: number;
  message: string;
}

export interface SourceRouteSetting {
  source: string;
  label: string;
  enabled: boolean;
  route: SourceRoute;
  description: string;
  lastSyncedAt: string;
}

export interface RetentionSettings {
  eventsDays: number;
  transcriptsDays: number;
  rawPayloadDays: number;
  rawHours?: number;
  keyframeDays?: number;
  deleteAfterExport: boolean;
}

export interface RetentionPurgeRequest {
  before: string;
  source: string;
  project: string;
  confirmed: boolean;
}

export interface DashboardSettings {
  routes: SourceRouteSetting[];
  retention: RetentionSettings;
  plaintextWarningAcknowledged: boolean;
  privacyWarning?: string;
  captureIntervalSeconds?: number;
  workerCount?: number;
  queueSize?: number;
  modelEndpoint?: string;
  modelName?: string;
  modelRevision?: string;
}

export interface ModelStatus {
  configured: boolean;
  status: string;
  workerStatus: string;
  modelStatus: string;
  endpoint?: string;
  name?: string;
  revision?: string;
  activeWorkers?: number;
  processed?: number;
  healthStatus?: string;
  healthLatencyMs?: number;
  lastError?: string;
}

export interface DashboardRuntimeStatus {
  workerCount: number;
  queueSize: number;
  model: ModelStatus;
}

export interface RemoteConnectionSettings {
  apiBaseUrl: string;
  bearerToken: string;
  requireHttpsRemote: boolean;
}

export interface DashboardSnapshot {
  session: SessionStatus;
  timeline: TimelineEvent[];
  timelineTotalCount?: number;
  answer: AnswerCard;
  rankedResults: RankedResult[];
  queue: QueueStatus;
  settings: DashboardSettings;
  runtime?: DashboardRuntimeStatus;
}

export interface SearchResponse {
  answer: AnswerCard;
  rankedResults: RankedResult[];
}

export interface DashboardApi {
  getSnapshot(): Promise<DashboardSnapshot>;
  search(params: SearchParams): Promise<SearchResponse>;
  updateSettings(settings: DashboardSettings): Promise<DashboardSettings>;
  purge?(options: RetentionPurgeRequest): Promise<unknown>;
  setConnectionSettings?(settings: RemoteConnectionSettings): void;
}
