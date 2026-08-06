import { describe, expect, it } from "vitest";
import {
  captureFreshnessLabel,
  formatPendingAge,
  queueGuidance,
} from "./queueUx";
import type {
  DashboardRuntimeStatus,
  QueueStatus,
  SessionStatus,
} from "./types";

const session: SessionStatus = {
  id: "test-session",
  eventCount: 0,
  lastEventAt: "2026-08-05T12:00:00Z",
  participantCount: 0,
  privacyMode: "private",
  startedAt: "2026-08-05T12:00:00Z",
  state: "live",
  title: "Test",
  transport: "connected",
};

const queue: QueueStatus = {
  deadLettered: 0,
  failed: 0,
  lagSeconds: 2_382,
  lastIndexedAt: "2026-08-05T12:00:00Z",
  message: "",
  pending: 2_382,
  state: "stale",
};

describe("backlog UX", () => {
  it("formats the oldest pending age without presenting it as capture age", () => {
    expect(formatPendingAge(2_382)).toBe("39m 42s");
    expect(formatPendingAge(7_260)).toBe("2h 1m");
    expect(formatPendingAge(0)).toBe("None waiting");
  });

  it("reports capture freshness independently from semantic indexing", () => {
    expect(captureFreshnessLabel(session)).toBe("Capture is live");
    expect(captureFreshnessLabel({ ...session, state: "paused" })).toBe(
      "Capture paused",
    );
  });

  it("offers worker guidance based on reported runtime capacity", () => {
    const runtime: DashboardRuntimeStatus = {
      model: {
        activeWorkers: 1,
        configured: true,
        modelStatus: "healthy",
        status: "healthy",
        workerStatus: "running",
      },
      queueSize: 4_000,
      workerCount: 1,
    };

    expect(queueGuidance(queue, runtime)).toMatch(
      /increase Worker count.*watch VRAM/i,
    );
    expect(
      queueGuidance(queue, {
        ...runtime,
        model: { ...runtime.model, activeWorkers: 0 },
      }),
    ).toMatch(/No active inference worker/i);
  });
});
