import { useEffect, useState } from "react";

import {
  getServiceStatus,
  isTauri,
  listenForServiceStatus,
  sendServiceCommand,
} from "./lib/desktop";
import {
  initialServiceSnapshot,
  type ServiceCommand,
  type ServiceSnapshot,
  statusLabels,
} from "./types";

const DEFAULT_SERVER_URL = "http://127.0.0.1:5173/?api=http://127.0.0.1:8000";
const SERVER_URL_STORAGE_KEY = "life-context.desktop.server-url";

function loadServerUrl(): string {
  if (typeof window === "undefined") {
    return DEFAULT_SERVER_URL;
  }

  try {
    const stored = window.localStorage.getItem(SERVER_URL_STORAGE_KEY)?.trim() ?? "";
    return isAllowedDashboardUrl(stored) ? stored : DEFAULT_SERVER_URL;
  } catch {
    return DEFAULT_SERVER_URL;
  }
}

function formatError(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }

  return String(error);
}

function formatUpdatedAt(updatedAtMs: number): string {
  if (!updatedAtMs) {
    return "Not reported yet";
  }

  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(updatedAtMs));
}

function hostFor(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function isLoopbackHost(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  return normalized === "localhost" || normalized === "127.0.0.1" || normalized === "::1";
}

function isAllowedDashboardUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    if (parsed.username || parsed.password) {
      return false;
    }
    if (parsed.protocol === "https:") {
      return true;
    }
    return parsed.protocol === "http:" && isLoopbackHost(parsed.hostname);
  } catch {
    return false;
  }
}

function browserPreviewSnapshot(
  command: ServiceCommand,
): ServiceSnapshot {
  const statusByCommand: Record<ServiceCommand, ServiceSnapshot["status"]> = {
    start: "running",
    pause: "paused",
    stop: "stopped",
  };

  return {
    status: statusByCommand[command],
    detail: `${command[0].toUpperCase()}${command.slice(1)} intent simulated in browser preview.`,
    updatedAtMs: Date.now(),
  };
}

