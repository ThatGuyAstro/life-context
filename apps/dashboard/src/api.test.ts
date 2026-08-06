import { describe, expect, it, vi } from "vitest";
import { ApiError, createApiClient } from "./api";
import { buildDemoSnapshot } from "./demoData";

describe("dashboard API client", () => {
  it("uses the typed demo client when no API base URL is configured", async () => {
    const client = createApiClient("");
    const snapshot = await client.getSnapshot();
    expect(snapshot.session.state).toBe("live");

    const response = await client.search({ query: "launch" });
    expect(response.rankedResults[0].title).toBe("Revised launch brief");
  });

  it("serializes search filters and routes settings to the context API", async () => {
    const snapshot = buildDemoSnapshot();
    const fetcher = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/query")) {
          return new Response(
            JSON.stringify({
              answer: snapshot.answer,
              rankedResults: snapshot.rankedResults,
            }),
            { status: 200 },
          );
        }
        if (url.endsWith("/config")) {
          return new Response(JSON.stringify(snapshot.settings), {
            status: 200,
          });
        }
        return new Response(JSON.stringify(snapshot), { status: 200 });
      },
    );
    const client = createApiClient("https://context.example/", fetcher);

    await client.search({
      query: "launch brief",
      filters: { source: "messages", dateRange: "7d", importantOnly: true },
    });
    expect(String(fetcher.mock.calls[0][0])).toBe(
      "https://context.example/query",
    );
    expect(fetcher.mock.calls[0][1]).toEqual(
      expect.objectContaining({ method: "POST" }),
    );
    expect(
      JSON.parse(String((fetcher.mock.calls[0][1] as RequestInit).body)),
    ).toEqual(
      expect.objectContaining({
        query: "launch brief",
        filters: { source: "messages", range: "7d", important: true },
      }),
    );

    await client.updateSettings(snapshot.settings);
    expect(fetcher.mock.calls[1][1]).toEqual(
      expect.objectContaining({ method: "POST" }),
    );
    expect(
      JSON.parse(String((fetcher.mock.calls[1][1] as RequestInit).body)),
    ).toMatchObject({
      operation: "update",
      settings: {
        queue_size: 1000,
        source_routing: {
          clipboard: { enabled: true, route: "local" },
        },
        worker_count: 1,
        retention: { keyframe_days: 14, raw_hours: 168 },
      },
    });
  });

  it("surfaces non-success responses as ApiError", async () => {
    const fetcher = vi.fn(async () => new Response("", { status: 503 }));
    const client = createApiClient("https://context.example", fetcher);

    await expect(client.getSnapshot()).rejects.toMatchObject({ status: 503 });
  });
});
