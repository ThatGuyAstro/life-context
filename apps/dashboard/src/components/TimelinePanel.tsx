import { Icon } from "./Icon";
import { SourceIcon } from "./SourceIcon";
import { StatusBadge } from "./StatusBadge";
import {
  captureFreshnessLabel,
  formatPendingAge,
  queueGuidance,
} from "../queueUx";
import type {
  DashboardRuntimeStatus,
  DateRange,
  QueueStatus,
  SessionStatus,
  SourceKind,
  TimelineEvent,
  TimelineFilters,
} from "../types";

const sourceOptions: Array<{ id: SourceKind | "all"; label: string }> = [
  { id: "all", label: "All sources" },
  { id: "messages", label: "Messages" },
  { id: "documents", label: "Documents" },
  { id: "calendar", label: "Calendar" },
  { id: "tasks", label: "Tasks" },
];

const rangeOptions: Array<{ id: DateRange; label: string }> = [
  { id: "all", label: "Any time" },
  { id: "today", label: "Today" },
  { id: "7d", label: "Last 7 days" },
  { id: "30d", label: "Last 30 days" },
];

interface TimelinePanelProps {
  events: TimelineEvent[];
  filters: TimelineFilters;
  onFiltersChange: (filters: TimelineFilters) => void;
  queue: QueueStatus;
  runtime?: DashboardRuntimeStatus;
  session: SessionStatus;
}

export function TimelinePanel({
  events,
  filters,
  onFiltersChange,
  queue,
  runtime,
  session,
}: TimelinePanelProps) {
  const latestEvent = events.reduce<TimelineEvent | undefined>(
    (latest, event) =>
      !latest ||
      new Date(event.occurredAt).getTime() >
        new Date(latest.occurredAt).getTime()
        ? event
        : latest,
    undefined,
  );
  const streamLabel =
    session.transport === "disconnected"
      ? "Disconnected"
      : session.state === "reconnecting" || session.transport === "degraded"
        ? "Reconnecting"
        : session.state === "paused"
          ? "Capture paused"
          : session.state === "idle"
            ? "Capture idle"
            : latestEvent
              ? "Live stream"
              : "No recent events";
  return (
    <section
      aria-labelledby="timeline-heading"
      className="panel timeline-panel"
      id="timeline"
    >
      <div className="panel-heading timeline-heading">
        <div>
          <div className="section-kicker">
            <span className={`live-line timeline-line-${session.state}`} />
            {streamLabel}
            <span className="timeline-updated">
              {latestEvent
                ? ` · Latest ${latestEvent.relativeLabel}`
                : " · No events returned"}
            </span>
          </div>
          <h2 id="timeline-heading">Context timeline</h2>
        </div>
        <button className="secondary-button" type="button">
          <Icon name="plus" size={16} /> Add note
        </button>
      </div>

      <QueueNotice queue={queue} runtime={runtime} session={session} />
      <TimelineFiltersBar
        filters={filters}
        onFiltersChange={onFiltersChange}
        resultCount={events.length}
      />

      <div className="timeline-list">
        {events.length > 0 ? (
          events.map((event) => <TimelineItem event={event} key={event.id} />)
        ) : (
          <EmptyTimeline />
        )}
      </div>

      {events.length > 0 ? (
        <button className="load-more-button" type="button">
          Load earlier context <Icon name="chevronDown" size={15} />
        </button>
      ) : null}
    </section>
  );
}

function QueueNotice({
  queue,
  runtime,
  session,
}: {
  queue: QueueStatus;
  runtime?: DashboardRuntimeStatus;
  session: SessionStatus;
}) {
  const hasHistoricalFailures = queue.failed > 0 || queue.deadLettered > 0;
  const captureLabel = captureFreshnessLabel(session);
  const title =
    queue.state === "fresh"
      ? hasHistoricalFailures
        ? `${captureLabel}; semantic index is caught up`
        : `${captureLabel}; semantic index is current`
      : queue.state === "stale"
        ? `${captureLabel}; semantic indexing is behind`
        : `${captureLabel}; semantic indexing is offline`;
  return (
    <div
      aria-label="Capture and semantic indexing status"
      className={`queue-notice queue-${queue.state}`}
      role="status"
    >
      <div className="queue-notice-icon">
        <Icon
          name={queue.state === "offline" ? "cloud" : "refresh"}
          size={15}
        />
      </div>
      <div className="queue-notice-copy">
        <strong>{title}</strong>
        <span>
          {queue.pending > 0
            ? `Oldest pending analysis: ${formatPendingAge(queue.lagSeconds)}. `
            : ""}
          This queue measures semantic analysis, not capture loss.
        </span>
        <span className="queue-guidance">
          {queueGuidance(queue, runtime)}{" "}
          {queue.state !== "fresh" ? (
            <a href="#worker-settings">Open worker settings</a>
          ) : null}
        </span>
      </div>
      <span
        aria-label={`${queue.pending} pending, ${queue.failed} failed, ${queue.deadLettered} dead-lettered`}
        className="queue-count"
      >
        {queue.pending} pending · {queue.failed} failed · {queue.deadLettered}{" "}
        dead-lettered
      </span>
    </div>
  );
}

