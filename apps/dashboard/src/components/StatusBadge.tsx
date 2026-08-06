import type { QueueState, SessionState } from "../types";

interface StatusBadgeProps {
  state: SessionState | QueueState;
  label?: string;
  compact?: boolean;
}

const labels: Record<SessionState | QueueState, string> = {
  live: "Live",
  paused: "Paused",
  idle: "Idle",
  reconnecting: "Reconnecting",
  fresh: "Fresh",
  stale: "Stale",
  offline: "Offline",
};

export function StatusBadge({ state, label, compact = false }: StatusBadgeProps) {
  return (
    <span className={`status-badge status-${state}${compact ? " status-compact" : ""}`}>
      <span className="status-dot" />
      {label ?? labels[state]}
    </span>
  );
}
