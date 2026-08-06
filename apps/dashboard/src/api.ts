import type {
  AnswerCard,
  Confidence,
  DashboardApi,
  DashboardRuntimeStatus,
  DashboardSettings,
  DashboardSnapshot,
  ModelStatus,
  RemoteConnectionSettings,
  QueueStatus,
  RankedResult,
  RetentionPurgeRequest,
  SearchParams,
  SearchResponse,
  SessionStatus,
  SourceKind,
  SourceRoute,
  TimelineEvent,
} from "./types";
import { buildDemoSnapshot, searchDemoSnapshot } from "./demoData";

const SAME_ORIGIN_API_BASE_URL = "/api";

function queryApiBaseUrl(): string {
  if (typeof window === "undefined") return "";
  try {
    return new URLSearchParams(window.location.search).get("api")?.trim() ?? "";
  } catch {
    return "";
  }
}

function envValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function resolveApiBaseUrl(): string {
  return (
    queryApiBaseUrl() ||
    envValue(import.meta.env.VITE_API_BASE_URL) ||
    SAME_ORIGIN_API_BASE_URL
  );
}

export const configuredApiBaseUrl = resolveApiBaseUrl();
export const configuredApiBearerToken = envValue(
  import.meta.env.VITE_API_BEARER_TOKEN,
);

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

type FetchLike = typeof fetch;
type JsonRecord = Record<string, unknown>;

export interface ApiClientOptions {
  bearerToken?: string;
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readString(record: JsonRecord, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return undefined;
}

function readNumber(record: JsonRecord, ...keys: string[]): number | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (
      typeof value === "string" &&
      value.trim() &&
      Number.isFinite(Number(value))
    )
      return Number(value);
  }
  return undefined;
}

function readBoolean(
  record: JsonRecord,
  ...keys: string[]
): boolean | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "boolean") return value;
  }
  return undefined;
}

function asArray(value: unknown, ...keys: string[]): unknown[] {
  if (Array.isArray(value)) return value;
  if (isRecord(value)) {
    for (const key of keys) {
      if (Array.isArray(value[key])) return value[key] as unknown[];
    }
  }
  return [];
}

function buildUrl(baseUrl: string, path: string): string {
  const normalizedBase = baseUrl.replace(/\/$/, "");
  return `${normalizedBase}${path}`;
}

export function validateRemoteConnection(
  settings: RemoteConnectionSettings,
): string | null {
  const endpoint = settings.apiBaseUrl.trim();
  if (!endpoint) return null;
  let parsed: URL;
  try {
    parsed = new URL(
      endpoint,
      typeof window === "undefined"
        ? "http://dashboard.local"
        : window.location.origin,
    );
  } catch {
    return "Enter a valid server URL.";
  }
  if (parsed.username || parsed.password) {
    return "Remote dashboard connections must not include embedded credentials.";
  }
  const isRemote = !["", "localhost", "127.0.0.1", "::1"].includes(
    parsed.hostname.toLowerCase(),
  );
  if (isRemote && settings.requireHttpsRemote && parsed.protocol !== "https:") {
    return "Remote dashboard connections require HTTPS.";
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    return "The server URL must use HTTP or HTTPS.";
  }
  return null;
}

/** Validate the configured OpenAI-compatible local inference endpoint. */
export function validateModelEndpoint(value: string): string | null {
  const endpoint = value.trim();
  if (!endpoint) return null;
  let parsed: URL;
  try {
    parsed = new URL(endpoint);
  } catch {
    return "Enter a valid model endpoint URL.";
  }
  if (!["http:", "https:"].includes(parsed.protocol)) {
    return "The model endpoint must use HTTP or HTTPS.";
  }
  if (parsed.username || parsed.password) {
    return "Model endpoints must not include embedded credentials.";
  }
  const isLoopback = ["", "localhost", "127.0.0.1", "::1"].includes(
    parsed.hostname.toLowerCase(),
  );
  if (parsed.protocol === "http:" && !isLoopback) {
    return "Non-loopback model endpoints require HTTPS.";
  }
  return null;
}

