const DEFAULT_API_PROXY_TARGET = "http://127.0.0.1:8000";

function isLoopbackHost(hostname: string): boolean {
  const normalized = hostname
    .toLowerCase()
    .replace(/^\[|\]$/g, "")
    .replace(/\.$/, "");
  return (
    normalized === "localhost" ||
    normalized === "127.0.0.1" ||
    normalized === "::1"
  );
}

export function validateApiProxyTarget(value: string): string {
  const target = value.trim();
  let parsed: URL;
  try {
    parsed = new URL(target);
  } catch {
    throw new Error(
      "dashboard API proxy target must be an absolute HTTP(S) URL",
    );
  }
  if (!(parsed.protocol === "http:" || parsed.protocol === "https:")) {
    throw new Error("dashboard API proxy target must use HTTP or HTTPS");
  }
  if (parsed.username || parsed.password) {
    throw new Error(
      "dashboard API proxy target must not include embedded credentials",
    );
  }
  if (parsed.protocol === "http:" && !isLoopbackHost(parsed.hostname)) {
    throw new Error(
      "non-loopback HTTP dashboard API proxy target is not allowed; use HTTPS",
    );
  }
  return target;
}

export function createApiProxy(env: Record<string, string>) {
  const proxyBearerToken = env.VITE_API_PROXY_BEARER_TOKEN?.trim();
  const target = validateApiProxyTarget(
    env.VITE_API_PROXY_TARGET?.trim() || DEFAULT_API_PROXY_TARGET,
  );
  const targetPath = new URL(target).pathname.replace(/\/+$/, "");
  return {
    target,
    changeOrigin: true,
    rewrite: (path: string) => {
      const rewritten = path.replace(/^\/api/, "") || "/";
      if (
        targetPath &&
        (rewritten === targetPath || rewritten.startsWith(`${targetPath}/`))
      ) {
        return rewritten.slice(targetPath.length) || "/";
      }
      return rewritten;
    },
    ...(proxyBearerToken
      ? { headers: { Authorization: `Bearer ${proxyBearerToken}` } }
      : {}),
  };
}
