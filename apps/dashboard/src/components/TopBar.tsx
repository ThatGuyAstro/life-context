import { Icon } from "./Icon";
import { StatusBadge } from "./StatusBadge";
import type { SessionStatus } from "../types";

interface TopBarProps {
  session: SessionStatus;
  apiMode: "demo" | "connected";
  onMenuClick: () => void;
  onRefresh: () => void;
  refreshing: boolean;
}

export function TopBar({ session, apiMode, onMenuClick, onRefresh, refreshing }: TopBarProps) {
  return (
    <header className="topbar">
      <button aria-label="Open navigation" className="icon-button mobile-menu" onClick={onMenuClick} type="button">
        <Icon name="menu" size={20} />
      </button>
      <div className="breadcrumbs" aria-label="Breadcrumb">
        <span>Personal workspace</span>
        <Icon name="chevronRight" size={13} />
        <strong>Dashboard</strong>
      </div>
      <div className="topbar-actions">
        <span className={`api-mode ${apiMode === "connected" ? "api-mode-connected" : ""}`}>
          <span className="api-mode-dot" />
          {apiMode === "connected" ? "API connected" : "Demo data"}
        </span>
        <button aria-label="Refresh dashboard" className={`icon-button${refreshing ? " is-spinning" : ""}`} onClick={onRefresh} type="button">
          <Icon name="refresh" size={18} />
        </button>
        <button aria-label="Notifications" className="icon-button has-notification" type="button">
          <Icon name="bell" size={18} />
        </button>
        <div className="topbar-avatar" title="Personal workspace">AK</div>
      </div>
    </header>
  );
}

export function SessionBadge({ session }: { session: SessionStatus }) {
  return <StatusBadge state={session.state} label={session.state === "live" ? "Session live" : undefined} />;
}