async function requestJson<T>(
  fetcher: FetchLike,
  baseUrl: string,
  path: string,
  init?: RequestInit,
  bearerToken = "",
): Promise<T> {
  const response = await fetcher(buildUrl(baseUrl, path), {
    ...init,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...(bearerToken ? { Authorization: `Bearer ${bearerToken}` } : {}),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    throw new ApiError(
      `Dashboard API returned ${response.status}`,
      response.status,
    );
  }

  const body = await response.text();
  return (body ? JSON.parse(body) : {}) as T;
}

async function requestOptional<T>(
  fetcher: FetchLike,
  baseUrl: string,
  path: string,
  bearerToken = "",
): Promise<T | null> {
  try {
    return await requestJson<T>(fetcher, baseUrl, path, undefined, bearerToken);
  } catch {
    return null;
  }
}

function buildSearchPayload(params: SearchParams): JsonRecord {
  return {
    query: params.query,
    limit: 10,
    include_evidence: true,
    filters: {
      ...(params.filters?.source && params.filters.source !== "all"
        ? { source: params.filters.source }
        : {}),
      ...(params.filters?.dateRange && params.filters.dateRange !== "all"
        ? { range: params.filters.dateRange }
        : {}),
      ...(params.filters?.importantOnly ? { important: true } : {}),
    },
  };
}

function normalizeSource(value: unknown): SourceKind {
  const source = String(value ?? "").toLowerCase();
  if (
    source.includes("message") ||
    source.includes("chat") ||
    source.includes("clipboard")
  )
    return "messages";
  if (
    source.includes("calendar") ||
    source.includes("meeting") ||
    source.includes("event")
  )
    return "calendar";
  if (
    source.includes("task") ||
    source.includes("todo") ||
    source.includes("issue")
  )
    return "tasks";
  return "documents";
}

function sourceLabel(source: SourceKind, original?: unknown): string {
  if (typeof original === "string" && original.trim()) {
    const normalized = original.trim().toLowerCase();
    const aliases: Record<SourceKind, string[]> = {
      messages: ["message", "messages", "chat", "clipboard"],
      documents: ["document", "documents", "file", "files", "note", "notes"],
      calendar: ["calendar", "meeting", "meetings", "event", "events"],
      tasks: ["task", "tasks", "todo", "todos", "issue", "issues"],
    };
    if (aliases[source].includes(normalized))
      return {
        messages: "Messages",
        documents: "Documents",
        calendar: "Calendar",
        tasks: "Tasks",
      }[source];
    return original;
  }
  return {
    messages: "Messages",
    documents: "Documents",
    calendar: "Calendar",
    tasks: "Tasks",
  }[source];
}

const routeSourceLabels: Record<string, string> = {
  browser: "Browser",
  clipboard: "Clipboard",
  documents: "Documents",
  filesystem: "Filesystem",
  foreground: "Foreground window",
  git: "Git",
  messages: "Messages",
  powershell: "PowerShell",
  process: "Processes",
  screenshots: "Screenshots",
  tasks: "Tasks",
};

function normalizeRouteSource(value: unknown, fallback = "source"): string {
  const normalized = String(value ?? fallback)
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, "_");
  return normalized || fallback;
}

