# Life Context dashboard

Standalone React/Vite dashboard for the Life Context workspace. The dashboard uses the same-origin `/api` path by default; the Vite development server proxies that path to the local context server. A direct API URL can be selected with the query string or environment configuration.

Use Node.js 18+ and pnpm 9+ for the workspace commands below.

## Run

Run the dashboard commands below from `apps/dashboard`.

```powershell
pnpm install --frozen-lockfile
pnpm run dev
```

The local API is expected at `http://127.0.0.1:8000` and the dashboard at
`http://127.0.0.1:5173/`. Verify both the page and the same-origin proxy before
debugging the UI:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/health
Invoke-RestMethod http://127.0.0.1:5173/api/v1/health
```

Keep the dashboard terminal open for Vite logs. To capture a local diagnostic
log from the repository root, use the ignored artifact directory:

```powershell
New-Item -ItemType Directory -Force .\.context-artifacts\local-site | Out-Null
pnpm dashboard *> .\.context-artifacts\local-site\dashboard.log
```

Restart with `Ctrl+C` in the terminal that owns Vite, then rerun `pnpm run dev`.
If port `5173` remains occupied after the terminal closed, inspect the owning
PID and its command line first; stop it only when it belongs to this checkout,
never by killing all Node processes. A healthy proxy with a stale tab usually
needs a hard reload or a new tab.

The API base precedence is:

1. explicit `?api=` query parameter;
2. `VITE_API_BASE_URL`;
3. same-origin `/api`.

For a direct local API connection:

```powershell
$env:VITE_API_BASE_URL = "http://127.0.0.1:8000"
pnpm run dev
```

To override a running dashboard without rebuilding it, open a URL such as:

```text
http://127.0.0.1:5173/?api=https://context.example/v1
```

For a remote self-hosted server, use HTTPS and configure a bearer token for direct browser API requests:

```powershell
$env:VITE_API_BASE_URL = "https://context.example/v1"
$env:VITE_API_BEARER_TOKEN = "replace-with-the-server-token"
pnpm run dev
```

The remote-connection setting keeps HTTPS required by default. Unchecking that
control is an explicit development-only opt-out: any bearer token and context
sent to a non-loopback `http://` URL can be observed in transit. Keep `?api=`
and `VITE_API_BASE_URL` on loopback HTTP or remote HTTPS as well.

`VITE_API_BEARER_TOKEN` is embedded in a browser build, so do not commit it or use direct-token mode for a shared/public deployment. Direct cross-origin mode also depends on the remote deployment allowing the dashboard origin and `Authorization` header; the dashboard does not change server CORS policy. For development or a private deployment, the same-origin proxy can keep the token out of browser JavaScript and also authenticate artifact links opened from the dashboard:

```powershell
$env:VITE_API_PROXY_TARGET = "https://context.example"
$env:VITE_API_PROXY_BEARER_TOKEN = "replace-with-the-server-token"
pnpm run dev
```

Keep `VITE_API_PROXY_TARGET` on HTTPS for a remote server. The proxy rewrites `/api/status` to `/status`; use the direct `VITE_API_BASE_URL` form with the server's `/v1` prefix when the remote deployment does not expose the localhost aliases. Artifact references returned by the server (for example `/v1/artifacts/<sha256>`) are resolved through the configured API base, so proxy-mode links remain same-origin.

The typed client uses the context server's `/status`, `/timeline`, `/query`, and `/config` endpoints. The local interfaces and response adapters live in `src/types.ts` and `src/api.ts`; no shared contract or server route changes are required. Passing an empty base to `createApiClient("")` remains available for component/unit tests that intentionally use the built-in demo snapshot.

## Verify

```powershell
pnpm run typecheck
pnpm test
pnpm run build
```

The current local dashboard rerun passed typecheck and production build, with
**33 Vitest tests in 9 files, all passing**. This count does not include the
live browser smoke scripts.

For the live browser smoke, start the local server and dashboard, then return to
the repository root. Leave
the dashboard API base unset so the Vite development server uses its same-origin
`/api` proxy:

```powershell
python tests/browser/dashboard_smoke.py `
  --api-url http://127.0.0.1:8000 `
  --dashboard-url http://127.0.0.1:5173/
```

Run the companion settings/privacy smoke against the same local pair with:

```powershell
python tests/browser/dashboard_settings_smoke.py `
  --dashboard-url http://127.0.0.1:5173/
```

This smoke requires a host Python installation with Playwright and Chromium.
The direct `?api=http://127.0.0.1:8000` form is cross-origin and requires the
server to allow the dashboard origin and headers through CORS; the local
server does not enable that policy by default. The local browser result is
same-origin proxy evidence. The isolated remote HTTPS/bearer smoke also passes
the proxy and artifact-download path, but remains a fixture/security check,
not a real second-host deployment. To exercise it, run:

```powershell
python tests/browser/dashboard_security_smoke.py
```

That smoke generates a short-lived loopback certificate and sets
`NODE_EXTRA_CA_CERTS` only on its temporary Vite child. This is required for
Node to verify the self-signed fixture; production deployments must use a
normally trusted certificate. The browser remains on same-origin `/api`
artifact links, so Vite injects the bearer token without requiring broad CORS
or browser certificate bypasses.
