"""Opt-in end-to-end smoke coverage for the real Windows collector."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit

import psutil
import pytest

from services.collector.capture import (
    CaptureBackendError,
    CaptureUnavailableError,
    ImageDependencyError,
    ScreenCaptureService,
)
from services.collector.transport.spool import LocalSpool
from tests.acceptance.test_server_process_lifecycle import (
    _http_json,
    _RealUvicornProcess,
)

_RAW_KEY_FIELDS = frozenset(
    {
        "key",
        "keys",
        "key_event",
        "key_events",
        "keyboard",
        "keyboard_event",
        "keyboard_events",
        "key_down",
        "key_up",
        "raw_input",
        "raw_keyboard",
        "raw_keys",
        "scan_code",
        "virtual_key",
    }
)


def _discover_live_monitors() -> tuple[str, tuple[Any, ...]]:
    try:
        service = ScreenCaptureService()
    except (CaptureUnavailableError, ImageDependencyError) as error:
        pytest.skip(f"no display backend available: {error}")

    try:
        try:
            monitors = tuple(service.backend.enumerate_monitors())
        except (CaptureUnavailableError, CaptureBackendError) as error:
            pytest.skip(f"no display backend available: {error}")
        if not monitors:
            pytest.skip("no physical display monitors were discovered")
        return service.backend_name, monitors
    finally:
        service.close()


def _process_tree(pid: int) -> set[int]:
    try:
        return {
            pid,
            *(child.pid for child in psutil.Process(pid).children(recursive=True)),
        }
    except psutil.NoSuchProcess:
        return {pid}


def _assert_processes_gone(pids: set[int], label: str) -> None:
    deadline = time.monotonic() + 5
    live: list[int] = []
    while time.monotonic() < deadline:
        live = []
        for pid in pids:
            try:
                process = psutil.Process(pid)
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    live.append(pid)
            except psutil.NoSuchProcess:
                continue
        if not live:
            return
        time.sleep(0.05)
    assert not live, f"owned {label} processes remain after shutdown: {live}"


def _terminate_owned_processes(process: subprocess.Popen[str], pids: set[int]) -> None:
    for pid in sorted(pids, reverse=True):
        try:
            owned = psutil.Process(pid)
            if owned.is_running():
                owned.terminate()
        except psutil.NoSuchProcess:
            continue
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_one_collector_tick(
    repository_root: Path,
    spool_dir: Path,
    server_url: str,
) -> tuple[str, set[int]]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith("COLLECTOR_") or key.startswith("CONTEXT_SERVER_"):
            environment.pop(key, None)
    environment.update(
        {
            "COLLECTOR_SERVER_URL": server_url,
            "COLLECTOR_SPOOL_DIR": str(spool_dir),
            "COLLECTOR_FSYNC": "true",
            "COLLECTOR_MAX_ATTEMPTS": "1",
            "COLLECTOR_REQUEST_TIMEOUT_SECONDS": "5",
            "COLLECTOR_BACKOFF_INITIAL_SECONDS": "0",
            "COLLECTOR_BACKOFF_MAX_SECONDS": "0",
            "COLLECTOR_CLIPBOARD_ENABLED": "false",
            "COLLECTOR_BROWSER_ENABLED": "false",
            "COLLECTOR_INCLUDE_POWERSHELL_HISTORY": "false",
            "COLLECTOR_INCLUDE_PROCESSES": "false",
            "COLLECTOR_ALLOW_REMOTE_CLIPBOARD": "false",
            "COLLECTOR_SOURCE_ROUTING": json.dumps(
                {
                    "screenshots": {"enabled": True, "route": "remote"},
                    "foreground": {"enabled": False, "route": "remote"},
                    "filesystem": {"enabled": False, "route": "remote"},
                    "git": {"enabled": False, "route": "remote"},
                    "powershell": {"enabled": False, "route": "remote"},
                    "process": {"enabled": False, "route": "remote"},
                    "browser": {"enabled": False, "route": "remote"},
                    "clipboard": {"enabled": False, "route": "local"},
                },
                separators=(",", ":"),
            ),
            # The collector has no inference path, and this makes the smoke's
            # no-GPU boundary explicit in the child environment as well.
            "CUDA_VISIBLE_DEVICES": "",
            "PYTHONUNBUFFERED": "1",
        }
    )
    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        [sys.executable, "-m", "services.collector", "--once"],
        cwd=repository_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
    )
    owned_pids = _process_tree(process.pid)
    try:
        output, _ = process.communicate(timeout=90)
    except BaseException:
        _terminate_owned_processes(process, owned_pids)
        _assert_processes_gone(owned_pids, "collector")
        raise

    _assert_processes_gone(owned_pids, "collector")
    assert process.returncode == 0, (
        f"collector exited with code {process.returncode}; output:\n{output}"
    )
    return output, owned_pids


def _runtime_report(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        try:
            report = ast.literal_eval(line)
        except (SyntaxError, ValueError):
            continue
        if isinstance(report, dict) and "sequence_id" in report:
            return report
    raise AssertionError(f"collector did not print a runtime report:\n{output}")


def _capture_field_violations(value: object, path: str = "$") -> list[str]:
    violations: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).casefold().replace("-", "_").replace(" ", "_")
            child_path = f"{path}.{key}"
            if normalized in _RAW_KEY_FIELDS:
                violations.append(child_path)
            elif normalized == "keylogging" and nested is not False:
                violations.append(f"{child_path}=not-false")
            elif normalized in {"audio", "video"} and nested not in {False, None}:
                violations.append(f"{child_path}=captured")
            violations.extend(_capture_field_violations(nested, child_path))
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            violations.extend(_capture_field_violations(nested, f"{path}[{index}]"))
    return violations


def _assert_no_capture_fields(value: object) -> None:
    assert not (violations := _capture_field_violations(value)), (
        f"audio/video/raw-keystroke capture found at: {violations}"
    )


def _assert_no_listener(port: int) -> None:
    deadline = time.monotonic() + 5
    listeners: list[Any] = []
    while time.monotonic() < deadline:
        listeners = []
        for connection in psutil.net_connections(kind="tcp"):
            local_address = connection.laddr
            local_port = getattr(local_address, "port", None)
            if local_port is None and local_address:
                local_port = local_address[1]
            if local_port == port and connection.status == psutil.CONN_LISTEN:
                listeners.append(connection)
        if not listeners:
            return
        time.sleep(0.05)
    assert not listeners, f"owned listener remains on port {port}: {listeners}"


def _json_column(row: sqlite3.Row, name: str) -> object:
    value = row[name]
    return json.loads(value) if value else {}


@pytest.mark.skipif(os.name != "nt", reason="Windows collector smoke requires Windows")
def test_real_windows_collector_one_tick_transport_spool_and_cleanup() -> None:
    """Capture the live desktop once, deliver it, then prove clean shutdown."""

    backend_name, monitors = _discover_live_monitors()
    repository_root = Path(__file__).resolve().parents[4]
    expected_monitor_ids = {monitor.monitor_id for monitor in monitors}

    with TemporaryDirectory(prefix="life-context-windows-collector-smoke-") as temporary:
        root = Path(temporary).resolve()
        data_dir = root / "server-data"
        spool_dir = root / "collector-spool"
        server_port: int | None = None
        collector_pids: set[int] = set()

        with _RealUvicornProcess(repository_root, data_dir) as server:
            server_port = int(urlsplit(server.base_url).port or 0)
            assert server_port > 0
            server_pids = server._process_tree()

            initial_status = _http_json(server.base_url, "GET", "/v1/status")
            assert initial_status["model"]["configured"] is False
            assert initial_status["model"]["active_workers"] == 0
            assert initial_status["model"]["worker_status"] == "not_configured"

            output, collector_pids = _run_one_collector_tick(
                repository_root,
                spool_dir,
                server.base_url,
            )
            report = _runtime_report(output)
            artifact_count = len(monitors) + 1
            assert report["monitor_count"] == len(monitors)
            assert report["event_count"] == artifact_count
            assert report["transport"]["ok"] is True
            assert report["transport"]["artifacts"]["uploaded"] == artifact_count
            assert report["transport"]["batches"]["uploaded"] == 1

            live_status = _http_json(server.base_url, "GET", "/v1/status")
            assert live_status["event_count"] == artifact_count
            assert live_status["artifact_count"] == artifact_count
            assert live_status["capture_count"] == 1
            assert live_status["model"]["configured"] is False
            assert live_status["model"]["active_workers"] == 0
            assert live_status["model"]["worker_status"] == "not_configured"
            assert live_status["model"]["processed"] == 0

        assert server_port is not None
        _assert_processes_gone(server_pids, "server")
        _assert_processes_gone(collector_pids, "collector")
        _assert_no_listener(server_port)

        spool = LocalSpool(spool_dir)
        spool_status = spool.status()
        artifact_digests = spool.list_uploaded_artifacts()
        assert spool_status["pending_batches"] == 0
        assert spool_status["processing_batches"] == 0
        assert spool_status["pending_artifacts"] == 0
        assert spool_status["uploaded_artifacts"] == len(monitors) + 1
        assert len(artifact_digests) == len(monitors) + 1

        local_artifacts: dict[str, dict[str, Any]] = {}
        for digest in artifact_digests:
            stored = spool.artifact(digest)
            content = stored.path.read_bytes()
            assert hashlib.sha256(content).hexdigest() == digest
            assert stored.size == len(content)
            assert stored.metadata["content_type"] == "image/jpeg"
            metadata = dict(stored.metadata)
            local_artifacts[str(metadata["name"])] = {
                "digest": digest,
                "metadata": metadata,
                "content": content,
            }

        expected_names = {
            *(f"{monitor_id}.jpg" for monitor_id in expected_monitor_ids),
            "contact-sheet.jpg",
        }
        assert set(local_artifacts) == expected_names
        for monitor_id in expected_monitor_ids:
            metadata = local_artifacts[f"{monitor_id}.jpg"]["metadata"]
            assert metadata["monitor_id"] == monitor_id
            assert metadata["retention_tier"] == "raw"
        assert local_artifacts["contact-sheet.jpg"]["metadata"]["retention_tier"] == "keyframe"

        database = data_dir / "context.sqlite3"
        assert database.is_file()
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
        try:
            artifact_rows = connection.execute(
                "SELECT digest, size_bytes, media_type, relative_path, retention_tier, "
                "created_at, expires_at, metadata_json FROM artifacts ORDER BY digest"
            ).fetchall()
            assert len(artifact_rows) == len(monitors) + 1
            assert {str(row["digest"]) for row in artifact_rows} == set(artifact_digests)

            server_tiers: dict[str, str] = {}
            for row in artifact_rows:
                digest = str(row["digest"])
                local = next(
                    artifact
                    for artifact in local_artifacts.values()
                    if artifact["digest"] == digest
                )
                server_path = data_dir / "artifacts" / str(row["relative_path"])
                server_content = server_path.read_bytes()
                assert hashlib.sha256(server_content).hexdigest() == digest
                assert server_content == local["content"]
                assert int(row["size_bytes"]) == len(server_content)
                assert row["media_type"] == "image/jpeg"
                assert row["retention_tier"] == local["metadata"]["retention_tier"]
                assert row["expires_at"] is not None
                assert str(row["expires_at"]) > str(row["created_at"])
                server_tiers[digest] = str(row["retention_tier"])
                _assert_no_capture_fields(_json_column(row, "metadata_json"))

            assert sorted(server_tiers.values()).count("raw") == len(monitors)
            assert sorted(server_tiers.values()).count("keyframe") == 1

            capture_rows = connection.execute(
                "SELECT metadata_json FROM captures"
            ).fetchall()
            assert len(capture_rows) == 1
            _assert_no_capture_fields(_json_column(capture_rows[0], "metadata_json"))

            observation_rows = connection.execute(
                "SELECT source_id, payload_json, metadata_json FROM observations"
            ).fetchall()
            assert len(observation_rows) == len(monitors) + 1
            source_ids = {str(row["source_id"]) for row in observation_rows}
            assert {f"screen:{monitor_id}" for monitor_id in expected_monitor_ids} <= source_ids
            contact_source_ids = {
                f"screen:{monitor_id}" for monitor_id in expected_monitor_ids
            }
            assert len(source_ids - contact_source_ids) == 1

            observed_monitor_ids: set[str] = set()
            observed_adapters: set[str] = set()
            for row in observation_rows:
                payload = _json_column(row, "payload_json")
                metadata = _json_column(row, "metadata_json")
                _assert_no_capture_fields(payload)
                _assert_no_capture_fields(metadata)
                if isinstance(payload, dict) and isinstance(payload.get("monitor"), dict):
                    observed_monitor_ids.add(str(payload["monitor"]["monitor_id"]))
                if isinstance(metadata, dict) and metadata.get("capture_kind") == "monitor":
                    observed_adapters.add(str(metadata["adapter"]))
            assert observed_monitor_ids == expected_monitor_ids
            assert observed_adapters == {backend_name}
        finally:
            connection.close()

        print(
            "windows_collector_smoke_report="
            + json.dumps(
                {
                    "backend": backend_name,
                    "monitor_count": len(monitors),
                    "artifact_count": len(artifact_digests),
                    "artifact_sha256": sorted(artifact_digests),
                    "retention_tiers": sorted(server_tiers.values()),
                    "spool": spool_status,
                    "model_inference": "not_configured",
                    "owned_processes_clean": True,
                    "owned_port_clean": True,
                },
                sort_keys=True,
            )
        )