function routeSourceLabel(source: string, original?: unknown): string {
  if (typeof original === "string" && original.trim()) return original.trim();
  if (routeSourceLabels[source]) return routeSourceLabels[source];
  return source
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function normalizeRoute(value: unknown): SourceRoute {
  const route = String(value ?? "local")
    .trim()
    .toLowerCase();
  return (route || "local") as SourceRoute;
}

function normalizeTimestamp(
  value: unknown,
  fallback = new Date().toISOString(),
): string {
  if (typeof value === "string" && !Number.isNaN(new Date(value).getTime()))
    return value;
  if (typeof value === "number" && Number.isFinite(value))
    return new Date(value * (value < 10_000_000_000 ? 1000 : 1)).toISOString();
  return fallback;
}

function relativeLabel(timestamp: string): string {
  const ageSeconds = Math.max(
    0,
    Math.round((Date.now() - new Date(timestamp).getTime()) / 1000),
  );
  if (ageSeconds < 60) return "Just now";
  if (ageSeconds < 3600) return `${Math.round(ageSeconds / 60)} min ago`;
  if (ageSeconds < 86_400) return `${Math.round(ageSeconds / 3600)} hr ago`;
  if (ageSeconds < 172_800) return "Yesterday";
  return new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
  }).format(new Date(timestamp));
}

function absoluteUrl(value: unknown, baseUrl: string): string | undefined {
  if (typeof value !== "string" || !value.trim()) return undefined;
  const reference = value.trim();
  try {
    if (baseUrl.startsWith("/") && typeof window !== "undefined") {
      const relativeBase = new URL(
        `${baseUrl.replace(/\/$/, "")}/`,
        window.location.origin,
      );
      const basePath = relativeBase.pathname.replace(/\/$/, "");
      if (reference.startsWith("/")) {
        const referencePath = reference.split(/[?#]/, 1)[0];
        if (
          referencePath === basePath ||
          referencePath.startsWith(`${basePath}/`)
        ) {
          return new URL(reference, window.location.origin).toString();
        }
        return new URL(reference.replace(/^\/+/, ""), relativeBase).toString();
      }
      return new URL(reference, relativeBase).toString();
    }
    return new URL(reference, `${baseUrl.replace(/\/$/, "")}/`).toString();
  } catch {
    return undefined;
  }
}

function mapTimelineEntry(
  value: unknown,
  index: number,
  baseUrl = "",
): TimelineEvent {
  const entry = isRecord(value) ? value : { text: String(value) };
  const metadata = isRecord(entry.metadata) ? entry.metadata : {};
  const originalSource =
    entry.source_kind ?? entry.source ?? entry.origin ?? entry.type;
  const occurredAt = normalizeTimestamp(
    entry.occurred_at ??
      entry.timestamp ??
      entry.captured_at ??
      entry.created_at,
  );
  const text =
    readString(entry, "text", "content", "excerpt", "body", "summary") ??
    "Context event received.";
  const title =
    readString(entry, "title", "name", "subject") ??
    (text.split(/[.!?]/, 1)[0].slice(0, 80) || "Context event");
  const sourceFromMetadata = readString(
    metadata,
    "source_kind",
    "source",
    "origin",
  );
  const resolvedSource = originalSource ?? sourceFromMetadata;
  const resolvedSourceKind = normalizeSource(resolvedSource);
  const tags = Array.isArray(entry.tags)
    ? entry.tags.filter((tag): tag is string => typeof tag === "string")
    : [];
  const participant =
    readString(entry, "participant", "author", "actor") ??
    readString(metadata, "participant", "author");
  return {
    id:
      readString(entry, "id", "event_id", "observation_id") ??
      `timeline-${index}`,
    occurredAt,
    relativeLabel: relativeLabel(occurredAt),
    source: resolvedSourceKind,
    sourceLabel: sourceLabel(
      resolvedSourceKind,
      entry.source_label ?? resolvedSource,
    ),
    title,
    summary: text,
    tags: tags.length ? tags : [resolvedSourceKind],
    participant,
    important: readBoolean(entry, "important", "is_important") ?? false,
    evidenceUrl: absoluteUrl(
      entry.artifact_url ?? metadata.artifact_url ?? entry.uri,
      baseUrl,
    ),
  };
}

function mapScore(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value))
    return Math.max(0, Math.min(1, value > 1 ? value / 100 : value));
  return 0;
}

