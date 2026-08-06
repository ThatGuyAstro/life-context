import { useState } from "react";
import { Icon } from "./Icon";
import { SourceIcon } from "./SourceIcon";
import { formatPendingAge, queueGuidance } from "../queueUx";
import type {
  DashboardRuntimeStatus,
  DashboardSettings,
  QueueStatus,
  RemoteConnectionSettings,
  RetentionPurgeRequest,
  SourceRoute,
  SourceRouteSetting,
} from "../types";

interface SettingsPanelProps {
  settings: DashboardSettings;
  runtime?: DashboardRuntimeStatus;
  queue: QueueStatus;
  connectionSettings: RemoteConnectionSettings;
  onChange: (settings: DashboardSettings) => void;
  onConnectionChange: (settings: RemoteConnectionSettings) => void;
  onSave: () => void;
  onPurge: (options: RetentionPurgeRequest) => void;
  saving: boolean;
  saved: boolean;
  purging: boolean;
}

const routeOptions: Array<{ value: SourceRoute; label: string }> = [
  { value: "local", label: "Local only" },
  { value: "local-only", label: "Local only" },
  { value: "remote", label: "Remote server" },
  { value: "remote-only", label: "Remote only" },
  { value: "cloud", label: "Cloud index" },
  { value: "excluded", label: "Do not index" },
];

const plaintextWarningFallback =
  "Raw clipboard text and screenshots are stored as plaintext in the configured archive. Clipboard is local-only by default; configure remote routing explicitly.";

