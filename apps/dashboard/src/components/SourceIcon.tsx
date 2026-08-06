import { Icon, type IconName } from "./Icon";
import type { SourceKind } from "../types";

const sourceIcons: Record<SourceKind, IconName> = {
  messages: "message",
  documents: "document",
  calendar: "calendar",
  tasks: "task",
};

export function SourceIcon({
  source,
  size = 17,
}: {
  source: SourceKind | string;
  size?: number;
}) {
  const normalized = source.toLowerCase();
  const icon =
    sourceIcons[source as SourceKind] ??
    (normalized.includes("clip") ||
    normalized.includes("message") ||
    normalized.includes("chat")
      ? "message"
      : undefined) ??
    (normalized.includes("calendar") || normalized.includes("meeting")
      ? "calendar"
      : undefined) ??
    (normalized.includes("task") || normalized.includes("todo")
      ? "task"
      : undefined) ??
    "document";
  return <Icon name={icon} size={size} />;
}