function confidenceFrom(value: unknown, score = 0.5): Confidence {
  if (
    typeof value === "string" &&
    ["high", "medium", "low"].includes(value.toLowerCase())
  )
    return value.toLowerCase() as Confidence;
  if (typeof value === "number")
    return value >= 0.8 ? "high" : value >= 0.55 ? "medium" : "low";
  return score >= 0.8 ? "high" : score >= 0.55 ? "medium" : "low";
}

function mapSearchResponse(
  value: unknown,
  query: string,
  baseUrl = "",
): SearchResponse {
  const payload = isRecord(value) ? value : {};
  const answerPayload = isRecord(payload.answer) ? payload.answer : {};
  const rawEvidence = asArray(payload.evidence, "items");
  const rawResults = asArray(payload.results, "hits", "items", "documents");
  const candidates = rawResults.length ? rawResults : rawEvidence;
  const rankedResults: RankedResult[] = candidates.map((item, index) => {
    const result = isRecord(item) ? item : { text: String(item) };
    const metadata = isRecord(result.metadata) ? result.metadata : {};
    const sourceValue = result.source ?? result.origin ?? metadata.source;
    const source = normalizeSource(sourceValue);
    const score = mapScore(
      result.score ?? result.combined_score ?? result.similarity,
    );
    const snippet =
      readString(result, "snippet", "text", "content", "excerpt", "body") ??
      "Related context item.";
    return {
      id:
        readString(result, "id", "event_id", "document_id") ??
        `result-${index}`,
      rank: index + 1,
      title:
        readString(result, "title", "name", "citation") ?? snippet.slice(0, 72),
      snippet,
      source,
      sourceLabel: sourceLabel(source, result.source_label ?? sourceValue),
      occurredAt: relativeLabel(
        normalizeTimestamp(
          result.timestamp ?? result.occurred_at ?? result.created_at,
        ),
      ),
      score,
      reason:
        readString(result, "reason", "explanation") ??
        (score >= 0.8 ? "Strong semantic match" : "Related context"),
      tags: Array.isArray(result.tags)
        ? result.tags.filter((tag): tag is string => typeof tag === "string")
        : [],
      evidenceUrl: absoluteUrl(
        result.artifact_url ?? metadata.artifact_url ?? result.uri,
        baseUrl,
      ),
    };
  });

  const evidence = (
    rawEvidence.length ? rawEvidence : candidates.slice(0, 3)
  ).map((item, index) => {
    const evidenceItem = isRecord(item) ? item : { text: String(item) };
    const score = mapScore(evidenceItem.score ?? evidenceItem.combined_score);
    const sourceValue = evidenceItem.source ?? evidenceItem.origin;
    const source = normalizeSource(sourceValue);
    return {
      id:
        readString(evidenceItem, "id", "event_id", "document_id") ??
        `evidence-${index}`,
      source,
      sourceLabel: sourceLabel(
        source,
        evidenceItem.source_label ?? sourceValue,
      ),
      title:
        readString(evidenceItem, "title", "citation", "name") ??
        "Context source",
      detail:
        readString(evidenceItem, "excerpt", "text", "content", "snippet") ??
        "Supporting context was returned by the server.",
      occurredAt: relativeLabel(
        normalizeTimestamp(evidenceItem.timestamp ?? evidenceItem.occurred_at),
      ),
      confidence: confidenceFrom(evidenceItem.confidence, score),
      url: absoluteUrl(
        evidenceItem.artifact_url ??
          (isRecord(evidenceItem.metadata)
            ? evidenceItem.metadata.artifact_url
            : undefined) ??
          evidenceItem.uri,
        baseUrl,
      ),
    };
  });

  const answerText =
    readString(payload, "answer_text", "text") ??
    readString(answerPayload, "text", "answer") ??
    "No synthesized answer was returned.";
  const rawConfidence = answerPayload.confidence ?? payload.confidence;
  const confidence = confidenceFrom(
    rawConfidence,
    rankedResults[0]?.score ?? 0.5,
  );
  const answer: AnswerCard = {
    query,
    answer: answerText,
    confidence,
    generatedAt: new Date().toISOString(),
    caveat:
      Array.isArray(payload.warnings) && payload.warnings.length
        ? String(payload.warnings[0])
        : undefined,
    evidence,
  };
  return { answer, rankedResults };
}

