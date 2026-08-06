import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, extname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const task = process.argv[2];
const checkOnly = process.argv.includes("--check");
const taskArgs = process.argv
  .slice(3)
  .filter((argument) => argument !== "--check");

const pythonFileEntrypoints = {
  server: ["server.py", "server/main.py", "src/server.py"],
  collector: ["collector.py", "collector/main.py", "src/collector.py"],
  cli: ["cli.py", "src/cli.py"],
};

const pythonModuleEntrypoints = {
  server: [
    {
      module: "services.context_server",
      marker: "services/context_server/__main__.py",
    },
  ],
  collector: [
    { module: "services.collector", marker: "services/collector/__main__.py" },
  ],
  cli: [{ module: "apps.cli", marker: "apps/cli/__main__.py" }],
};

const taskLabels = {
  server: "server",
  collector: "collector",
  dashboard: "dashboard",
  cli: "CLI",
  test: "tests",
  format: "formatting",
};

const ignoredDirectories = new Set([
  ".git",
  ".context-artifacts",
  ".mypy_cache",
  ".pnpm-store",
  ".pytest_cache",
  ".ruff_cache",
  ".venv",
  ".vite",
  "artifacts",
  "build",
  "coverage",
  "data",
  "dist",
  "htmlcov",
  "logs",
  "model",
  "models",
  "node_modules",
  "spool",
  "target",
  "temp",
  "tmp",
]);

function printHelp() {
  console.log("Usage: node scripts/run-task.mjs <task> [--check]");
  console.log("Tasks: server, collector, dashboard, cli, test, format");
}

function envKeyFor(taskName) {
  return `LIFE_CONTEXT_${taskName.toUpperCase()}_CMD`;
}

function unavailable(taskName, reason) {
  const envKey = envKeyFor(taskName);
  console.error(
    `[life-context] ${taskLabels[taskName] ?? taskName} is not runnable yet.`,
  );
  console.error(`[life-context] ${reason}`);
  console.error(
    `[life-context] Add the documented entrypoint or set ${envKey} to a command.`,
  );
  process.exitCode = 2;
}

function run(executable, args = [], options = {}) {
  const actualExecutable = normalizedExecutable(executable);
  const result = spawnSync(actualExecutable, args, {
    cwd: ROOT,
    env: { ...process.env, ...(options.env ?? {}) },
    shell: options.shell ?? needsShell(actualExecutable),
    stdio: "inherit",
  });

  if (result.error) {
    console.error(
      `[life-context] Could not start ${actualExecutable}: ${result.error.message}`,
    );
    return 2;
  }

  return result.status ?? 2;
}

function normalizedExecutable(executable) {
  if (process.platform === "win32" && executable === "pnpm") {
    return "pnpm.cmd";
  }
  return executable;
}

function needsShell(executable) {
  return process.platform === "win32" && /\.(cmd|bat)$/i.test(executable);
}

function runOverride(taskName) {
  const envKey = envKeyFor(taskName);
  const command = process.env[envKey]?.trim();
  if (!command) {
    return null;
  }

  console.log(`[life-context] Running ${taskName} from ${envKey}.`);
  return run(command, [], { shell: true });
}

function commandAvailable(executable, args = ["--version"]) {
  const actualExecutable = normalizedExecutable(executable);
  const result = spawnSync(actualExecutable, args, {
    cwd: ROOT,
    shell: needsShell(actualExecutable),
    stdio: "ignore",
  });
  return !result.error && result.status === 0;
}

function pythonCommand() {
  const virtualEnvironmentPython = join(
    ROOT,
    ".venv",
    process.platform === "win32" ? "Scripts" : "bin",
    process.platform === "win32" ? "python.exe" : "python",
  );
  if (existsSync(virtualEnvironmentPython)) {
    return { executable: virtualEnvironmentPython, prefix: [] };
  }
  const candidates =
    process.platform === "win32"
      ? [["python"], ["py", "-3"]]
      : [["python3"], ["python"]];
  for (const candidate of candidates) {
    const [executable, ...prefix] = candidate;
    if (commandAvailable(executable, [...prefix, "--version"])) {
      return { executable, prefix };
    }
  }
  return null;
}

