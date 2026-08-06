import type {
  DashboardRuntimeStatus,
  QueueStatus,
  SessionStatus,
} from "./types";

export type DashboardSystemStatusTone =
  | "nominal"
  | "attention"
  | "offline"
  | "unknown";

export interface DashboardSystemStatus {
  label: string;
  tone: DashboardSystemStatusTone;
}

const unavailableStatuses = new Set([
  "error",
  "failed",
  "offline",
  "unavailable",
  "unhealthy",
]);

const attentionStatuses = new Set([
  "degraded",
  "starting",
  "stopped",
  "unknown",
]);

function normalizedStatus(value: string | undefined): string {
  return value?.trim().toLowerCase().replace(/[\s-]+/g, "_") ?? "";
}

export function getDashboardSystemStatus(
  session: SessionStatus,
  queue: QueueStatus,
  runtime?: DashboardRuntimeStatus,
): DashboardSystemStatus {
  if (session.transport === "disconnected") {
    return { label: "Session disconnected", tone: "offline" };
  }
  if (queue.state === "offline") {
    return { label: "Queue offline", tone: "offline" };
  }
  if (
    session.state === "reconnecting" ||
    session.transport === "degraded"
  ) {
    return { label: "Session reconnecting", tone: "attention" };
  }

  const model = runtime?.model;
  if (model?.configured) {
    const health = normalizedStatus(model.healthStatus);
    const modelStatus = normalizedStatus(model.modelStatus || model.status);
    if (unavailableStatuses.has(health) || unavailableStatuses.has(modelStatus)) {
      return { label: "Model unavailable", tone: "offline" };
    }
    if (attentionStatuses.has(health) || attentionStatuses.has(modelStatus)) {
      return { label: "Model needs attention", tone: "attention" };
    }
  }

  if (queue.state === "stale") {
    return { label: "Queue catching up", tone: "attention" };
  }
  if (queue.failed > 0 || queue.deadLettered > 0) {
    return { label: "Queue has failures", tone: "attention" };
  }
  if (session.state === "paused") {
    return { label: "Capture paused", tone: "attention" };
  }
  if (session.state === "idle") {
    return { label: "Capture idle", tone: "attention" };
  }
  if (model && !model.configured) {
    return { label: "Model not configured", tone: "attention" };
  }
  if (!runtime) {
    return { label: "Status unavailable", tone: "unknown" };
  }

  const health = normalizedStatus(model?.healthStatus);
  const modelStatus = normalizedStatus(model?.modelStatus || model?.status);
  const workerStatus = normalizedStatus(model?.workerStatus);
  if (
    session.state === "live" &&
    queue.state === "fresh" &&
    health === "healthy" &&
    ["healthy", "ready", "running"].includes(modelStatus) &&
    ["healthy", "ready", "running"].includes(workerStatus)
  ) {
    return { label: "Systems nominal", tone: "nominal" };
  }

  return { label: "Status unavailable", tone: "unknown" };
}