export function SettingsPanel({
  settings,
  runtime,
  queue,
  connectionSettings,
  onChange,
  onConnectionChange,
  onSave,
  onPurge,
  saving,
  saved,
  purging,
}: SettingsPanelProps) {
  const [purgeBefore, setPurgeBefore] = useState("");
  const [purgeSource, setPurgeSource] = useState("");
  const [purgeProject, setPurgeProject] = useState("");
  const [purgeConfirmed, setPurgeConfirmed] = useState(false);

  function updateRoute(
    source: SourceRouteSetting,
    patch: Partial<SourceRouteSetting>,
  ) {
    onChange({
      ...settings,
      routes: settings.routes.map((item) =>
        item.source === source.source ? { ...item, ...patch } : item,
      ),
    });
  }

  function updateRetentionControls(
    patch: Partial<
      Pick<DashboardSettings["retention"], "rawHours" | "keyframeDays">
    >,
  ) {
    onChange({
      ...settings,
      retention: {
        ...settings.retention,
        rawHours:
          settings.retention.rawHours ??
          Math.round(settings.retention.rawPayloadDays * 24),
        keyframeDays: settings.retention.keyframeDays ?? 14,
        ...patch,
      },
    });
  }

  function updateSetting<K extends keyof DashboardSettings>(
    key: K,
    value: DashboardSettings[K],
  ) {
    onChange({ ...settings, [key]: value });
  }

  const canPurge = Boolean(purgeBefore.trim() && purgeConfirmed);
  const workerCount = settings.workerCount ?? runtime?.workerCount ?? 1;
  const queueSize = settings.queueSize ?? runtime?.queueSize ?? 1000;
  const model = runtime?.model;

  function submitPurge() {
    if (!canPurge || purging) return;
    onPurge({
      before: purgeBefore.trim(),
      source: purgeSource.trim(),
      project: purgeProject.trim(),
      confirmed: true,
    });
  }

  return (
    <section
      aria-labelledby="settings-heading"
      className="settings-panel"
      id="settings"
    >
      <div className="settings-heading">
        <div>
          <div className="section-kicker">
            <Icon name="settings" size={14} /> Workspace controls
          </div>
          <h2 id="settings-heading">Keep your context yours.</h2>
          <p>
            Choose where each source is processed, how long it stays available,
            and how the local worker runs.
          </p>
        </div>
        <button
          className="secondary-button save-settings"
          disabled={saving}
          onClick={onSave}
          type="button"
        >
          {saving ? (
            <span className="button-spinner dark" />
          ) : saved ? (
            <Icon name="check" size={15} />
          ) : null}
          {saving ? "Saving" : saved ? "Saved" : "Save changes"}
        </button>
      </div>

      <div className="settings-grid">
        <div className="settings-card source-routing-card">
          <div className="settings-card-heading">
            <div>
              <h3>Source routing</h3>
              <p>Route every configured source independently.</p>
            </div>
            <Icon name="shuffle" size={18} />
          </div>
          <div className="route-list">
            {settings.routes.map((source) => (
              <div
                className={`route-row${source.enabled ? "" : " is-disabled"}`}
                key={source.source}
              >
                <div className={`source-avatar source-${source.source}`}>
                  <SourceIcon source={source.source} size={16} />
                </div>
                <div className="route-copy">
                  <strong>{source.label}</strong>
                  <span>{source.description}</span>
                </div>
                <select
                  aria-label={`${source.label} route`}
                  className="route-select"
                  disabled={!source.enabled}
                  onChange={(event) =>
                    updateRoute(source, {
                      route: event.target.value as SourceRoute,
                    })
                  }
                  value={source.route}
                >
                  {routeOptions.some(
                    (option) => option.value === source.route,
                  ) ? null : (
                    <option
                      value={source.route}
                    >{`Configured: ${source.route}`}</option>
                  )}
                  {routeOptions.map((route) => (
                    <option key={route.value} value={route.value}>
                      {route.label}
                    </option>
                  ))}
                </select>
                <button
                  aria-label={`${source.enabled ? "Disable" : "Enable"} ${source.label}`}
                  aria-pressed={source.enabled}
                  className={`switch${source.enabled ? " is-on" : ""}`}
                  onClick={() =>
                    updateRoute(source, { enabled: !source.enabled })
                  }
                  type="button"
                >
                  <span />
                </button>
              </div>
            ))}
          </div>
        </div>

        <div className="settings-card retention-card">
          <div className="settings-card-heading">
            <div>
              <h3>Retention</h3>
              <p>Automatic deletion windows.</p>
            </div>
            <Icon name="clock" size={18} />
          </div>
          <div className="retention-fields">
            <RetentionField
              label="Context events"
              value={settings.retention.eventsDays}
              options={[30, 90, 365, 730]}
              onChange={(value) =>
                onChange({
                  ...settings,
                  retention: { ...settings.retention, eventsDays: value },
                })
              }
            />
            <RetentionField
              label="Transcripts"
              value={settings.retention.transcriptsDays}
              options={[7, 30, 90, 365]}
              onChange={(value) =>
                onChange({
                  ...settings,
                  retention: { ...settings.retention, transcriptsDays: value },
                })
              }
            />
            <RetentionField
              label="Raw payloads"
              value={settings.retention.rawPayloadDays}
              options={[1, 7, 30, 90]}
              onChange={(value) =>
                onChange({
                  ...settings,
                  retention: { ...settings.retention, rawPayloadDays: value },
                })
              }
            />
            <RetentionNumberField
              label="Raw retention hours"
              value={
                settings.retention.rawHours ??
                Math.round(settings.retention.rawPayloadDays * 24)
              }
              onChange={(value) => updateRetentionControls({ rawHours: value })}
            />
            <RetentionNumberField
              label="Keyframe retention days"
              value={settings.retention.keyframeDays ?? 14}
              onChange={(value) =>
                updateRetentionControls({ keyframeDays: value })
              }
            />
          </div>
          <label className="checkbox-row">
            <input
              checked={settings.retention.deleteAfterExport}
              onChange={(event) =>
                onChange({
                  ...settings,
                  retention: {
                    ...settings.retention,
                    deleteAfterExport: event.target.checked,
                  },
                })
              }
              type="checkbox"
            />
            <span className="checkbox-mark">
              <Icon name="check" size={12} />
            </span>
            Delete exportable context after download
          </label>
        </div>

        <div className="settings-card runtime-card" id="worker-settings">
          <div className="settings-card-heading">
            <div>
              <h3>Worker and model</h3>
              <p>
                Configure the local OpenAI-compatible VLM and inspect health.
              </p>
            </div>
            <Icon name="sparkle" size={18} />
          </div>
          <div className="runtime-status-grid" aria-label="Runtime status">
            <RuntimeStatusLine
              label="Workers"
              value={formatWorkerStatus(
                model,
                runtime?.workerCount ?? workerCount,
              )}
            />
            <RuntimeStatusLine label="Model" value={formatModelStatus(model)} />
            <RuntimeStatusLine
              label="Queue"
              value={`${queue.pending} pending · ${queue.failed} failed · ${queue.deadLettered} dead-lettered`}
            />
          </div>
          <div
            aria-label="Model health and backlog"
            className="runtime-detail-grid"
          >
            <RuntimeDetail
              label="Endpoint"
              value={
                model?.endpoint ?? settings.modelEndpoint ?? "Not configured"
              }
            />
            <RuntimeDetail
              label="Model"
              value={model?.name ?? settings.modelName ?? "Not configured"}
            />
            <RuntimeDetail
              label="Revision"
              value={
                model?.revision ?? settings.modelRevision ?? "Not reported"
              }
            />
            <RuntimeDetail label="Health" value={formatModelHealth(model)} />
            <RuntimeDetail
              label="Processed"
              value={formatMetric(model?.processed)}
            />
            <RuntimeDetail
              label="Active workers"
              value={formatMetric(model?.activeWorkers)}
            />
            <RuntimeDetail
              label="Oldest pending analysis"
              value={formatPendingAge(queue.lagSeconds)}
            />
          </div>
          <div className="runtime-guidance" role="note">
            <strong>Queue guidance</strong>
            <span>{queueGuidance(queue, runtime)}</span>
            <span>
              Pending age describes semantic model work. Capture freshness is
              reported separately in the timeline.
            </span>
          </div>
          {model?.lastError ? (
            <p className="runtime-error" role="alert">
              <Icon name="warning" size={13} /> {model.lastError}
            </p>
          ) : null}
          <div className="runtime-fields">
            <NumberControl
              label="Worker count"
              min={1}
              value={workerCount}
              onChange={(value) => updateSetting("workerCount", value)}
            />
            <NumberControl
              label="Queue capacity"
              min={1}
              value={queueSize}
              onChange={(value) => updateSetting("queueSize", value)}
            />
            <TextControl
              label="Model endpoint"
              value={settings.modelEndpoint ?? ""}
              placeholder="http://127.0.0.1:8080/v1"
              onChange={(value) => updateSetting("modelEndpoint", value)}
            />
            <TextControl
              label="Model name"
              value={settings.modelName ?? ""}
              placeholder="Optional model name"
              onChange={(value) => updateSetting("modelName", value)}
            />
            <TextControl
              label="Model revision"
              value={settings.modelRevision ?? ""}
              placeholder="Optional revision"
              onChange={(value) => updateSetting("modelRevision", value)}
            />
          </div>
        </div>

        <div className="settings-card remote-card">
          <div className="settings-card-heading">
            <div>
              <h3>Remote server</h3>
              <p>
                Use HTTPS and a bearer token for a self-hosted server outside
                this machine.
              </p>
            </div>
            <Icon name="globe" size={18} />
          </div>
          <div className="remote-fields">
            <TextControl
              label="Remote server URL"
              value={connectionSettings.apiBaseUrl}
              placeholder="https://context.example/v1"
              onChange={(value) =>
                onConnectionChange({ ...connectionSettings, apiBaseUrl: value })
              }
            />
            <label className="settings-text-field">
              <span>Bearer token</span>
              <input
                aria-label="Bearer token"
                autoComplete="new-password"
                onChange={(event) =>
                  onConnectionChange({
                    ...connectionSettings,
                    bearerToken: event.target.value,
                  })
                }
                placeholder="Required for authenticated remote servers"
                type="password"
                value={connectionSettings.bearerToken}
              />
            </label>
          </div>
          <label className="checkbox-row remote-https-toggle">
            <input
              aria-label="Require HTTPS for remote server"
              checked={connectionSettings.requireHttpsRemote}
              onChange={(event) =>
                onConnectionChange({
                  ...connectionSettings,
                  requireHttpsRemote: event.target.checked,
                })
              }
              type="checkbox"
            />
            <span className="checkbox-mark">
              <Icon name="check" size={12} />
            </span>
            Require HTTPS for remote connections
          </label>
          <p className="settings-help">
            <Icon name="lock" size={12} /> The token stays in this dashboard
            session and is not sent as a server settings value.
          </p>
        </div>
      </div>

      <div className="purge-controls">
        <div className="settings-card-heading">
          <div>
            <h3>Manual purge</h3>
            <p>
              Remove matching context before it reaches automatic retention.
            </p>
          </div>
          <Icon name="warning" size={18} />
        </div>
        <div className="purge-fields">
          <label className="purge-field">
            <span>Purge before date</span>
            <input
              aria-label="Purge before date"
              onChange={(event) => setPurgeBefore(event.target.value)}
              placeholder="YYYY-MM-DD"
              type="text"
              value={purgeBefore}
            />
          </label>
          <label className="purge-field">
            <span>Purge source</span>
            <input
              aria-label="Purge source"
              onChange={(event) => setPurgeSource(event.target.value)}
              placeholder="Optional source"
              type="text"
              value={purgeSource}
            />
          </label>
          <label className="purge-field">
            <span>Purge project</span>
            <input
              aria-label="Purge project"
              onChange={(event) => setPurgeProject(event.target.value)}
              placeholder="Optional project"
              type="text"
              value={purgeProject}
            />
          </label>
        </div>
        <div className="purge-actions">
          <label className="checkbox-row purge-confirmation">
            <input
              checked={purgeConfirmed}
              onChange={(event) => setPurgeConfirmed(event.target.checked)}
              type="checkbox"
            />
            <span className="checkbox-mark">
              <Icon name="check" size={12} />
            </span>
            Confirm permanent purge
          </label>
          <button
            className="danger-button"
            disabled={!canPurge || purging}
            onClick={submitPurge}
            type="button"
          >
            {purging ? "Purging" : "Purge matching data"}
          </button>
        </div>
      </div>

      <div
        aria-live="polite"
        className={`plaintext-warning${settings.plaintextWarningAcknowledged ? " is-acknowledged" : ""}`}
        role="alert"
      >
        <div className="warning-icon">
          <Icon name="warning" size={18} />
        </div>
        <div className="warning-copy">
          <strong>Plaintext warning</strong>
          <p>{settings.privacyWarning ?? plaintextWarningFallback}</p>
        </div>
        <label className="acknowledge-row">
          <input
            aria-label="I understand"
            checked={settings.plaintextWarningAcknowledged}
            onChange={(event) =>
              onChange({
                ...settings,
                plaintextWarningAcknowledged: event.target.checked,
              })
            }
            type="checkbox"
          />
          <span className="checkbox-mark">
            <Icon name="check" size={12} />
          </span>
          I understand — warning stays visible
        </label>
      </div>
    </section>
  );
}