function mapQueue(statusPayload: unknown): QueueStatus {
  const status = isRecord(statusPayload) ? statusPayload : {};
  const backlog = isRecord(status.backlog) ? status.backlog : {};
  const queue = isRecord(status.queue)
    ? status.queue
    : isRecord(status.backlog)
      ? status.backlog
      : isRecord(status.metrics)
        ? status.metrics
        : status;
  const readQueueNumber = (...keys: string[]) =>
    readNumber(queue, ...keys) ?? readNumber(backlog, ...keys);
  const lagSeconds =
    readQueueNumber(
      "staleness_seconds",
      "oldest_stale_age_s",
      "oldest_pending_age_seconds",
      "lag_seconds",
      "lag",
    ) ?? 0;
  const pending =
    readQueueNumber("pending", "pending_count", "queued", "backlog") ?? 0;
  const failed = readQueueNumber("failed", "failed_count") ?? 0;
  const deadLettered =
    readQueueNumber("dead_lettered", "dead_lettered_count", "dead") ?? 0;
  const statusText = [
    status.status,
    status.state,
    queue.status,
    queue.state,
    backlog.status,
    backlog.state,
  ]
    .filter((value): value is string => typeof value === "string")
    .join(" ")
    .toLowerCase();
  const staleCount = readQueueNumber("stale_count") ?? 0;
  const state =
    statusText.includes("offline") ||
    statusText.includes("unhealthy") ||
    statusText.includes("unavailable")
      ? "offline"
      : lagSeconds > 120 || staleCount > 0
        ? "stale"
        : "fresh";
  return {
    state,
    lastIndexedAt: normalizeTimestamp(queue.updated_at ?? status.updated_at),
    lagSeconds,
    pending,
    failed,
    deadLettered,
    message:
      readString(queue, "message", "detail") ??
      readString(backlog, "message", "detail") ??
      (state === "fresh"
        ? "The context index is caught up."
        : state === "stale"
          ? "New events may arrive out of order while the queue catches up."
          : "The context index is not currently reachable."),
  };
}

function mapSession(
  statusPayload: unknown,
  timelineLength: number,
): SessionStatus {
  const status = isRecord(statusPayload) ? statusPayload : {};
  const stateText = String(
    status.state ?? status.lifecycle_state ?? status.status ?? "live",
  ).toLowerCase();
  const state =
    stateText.includes("pause") || stateText.includes("stop")
      ? "paused"
      : stateText.includes("reconnect")
        ? "reconnecting"
         : stateText.includes("idle")
           ? "idle"
           : "live";
  const transportText = String(
    status.transport ?? status.connection_status ?? status.status ?? "",
  ).toLowerCase();
  const transport =
    transportText.includes("disconnect") ||
    transportText.includes("offline") ||
    transportText.includes("unavailable") ||
    transportText.includes("error")
      ? "disconnected"
      : state === "reconnecting" || transportText.includes("degraded")
        ? "degraded"
        : "connected";
  return {
    id: readString(status, "session_id", "id") ?? "connected-session",
    title:
      readString(status, "title", "session_title") ?? "Connected workspace",
    state,
    startedAt: normalizeTimestamp(status.started_at ?? status.startedAt),
    lastEventAt: normalizeTimestamp(status.last_event_at ?? status.updated_at),
    eventCount:
      readNumber(
        status,
        "event_count",
        "events",
        "indexed_events",
        "completed",
      ) ?? timelineLength,
    participantCount:
      readNumber(status, "participant_count", "participants") ?? 0,
    privacyMode:
      String(
        status.privacy_mode ?? status.privacy ?? "private",
      ).toLowerCase() === "shared"
        ? "shared"
        : "private",
    transport,
  };
}