function TimelineFiltersBar({
  filters,
  onFiltersChange,
  resultCount,
}: {
  filters: TimelineFilters;
  onFiltersChange: (filters: TimelineFilters) => void;
  resultCount: number;
}) {
  return (
    <div className="filters-bar">
      <div
        className="source-filters"
        aria-label="Filter timeline by source"
        role="group"
      >
        {sourceOptions.map((option) => (
          <button
            aria-pressed={filters.source === option.id}
            className={`filter-chip${filters.source === option.id ? " is-selected" : ""}`}
            key={option.id}
            onClick={() => onFiltersChange({ ...filters, source: option.id })}
            type="button"
          >
            {option.id !== "all" ? (
              <SourceIcon source={option.id} size={14} />
            ) : (
              <Icon name="filter" size={14} />
            )}
            {option.label}
          </button>
        ))}
      </div>
      <div className="filter-actions">
        <label className="select-control compact-select">
          <span className="sr-only">Timeline time range</span>
          <select
            aria-label="Timeline time range"
            onChange={(event) =>
              onFiltersChange({
                ...filters,
                dateRange: event.target.value as DateRange,
              })
            }
            value={filters.dateRange}
          >
            {rangeOptions.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
          <Icon name="chevronDown" size={14} />
        </label>
        <button
          aria-pressed={filters.importantOnly}
          className={`important-toggle${filters.importantOnly ? " is-selected" : ""}`}
          onClick={() =>
            onFiltersChange({
              ...filters,
              importantOnly: !filters.importantOnly,
            })
          }
          type="button"
        >
          <span className="important-star">✦</span> Important
        </button>
        <span className="result-count">{resultCount} shown</span>
      </div>
    </div>
  );
}

function TimelineItem({ event }: { event: TimelineEvent }) {
  return (
    <article
      className={`timeline-item${event.important ? " is-important" : ""}`}
    >
      <time className="timeline-time" dateTime={event.occurredAt}>
        {event.relativeLabel}
      </time>
      <div className="timeline-rail" aria-hidden="true">
        <span className="timeline-dot" />
      </div>
      <div className="timeline-content">
        <div className="timeline-item-head">
          <div className={`source-avatar source-${event.source}`}>
            <SourceIcon source={event.source} size={16} />
          </div>
          <div className="timeline-source">
            {event.sourceLabel}
            {event.participant ? <span> · {event.participant}</span> : null}
          </div>
          <button
            aria-label={`More options for ${event.title}`}
            className="icon-button item-more"
            type="button"
          >
            <Icon name="more" size={16} />
          </button>
        </div>
        <h3>
          {event.title}
          {event.important ? (
            <span className="important-mark" title="Important context">
              ✦
            </span>
          ) : null}
        </h3>
        <p>
          {event.evidenceUrl ? (
            <a href={event.evidenceUrl} target="_blank" rel="noreferrer">
              {event.summary}
            </a>
          ) : (
            event.summary
          )}
        </p>
        <div className="tag-row">
          {event.tags.map((tag) => (
            <span className="tag" key={tag}>
              {tag}
            </span>
          ))}
        </div>
      </div>
    </article>
  );
}

function EmptyTimeline() {
  return (
    <div className="empty-state">
      <div className="empty-icon">
        <Icon name="filter" size={20} />
      </div>
      <strong>No context matches these filters</strong>
      <p>Try another source or widen the time range.</p>
    </div>
  );
}
