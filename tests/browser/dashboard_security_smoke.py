"""End-to-end dashboard security smoke with isolated temporary services.

The smoke covers both sides of the dashboard API boundary:

* a local HTTP API reached through the same-origin Vite ``/api`` proxy; and
* an HTTPS API with bearer authentication reached through a proxy-only token.

It also checks that unsafe remote settings and unsafe Vite proxy targets fail
visibly.  Every service uses temporary data and the browser is launched with
GPU acceleration disabled.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from dashboard_settings_smoke import run_browser as run_settings_browser
from dashboard_smoke import (
    SEED_EVENTS,
    artifact_digest,
    installed_chromium,
)
from dashboard_smoke import (
    run_browser as run_dashboard_browser,
)
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "apps" / "dashboard"
VITE_ENTRYPOINT = DASHBOARD / "node_modules" / "vite" / "bin" / "vite.js"
REMOTE_TOKEN = "dashboard-remote-smoke-token"
TIMEOUT_SECONDS = 25.0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def create_certificate(directory: Path) -> tuple[Path, Path]:
    """Create a short-lived certificate trusted only by the temporary Vite process."""

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.DNSName("localhost"),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path = directory / "remote-key.pem"
    cert_path = directory / "remote-cert.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


class Child:
    def __init__(
        self,
        name: str,
        args: list[str],
        env: dict[str, str],
        cwd: Path,
        log_path: Path,
    ) -> None:
        self.name = name
        self.log_path = log_path
        self.log = log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            args,
            cwd=str(cwd),
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )

    def output(self) -> str:
        try:
            self.log.flush()
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def stop(self) -> None:
        if self.process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        try:
            self.log.close()
        except OSError:
            pass


def base_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "CUDA_VISIBLE_DEVICES": "",
            "NVIDIA_VISIBLE_DEVICES": "void",
            "CONTEXT_SERVER_LOG_LEVEL": "WARNING",
            "CONTEXT_SERVER_JSON_LOGS": "false",
            "CONTEXT_SERVER_REQUIRE_HTTPS_REMOTE": "true",
            "CONTEXT_SERVER_MODEL_NAME": "dashboard-smoke-model",
            "CONTEXT_SERVER_MODEL_REVISION": "dashboard-smoke-rev",
        }
    )
    return env


def start_api(
    name: str,
    port: int,
    data_dir: Path,
    log_path: Path,
    *,
    host: str,
    token: str = "",
    cert: Path | None = None,
    key: Path | None = None,
) -> Child:
    env = base_env()
    env.update(
        {
            "CONTEXT_SERVER_HOST": host,
            "CONTEXT_SERVER_PORT": str(port),
            "CONTEXT_SERVER_DATA_DIR": str(data_dir),
            "CONTEXT_SERVER_AUTH_TOKEN": token,
        }
    )
    args = [
        sys.executable,
        "-m",
        "uvicorn",
        "services.context_server.main:app",
        "--host",
        host,
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    if cert is not None and key is not None:
        args.extend(["--ssl-certfile", str(cert), "--ssl-keyfile", str(key)])
    return Child(name, args, env, ROOT, log_path)


def start_vite(
    name: str,
    port: int,
    log_path: Path,
    *,
    target: str | None = None,
    bearer: str = "",
    extra_ca: Path | None = None,
) -> Child:
    env = base_env()
    for key in (
        "VITE_API_BASE_URL",
        "VITE_API_BEARER_TOKEN",
        "VITE_API_PROXY_TARGET",
        "VITE_API_PROXY_BEARER_TOKEN",
    ):
        env.pop(key, None)
    if target is not None:
        env["VITE_API_PROXY_TARGET"] = target
    if bearer:
        env["VITE_API_PROXY_BEARER_TOKEN"] = bearer
    if extra_ca is not None:
        env["NODE_EXTRA_CA_CERTS"] = str(extra_ca)
    return Child(
        name,
        [
            shutil.which("node") or "node",
            str(VITE_ENTRYPOINT),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env,
        DASHBOARD,
        log_path,
    )


def wait_for_url(url: str, *, verify: bool = True, child: Child | None = None) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_error = "not attempted"
    while time.monotonic() < deadline:
        if child is not None and child.process.poll() is not None:
            raise AssertionError(
                f"{child.name} exited early ({child.process.returncode})\n{child.output()}"
            )
        try:
            response = httpx.get(url, timeout=2, verify=verify, trust_env=False)
            if response.status_code < 500:
                return
            last_error = f"HTTP {response.status_code}"
        except Exception as error:
            last_error = str(error)
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {url}: {last_error}")


def seed_api(base_url: str, *, token: str = "", verify: bool = True) -> str:
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(
        timeout=8,
        verify=verify,
        trust_env=False,
        headers={"Accept": "application/json", **auth},
    ) as client:
        health = client.get(f"{base_url.rstrip('/')}/health")
        health.raise_for_status()
        refs: list[dict[str, Any]] = []
        for event in SEED_EVENTS:
            digest = hashlib.sha256(event.artifact_body).hexdigest()
            response = client.put(
                f"{base_url.rstrip('/')}/artifacts/{digest}",
                content=event.artifact_body,
                headers={
                    "Content-Type": "text/plain",
                    "X-Artifact-SHA256": digest,
                    "X-Artifact-Metadata": (
                        '{"retention_tier":"raw","evidence_label":"browser-security-smoke"}'
                    ),
                    **auth,
                },
            )
            if response.status_code not in {200, 201}:
                raise AssertionError(
                    f"artifact upload failed: {response.status_code} {response.text}"
                )
            refs.append(
                {
                    "artifact_id": digest,
                    "kind": "text",
                    "media_type": "text/plain",
                    "size_bytes": len(event.artifact_body),
                    "sha256": digest,
                    "metadata": {"role": "dashboard-browser-security-smoke"},
                }
            )
        captured_at = "2026-08-05T20:01:00Z"
        envelope = {
            "envelope_id": "dashboard-security-smoke-envelope",
            "schema_version": "1",
            "captured_at": captured_at,
            "source_kind": "application",
            "producer": "dashboard-browser-security-smoke",
            "metadata": {"smoke": "dashboard-security", "deterministic": True},
            "events": [
                {
                    "event_id": event.event_id,
                    "source_id": event.source_id,
                    "source_kind": event.source_kind,
                    "event_type": "observed",
                    "occurred_at": event.occurred_at,
                    "captured_at": captured_at,
                    "payload": {"text": event.text},
                    "artifacts": [refs[index]],
                    "metadata": {"name": event.text.split(":", 1)[-1].rstrip(".")},
                }
                for index, event in enumerate(SEED_EVENTS)
            ],
        }
        response = client.post(
            f"{base_url.rstrip('/')}/ingest/batches",
            json={"records": [envelope]},
            headers={"Idempotency-Key": "dashboard-security-smoke-batch", **auth},
        )
        if response.status_code != 202:
            raise AssertionError(f"ingest failed: {response.status_code} {response.text}")
        timeline = client.get(f"{base_url.rstrip('/')}/timeline?limit=50", headers=auth)
        timeline.raise_for_status()
        ids = {item.get("id") for item in timeline.json().get("items", [])}
        expected = {f"observation:{event.event_id}" for event in SEED_EVENTS}
        if not expected.issubset(ids):
            raise AssertionError(f"seeded observations missing: {expected - ids}")
    return artifact_digest(SEED_EVENTS[0].artifact_body)


def run_visible_connection_validation(url: str) -> None:
    console_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=installed_chromium(playwright),
            headless=True,
            args=["--disable-gpu"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1100})
        page.set_default_timeout(12_000)
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text) if message.type == "error" else None
            ),
        )
        try:
            page.goto(url, wait_until="networkidle", timeout=12_000)
            page.get_by_role("heading", name="Your context, in focus.", exact=True).wait_for()
            page.get_by_text("API connected", exact=True).wait_for()
            remote_url = page.get_by_label("Remote server URL")
            token = page.get_by_label("Bearer token")
            save = page.get_by_role("button", name="Save changes", exact=True)

            remote_url.fill("http://remote.example.test/v1")
            token.fill(REMOTE_TOKEN)
            save.click()
            page.locator(".inline-error").filter(
                has_text="Remote dashboard connections require HTTPS."
            ).wait_for()

            remote_url.fill("https://user:password@remote.example.test/v1")
            save.click()
            page.locator(".inline-error").filter(
                has_text="must not include embedded credentials"
            ).wait_for()
            if page.get_by_role("button", name="Saved", exact=True).count():
                raise AssertionError("embedded credentials were shown as saved")
            if console_errors:
                raise AssertionError(f"console errors: {console_errors[:10]}")
            print("settings_validation=PASS remote-http-visible+embedded-credentials-visible")
        finally:
            browser.close()


def run_invalid_proxy_target(
    target: str,
    expected: str,
    temp_dir: Path,
    label: str,
    children: list[Child],
) -> None:
    child = start_vite(label, free_port(), temp_dir / f"{label}.log", target=target)
    children.append(child)
    deadline = time.monotonic() + 15
    while child.process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if child.process.poll() is None:
        child.stop()
        raise AssertionError(f"{label} did not fail during Vite startup")
    output = child.output()
    if expected not in output:
        raise AssertionError(f"{label} output did not contain {expected!r}:\n{output}")
    print(f"{label}=PASS visible={expected}")


def main() -> int:
    children: list[Child] = []
    temporary = tempfile.mkdtemp(prefix="life-context-dashboard-security-")
    try:
        temp_dir = Path(temporary)
        cert, key = create_certificate(temp_dir)

        local_port = free_port()
        local_vite_port = free_port()
        local_api = start_api(
            "local-api",
            local_port,
            temp_dir / "local-data",
            temp_dir / "local-api.log",
            host="127.0.0.1",
        )
        local_vite = start_vite(
            "local-vite",
            local_vite_port,
            temp_dir / "local-vite.log",
            target=f"http://127.0.0.1:{local_port}",
        )
        children.extend([local_api, local_vite])
        wait_for_url(f"http://127.0.0.1:{local_port}/health", child=local_api)
        wait_for_url(f"http://127.0.0.1:{local_vite_port}/", child=local_vite)
        local_digest = seed_api(f"http://127.0.0.1:{local_port}")
        run_dashboard_browser(f"http://127.0.0.1:{local_vite_port}/", local_digest)
        run_settings_browser(f"http://127.0.0.1:{local_vite_port}/")
        print("local_same_origin=PASS query+timeline+evidence+settings")

        remote_port = free_port()
        remote_vite_port = free_port()
        remote_api = start_api(
            "remote-https-api",
            remote_port,
            temp_dir / "remote-data",
            temp_dir / "remote-api.log",
            host="0.0.0.0",
            token=REMOTE_TOKEN,
            cert=cert,
            key=key,
        )
        remote_vite = start_vite(
            "remote-https-vite",
            remote_vite_port,
            temp_dir / "remote-vite.log",
            target=f"https://127.0.0.1:{remote_port}/v1",
            bearer=REMOTE_TOKEN,
            extra_ca=cert,
        )
        children.extend([remote_api, remote_vite])
        remote_base = f"https://127.0.0.1:{remote_port}/v1"
        wait_for_url(f"{remote_base}/health", verify=False, child=remote_api)
        wait_for_url(f"http://127.0.0.1:{remote_vite_port}/", child=remote_vite)
        remote_digest = seed_api(remote_base, token=REMOTE_TOKEN, verify=False)
        run_dashboard_browser(f"http://127.0.0.1:{remote_vite_port}/", remote_digest)
        run_settings_browser(f"http://127.0.0.1:{remote_vite_port}/")
        run_visible_connection_validation(f"http://127.0.0.1:{remote_vite_port}/")
        print("remote_https_bearer=PASS proxied HTTPS+bearer+query+timeline+evidence+settings")

        run_invalid_proxy_target(
            "http://remote.example.test/v1",
            "non-loopback HTTP dashboard API proxy target is not allowed; use HTTPS",
            temp_dir,
            "remote_http_proxy_rejection",
            children,
        )
        run_invalid_proxy_target(
            "https://user:password@remote.example.test/v1",
            "dashboard API proxy target must not include embedded credentials",
            temp_dir,
            "embedded_credentials_proxy_rejection",
            children,
        )
        print("dashboard_security_smoke=PASS")
        return 0
    except Exception as error:
        print(f"dashboard_security_smoke=FAIL {error}", file=sys.stderr)
        for child in children:
            try:
                print(f"--- {child.name} ---\n{child.output()}", file=sys.stderr)
            except Exception:
                pass
        return 1
    finally:
        for child in reversed(children):
            child.stop()
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