function runPythonFile(taskName, path, args = []) {
  const python = pythonCommand();
  if (!python) {
    unavailable(taskName, "Python 3.11 or newer was not found on PATH.");
    return 2;
  }
  return run(python.executable, [...python.prefix, path, ...args]);
}

function runPythonModule(taskName, moduleName, args = [], extraEnv = {}) {
  const python = pythonCommand();
  if (!python) {
    unavailable(taskName, "Python 3.11 or newer was not found on PATH.");
    return 2;
  }
  return run(python.executable, [...python.prefix, "-m", moduleName, ...args], {
    env: extraEnv,
  });
}

function findPythonEntrypoint(taskName) {
  for (const candidate of pythonFileEntrypoints[taskName] ?? []) {
    if (existsSync(join(ROOT, candidate))) {
      return candidate;
    }
  }
  return null;
}

function runPythonTask(taskName, args = []) {
  for (const candidate of pythonModuleEntrypoints[taskName] ?? []) {
    if (existsSync(join(ROOT, candidate.marker))) {
      console.log(
        `[life-context] Running ${taskName} from python -m ${candidate.module}.`,
      );
      return runPythonModule(taskName, candidate.module, args);
    }
  }

  const entrypoint = findPythonEntrypoint(taskName);
  if (!entrypoint) {
    unavailable(
      taskName,
      "No supported Python entrypoint exists in the repository yet.",
    );
    return 2;
  }
  console.log(`[life-context] Running ${taskName} from ${entrypoint}.`);
  return runPythonFile(taskName, entrypoint, args);
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}

function dashboardPackage() {
  for (const candidate of [
    "apps/dashboard/package.json",
    "dashboard/package.json",
  ]) {
    const path = join(ROOT, candidate);
    const packageJson = readJson(path);
    if (packageJson?.scripts?.dev) {
      return dirname(candidate);
    }
  }
  return null;
}

function runDashboard(args = []) {
  const packageDirectory = dashboardPackage();
  if (!packageDirectory) {
    unavailable(
      "dashboard",
      "No dashboard package with a `dev` script exists yet.",
    );
    return 2;
  }
  if (!commandAvailable("pnpm", ["--version"])) {
    unavailable("dashboard", "pnpm was not found on PATH.");
    return 2;
  }
  console.log(`[life-context] Starting dashboard from ${packageDirectory}.`);
  return run("pnpm", ["--dir", packageDirectory, "dev", ...args]);
}

function walkFiles(directory, predicate) {
  if (!existsSync(directory)) {
    return [];
  }

  const results = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (ignoredDirectories.has(entry.name)) {
      continue;
    }
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      results.push(...walkFiles(path, predicate));
    } else if (predicate(path)) {
      results.push(path);
    }
  }
  return results;
}

function workspacePackagesWithScript(scriptName) {
  const packageFiles = walkFiles(ROOT, (path) => {
    const normalized = relative(ROOT, path).split("\\").join("/");
    return normalized.startsWith("apps/") || normalized.startsWith("packages/")
      ? path.endsWith("package.json")
      : false;
  });
  return packageFiles.filter((path) => readJson(path)?.scripts?.[scriptName]);
}

