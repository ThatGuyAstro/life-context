import { Icon } from "./Icon";
import { getDashboardSystemStatus } from "../dashboardStatus";
import type {
  DashboardRuntimeStatus,
  QueueStatus,
  SessionStatus,
} from "../types";

export type DashboardSection = "overview" | "timeline" | "settings";

interface SidebarProps {
  activeSection: DashboardSection;
  onNavigate: (section: DashboardSection) => void;
  mobileOpen: boolean;
  onClose: () => void;
  session: SessionStatus;
  queue: QueueStatus;
  runtime?: DashboardRuntimeStatus;
  timelineCount: number;
}

const navItems: Array<{ id: DashboardSection; label: string; icon: "folder" | "clock" | "settings" }> = [
  { id: "overview", label: "Overview", icon: "folder" },
  { id: "timeline", label: "Timeline", icon: "clock" },
  { id: "settings", label: "Settings", icon: "settings" },
];

export function Sidebar({
  activeSection,
  onNavigate,
  mobileOpen,
  onClose,
  session,
  queue,
  runtime,
  timelineCount,
}: SidebarProps) {
  const systemStatus = getDashboardSystemStatus(session, queue, runtime);
  return (
    <>
      <button
        aria-label="Close navigation"
        className={`sidebar-scrim${mobileOpen ? " is-visible" : ""}`}
        onClick={onClose}
        type="button"
      />
      <aside className={`sidebar${mobileOpen ? " is-open" : ""}`}>
        <div className="brand-lockup">
          <div aria-hidden="true" className="brand-mark">
            <span />
            <span />
            <span />
          </div>
          <div>
            <div className="brand-name">Life Context</div>
            <div className="brand-kicker">Personal knowledge</div>
          </div>
          <button aria-label="Close navigation" className="icon-button sidebar-close" onClick={onClose} type="button">
            <Icon name="close" size={18} />
          </button>
        </div>

        <div className="workspace-switcher">
          <div className="workspace-avatar">LC</div>
          <div className="workspace-copy">
            <strong>Personal workspace</strong>
            <span>Private · local-first</span>
          </div>
          <Icon name="chevronDown" size={15} />
        </div>

        <nav aria-label="Dashboard navigation" className="sidebar-nav">
          <span className="nav-label">Workspace</span>
          {navItems.map((item) => (
            <button
              className={`nav-item${activeSection === item.id ? " is-active" : ""}`}
              key={item.id}
              onClick={() => {
                onNavigate(item.id);
                onClose();
              }}
              type="button"
            >
              <Icon name={item.icon} size={18} />
              <span>{item.label}</span>
              {item.id === "timeline" ? (
                <span className="nav-count">{timelineCount.toLocaleString()}</span>
              ) : null}
            </button>
          ))}
        </nav>

        <div className="sidebar-spacer" />

        <div className="sidebar-tip">
          <div className="tip-icon"><Icon name="sparkle" size={17} /></div>
          <strong>Search your day</strong>
          <p>Ask about a person, decision, or the thread you lost.</p>
          <button className="text-button" onClick={() => onNavigate("overview")} type="button">
            Try a query <Icon name="arrowUpRight" size={14} />
          </button>
        </div>

        <div className="sidebar-footer">
          <span className={`footer-status footer-status-${systemStatus.tone}`}>
            <span className="footer-pulse" /> {systemStatus.label}
          </span>
          <span className="sidebar-version">v0.1 preview</span>
        </div>
      </aside>
    </>
  );
}