function mapRuntimeStatus(statusPayload: unknown): DashboardRuntimeStatus {
  const status = isRecord(statusPayload) ? statusPayload : {};
  const modelPayload = isRecord(status.model) ? status.model : {};
  const workerCount =
    readNumber(status, "worker_count") ??
    readNumber(modelPayload, "worker_count") ??
    1;
  const queueSize = readNumber(status, "queue_size") ?? 1000;
  const configured =
    readBoolean(modelPayload, "configured") ??
    Boolean(readString(modelPayload, "endpoint"));
  const healthPayload = isRecord(modelPayload.health)
    ? modelPayload.health
    : {};
  const model: ModelStatus = {
    configured,
    status:
      readString(modelPayload, "status", "availability") ??
      (configured ? "configured" : "not_configured"),
    workerStatus:
      readString(modelPayload, "worker_status") ??
      (configured ? "configured" : "not_configured"),
    modelStatus:
      readString(modelPayload, "model_status", "availability") ??
      (configured ? "unknown" : "not_configured"),
    endpoint: readString(modelPayload, "endpoint"),
    name: readString(modelPayload, "model", "name"),
    revision: readString(modelPayload, "model_revision", "revision"),
    activeWorkers: readNumber(modelPayload, "active_workers"),
    processed: readNumber(modelPayload, "processed"),
    healthStatus:
      readString(healthPayload, "status") ??
      readString(modelPayload, "health_status"),
    healthLatencyMs:
      readNumber(healthPayload, "latency_ms") ??
      readNumber(modelPayload, "health_latency_ms"),
    lastError: readString(modelPayload, "last_error"),
  };
  return { workerCount, queueSize, model };
}