function App() {
  const [snapshot, setSnapshot] = useState(initialServiceSnapshot);
  const [serverUrl, setServerUrl] = useState(loadServerUrl);
  const [serverUrlDraft, setServerUrlDraft] = useState(loadServerUrl);
  const [serverUrlError, setServerUrlError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [dashboardRevision, setDashboardRevision] = useState(0);

  useEffect(() => {
    if (!isTauri) {
      return;
    }

    let disposed = false;
    let unlisten: (() => void) | undefined;

    void getServiceStatus()
      .then((nextSnapshot) => {
        if (!disposed) {
          setSnapshot(nextSnapshot);
        }
      })
      .catch((error: unknown) => {
        if (!disposed) {
          setNotice(`Status read failed: ${formatError(error)}`);
        }
      });

    void listenForServiceStatus((nextSnapshot) => {
      if (!disposed) {
        setSnapshot(nextSnapshot);
        setBusy(false);
      }
    })
      .then((cleanup) => {
        if (disposed) {
          cleanup();
        } else {
          unlisten = cleanup;
        }
      })
      .catch((error: unknown) => {
        if (!disposed) {
          setNotice(`Event subscription failed: ${formatError(error)}`);
        }
      });

    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  async function refreshStatus() {
    if (!isTauri) {
      setNotice("Browser preview: no native supervisor is attached.");
      return;
    }

    setBusy(true);
    try {
      setSnapshot(await getServiceStatus());
      setNotice("Status refreshed.");
    } catch (error: unknown) {
      setNotice(`Status read failed: ${formatError(error)}`);
    } finally {
      setBusy(false);
    }
  }

  async function runCommand(command: ServiceCommand) {
    setNotice("");
    setBusy(true);

    if (!isTauri) {
      setSnapshot(browserPreviewSnapshot(command));
      setNotice("Browser preview: service intent simulated locally.");
      setBusy(false);
      return;
    }

    try {
      setSnapshot(await sendServiceCommand(command));
    } catch (error: unknown) {
      setNotice(`${command} intent failed: ${formatError(error)}`);
    } finally {
      setBusy(false);
    }
  }

  function applyServerUrl() {
    const candidate = serverUrlDraft.trim();

    if (!isAllowedDashboardUrl(candidate)) {
      setServerUrlError("Use HTTPS for remote dashboards; HTTP is limited to localhost.");
      return;
    }

    setServerUrl(candidate);
    setServerUrlError("");
    setDashboardRevision((revision) => revision + 1);

    try {
      window.localStorage.setItem(SERVER_URL_STORAGE_KEY, candidate);
    } catch {
      setNotice("Target updated for this session; local storage is unavailable.");
      return;
    }

    setNotice("Dashboard target updated.");
  }

  const startDisabled =
    busy || snapshot.status === "running" || snapshot.status === "starting";
  const pauseDisabled = busy || snapshot.status !== "running";
  const stopDisabled =
    busy || snapshot.status === "stopped" || snapshot.status === "stopping";

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">
            LC
          </div>
          <div>
            <p className="brand-name">Life Context</p>
            <p className="brand-subtitle">Desktop control shell</p>
          </div>
        </div>

        <div className="topbar-actions">
          <span className={`runtime-pill ${isTauri ? "native" : "preview"}`}>
            <span className="runtime-dot" aria-hidden="true" />
            {isTauri ? "Tauri desktop" : "Browser preview"}
          </span>
          <button className="text-button" type="button" onClick={() => void refreshStatus()}>
            Refresh status
          </button>
        </div>
      </header>

      <main className="workspace">
        <section className="control-panel panel" aria-labelledby="control-heading">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Service control</p>
              <h1 id="control-heading">Keep the runtime close at hand.</h1>
            </div>
            <div className={`status-badge status-${snapshot.status}`}>
              <span className="status-dot" aria-hidden="true" />
              {statusLabels[snapshot.status]}
            </div>
          </div>

          <div className="status-card">
            <div>
              <p className="status-card-label">Current supervisor state</p>
              <p className="status-detail">{snapshot.detail}</p>
            </div>
            <p className="status-time">Updated {formatUpdatedAt(snapshot.updatedAtMs)}</p>
          </div>

          <div className="control-grid" aria-label="Service controls">
            <button
              className="control-button primary"
              type="button"
              disabled={startDisabled}
              onClick={() => void runCommand("start")}
            >
              <span className="button-icon" aria-hidden="true">
                ▶
              </span>
              Start
            </button>
            <button
              className="control-button secondary"
              type="button"
              disabled={pauseDisabled}
              onClick={() => void runCommand("pause")}
            >
              <span className="button-icon" aria-hidden="true">
                ‖
              </span>
              Pause
            </button>
            <button
              className="control-button danger"
              type="button"
              disabled={stopDisabled}
              onClick={() => void runCommand("stop")}
            >
              <span className="button-icon" aria-hidden="true">
                ■
              </span>
              Stop
            </button>
          </div>

          <div className="boundary-note">
            <span className="note-icon" aria-hidden="true">
              ↗
            </span>
            <p>
              Native actions are intents only. The service-supervisor boundary
              owns sidecar/process policy.
            </p>
          </div>

          <div className="server-config">
            <div className="field-heading">
              <div>
                <p className="eyebrow">Connection</p>
                <h2>Dashboard server URL</h2>
              </div>
              <span className="field-hint">Saved on this device</span>
            </div>
            <div className="url-row">
              <label className="sr-only" htmlFor="server-url">
                Dashboard server URL
              </label>
              <input
                id="server-url"
                type="url"
                inputMode="url"
                value={serverUrlDraft}
                onChange={(event) => setServerUrlDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    applyServerUrl();
                  }
                }}
                aria-invalid={Boolean(serverUrlError)}
                placeholder={DEFAULT_SERVER_URL}
              />
              <button className="apply-button" type="button" onClick={applyServerUrl}>
                Apply
              </button>
            </div>
            {serverUrlError ? <p className="field-error">{serverUrlError}</p> : null}
          </div>
        </section>

        <section className="dashboard-panel panel" aria-labelledby="dashboard-heading">
          <div className="dashboard-heading">
            <div>
              <p className="eyebrow">Dashboard webview target</p>
              <h2 id="dashboard-heading">Live workspace</h2>
              <p className="dashboard-description">
                The configured HTTP(S) dashboard stays inside this desktop shell.
              </p>
            </div>
            <div className="target-actions">
              <span className="target-host" title={serverUrl}>
                {hostFor(serverUrl)}
              </span>
              <button
                className="reload-button"
                type="button"
                onClick={() => setDashboardRevision((revision) => revision + 1)}
              >
                Reload
              </button>
            </div>
          </div>

          <div className="webview-frame">
            <iframe
              key={`${serverUrl}-${dashboardRevision}`}
              title="Configured dashboard"
              src={serverUrl}
              loading="eager"
              referrerPolicy="no-referrer"
            />
            <div className="webview-statusbar">
              <span>
                <span className="webview-dot" aria-hidden="true" />
                Embedded target
              </span>
              <a href={serverUrl} target="_blank" rel="noreferrer">
                Open target ↗
              </a>
            </div>
          </div>
        </section>
      </main>

      <footer className="app-footer">
        <span>Tray menu mirrors the service intents.</span>
        <span className="footer-notice" role="status" aria-live="polite">
          {notice}
        </span>
      </footer>
    </div>
  );
}

export default App;
