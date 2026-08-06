import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { buildDemoSnapshot, searchDemoSnapshot } from "./demoData";
import type {
  DashboardApi,
  DashboardSettings,
  DashboardSnapshot,
} from "./types";

function makeApi(snapshot: DashboardSnapshot = buildDemoSnapshot()) {
  const updateSettings = vi.fn(async (settings: DashboardSettings) => settings);
  const setConnectionSettings = vi.fn();
  const api: DashboardApi = {
    getSnapshot: vi.fn(async () => snapshot),
    search: vi.fn(async (params) => searchDemoSnapshot(snapshot, params)),
    updateSettings,
    setConnectionSettings,
  };
  return { api, setConnectionSettings, updateSettings };
}

describe("dashboard MVP settings controls", () => {
  it("keeps the plaintext warning visible and exposes per-source, runtime, and remote controls", async () => {
    const user = userEvent.setup();
    const { api, setConnectionSettings, updateSettings } = makeApi();
    render(<App api={api} />);
    await screen.findByRole("heading", { name: /your context, in focus/i });

    const warning = screen.getByRole("alert");
    expect(warning).toHaveTextContent(/plaintext/i);
    expect(warning).toHaveTextContent(/clipboard is local-only by default/i);

    const clipboardRoute = screen.getByRole("combobox", {
      name: "Clipboard route",
    });
    const screenshotsRoute = screen.getByRole("combobox", {
      name: "Screenshots route",
    });
    expect(clipboardRoute).toHaveValue("local");
    expect(screenshotsRoute).toHaveValue("remote");

    expect(
      screen.getByRole("spinbutton", { name: "Worker count" }),
    ).toHaveValue(1);
    expect(
      screen.getByRole("spinbutton", { name: "Queue capacity" }),
    ).toHaveValue(1000);
    expect(screen.getByRole("textbox", { name: "Model endpoint" })).toHaveValue(
      "",
    );
    expect(screen.getByLabelText("Model health and backlog")).toHaveTextContent(
      /oldest pending analysis/i,
    );
    expect(screen.getByRole("note")).toHaveTextContent(
      /no active inference worker is reported/i,
    );
    expect(screen.getByRole("note")).toHaveTextContent(
      /capture freshness is reported separately/i,
    );
    expect(
      screen.getByRole("textbox", { name: "Remote server URL" }),
    ).toHaveValue("");
    expect(
      screen.getByRole("checkbox", { name: "Require HTTPS for remote server" }),
    ).toBeChecked();

    await user.click(screen.getByRole("checkbox", { name: "I understand" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/plaintext/i);
    await user.selectOptions(clipboardRoute, "remote");
    await user.clear(screen.getByRole("spinbutton", { name: "Worker count" }));
    await user.type(
      screen.getByRole("spinbutton", { name: "Worker count" }),
      "3",
    );
    await user.clear(
      screen.getByRole("spinbutton", { name: "Queue capacity" }),
    );
    await user.type(
      screen.getByRole("spinbutton", { name: "Queue capacity" }),
      "256",
    );
    await user.type(
      screen.getByRole("textbox", { name: "Model endpoint" }),
      "http://127.0.0.1:8080/v1",
    );
    await user.type(
      screen.getByRole("textbox", { name: "Model name" }),
      "local-vlm",
    );
    await user.type(
      screen.getByRole("textbox", { name: "Model revision" }),
      "revision-a",
    );
    await user.type(
      screen.getByRole("textbox", { name: "Remote server URL" }),
      "https://context.example/v1",
    );
    await user.type(screen.getByLabelText("Bearer token"), "session-token");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(updateSettings).toHaveBeenCalledTimes(1));
    expect(updateSettings).toHaveBeenCalledWith(
      expect.objectContaining({
        plaintextWarningAcknowledged: true,
        queueSize: 256,
        workerCount: 3,
        modelEndpoint: "http://127.0.0.1:8080/v1",
        modelName: "local-vlm",
        modelRevision: "revision-a",
        routes: expect.arrayContaining([
          expect.objectContaining({ source: "clipboard", route: "remote" }),
          expect.objectContaining({ source: "screenshots", route: "remote" }),
        ]),
      }),
    );
    expect(updateSettings.mock.calls[0][0]).not.toHaveProperty("bearerToken");
    expect(setConnectionSettings).toHaveBeenCalledWith({
      apiBaseUrl: "https://context.example/v1",
      bearerToken: "session-token",
      requireHttpsRemote: true,
    });
  });

  it("blocks an insecure remote URL before saving server settings", async () => {
    const user = userEvent.setup();
    const { api, updateSettings } = makeApi();
    render(<App api={api} />);
    await screen.findByRole("heading", { name: /your context, in focus/i });

    await user.type(
      screen.getByRole("textbox", { name: "Remote server URL" }),
      "http://remote.example/v1",
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findByText("Remote dashboard connections require HTTPS."),
    ).toBeInTheDocument();
    expect(updateSettings).not.toHaveBeenCalled();
  });

  it("blocks a non-loopback HTTP model endpoint before saving", async () => {
    const user = userEvent.setup();
    const { api, updateSettings } = makeApi();
    render(<App api={api} />);
    await screen.findByRole("heading", { name: /your context, in focus/i });

    await user.type(
      screen.getByRole("textbox", { name: "Model endpoint" }),
      "http://model.example/v1",
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findByText("Non-loopback model endpoints require HTTPS."),
    ).toBeInTheDocument();
    expect(updateSettings).not.toHaveBeenCalled();
  });
});
