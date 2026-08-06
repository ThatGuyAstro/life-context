import { describe, expect, it, vi } from "vitest";
import { createApiClient } from "./api";
import type { DashboardSettings } from "./types";

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
    status: 200,
  });
}

const liveStatus = {
  event_count: 42,
  last_event_at: "2026-08-05T16:30:00Z",
  queue: {
    failed_count: 2,
    oldest_pending_age_seconds: 241,
    pending_count: 7,
    updated_at: "2026-08-05T16:29:00Z",
  },
  session_id: "session-live",
  started_at: "2026-08-05T08:00:00Z",
  state: "running",
  title: "Live workspace",
};

const liveTimeline = {
  events: [
    {
      artifact_url: "/v1/artifacts/timeline-sha256",
      event_id: "event-live-1",
      occurred_at: "2026-08-05T16:28:00Z",
      source: "documents",
      summary: "A source event with a retrievable artifact.",
      title: "Artifact-backed event",
    },
  ],
};

const liveConfig = {
  plaintext_warning_acknowledged: true,
  retention: {
    delete_after_export: true,
    events_days: 90,
    raw_hours: 48,
    transcripts_days: 30,
  },
  source_routes: [
    {
      description: "Private messages",
      destination: "local",
      enabled: true,
      label: "Messages",
      last_synced_at: "Just now",
      source: "messages",
    },
  ],
};

