import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { createApiClient } from "./api";
import { buildDemoSnapshot, searchDemoSnapshot } from "./demoData";
import type { DashboardApi, DashboardSettings, DashboardSnapshot } from "./types";

type RetentionSettingsWithControls = DashboardSettings["retention"] & {
  rawHours: number;
  keyframeDays: number;
};

type RetentionPurgeRequest = {
  before: string;
  source: string;
  project: string;
  confirmed: boolean;
};

type RetentionDashboardApi = DashboardApi & {
  purge(options: RetentionPurgeRequest): Promise<unknown>;
};

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), {
    headers: { "Content-Type": "application/json" },
    status: 200,
  });
}

function snapshotWithRetentionControls(): DashboardSnapshot {
  const snapshot = buildDemoSnapshot();
  return {
    ...snapshot,
    settings: {
      ...snapshot.settings,
      retention: {
        ...snapshot.settings.retention,
        rawHours: 48,
        keyframeDays: 14,
      } as RetentionSettingsWithControls,
    },
  };
}

function makeApi(snapshot = snapshotWithRetentionControls()) {
  const updateSettings = vi.fn(async (settings: DashboardSettings) => settings);
  const purge = vi.fn(async (options: RetentionPurgeRequest) => ({ purge: options }));
  const api = {
    getSnapshot: vi.fn(async () => snapshot),
    search: vi.fn(async (params) => searchDemoSnapshot(snapshot, params)),
    updateSettings,
    purge,
  } as unknown as RetentionDashboardApi;
  return { api, purge, updateSettings };
}

describe("dashboard retention controls", () => {
  it("renders raw-hours and keyframe-days controls and includes edits in the settings payload", async () => {
    const user = userEvent.setup();
    const { api, updateSettings } = makeApi();
    render(<App api={api} />);
    await screen.findByRole("heading", { name: /your context, in focus/i });

    const rawHours = screen.getByLabelText(/raw.*hours/i) as HTMLInputElement;
    const keyframeDays = screen.getByLabelText(/keyframe.*days/i) as HTMLInputElement;
    expect(Number(rawHours.value)).toBe(48);
    expect(Number(keyframeDays.value)).toBe(14);

    await user.clear(rawHours);
    await user.type(rawHours, "72");
    await user.clear(keyframeDays);
    await user.type(keyframeDays, "21");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(updateSettings).toHaveBeenCalledTimes(1));
    expect(updateSettings).toHaveBeenCalledWith(expect.objectContaining({
      retention: expect.objectContaining({ rawHours: 72, keyframeDays: 21 }),
    }));
  });

  it("maps raw_hours and keyframe_days from the live config and sends their wire names on update", async () => {
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/status")) return jsonResponse({ state: "running" });
      if (url.includes("/timeline?limit=50")) return jsonResponse({ events: [] });
      if (url.endsWith("/config") && init?.method === "POST") {
        return jsonResponse({ settings: { retention: { raw_hours: 72, keyframe_days: 21 } } });
      }
      return jsonResponse({ retention: { raw_hours: 48, keyframe_days: 14 } });
    });

    const client = createApiClient("https://context.example", fetcher);
    const snapshot = await client.getSnapshot();
    expect(snapshot.settings.retention).toMatchObject({ rawHours: 48, keyframeDays: 14 });

    const settings = {
      ...snapshot.settings,
      retention: { ...snapshot.settings.retention, rawHours: 72, keyframeDays: 21 },
    } as DashboardSettings;
    await client.updateSettings(settings);

    const postCall = fetcher.mock.calls.find(([, init]) => init?.method === "POST");
    expect(postCall).toBeDefined();
    expect(JSON.parse(String(postCall?.[1]?.body))).toMatchObject({
      operation: "update",
      settings: { retention: { raw_hours: 72, keyframe_days: 21 } },
    });
  });

  it("serializes purge date/source/project filters with explicit confirmation", async () => {
    const fetcher = vi.fn(async () => jsonResponse({ deleted: 3 }));
    const client = createApiClient("https://context.example", fetcher) as RetentionDashboardApi;
    const options: RetentionPurgeRequest = {
      before: "2026-01-01",
      source: "clipboard",
      project: "life-context",
      confirmed: true,
    };

    await client.purge(options);

    expect(fetcher).toHaveBeenCalledWith(
      "https://context.example/purge",
      expect.objectContaining({
        body: JSON.stringify(options),
        method: "POST",
      }),
    );
  });

  it("exposes date/source/project purge controls and requires explicit destructive confirmation", async () => {
    const user = userEvent.setup();
    const { api, purge } = makeApi();
    render(<App api={api} />);
    await screen.findByRole("heading", { name: /your context, in focus/i });

    const before = screen.getByLabelText(/purge.*(date|before)/i);
    const source = screen.getByLabelText(/purge.*source/i);
    const project = screen.getByLabelText(/purge.*project/i);
    const confirmation = screen.getByRole("checkbox", { name: /permanent|destructive|confirm.*purge/i });
    const purgeButton = screen.getByRole("button", { name: /purge/i });

    expect(purgeButton).toBeDisabled();
    await user.type(before, "2026-01-01");
    await user.type(source, "clipboard");
    await user.type(project, "life-context");
    await user.click(confirmation);
    await user.click(purgeButton);

    await waitFor(() => expect(purge).toHaveBeenCalledWith({
      before: "2026-01-01",
      source: "clipboard",
      project: "life-context",
      confirmed: true,
    }));
  });
});
