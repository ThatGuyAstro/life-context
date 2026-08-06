import type { ReactNode, SVGProps } from "react";

export type IconName =
  | "arrowUpRight"
  | "bell"
  | "calendar"
  | "check"
  | "chevronDown"
  | "chevronRight"
  | "clock"
  | "close"
  | "cloud"
  | "database"
  | "document"
  | "filter"
  | "folder"
  | "globe"
  | "info"
  | "lock"
  | "menu"
  | "message"
  | "moon"
  | "more"
  | "pause"
  | "play"
  | "plus"
  | "refresh"
  | "search"
  | "settings"
  | "shuffle"
  | "sparkle"
  | "sun"
  | "task"
  | "warning"
  | "x";

const paths: Record<IconName, ReactNode> = {
  arrowUpRight: (
    <>
      <path d="M7 17 17 7" />
      <path d="M8 7h9v9" />
    </>
  ),
  bell: (
    <>
      <path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9" />
      <path d="M10 21h4" />
    </>
  ),
  calendar: (
    <>
      <rect x="3" y="4" width="18" height="17" rx="2" />
      <path d="M16 2v4M8 2v4M3 10h18" />
    </>
  ),
  check: <path d="m5 12 4 4L19 6" />,
  chevronDown: <path d="m6 9 6 6 6-6" />,
  chevronRight: <path d="m9 6 6 6-6 6" />,
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  close: <path d="m6 6 12 12M18 6 6 18" />,
  cloud: (
    <path d="M7 18h10a4 4 0 0 0 .5-7.97A5.5 5.5 0 0 0 7 8.5a4.5 4.5 0 0 0 0 9.5Z" />
  ),
  database: (
    <>
      <ellipse cx="12" cy="5" rx="7" ry="3" />
      <path d="M5 5v7c0 1.66 3.13 3 7 3s7-1.34 7-3V5M5 12v7c0 1.66 3.13 3 7 3s7-1.34 7-3v-7" />
    </>
  ),
  document: (
    <>
      <path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z" />
      <path d="M14 3v6h6M8 13h8M8 17h5" />
    </>
  ),
  filter: (
    <>
      <path d="M4 6h16M7 12h10M10 18h4" />
    </>
  ),
  folder: (
    <path d="M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" />
  ),
  globe: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5M12 8h.01" />
    </>
  ),
  lock: (
    <>
      <rect x="5" y="10" width="14" height="11" rx="2" />
      <path d="M8 10V7a4 4 0 0 1 8 0v3" />
    </>
  ),
  menu: <path d="M4 7h16M4 12h16M4 17h16" />,
  message: (
    <>
      <path d="M20 11.5a7.5 7.5 0 0 1-8 7.5 8.4 8.4 0 0 1-3.3-.66L4 20l1.54-3.53A7.3 7.3 0 0 1 4 11.5 7.5 7.5 0 0 1 12 4a7.5 7.5 0 0 1 8 7.5Z" />
      <path d="M8 12h.01M12 12h.01M16 12h.01" />
    </>
  ),
  moon: (
    <path d="M20 15.7A8.5 8.5 0 0 1 8.3 4 8.5 8.5 0 1 0 20 15.7Z" />
  ),
  more: (
    <>
      <circle cx="5" cy="12" r="1" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" />
      <circle cx="19" cy="12" r="1" fill="currentColor" stroke="none" />
    </>
  ),
  pause: <path d="M9 5v14M15 5v14" />,
  play: <path d="m9 5 10 7-10 7Z" />,
  plus: <path d="M12 5v14M5 12h14" />,
  refresh: (
    <>
      <path d="M20 11a8 8 0 0 0-14.8-3L3 11" />
      <path d="M3 5v6h6M4 13a8 8 0 0 0 14.8 3L21 13" />
      <path d="M21 19v-6h-6" />
    </>
  ),
  search: (
    <>
      <circle cx="10.7" cy="10.7" r="6.7" />
      <path d="m16 16 5 5" />
    </>
  ),
  settings: (
    <>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-1.48 1.48-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.04 1.56V20h-2.1v-.42a1.7 1.7 0 0 0-1.04-1.56 1.7 1.7 0 0 0-1.88.34l-.06.06-1.48-1.48.06-.06A1.7 1.7 0 0 0 9.2 15a1.7 1.7 0 0 0-1.56-1.04H7.2v-2.1h.44A1.7 1.7 0 0 0 9.2 10.8a1.7 1.7 0 0 0-.34-1.88l-.06-.06 1.48-1.48.06.06a1.7 1.7 0 0 0 1.88.34 1.7 1.7 0 0 0 1.04-1.56V5.8h2.1v.42a1.7 1.7 0 0 0 1.04 1.56 1.7 1.7 0 0 0 1.88-.34l.06-.06 1.48 1.48-.06.06A1.7 1.7 0 0 0 19.4 10.8a1.7 1.7 0 0 0 1.56 1.04h.44v2.1h-.44A1.7 1.7 0 0 0 19.4 15Z" />
    </>
  ),
  shuffle: (
    <>
      <path d="M3 7h2.5a4 4 0 0 1 3.2 1.6l6.6 8.8A4 4 0 0 0 18.5 19H21" />
      <path d="m18 16 3 3-3 3M3 17h2.5a4 4 0 0 0 3.2-1.6l1.15-1.53M15.7 8.1l1.6-2.1A4 4 0 0 1 20.5 4H21" />
      <path d="m18 7 3-3-3-3" />
    </>
  ),
  sparkle: (
    <>
      <path d="m12 3-1.2 4.3L7 9l3.8 1.7L12 15l1.2-4.3L17 9l-3.8-1.7Z" />
      <path d="m19 15-.6 2.1L16 18l2.4.9L19 21l.6-2.1L22 18l-2.4-.9ZM5 14l-.6 2.1L2 17l2.4.9L5 20l.6-2.1L8 17l-2.4-.9Z" />
    </>
  ),
  sun: (
    <>
      <circle cx="12" cy="12" r="3.5" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42" />
    </>
  ),
  task: (
    <>
      <rect x="4" y="4" width="16" height="16" rx="3" />
      <path d="m8 12 2.5 2.5L16 9M8 8h.01M8 16h.01" />
    </>
  ),
  warning: (
    <>
      <path d="m12 3 9 17H3Z" />
      <path d="M12 9v4M12 16h.01" />
    </>
  ),
  x: <path d="m7 7 10 10M17 7 7 17" />,
};

interface IconProps extends SVGProps<SVGSVGElement> {
  name: IconName;
  size?: number;
}

export function Icon({ name, size = 18, ...props }: IconProps) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height={size}
      viewBox="0 0 24 24"
      width={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.7"
      {...props}
    >
      {paths[name]}
    </svg>
  );
}
