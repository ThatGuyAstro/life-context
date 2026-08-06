import { afterEach, describe, expect, it, vi } from "vitest";

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
    status: 200,
  });
}

describe("dashboard API URL configuration", () => {
  afterEach(() => {
    window.history.replaceState({}, "", "/");
    vi.resetModules();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("uses the explicit api query parameter ahead of the environment fallback", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://env.example/v1");
    window.history.replaceState({}, "", "/?api=https%3A%2F%2Fcontext.example%2Fv1%2F");

    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/status")) {
        return jsonResponse({ state: "running", session_id: "session-query-api" });
      }
      if (url.includes("/timeline?limit=50")) {
        return jsonResponse({ events: [] });
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetcher);

    vi.resetModules();
    const { configuredApiBaseUrl, createApiClient } = await import("./api");

    expect(configuredApiBaseUrl).toBe("https://context.example/v1/");
    await createApiClient().getSnapshot();

    expect(fetcher.mock.calls.map(([input]) => String(input)).sort()).toEqual([
      "https://context.example/v1/config",
      "https://context.example/v1/status",
      "https://context.example/v1/timeline?limit=50",
    ]);
  });

  it("uses the same-origin /api proxy when no override is configured", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/status")) {
        return jsonResponse({ state: "running", session_id: "session-proxy" });
      }
      if (url.includes("/timeline?limit=50")) {
        return jsonResponse({ events: [{ artifact_url: "/v1/artifacts/proxy-sha256" }] });
      }
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", fetcher);

    vi.resetModules();
    const { configuredApiBaseUrl, createApiClient } = await import("./api");

    expect(configuredApiBaseUrl).toBe("/api");
    const snapshot = await createApiClient().getSnapshot();

    expect(fetcher.mock.calls.map(([input]) => String(input)).sort()).toEqual([
      "/api/config",
      "/api/status",
      "/api/timeline?limit=50",
    ]);
    expect(snapshot.timeline[0].evidenceUrl).toBe(
      new URL("/api/v1/artifacts/proxy-sha256", window.location.origin).toString(),
    );
  });

  it("uses the remote environment base and bearer token when no query override exists", async () => {
    vi.stubEnv("VITE_API_BASE_URL", " https://remote.example/v1/ ");
    vi.stubEnv("VITE_API_BEARER_TOKEN", " remote-dashboard-token ");

    const fetcher = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse({}));
    vi.stubGlobal("fetch", fetcher);

    vi.resetModules();
    const { configuredApiBaseUrl, configuredApiBearerToken, createApiClient } = await import("./api");

    expect(configuredApiBaseUrl).toBe("https://remote.example/v1/");
    expect(configuredApiBearerToken).toBe("remote-dashboard-token");
    await createApiClient().getSnapshot();

    for (const [, init] of fetcher.mock.calls) {
      expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer remote-dashboard-token");
    }
  });

  it.each([
    [
      "http://remote.example/v1",
      "Remote dashboard connections require HTTPS.",
    ],
    [
      "https://user:password@remote.example/v1",
      "Remote dashboard connections must not include embedded credentials.",
    ],
  ])("rejects unsafe remote connection %s visibly", async (apiBaseUrl, message) => {
    const { validateRemoteConnection } = await import("./api");

    expect(
      validateRemoteConnection({
        apiBaseUrl,
        bearerToken: "smoke-token",
        requireHttpsRemote: true,
      }),
    ).toBe(message);
  });
});
