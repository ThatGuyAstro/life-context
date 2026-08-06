import { describe, expect, it } from "vitest";

import { createApiProxy } from "./apiProxy";

describe("dashboard API proxy target policy", () => {
  it("keeps the default proxy local and forwards a configured bearer token", () => {
    expect(createApiProxy({})).toMatchObject({
      target: "http://127.0.0.1:8000",
      changeOrigin: true,
    });

    expect(
      createApiProxy({
        VITE_API_PROXY_TARGET: " https://remote.example/v1 ",
        VITE_API_PROXY_BEARER_TOKEN: " remote-token ",
      }),
    ).toMatchObject({
      target: "https://remote.example/v1",
      headers: { Authorization: "Bearer remote-token" },
    });
  });

  it.each(["http://localhost:8000", "http://[::1]:8000"])(
    "allows loopback HTTP target %s",
    (target) => {
      expect(createApiProxy({ VITE_API_PROXY_TARGET: target }).target).toBe(
        target,
      );
    },
  );

  it.each([
    "http://remote.example/v1",
    "https://user:password@remote.example/v1",
  ])("rejects unsafe proxy target %s", (target) => {
    expect(() => createApiProxy({ VITE_API_PROXY_TARGET: target })).toThrow();
  });
});
