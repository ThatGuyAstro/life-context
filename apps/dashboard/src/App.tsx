import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createApiClient,
  configuredApiBaseUrl,
  configuredApiBearerToken,
  validateModelEndpoint,
  validateRemoteConnection,
} from "./api";
import { Icon } from "./components/Icon";
import { QueryPanel } from "./components/QueryPanel";
import { SessionOverview, MetricsStrip } from "./components/SessionOverview";
import { SettingsPanel } from "./components/SettingsPanel";
import { Sidebar, type DashboardSection } from "./components/Sidebar";
import { TimelinePanel } from "./components/TimelinePanel";
import { TopBar } from "./components/TopBar";
import type {
  DashboardApi,
  DashboardSettings,
  DashboardSnapshot,
  RemoteConnectionSettings,
  RetentionPurgeRequest,
  TimelineFilters,
} from "./types";
import "./styles.css";

const defaultApi = createApiClient();

const defaultFilters: TimelineFilters = {
  source: "all",
  dateRange: "all",
  importantOnly: false,
};

interface AppProps {
  api?: DashboardApi;
}

export function App({ api = defaultApi }: AppProps) {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filters, setFilters] = useState<TimelineFilters>(defaultFilters);
  const [query, setQuery] = useState("");
  const [activeSection, setActiveSection] =
    useState<DashboardSection>("overview");
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [searching, setSearching] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsSaved, setSettingsSaved] = useState(false);
  const [purging, setPurging] = useState(false);
  const [connectionSettings, setConnectionSettings] =
    useState<RemoteConnectionSettings>(() => ({
      apiBaseUrl: configuredApiBaseUrl === "/api" ? "" : configuredApiBaseUrl,
      bearerToken: configuredApiBearerToken,
      requireHttpsRemote: true,
    }));

  const loadSnapshot = useCallback(async () => {
    setError(null);
    setRefreshing(true);
    try {
      const nextSnapshot = await api.getSnapshot();
      setSnapshot(nextSnapshot);
    } catch (loadError) {
      setError(
        loadError instanceof Error
          ? loadError.message
          : "Unable to load the dashboard.",
      );
    } finally {
      setRefreshing(false);
    }
  }, [api]);

  useEffect(() => {
    void loadSnapshot();
  }, [loadSnapshot]);

  const visibleTimeline = useMemo(() => {
    if (!snapshot) return [];
    const anchor = new Date(
      snapshot.timeline[0]?.occurredAt ?? snapshot.session.lastEventAt,
    ).getTime();
    return snapshot.timeline.filter((event) => {
      if (filters.source !== "all" && event.source !== filters.source)
        return false;
      if (filters.importantOnly && !event.important) return false;
      if (filters.dateRange === "all") return true;
      const occurredAt = new Date(event.occurredAt).getTime();
      if (filters.dateRange === "today") {
        return (
          new Date(event.occurredAt).toDateString() ===
          new Date(anchor).toDateString()
        );
      }
      const rangeDays = filters.dateRange === "7d" ? 7 : 30;
      return anchor - occurredAt <= rangeDays * 24 * 60 * 60 * 1000;
    });
  }, [filters, snapshot]);

  async function handleSearch() {
    const normalizedQuery = query.trim();
    if (!normalizedQuery) return;
    setError(null);
    setSearching(true);
    try {
      const response = await api.search({ query: normalizedQuery, filters });
      setSnapshot((current) =>
        current
          ? {
              ...current,
              answer: response.answer,
              rankedResults: response.rankedResults,
            }
          : current,
      );
      window.setTimeout(() => {
        const answer = document.getElementById("context-answer");
        if (!answer) return;
        const bounds = answer.getBoundingClientRect();
        if (bounds.top < 0 || bounds.bottom > window.innerHeight) {
          answer.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
        }
        answer.focus({ preventScroll: true });
      }, 0);
    } catch (searchError) {
      setError(
        searchError instanceof Error
          ? searchError.message
          : "Unable to search this context.",
      );
    } finally {
      setSearching(false);
    }
  }

  function updateSettings(settings: DashboardSettings) {
    setSnapshot((current) => (current ? { ...current, settings } : current));
    setSettingsSaved(false);
  }

  async function saveSettings() {
    if (!snapshot) return;
    const modelEndpointError = validateModelEndpoint(
      snapshot.settings.modelEndpoint ?? "",
    );
    if (modelEndpointError) {
      setError(modelEndpointError);
      return;
    }
    const connectionError = validateRemoteConnection(connectionSettings);
    if (connectionError) {
      setError(connectionError);
      return;
    }
    setSettingsSaving(true);
    setSettingsSaved(false);
    setError(null);
    try {
      const savedSettings = await api.updateSettings(snapshot.settings);
      const mergedSettings: DashboardSettings = {
        ...savedSettings,
        plaintextWarningAcknowledged:
          snapshot.settings.plaintextWarningAcknowledged,
        privacyWarning:
          savedSettings.privacyWarning ?? snapshot.settings.privacyWarning,
      };
      setSnapshot((current) =>
        current ? { ...current, settings: mergedSettings } : current,
      );
      api.setConnectionSettings?.(connectionSettings);
      setSettingsSaved(true);
    } catch (saveError) {
      setError(
        saveError instanceof Error
          ? saveError.message
          : "Unable to save settings.",
      );
    } finally {
      setSettingsSaving(false);
    }
  }

  async function purgeContext(options: RetentionPurgeRequest) {
    if (!api.purge) {
      setError("Manual purge is unavailable for this dashboard connection.");
      return;
    }
    setPurging(true);
    setError(null);
    try {
      await api.purge(options);
    } catch (purgeError) {
      setError(
        purgeError instanceof Error
          ? purgeError.message
          : "Unable to purge context.",
      );
    } finally {
      setPurging(false);
    }
  }

  function navigate(section: DashboardSection) {
    setActiveSection(section);
    const targetId = section === "overview" ? "overview" : section;
    window.setTimeout(
      () =>
        document
          .getElementById(targetId)
          ?.scrollIntoView({ behavior: "smooth", block: "start" }),
      0,
    );
  }

  function toggleSession() {
    setSnapshot((current) =>
      current
        ? {
            ...current,
            session: {
              ...current.session,
              state: current.session.state === "paused" ? "live" : "paused",
            },
          }
        : current,
    );
  }

  if (error && !snapshot) {
    return <ErrorState message={error} onRetry={() => void loadSnapshot()} />;
  }

  if (!snapshot) {
    return <LoadingState />;
  }

  return (
    <div className="app-shell">
      <Sidebar
        activeSection={activeSection}
        mobileOpen={mobileNavOpen}
        onClose={() => setMobileNavOpen(false)}
        onNavigate={navigate}
        queue={snapshot.queue}
        runtime={snapshot.runtime}
        session={snapshot.session}
        timelineCount={snapshot.timelineTotalCount ?? snapshot.timeline.length}
      />
      <div className="main-area">
        <TopBar
          apiMode={configuredApiBaseUrl ? "connected" : "demo"}
          onMenuClick={() => setMobileNavOpen(true)}
          onRefresh={() => void loadSnapshot()}
          refreshing={refreshing}
          session={snapshot.session}
        />
        <main className="content">
          {error ? (
            <div className="inline-error" role="alert">
              <Icon name="warning" size={15} /> {error}
              <button onClick={() => setError(null)} type="button">
                <Icon name="close" size={14} />
              </button>
            </div>
          ) : null}
          <SessionOverview
            onPause={toggleSession}
            queue={snapshot.queue}
            session={snapshot.session}
          />
          <MetricsStrip
            queue={snapshot.queue}
            session={snapshot.session}
            timeline={snapshot.timeline}
          />
          <div className="dashboard-grid">
            <TimelinePanel
              events={visibleTimeline}
              filters={filters}
              onFiltersChange={setFilters}
              queue={snapshot.queue}
              runtime={snapshot.runtime}
              session={snapshot.session}
            />
            <QueryPanel
              answer={snapshot.answer}
              loading={searching}
              onQueryChange={setQuery}
              onSubmit={() => void handleSearch()}
              query={query}
              results={snapshot.rankedResults}
            />
          </div>
          <SettingsPanel
            connectionSettings={connectionSettings}
            onChange={updateSettings}
            onConnectionChange={setConnectionSettings}
            onPurge={(options) => void purgeContext(options)}
            onSave={() => void saveSettings()}
            purging={purging}
            queue={snapshot.queue}
            saved={settingsSaved}
            saving={settingsSaving}
            settings={snapshot.settings}
            runtime={snapshot.runtime}
          />
          <footer className="content-footer">
            <span>
              <Icon name="lock" size={13} /> Your workspace is private by
              default.
            </span>
            <span>
              Last refreshed just now ·{" "}
              <button type="button">Privacy guide</button>
            </span>
          </footer>
        </main>
      </div>
    </div>
  );
}

function LoadingState() {
  return (
    <div aria-busy="true" className="loading-screen">
      <div className="loading-mark">
        <span />
        <span />
        <span />
      </div>
      <p>Loading your context…</p>
    </div>
  );
}

function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="error-screen">
      <div className="error-card">
        <div className="warning-icon">
          <Icon name="warning" size={20} />
        </div>
        <div>
          <span className="section-kicker">Dashboard unavailable</span>
          <h1>We could not load your context.</h1>
          <p>{message}</p>
        </div>
        <button className="primary-button" onClick={onRetry} type="button">
          <Icon name="refresh" size={15} /> Try again
        </button>
      </div>
    </div>
  );
}