function mapSettings(
  value: unknown,
  fallbackSettings?: DashboardSettings,
): DashboardSettings {
  const fallback = fallbackSettings ?? buildDemoSnapshot().settings;
  const response = isRecord(value) ? value : {};
  const payload = isRecord(response.settings) ? response.settings : response;
  const routeValues = asArray(payload.routes, "source_routes", "routing");
  const sourceRouting = isRecord(payload.source_routing)
    ? payload.source_routing
    : {};
  if (!routeValues.length && Object.keys(sourceRouting).length) {
    for (const [sourceName, routeValue] of Object.entries(sourceRouting)) {
      const route = isRecord(routeValue) ? routeValue : {};
      routeValues.push({ source: sourceName, ...route });
    }
  }
  if (!routeValues.length && isRecord(payload.routes)) {
    for (const [sourceName, routeValue] of Object.entries(payload.routes)) {
      const route = isRecord(routeValue) ? routeValue : {};
      routeValues.push({ source: sourceName, ...route });
    }
  }
  const routes = routeValues.length
    ? routeValues.map((route, index) => {
        const item = isRecord(route) ? route : {};
        const source = normalizeRouteSource(
          item.source ?? item.source_kind ?? `source-${index}`,
        );
        return {
          source,
          label: routeSourceLabel(source, item.label ?? item.name),
          enabled: readBoolean(item, "enabled", "active") ?? true,
          route: normalizeRoute(item.route ?? item.destination),
          description:
            readString(item, "description") ??
            `Configured ${routeSourceLabel(source).toLowerCase()} source`,
          lastSyncedAt:
            readString(item, "last_synced_at", "lastSyncedAt") ?? "Unknown",
        };
      })
    : fallback.routes;
  const retention = isRecord(payload.retention) ? payload.retention : {};
  const rawHours = readNumber(retention, "raw_hours", "rawHours");
  const keyframeDays = readNumber(retention, "keyframe_days", "keyframeDays");
  const mappedRetention: DashboardSettings["retention"] = {
    eventsDays:
      readNumber(retention, "events_days", "eventsDays") ??
      fallback.retention.eventsDays,
    transcriptsDays:
      readNumber(retention, "transcripts_days", "transcriptsDays") ??
      fallback.retention.transcriptsDays,
    rawPayloadDays:
      readNumber(retention, "raw_payload_days", "rawPayloadDays") ??
      (rawHours !== undefined
        ? rawHours / 24
        : fallback.retention.rawPayloadDays),
    deleteAfterExport:
      readBoolean(retention, "delete_after_export", "deleteAfterExport") ??
      fallback.retention.deleteAfterExport,
  };
  if (rawHours !== undefined) mappedRetention.rawHours = rawHours;
  if (keyframeDays !== undefined) mappedRetention.keyframeDays = keyframeDays;
  return {
    routes,
    retention: mappedRetention,
    plaintextWarningAcknowledged:
      readBoolean(
        payload,
        "plaintext_warning_acknowledged",
        "plaintextWarningAcknowledged",
      ) ?? fallback.plaintextWarningAcknowledged,
    privacyWarning:
      readString(response, "privacy_warning", "privacyWarning") ??
      readString(payload, "privacy_warning", "privacyWarning") ??
      fallback.privacyWarning,
    captureIntervalSeconds:
      readNumber(
        payload,
        "capture_interval_seconds",
        "captureIntervalSeconds",
      ) ?? fallback.captureIntervalSeconds,
    workerCount:
      readNumber(payload, "worker_count", "workerCount") ??
      fallback.workerCount,
    queueSize:
      readNumber(payload, "queue_size", "queueSize") ?? fallback.queueSize,
    modelEndpoint:
      readString(payload, "model_endpoint", "modelEndpoint") ??
      fallback.modelEndpoint,
    modelName:
      readString(payload, "model_name", "modelName") ?? fallback.modelName,
    modelRevision:
      readString(payload, "model_revision", "modelRevision") ??
      fallback.modelRevision,
  };
}

function serializeSettings(settings: DashboardSettings): JsonRecord {
  const { rawHours, keyframeDays } = settings.retention;
  const sourceRouting = Object.fromEntries(
    settings.routes.map((route) => [
      route.source,
      {
        enabled: route.enabled,
        route: route.route,
      },
    ]),
  );
  const retention: Record<string, unknown> = {
    events_days: settings.retention.eventsDays,
    transcripts_days: settings.retention.transcriptsDays,
    raw_payload_days: settings.retention.rawPayloadDays,
    delete_after_export: settings.retention.deleteAfterExport,
    raw_hours: rawHours ?? settings.retention.rawPayloadDays * 24,
    keyframe_days: keyframeDays ?? 14,
  };
  return {
    ...(settings.captureIntervalSeconds === undefined
      ? {}
      : { capture_interval_seconds: settings.captureIntervalSeconds }),
    ...(settings.workerCount === undefined
      ? {}
      : { worker_count: settings.workerCount }),
    ...(settings.queueSize === undefined
      ? {}
      : { queue_size: settings.queueSize }),
    ...(settings.modelEndpoint === undefined
      ? {}
      : { model_endpoint: settings.modelEndpoint }),
    ...(settings.modelName === undefined
      ? {}
      : { model_name: settings.modelName }),
    ...(settings.modelRevision === undefined
      ? {}
      : { model_revision: settings.modelRevision }),
    source_routing: sourceRouting,
    retention,
  };
}

