"""Clean-checkout smoke checks for the repository bootstrap surface.

These checks deliberately inspect manifests and invoke only short-lived help or
override commands.  They must not start the long-running server or collector,
and they must not write application data.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tomllib
import unittest
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUN_TASK = ROOT / "scripts" / "run-task.mjs"
DESKTOP_README = ROOT / "apps" / "desktop" / "README.md"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"expected an object in {path}")
    return value


def _run_node(
    *arguments: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(RUN_TASK), *arguments],
        cwd=ROOT,
        env=dict(env) if env is not None else None,
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=15,
        check=False,
    )


def _run_python(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=15,
        check=False,
    )


def _markdown_default(path: Path, variable: str) -> str:
    pattern = re.compile(
        rf"^\|\s*`{re.escape(variable)}`\s*\|\s*([^|]+?)\s*\|",
        re.MULTILINE,
    )
    match = pattern.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise AssertionError(f"{variable} is not documented in {path}")
    return match.group(1).strip().strip("`")


def _display_default(value: object) -> str:
    if value is None:
        return "empty"
    if isinstance(value, str) and not value:
        return "empty"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


@contextlib.contextmanager
def _without_runtime_environment() -> Iterator[None]:
    """Remove runtime variables while preserving and restoring the caller env."""

    names = [
        name
        for name in os.environ
        if name.startswith(("CONTEXT_SERVER_", "COLLECTOR_"))
    ]
    saved = {name: os.environ[name] for name in names}
    for name in names:
        del os.environ[name]
    try:
        yield
    finally:
        os.environ.update(saved)


class BootstrapSmokeTests(unittest.TestCase):
    def test_package_workspace_and_lock_manifests_are_consistent(self) -> None:
        root_package = _read_json(ROOT / "package.json")
        self.assertTrue(root_package["private"])
        self.assertEqual(root_package["engines"]["node"], ">=18")
        expected_scripts = {
            "server": "node scripts/run-task.mjs server",
            "collector": "node scripts/run-task.mjs collector",
            "dashboard": "node scripts/run-task.mjs dashboard",
            "cli": "node scripts/run-task.mjs cli",
            "test": "node scripts/run-task.mjs test",
            "format": "node scripts/run-task.mjs format",
            "format:check": "node scripts/run-task.mjs format --check",
            "help": "node scripts/run-task.mjs --help",
        }
        self.assertEqual(
            {name: root_package["scripts"].get(name) for name in expected_scripts},
            expected_scripts,
        )

        workspace_text = (ROOT / "pnpm-workspace.yaml").read_text(encoding="utf-8")
        workspace_patterns = [
            line.strip()[2:].strip().strip('"\'')
            for line in workspace_text.splitlines()
            if line.strip().startswith("- ")
        ]
        self.assertEqual(workspace_patterns, ["apps/*", "packages/*"])

        workspace_manifests = sorted(
            path
            for parent in (ROOT / "apps", ROOT / "packages")
            if parent.is_dir()
            for path in parent.glob("*/package.json")
        )
        self.assertGreaterEqual(len(workspace_manifests), 2)
        workspace_dirs = {
            path.parent.relative_to(ROOT).as_posix() for path in workspace_manifests
        }
        self.assertEqual(
            workspace_dirs,
            {"apps/dashboard", "apps/desktop", "packages/contracts"},
        )
        for path in workspace_manifests:
            package = _read_json(path)
            self.assertTrue(package.get("name"), path)
            self.assertTrue(package.get("private"), path)

        lock_text = (ROOT / "pnpm-lock.yaml").read_text(encoding="utf-8")
        importer_block = lock_text.split("\npackages:\n", 1)[0]
        importers = set(
            re.findall(
                r"^  (\.|(?:apps|packages)/[^:\n]+):(?:\s*\{\})?$",
                importer_block,
                re.MULTILINE,
            )
        )
        self.assertEqual(
            importers,
            {".", "apps/dashboard", "apps/desktop", "packages/contracts"},
            "pnpm-lock.yaml must contain an importer for every workspace package",
        )

        root_python = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        pytest_options = root_python["tool"]["pytest"]["ini_options"]
        self.assertIn("tests", pytest_options["testpaths"])
        self.assertIn("packages", pytest_options["testpaths"])
        self.assertIn("services", pytest_options["testpaths"])
        self.assertEqual(pytest_options["pythonpath"], ["packages/contracts/python"])

        contracts_python = tomllib.loads(
            (ROOT / "packages" / "contracts" / "pyproject.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            contracts_python["project"]["requires-python"],
            ">=3.11",
        )

    def test_run_task_help_and_short_lived_command_discovery(self) -> None:
        help_result = _run_node("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("server, collector, dashboard, cli, test, format", help_result.stdout)

        unknown_result = _run_node("not-a-real-task")
        self.assertEqual(unknown_result.returncode, 2)
        self.assertIn("Unknown task: not-a-real-task", unknown_result.stderr)

        probes = {
            "collector": ("python -m services.collector", "life-context-collector"),
            "cli": ("python -m apps.cli", "contextctl"),
            "dashboard": ("apps/dashboard", "vite"),
        }
        for task, (discovery_marker, help_marker) in probes.items():
            with self.subTest(task=task):
                result = _run_node(task, "--help")
                combined = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, combined)
                self.assertIn(discovery_marker, combined)
                self.assertIn(help_marker, combined)

        dispatcher = RUN_TASK.read_text(encoding="utf-8")
        self.assertIn('module: "services.context_server"', dispatcher)
        self.assertIn('marker: "services/context_server/__main__.py"', dispatcher)

        override_environment = os.environ.copy()
        override_environment["LIFE_CONTEXT_SERVER_CMD"] = "node --version"
        override_result = _run_node("server", env=override_environment)
        self.assertEqual(override_result.returncode, 0, override_result.stderr)
        self.assertIn("Running server from LIFE_CONTEXT_SERVER_CMD", override_result.stdout)

    def test_python_module_entrypoints_exist_and_import_without_starting_services(self) -> None:
        entrypoints = {
            "services.context_server": ROOT / "services/context_server/__main__.py",
            "services.collector": ROOT / "services/collector/__main__.py",
            "apps.cli": ROOT / "apps/cli/__main__.py",
        }
        for module, marker in entrypoints.items():
            with self.subTest(module=module):
                self.assertTrue(marker.is_file(), marker)
                probe = _run_python(
                    "-c",
                    (
                        "import importlib; "
                        f"entrypoint = importlib.import_module('{module}.__main__'); "
                        "assert callable(entrypoint.main)"
                    ),
                )
                self.assertEqual(probe.returncode, 0, probe.stderr)

        for module in ("services.collector", "apps.cli"):
            with self.subTest(help_module=module):
                result = _run_python("-m", module, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_documented_environment_defaults_match_effective_configuration(self) -> None:
        from services.collector.runtime import CollectorConfig
        from services.collector.transport.config import TransportConfig
        from services.context_server.config import load_settings

        server = load_settings(environ={})
        server_defaults = {
            "CONTEXT_SERVER_HOST": server.host,
            "CONTEXT_SERVER_PORT": server.port,
            "CONTEXT_SERVER_DATA_DIR": server.data_dir,
            "CONTEXT_SERVER_CAPTURE_INTERVAL_SECONDS": server.capture_interval_seconds,
            "CONTEXT_SERVER_WORKER_COUNT": server.worker_count,
            "CONTEXT_SERVER_QUEUE_SIZE": server.queue_size,
            "CONTEXT_SERVER_MODEL_ENDPOINT": server.model_endpoint,
            "CONTEXT_SERVER_MODEL_NAME": server.model_name,
            "CONTEXT_SERVER_MODEL_REVISION": server.model_revision,
            "CONTEXT_SERVER_AUTH_TOKEN": server.auth_token,
            "CONTEXT_SERVER_REQUIRE_HTTPS_REMOTE": server.require_https_remote,
            "CONTEXT_SERVER_CONFIG_FILE": server.config_file,
        }
        for variable, value in server_defaults.items():
            with self.subTest(variable=variable):
                self.assertEqual(
                    _markdown_default(DESKTOP_README, variable),
                    _display_default(value),
                )

        with _without_runtime_environment():
            collector = CollectorConfig.from_environment()
            transport = TransportConfig.from_env(spool_dir=collector.spool_dir)
        collector_defaults = {
            "COLLECTOR_SERVER_URL": transport.server_url,
            "COLLECTOR_SPOOL_DIR": collector.spool_dir,
            "COLLECTOR_ROOTS": "current directory",
            "COLLECTOR_CAPTURE_INTERVAL_SECONDS": collector.interval_seconds,
            "COLLECTOR_JPEG_QUALITY": collector.jpeg_quality,
            "COLLECTOR_CLIPBOARD_ENABLED": collector.clipboard_enabled,
            "COLLECTOR_ALLOW_REMOTE_CLIPBOARD": collector.remote_clipboard,
            "COLLECTOR_BROWSER_ENABLED": collector.browser_enabled,
            "COLLECTOR_INCLUDE_POWERSHELL_HISTORY": collector.include_git_history,
            "COLLECTOR_INCLUDE_PROCESSES": collector.include_processes,
            "COLLECTOR_MAX_PROCESSES": collector.max_processes,
            "COLLECTOR_BEARER_TOKEN": transport.bearer_token,
            "COLLECTOR_INGEST_PATH": transport.ingest_path,
            "COLLECTOR_ARTIFACT_PATH": transport.artifact_path,
            "COLLECTOR_CA_FILE": transport.ca_file,
            "COLLECTOR_INSECURE_TLS": transport.insecure_tls,
        }
        for variable, value in collector_defaults.items():
            with self.subTest(variable=variable):
                self.assertEqual(
                    _markdown_default(DESKTOP_README, variable),
                    _display_default(value),
                )

        supervisor_source = (
            ROOT / "apps/desktop/src-tauri/src/supervisor.rs"
        ).read_text(encoding="utf-8")
        self.assertEqual(_markdown_default(DESKTOP_README, "LIFE_CONTEXT_PYTHON"), "python")
        self.assertIn(
            'env::var("LIFE_CONTEXT_PYTHON").unwrap_or_else(|_| "python".into())',
            supervisor_source,
        )
        self.assertIn('env::var("LIFE_CONTEXT_ROOT")', supervisor_source)
        self.assertIn(
            "inferred from the Tauri manifest",
            _markdown_default(DESKTOP_README, "LIFE_CONTEXT_ROOT"),
        )


if __name__ == "__main__":
    unittest.main()