function formatWorkerStatus(
  model: DashboardRuntimeStatus["model"] | undefined,
  workerCount: number,
): string {
  if (!model) return `${workerCount} configured`;
  return `${workerCount} · ${humanizeStatus(model.workerStatus)}`;
}

function formatModelStatus(
  model: DashboardRuntimeStatus["model"] | undefined,
): string {
  if (!model) return "Not reported";
  return humanizeStatus(model.modelStatus || model.status);
}

function humanizeStatus(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function RuntimeStatusLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="runtime-status-line">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function RuntimeDetail({ label, value }: { label: string; value: string }) {
  return (
    <div className="runtime-detail">
      <span>{label}</span>
      <strong title={value}>{value}</strong>
    </div>
  );
}

function formatMetric(value: number | undefined): string {
  return value === undefined ? "Not reported" : value.toLocaleString();
}

function formatModelHealth(
  model: DashboardRuntimeStatus["model"] | undefined,
): string {
  if (!model?.healthStatus) return "Not reported";
  if (model.healthLatencyMs === undefined)
    return humanizeStatus(model.healthStatus);
  return `${humanizeStatus(model.healthStatus)} · ${model.healthLatencyMs.toFixed(1)}ms`;
}

function NumberControl({
  label,
  min,
  value,
  onChange,
}: {
  label: string;
  min: number;
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="settings-number-field">
      <span>{label}</span>
      <input
        aria-label={label}
        min={min}
        onChange={(event) => onChange(Number(event.target.value))}
        type="number"
        value={value}
      />
    </label>
  );
}

function TextControl({
  label,
  value,
  placeholder,
  onChange,
}: {
  label: string;
  value: string;
  placeholder: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="settings-text-field">
      <span>{label}</span>
      <input
        aria-label={label}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        type="text"
        value={value}
      />
    </label>
  );
}

function RetentionNumberField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <label className="retention-field">
      <span>{label}</span>
      <input
        aria-label={label}
        className="retention-number-input"
        min={1}
        onChange={(event) => onChange(Number(event.target.value))}
        type="number"
        value={value}
      />
    </label>
  );
}

function RetentionField({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: number;
  options: number[];
  onChange: (value: number) => void;
}) {
  return (
    <label className="retention-field">
      <span>{label}</span>
      <span className="retention-select-wrap">
        <select
          aria-label={`${label} retention`}
          onChange={(event) => onChange(Number(event.target.value))}
          value={value}
        >
          {options.map((option) => (
            <option key={option} value={option}>
              {option} days
            </option>
          ))}
        </select>
        <Icon name="chevronDown" size={14} />
      </span>
    </label>
  );
}