function runTests(args = []) {
  const overrideResult = runOverride("test");
  if (overrideResult !== null) {
    return overrideResult;
  }

  const pythonTests = walkFiles(ROOT, (path) => {
    const name = path.split(/[\\/]/).pop() ?? "";
    return (
      extname(path) === ".py" &&
      (name.startsWith("test_") || name.endsWith("_test.py"))
    );
  });
  const workspaceTests = workspacePackagesWithScript("test");
  let attempted = false;
  let status = 0;

  if (pythonTests.length > 0) {
    attempted = true;
    const python = pythonCommand();
    if (!python) {
      unavailable(
        "test",
        "Python tests were found, but Python 3.11 or newer was not found on PATH.",
      );
      return 2;
    }
    status = run(python.executable, [
      ...python.prefix,
      "-m",
      "pytest",
      ...args,
    ]);
  }

  if (workspaceTests.length > 0) {
    attempted = true;
    if (!commandAvailable("pnpm", ["--version"])) {
      unavailable(
        "test",
        "A workspace test script was found, but pnpm was not found on PATH.",
      );
      return 2;
    }
    const workspaceStatus = run("pnpm", [
      "-r",
      "--if-present",
      "test",
      ...args,
    ]);
    status = status === 0 ? workspaceStatus : status;
  }

  if (!attempted) {
    unavailable(
      "test",
      "No Python test files or workspace package test scripts exist yet.",
    );
    return 2;
  }
  return status;
}

function localBinary(name) {
  const relativeName =
    process.platform === "win32"
      ? `node_modules/.bin/${name}.cmd`
      : `node_modules/.bin/${name}`;
  const path = join(ROOT, relativeName);
  return existsSync(path) ? path : null;
}

function runFormatting() {
  const overrideResult = runOverride("format");
  if (overrideResult !== null) {
    return overrideResult;
  }

  const prettier = localBinary("prettier");
  const pythonFiles = walkFiles(ROOT, (path) => extname(path) === ".py");
  const python = pythonFiles.length > 0 ? pythonCommand() : null;
  let attempted = false;
  let missingFormatter = false;
  let status = 0;

  if (prettier) {
    attempted = true;
    const prettierArgs = checkOnly ? ["--check"] : ["--write"];
    status = run(prettier, [
      ...prettierArgs,
      "README.md",
      "package.json",
      "pnpm-workspace.yaml",
      "scripts/**/*.mjs",
    ]);
  } else {
    console.error(
      "[life-context] Prettier is not installed; run `pnpm install` before formatting JavaScript/Markdown/YAML.",
    );
    missingFormatter = true;
  }

  if (pythonFiles.length > 0 && python) {
    const ruffAvailable = commandAvailable(python.executable, [
      ...python.prefix,
      "-m",
      "ruff",
      "--version",
    ]);
    if (!ruffAvailable) {
      console.error(
        "[life-context] Ruff is not installed; install it before formatting Python files.",
      );
      missingFormatter = true;
    } else {
      attempted = true;
      const ruffArgs = checkOnly
        ? ["-m", "ruff", "format", "--check", "."]
        : ["-m", "ruff", "format", "."];
      const ruffStatus = run(python.executable, [
        ...python.prefix,
        ...ruffArgs,
      ]);
      status = status === 0 ? ruffStatus : status;
    }
  } else if (pythonFiles.length > 0) {
    console.error(
      "[life-context] Ruff is not installed; install it before formatting Python files.",
    );
    missingFormatter = true;
  }

  if (!attempted) {
    unavailable("format", "No local formatter is available yet.");
    return 2;
  }
  if (missingFormatter) {
    console.error(
      "[life-context] Formatting is incomplete because one or more required formatters are unavailable.",
    );
    return 2;
  }
  return status;
}

function main() {
  if (!task || task === "--help" || task === "-h") {
    printHelp();
    return task ? 0 : 2;
  }

  if (task === "format") {
    return runFormatting();
  }
  if (task === "test") {
    return runTests(taskArgs);
  }
  if (task === "dashboard") {
    const overrideResult = runOverride(task);
    return overrideResult === null ? runDashboard(taskArgs) : overrideResult;
  }
  if (task in pythonFileEntrypoints) {
    const overrideResult = runOverride(task);
    return overrideResult === null
      ? runPythonTask(task, taskArgs)
      : overrideResult;
  }

  console.error(`[life-context] Unknown task: ${task}`);
  printHelp();
  return 2;
}

process.exitCode = main();
