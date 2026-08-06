import { render, screen } from "@testing-library/react";
import { Component, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "./ErrorBoundary";

class BrokenView extends Component {
  render(): ReactNode {
    throw new Error("simulated render failure");
  }
}

describe("ErrorBoundary", () => {
  it("keeps render failures visible with a reload action", () => {
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);

    render(
      <ErrorBoundary>
        <BrokenView />
      </ErrorBoundary>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "The dashboard needs to be reloaded.",
    );
    expect(
      screen.getByRole("button", { name: "Reload dashboard" }),
    ).toBeInTheDocument();
    expect(consoleError).toHaveBeenCalled();
    consoleError.mockRestore();
  });
});
