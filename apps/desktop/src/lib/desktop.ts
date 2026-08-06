import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

import type { ServiceCommand, ServiceSnapshot } from "../types";

declare global {
  interface Window {
    __TAURI_INTERNALS__?: unknown;
  }
}

export const isTauri =
  typeof window !== "undefined" && Boolean(window.__TAURI_INTERNALS__);

const commandNames: Record<ServiceCommand, string> = {
  start: "start_service",
  pause: "pause_service",
  stop: "stop_service",
};

export function getServiceStatus(): Promise<ServiceSnapshot> {
  return invoke<ServiceSnapshot>("get_service_status");
}

export function sendServiceCommand(
  command: ServiceCommand,
): Promise<ServiceSnapshot> {
  return invoke<ServiceSnapshot>(commandNames[command]);
}

export function listenForServiceStatus(
  handler: (snapshot: ServiceSnapshot) => void,
): Promise<() => void> {
  return listen<ServiceSnapshot>("service-status", (event) => {
    handler(event.payload);
  });
}
