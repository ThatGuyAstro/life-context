import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { buildDemoSnapshot, searchDemoSnapshot } from "./demoData";
import type { DashboardApi } from "./types";

function makeApi(): {
  api: DashboardApi;
  search: ReturnType<typeof vi.fn>;
  updateSettings: ReturnType<typeof vi.fn>;
} {
  const snapshot = buildDemoSnapshot();
  const search = vi.fn(async (params) => searchDemoSnapshot(snapshot, params));
  const updateSettings = vi.fn(async (settings) => settings);

  return {
    api: {
      getSnapshot: vi.fn(async () => snapshot),
      search,
      updateSettings,
    },
    search,
    updateSettings,
  };
}

describe("dashboard", () => {
  it("renders live session status, queue freshness, timeline, answer, and ranked context", async () => {
    const { api } = makeApi();
    render(<App api={api} />);

    expect(
      await screen.findByRole("heading", { name: /your context, in focus/i }),
    ).toBeInTheDocument();
    expect(screen.getByText("Wednesday workspace")).toBeInTheDocument();
    expect(
      screen.getByText("Capture is live; semantic indexing is behind"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Oldest pending analysis: 5m 2s/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/queue measures semantic analysis, not capture loss/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open worker settings" }),
    ).toHaveAttribute("href", "#worker-settings");
    expect(
      screen.getByRole("heading", { name: "Context timeline" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Closest matches" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Plaintext warning")).toBeInTheDocument();
  });

  it("derives timeline and system labels from the snapshot instead of static nominal values", async () => {
    const snapshot = buildDemoSnapshot();
    const pausedSnapshot = {
      ...snapshot,
      session: { ...snapshot.session, state: "paused" as const },
      timeline: snapshot.timeline.slice(0, 2),
      timelineTotalCount: 2,
      queue: {
        ...snapshot.queue,
        state: "fresh" as const,
        pending: 3,
        failed: 3,
        deadLettered: 2,
      },
    };
    const api: DashboardApi = {
      getSnapshot: vi.fn(async () => pausedSnapshot),
      search: vi.fn(async (params) =>
        searchDemoSnapshot(pausedSnapshot, params),
      ),
      updateSettings: vi.fn(async (settings) => settings),
    };

    render(<App api={api} />);
    await screen.findByRole("heading", { name: /your context, in focus/i });

    expect(screen.getByRole("navigation")).toHaveTextContent("2");
    expect(screen.getAllByText("Capture paused").length).toBeGreaterThan(0);
    expect(
      screen.getByText("Capture paused; semantic index is caught up"),
    ).toBeInTheDocument();
    expect(screen.getByText("Queue has failures")).toBeInTheDocument();
    expect(
      screen.getByText("2 sources in loaded timeline"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/3 pending.*3 failed.*2 dead-lettered/, {
        selector: ".queue-count",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("All systems nominal")).not.toBeInTheDocument();
  });

  it("filters the timeline by source and importance", async () => {
    const user = userEvent.setup();
    const { api } = makeApi();
    render(<App api={api} />);
    await screen.findByRole("heading", { name: /your context, in focus/i });

    await user.click(screen.getByRole("button", { name: "Messages" }));
    expect(
      screen.getByRole("heading", {
        name: /Maya shared the revised launch brief/,
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", {
        name: "Product review moved to Thursday",
      }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Important/ }));
    expect(
      screen.getByRole("heading", {
        name: /Maya shared the revised launch brief/,
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", {
        name: "Jordan confirmed the analytics naming pass",
      }),
    ).not.toBeInTheDocument();
  });

  it("submits a natural-language query and replaces the answer", async () => {
    const user = userEvent.setup();
    const { api, search } = makeApi();
    let finishSearch = () => {};
    search.mockImplementationOnce(
      (params) =>
        new Promise((resolve) => {
          finishSearch = () =>
            resolve(searchDemoSnapshot(buildDemoSnapshot(), params));
        }),
    );
    render(<App api={api} />);
    await screen.findByRole("heading", { name: /your context, in focus/i });

    const query = screen.getByRole("textbox", { name: "Ask your context" });
    await user.type(query, "What changed around launch?");
    await user.keyboard("{Control>}{Enter}{/Control}");

    await waitFor(() =>
      expect(search).toHaveBeenCalledWith(
        expect.objectContaining({ query: "What changed around launch?" }),
      ),
    );
    expect(
      screen.getByText("Reviewing your recent activity"),
    ).toBeInTheDocument();
    expect(screen.getByText(/usually takes 20–30 seconds/i)).toBeInTheDocument();
    finishSearch();
    expect(
      await screen.findByText(
        /The strongest context for “What changed around launch\?”/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Context answer updated")).toBeInTheDocument();
  });

  it("updates source routing and retention settings before saving", async () => {
    const user = userEvent.setup();
    const { api, updateSettings } = makeApi();
    render(<App api={api} />);
    await screen.findByRole("heading", { name: /your context, in focus/i });

    await user.click(screen.getByRole("button", { name: "Disable Messages" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Raw payloads retention" }),
      "30",
    );
    await user.click(screen.getByRole("checkbox", { name: "I understand" }));
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(updateSettings).toHaveBeenCalledTimes(1));
    const savedSettings = updateSettings.mock.calls[0][0];
    expect(
      savedSettings.routes.find(
        (route: { source: string }) => route.source === "messages",
      ).enabled,
    ).toBe(false);
    expect(savedSettings.retention.rawPayloadDays).toBe(30);
    expect(savedSettings.plaintextWarningAcknowledged).toBe(true);
    expect(
      await screen.findByRole("button", { name: "Saved" }),
    ).toBeInTheDocument();
  });

  it("renders clickable artifact evidence links across the timeline, answer, and ranked results", async () => {
    const snapshot = buildDemoSnapshot();
    const artifactUrl = "https://context.example/v1/artifacts/sha256-123";
    const artifactSnapshot = {
      ...snapshot,
      answer: {
        ...snapshot.answer,
        evidence: [
          {
            ...snapshot.answer.evidence[0],
            detail: "Artifact evidence detail",
            title: "Artifact evidence citation",
            url: artifactUrl,
          },
          ...snapshot.answer.evidence.slice(1),
        ],
      },
      rankedResults: [
        {
          ...snapshot.rankedResults[0],
          evidenceUrl: artifactUrl,
          snippet: "Artifact ranked snippet",
          title: "Artifact ranked result",
        },
        ...snapshot.rankedResults.slice(1),
      ],
      timeline: [
        {
          ...snapshot.timeline[0],
          evidenceUrl: artifactUrl,
          summary: "Artifact timeline summary",
          title: "Artifact timeline event",
        },
        ...snapshot.timeline.slice(1),
      ],
    };
    const api: DashboardApi = {
      getSnapshot: vi.fn(async () => artifactSnapshot),
      search: vi.fn(async () => ({
        answer: artifactSnapshot.answer,
        rankedResults: artifactSnapshot.rankedResults,
      })),
      updateSettings: vi.fn(async (settings) => settings),
    };

    render(<App api={api} />);
    await screen.findByRole("heading", { name: /your context, in focus/i });

    expect(
      screen.getByRole("link", { name: "Artifact evidence citation" }),
    ).toHaveAttribute("href", artifactUrl);
    expect(
      screen.getByRole("link", { name: "Artifact ranked snippet" }),
    ).toHaveAttribute("href", artifactUrl);
    expect(
      screen.getByRole("link", { name: "Artifact timeline summary" }),
    ).toHaveAttribute("href", artifactUrl);
  });

  it("applies the settings returned by the server after saving", async () => {
    const user = userEvent.setup();
    const snapshot = buildDemoSnapshot();
    const savedSettings = {
      ...snapshot.settings,
      routes: snapshot.settings.routes.map((route) =>
        route.source === "messages"
          ? { ...route, enabled: false, route: "excluded" as const }
          : route,
      ),
      retention: { ...snapshot.settings.retention, rawPayloadDays: 30 },
    };
    const updateSettings = vi.fn(async () => savedSettings);
    const api: DashboardApi = {
      getSnapshot: vi.fn(async () => snapshot),
      search: vi.fn(async (params) => searchDemoSnapshot(snapshot, params)),
      updateSettings,
    };

    render(<App api={api} />);
    await screen.findByRole("heading", { name: /your context, in focus/i });

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Messages route" }),
      "excluded",
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Raw payloads retention" }),
      "30",
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(updateSettings).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByRole("button", { name: "Saved" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Messages route" }),
    ).toHaveValue("excluded");
    expect(
      screen.getByRole("combobox", { name: "Raw payloads retention" }),
    ).toHaveValue("30");
  });
});