describe("live dashboard API mapping", () => {
  it("preserves per-source routing and maps worker, model, and queue controls", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/status")) {
        return jsonResponse({
          model: {
            active_workers: 4,
            configured: true,
            endpoint: "http://127.0.0.1:8080/v1",
            model: "local-vlm",
            model_status: "healthy",
            revision: "revision-a",
            status: "running",
            worker_status: "running",
            health: { latency_ms: 0.6, status: "healthy" },
          },
          queue: { oldest_pending_age_seconds: 4, pending: 2 },
          queue_size: 256,
          state: "running",
          worker_count: 4,
        });
      }
      if (url.includes("/timeline?limit=50"))
        return jsonResponse({ events: [] });
      return jsonResponse({
        privacy_warning:
          "Clipboard text is plaintext and local-only by default.",
        settings: {
          model_endpoint: "http://127.0.0.1:8080/v1",
          model_name: "local-vlm",
          model_revision: "revision-a",
          queue_size: 256,
          source_routing: {
            clipboard: { enabled: true, route: "local" },
            screenshots: { enabled: true, route: "remote" },
          },
          worker_count: 4,
        },
      });
    });

    const snapshot = await createApiClient(
      "https://context.example",
      fetcher,
    ).getSnapshot();

    expect(snapshot.settings.routes).toEqual([
      expect.objectContaining({
        source: "clipboard",
        route: "local",
        label: "Clipboard",
      }),
      expect.objectContaining({
        source: "screenshots",
        route: "remote",
        label: "Screenshots",
      }),
    ]);
    expect(snapshot.settings).toMatchObject({
      modelEndpoint: "http://127.0.0.1:8080/v1",
      modelName: "local-vlm",
      modelRevision: "revision-a",
      queueSize: 256,
      workerCount: 4,
      privacyWarning: "Clipboard text is plaintext and local-only by default.",
    });
    expect(snapshot.runtime).toMatchObject({
      queueSize: 256,
      workerCount: 4,
      model: {
        configured: true,
        modelStatus: "healthy",
        workerStatus: "running",
        healthStatus: "healthy",
        healthLatencyMs: 0.6,
      },
    });
  });

  it("maps live queue status and timeline artifact links", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/status")) return jsonResponse(liveStatus);
      if (url.includes("/timeline?limit=50")) return jsonResponse(liveTimeline);
      return jsonResponse(liveConfig);
    });

    const snapshot = await createApiClient(
      "https://context.example",
      fetcher,
    ).getSnapshot();

    expect(snapshot.queue).toMatchObject({
      failed: 2,
      lagSeconds: 241,
      pending: 7,
      state: "stale",
    });
    expect(snapshot.queue.lastIndexedAt).toBe("2026-08-05T16:29:00Z");
    expect(snapshot.timeline[0]).toMatchObject({
      evidenceUrl: "https://context.example/v1/artifacts/timeline-sha256",
      id: "event-live-1",
      title: "Artifact-backed event",
    });
  });

  it("keeps historical failed and dead-lettered counts from the live backlog", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/status"))
        return jsonResponse({
          backlog: {
            dead_lettered_count: 2,
            failed_count: 5,
            oldest_pending_age_seconds: 9,
            pending_count: 3,
          },
          queue: { failed_count: 5, pending: 3 },
          state: "running",
        });
      if (url.includes("/timeline?limit=50"))
        return jsonResponse({ events: [], total_count: 0 });
      return jsonResponse(liveConfig);
    });

    const snapshot = await createApiClient(
      "https://context.example",
      fetcher,
    ).getSnapshot();

    expect(snapshot.queue).toMatchObject({
      deadLettered: 2,
      failed: 5,
      pending: 3,
    });
  });

  it("maps artifact citations returned by a query to absolute evidence links", async () => {
    const fetcher = vi.fn(async () =>
      jsonResponse({
        answer: {
          confidence: "high",
          text: "The answer is supported by the captured artifact.",
        },
        evidence: [
          {
            artifact_url: "/v1/artifacts/evidence-sha256",
            confidence: "high",
            detail: "The cited screenshot contains the relevant context.",
            id: "evidence-1",
            source: "documents",
            title: "Captured screenshot",
          },
        ],
        results: [
          {
            artifact_url: "/v1/artifacts/result-sha256",
            id: "result-1",
            score: 0.91,
            snippet: "A ranked result with an artifact citation.",
            source: "documents",
            title: "Ranked artifact result",
          },
        ],
      }),
    );

    const response = await createApiClient(
      "https://context.example",
      fetcher,
    ).search({ query: "artifact" });

    expect(response.rankedResults[0].evidenceUrl).toBe(
      "https://context.example/v1/artifacts/result-sha256",
    );
    expect(response.answer.evidence[0].url).toBe(
      "https://context.example/v1/artifacts/evidence-sha256",
    );
  });

  it("round trips settings through the live config endpoint", async () => {
    const fetcher = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/status")) return jsonResponse(liveStatus);
        if (url.includes("/timeline?limit=50"))
          return jsonResponse(liveTimeline);
        if (url.endsWith("/config") && init?.method === "POST") {
          return jsonResponse({
            settings: {
              plaintext_warning_acknowledged: true,
              retention: {
                delete_after_export: true,
                events_days: 180,
                raw_hours: 72,
                transcripts_days: 45,
              },
              routes: [
                {
                  description: "Private messages",
                  destination: "excluded",
                  enabled: false,
                  label: "Messages",
                  last_synced_at: "Just now",
                  source: "messages",
                },
              ],
            },
          });
        }
        return jsonResponse(liveConfig);
      },
    );

    const client = createApiClient("https://context.example", fetcher);
    const snapshot = await client.getSnapshot();
    const nextSettings: DashboardSettings = {
      ...snapshot.settings,
      plaintextWarningAcknowledged: true,
      routes: snapshot.settings.routes.map((route) =>
        route.source === "messages"
          ? { ...route, enabled: false, route: "excluded" }
          : route,
      ),
    };

    const savedSettings = await client.updateSettings(nextSettings);
    const postCall = fetcher.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );

    expect(postCall).toBeDefined();
    expect(JSON.parse(String(postCall?.[1]?.body))).toMatchObject({
      operation: "update",
      settings: {
        source_routing: {
          messages: { enabled: false, route: "excluded" },
        },
        retention: { raw_hours: 48, keyframe_days: 14 },
      },
    });
    expect(savedSettings).toMatchObject({
      plaintextWarningAcknowledged: true,
      retention: { eventsDays: 180, rawPayloadDays: 3, transcriptsDays: 45 },
      routes: [{ enabled: false, route: "excluded", source: "messages" }],
    });
  });
});