function mapLiveSnapshot(
  statusPayload: unknown,
  timelinePayload: unknown,
  configPayload: unknown,
  baseUrl = "",
): DashboardSnapshot {
  const timelineValues = asArray(
    timelinePayload,
    "timeline",
    "items",
    "events",
    "observations",
    "results",
  );
  const timeline = timelineValues.map((entry, index) =>
    mapTimelineEntry(entry, index, baseUrl),
  );
  const session = mapSession(statusPayload, timeline.length);
  const runtime = mapRuntimeStatus(statusPayload);
  const timelineRecord = isRecord(timelinePayload) ? timelinePayload : {};
  const timelineTotalCount = Math.max(
    timeline.length,
    readNumber(timelineRecord, "total_count", "totalCount", "total", "count") ??
      timeline.length,
  );
  return {
    session,
    timeline,
    timelineTotalCount,
    answer: {
      query: "",
      answer:
        "Ask a question to synthesize the context returned by your server.",
      confidence: "low",
      generatedAt: new Date().toISOString(),
      caveat:
        "Answers and evidence are generated from the configured context API.",
      evidence: [],
    },
    rankedResults: [],
    queue: mapQueue(statusPayload),
    settings: mapSettings(configPayload, {
      ...buildDemoSnapshot().settings,
      workerCount: runtime.workerCount,
      queueSize: runtime.queueSize,
    }),
    runtime,
  };
}

export function createApiClient(
  baseUrl = configuredApiBaseUrl,
  fetcher: FetchLike = fetch,
  options: ApiClientOptions = {},
): DashboardApi {
  let currentBaseUrl = baseUrl;
  let bearerToken = envValue(options.bearerToken ?? configuredApiBearerToken);

  if (!baseUrl) {
    let demoSnapshot = buildDemoSnapshot();
    return {
      getSnapshot: async () => demoSnapshot,
      search: async (params) => searchDemoSnapshot(demoSnapshot, params),
      updateSettings: async (settings) => {
        demoSnapshot = { ...demoSnapshot, settings };
        return settings;
      },
      purge: async (options) => ({ purge: options }),
      setConnectionSettings: (settings) => {
        const error = validateRemoteConnection(settings);
        if (error) throw new Error(error);
        currentBaseUrl = settings.apiBaseUrl.trim();
        bearerToken = settings.bearerToken.trim();
      },
    };
  }

  return {
    getSnapshot: async () => {
      const [status, timeline, config] = await Promise.all([
        requestJson<unknown>(
          fetcher,
          currentBaseUrl,
          "/status",
          undefined,
          bearerToken,
        ),
        requestJson<unknown>(
          fetcher,
          currentBaseUrl,
          "/timeline?limit=50",
          undefined,
          bearerToken,
        ),
        requestOptional<unknown>(
          fetcher,
          currentBaseUrl,
          "/config",
          bearerToken,
        ),
      ]);
      return mapLiveSnapshot(status, timeline, config, currentBaseUrl);
    },
    search: async (params) => {
      const payload = await requestJson<unknown>(
        fetcher,
        currentBaseUrl,
        "/query",
        {
          method: "POST",
          body: JSON.stringify(buildSearchPayload(params)),
        },
        bearerToken,
      );
      return mapSearchResponse(payload, params.query, currentBaseUrl);
    },
    updateSettings: async (settings) => {
      const payload = await requestJson<unknown>(
        fetcher,
        currentBaseUrl,
        "/config",
        {
          method: "POST",
          body: JSON.stringify({
            operation: "update",
            settings: serializeSettings(settings),
          }),
        },
        bearerToken,
      );
      return mapSettings(payload, settings);
    },
    purge: async (options: RetentionPurgeRequest) =>
      requestJson<unknown>(
        fetcher,
        currentBaseUrl,
        "/purge",
        {
          method: "POST",
          body: JSON.stringify(options),
        },
        bearerToken,
      ),
    setConnectionSettings: (settings) => {
      const error = validateRemoteConnection(settings);
      if (error) throw new Error(error);
      currentBaseUrl = settings.apiBaseUrl.trim() || SAME_ORIGIN_API_BASE_URL;
      bearerToken = settings.bearerToken.trim();
    },
  };
}
