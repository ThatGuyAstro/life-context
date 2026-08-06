export type ServiceStatus =
  | "stopped"
  | "starting"
  | "running"
  | "paused"
  | "stopping"
  | "error";

export type ServiceCommand = "start" | "pause" | "stop";

export interface ServiceSnapshot {
  status: ServiceStatus;
  detail: string;
  updatedAtMs: number;
}

export const initialServiceSnapshot: ServiceSnapshot = {
  status: "stopped",
  detail: "No service process is attached; ready for a start intent.",
  updatedAtMs: 0,
};

export const statusLabels: Record<ServiceStatus, string> = {
  stopped: "Stopped",
  starting: "Starting",
  running: "Running",
  paused: "Paused",
  stopping: "Stopping",
  error: "Needs attention",
};
