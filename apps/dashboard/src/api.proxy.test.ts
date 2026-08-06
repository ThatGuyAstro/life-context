import { describe, expect, it } from "vitest";
import { createApiProxy } from "./apiProxy";

describe("dashboard same-origin API proxy", () => {
  it("rewrites /api requests to the local server by default", () => {
    const proxy = createApiProxy({});

    expect(proxy.target).toBe("http://127.0.0.1:8000");
    expect(proxy.changeOrigin).toBe(true);
    expect(proxy.rewrite("/api/v1/artifacts/sha256")).toBe("/v1/artifacts/sha256");
    expect(proxy.headers).toBeUndefined();
  });

  it("supports an HTTPS target with a proxy-only bearer token", () => {
    const proxy = createApiProxy({
      VITE_API_PROXY_BEARER_TOKEN: " proxy-token ",
      VITE_API_PROXY_TARGET: " https://context.example ",
    });

    expect(proxy.target).toBe("https://context.example");
    expect(proxy.headers).toEqual({ Authorization: "Bearer proxy-token" });
  });

  it("does not double-prefix response URLs when the target includes /v1", () => {
    const proxy = createApiProxy({
      VITE_API_PROXY_TARGET: "https://context.example/v1",
    });

    expect(proxy.rewrite("/api/status")).toBe("/status");
    expect(proxy.rewrite("/api/v1/artifacts/sha256")).toBe(
      "/artifacts/sha256",
    );
  });
});
