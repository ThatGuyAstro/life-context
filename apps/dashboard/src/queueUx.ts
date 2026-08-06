import type {
  DashboardRuntimeStatus,
  QueueStatus,
  SessionStatus,
} from "./types";

export function formatPendingAge(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "None waiting";
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s`;

  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = Math.round(seconds % 60);
  if (minutes < 60) {
    return remainingSeconds > 0
      ? `${minutes}m ${remainingSeconds}s`
      : `${minutes}m`;
  }

  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes > 0 ? `${hours}h ${remainingMinutes}m` : `${hours}h`;
}

export function captureFreshnessLabel(session: SessionStatus): string {
  if (session.transport === "disconnected") return "Capture disconnected";
  if (session.state === "reconnecting" || session.transport === "degraded") {
    return "Capture reconnecting";
  }
  if (session.state === "paused") return "Capture paused";
  if (session.state === "idle") return "Capture idle";
  return "Capture is live";
}

export function queueGuidance(
  queue: QueueStatus,
  runtime?: DashboardRuntimeStatus,
): string {
  if (queue.state === "offline") {
    return "Check the model endpoint and start an inference worker in Settings.";
  }
  if (queue.state === "fresh") {
    return queue.failed > 0 || queue.deadLettered > 0
      ? "The live queue is caught up. Failed and dead-lettered counts are historical and need separate review."
      : "No worker action is needed.";
  }

  const activeWorkers = runtime?.model.activeWorkers;
  const workerStatus = runtime?.model.workerStatus.toLowerCase() ?? "";
  if (
    activeWorkers === 0 ||
    ["stopped", "offline", "not_configured", "unhealthy"].includes(workerStatus)
  ) {
    return "No active inference worker is reported. Check the model endpoint and worker status in Settings.";
  }

  const workerCount = runtime?.workerCount ?? 1;
  if (workerCount <= 1) {
    return "One worker is configured. If the GPU has headroom, increase Worker count in Settings and watch VRAM usage.";
  }
  return `${workerCount} workers are configured. If this age keeps growing, check model throughput before adding workers.`;
}
