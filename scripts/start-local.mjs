import { spawn, spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const RUN_TASK = resolve(ROOT, "scripts", "run-task.mjs");
const DEFAULT_SERVER_PORT = 8000;
const DEFAULT_DASHBOARD_PORT = 5173;
const READY_TIMEOUT_MS = 30_000;

const children = new Set();
let shuttingDown = false;

function printHelp() {
  console.log("Usage: pnpm start:local [options]");
  console.log("Options:");
  console.log("  --server-port <port>     API port (default: 8000)");
  console.log("  --dashboard-port <port>  Vite port (default: 5173)");
  console.log("  --help                   Show this help");
  console.log("");
  console.log("Environment overrides:");
  console.log("  LIFE_CONTEXT_LOCAL_SERVER_PORT");
  console.log("  LIFE_CONTEXT_LOCAL_DASHBOARD_PORT");
  console.log("  VITE_API_PROXY_TARGET  override the dashboard API target");
}

function parsePort(value, name) {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`${name} must be an integer between 1 and 65535`);
  }
  return port;
}

function parseArgs(argv) {
  const normalizedArgs = argv[0] === "--" ? argv.slice(1) : argv;
  const options = {
    serverPort:
      process.env.LIFE_CONTEXT_LOCAL_SERVER_PORT ?? DEFAULT_SERVER_PORT,
    dashboardPort:
      process.env.LIFE_CONTEXT_LOCAL_DASHBOARD_PORT ?? DEFAULT_DASHBOARD_PORT,
  };

  for (let index = 0; index < normalizedArgs.length; index += 1) {
    const argument = normalizedArgs[index];
    if (argument === "--help" || argument === "-h") {
      printHelp();
      process.exit(0);
    }
    if (argument === "--server-port" || argument === "--dashboard-port") {
      const value = normalizedArgs[index + 1];
      if (!value) {
        throw new Error(`${argument} requires a port value`);
      }
      if (argument === "--server-port") {
        options.serverPort = value;
      } else {
        options.dashboardPort = value;
      }
      index += 1;
      continue;
    }
    if (argument.startsWith("--server-port=")) {
      options.serverPort = argument.slice("--server-port=".length);
      continue;
    }
    if (argument.startsWith("--dashboard-port=")) {
      options.dashboardPort = argument.slice("--dashboard-port=".length);
      continue;
    }
    throw new Error(`Unknown option: ${argument}`);
  }

  return {
    serverPort: parsePort(options.serverPort, "server port"),
    dashboardPort: parsePort(options.dashboardPort, "dashboard port"),
  };
}

function prefixOutput(child, label) {
  child.stdout?.on("data", (chunk) => {
    for (const line of String(chunk).split(/(?<=\n)/)) {
      if (line.trim()) process.stdout.write(`[${label}] ${line}`);
    }
  });
  child.stderr?.on("data", (chunk) => {
    for (const line of String(chunk).split(/(?<=\n)/)) {
      if (line.trim()) process.stderr.write(`[${label}] ${line}`);
    }
  });
}

function spawnTask(label, args, env) {
  const child = spawn(process.execPath, [RUN_TASK, ...args], {
    cwd: ROOT,
    env: { ...process.env, ...env },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  children.add(child);
  prefixOutput(child, label);
  child.once("error", (error) => {
    if (!shuttingDown) {
      console.error(
        `[life-context] ${label} failed to start: ${error.message}`,
      );
      void shutdown(1);
    }
  });
  child.once("exit", (code, signal) => {
    children.delete(child);
    if (!shuttingDown) {
      const detail = signal ? `signal ${signal}` : `exit code ${code ?? 1}`;
      console.error(
        `[life-context] ${label} stopped unexpectedly (${detail}).`,
      );
      void shutdown(code === 0 ? 1 : (code ?? 1));
    }
  });
  return child;
}

function stopChild(child) {
  if (!child.pid || child.exitCode !== null || child.killed) return;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
      stdio: "ignore",
    });
  } else {
    child.kill("SIGTERM");
  }
}

async function shutdown(code = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const child of children) stopChild(child);
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
  process.exitCode = code;
}

async function waitForReady(url, label, childrenToWatch) {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  let lastError = "no response";
  while (Date.now() < deadline) {
    if (childrenToWatch.some((child) => child.exitCode !== null)) {
      throw new Error(`${label} process exited before becoming ready`);
    }
    try {
      const response = await fetch(url, {
        signal: AbortSignal.timeout(1_000),
      });
      if (response.ok) return;
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
  }
  throw new Error(`${label} did not become ready: ${lastError}`);
}

async function main() {
  const { serverPort, dashboardPort } = parseArgs(process.argv.slice(2));
  if (serverPort === dashboardPort) {
    throw new Error("server and dashboard ports must be different");
  }
  const server = spawnTask("server", ["server"], {
    CONTEXT_SERVER_HOST: "127.0.0.1",
    CONTEXT_SERVER_PORT: String(serverPort),
    // Probe the model capabilities before choosing the wire format. The
    // b9898 Gemma endpoint does not advertise JSON Schema support, so auto
    // resolves to json_object; the server still validates the returned JSON
    // with Pydantic. Set json_object explicitly for a deterministic fallback.
    CONTEXT_MODEL_STRUCTURED_OUTPUT_MODE:
      process.env.CONTEXT_MODEL_STRUCTURED_OUTPUT_MODE ?? "auto",
    CONTEXT_MODEL_REQUIRES_MULTIMODAL:
      process.env.CONTEXT_MODEL_REQUIRES_MULTIMODAL ?? "true",
  });
  const dashboard = spawnTask(
    "dashboard",
    [
      "dashboard",
      "--host",
      "127.0.0.1",
      "--strictPort",
      "--port",
      String(dashboardPort),
    ],
    {
      VITE_API_PROXY_TARGET:
        process.env.VITE_API_PROXY_TARGET ??
        `http://127.0.0.1:${serverPort}/v1`,
    },
  );

  await Promise.all([
    waitForReady(`http://127.0.0.1:${serverPort}/v1/health`, "API", [server]),
    waitForReady(`http://127.0.0.1:${dashboardPort}/`, "dashboard", [
      dashboard,
    ]),
  ]);

  console.log(
    `[life-context] API ready: http://127.0.0.1:${serverPort}/v1/health`,
  );
  console.log(
    `[life-context] Dashboard ready: http://127.0.0.1:${dashboardPort}/`,
  );
  console.log("[life-context] Press Ctrl+C to stop both services.");

  await new Promise((resolvePromise) => {
    const finish = () => {
      process.off("SIGINT", finish);
      process.off("SIGTERM", finish);
      void shutdown(0).then(resolvePromise);
    };
    process.once("SIGINT", finish);
    process.once("SIGTERM", finish);
  });
}

main().catch(async (error) => {
  console.error(`[life-context] Local startup failed: ${error.message}`);
  await shutdown(1);
});
