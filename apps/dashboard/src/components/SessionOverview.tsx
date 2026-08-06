import { Icon } from "./Icon";
import { StatusBadge } from "./StatusBadge";
import { formatPendingAge } from "../queueUx";
import type { QueueStatus, SessionStatus, TimelineEvent } from "../types";

interface SessionOverviewProps {
  session: SessionStatus;
  queue: QueueStatus;
  onPause: () => void;
}

export function SessionOverview({
  session,
  queue,
  onPause,
}: SessionOverviewProps) {
  const formattedStart = new Intl.DateTimeFormat("en", {
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(session.startedAt));

  return (
    <section aria-labelledby="page-title" className="hero-row">
      <div className="hero-copy">
        <div className="eyebrow">
          <span className="eyebrow-line" /> Memory operating system
        </div>
        <h1 id="page-title">
          Your context, <em>in focus.</em>
        </h1>
        <p>
          One calm place to see what is happening, find what mattered, and keep
          the thread.
        </p>
      </div>
      <div className="session-card">
        <div className="session-card-top">
          <div className="session-symbol">
            <Icon name="sun" size={18} />
          </div>
          <div className="session-title-wrap">
            <span className="session-label">Current session</span>
            <strong>{session.title}</strong>
          </div>
          <button
            aria-label="More session options"
            className="icon-button subtle-button"
            type="button"
          >
            <Icon name="more" size={17} />
          </button>
        </div>
        <div className="session-card-bottom">
          <StatusBadge state={session.state} />
          <span className="session-start">
            <Icon name="clock" size={14} /> Started {formattedStart}
          </span>
          <button className="session-pause" onClick={onPause} type="button">
            <Icon
              name={session.state === "paused" ? "play" : "pause"}
              size={13}
            />
            {session.state === "paused" ? "Resume" : "Pause"}
          </button>
        </div>
        <div className="session-progress" aria-hidden="true">
          <span />
        </div>
        <div className="session-stream-label">
          <span>
            <span className={`stream-pulse stream-${session.state}`} />
            {captureLabel(session, queue)}
          </span>
          <span>{queue.pending} awaiting analysis</span>
        </div>
      </div>
    </section>
  );
}

interface MetricsStripProps {
  session: SessionStatus;
  queue: QueueStatus;
  timeline: TimelineEvent[];
}

export function MetricsStrip({ session, queue, timeline }: MetricsStripProps) {
  const lagLabel =
    queue.state === "offline" ? "Offline" : formatPendingAge(queue.lagSeconds);
  const sourceCount = new Set(timeline.map((event) => event.source)).size;
  const queueHasHistory = queue.failed > 0 || queue.deadLettered > 0;

  return (
    <section aria-label="Workspace metrics" className="metrics-strip">
      <Metric
        icon="database"
        label="Indexed events"
        value={session.eventCount.toLocaleString()}
        caption={
          sourceCount > 0
            ? `${sourceCount} source${sourceCount === 1 ? "" : "s"} in loaded timeline`
            : "No source events loaded"
        }
        tone="purple"
      />
      <Metric
        icon="message"
        label="Participants"
        value={String(session.participantCount)}
        caption="In this session"
        tone="orange"
      />
      <Metric
        icon="clock"
        label="Oldest pending analysis"
        value={lagLabel}
        caption={
          queue.state === "offline"
            ? "Unavailable"
            : queue.state === "stale"
              ? "Semantic index catching up"
              : queueHasHistory
                ? "Historical failures"
                : "Caught up"
        }
        tone="blue"
        warning={queue.state !== "fresh" || queueHasHistory}
      />
      <Metric
        icon="lock"
        label="Privacy mode"
        value={session.privacyMode === "private" ? "Private" : "Shared"}
        caption={
          session.privacyMode === "private"
            ? "Session-scoped"
            : "Shared session"
        }
        tone="green"
      />
    </section>
  );
}

function captureLabel(session: SessionStatus, queue: QueueStatus): string {
  if (queue.state === "offline") return "Queue offline";
  if (session.transport === "disconnected") return "Capture disconnected";
  if (session.state === "reconnecting") return "Reconnecting";
  if (session.state === "paused") return "Capture paused";
  if (session.state === "idle") return "Capture idle";
  return "Capturing context";
}

function Metric({
  icon,
  label,
  value,
  caption,
  tone,
  warning = false,
}: {
  icon: "database" | "message" | "clock" | "lock";
  label: string;
  value: string;
  caption: string;
  tone: "purple" | "orange" | "blue" | "green";
  warning?: boolean;
}) {
  return (
    <div className="metric-card">
      <div className={`metric-icon metric-${tone}`}>
        <Icon name={icon} size={17} />
      </div>
      <div className="metric-copy">
        <span>{label}</span>
        <strong>{value}</strong>
      </div>
      <div className={`metric-caption${warning ? " is-warning" : ""}`}>
        {caption}
      </div>
    </div>
  );
}
