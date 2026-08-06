from __future__ import annotations

import ssl
import unittest
from pathlib import Path

from services.collector.transport.config import TransportConfig


class TransportConfigTests(unittest.TestCase):
    def test_environment_selects_local_or_remote_endpoint_and_security_options(self) -> None:
        config = TransportConfig.from_env(
            {
                "COLLECTOR_SERVER_URL": "https://remote.example/",
                "COLLECTOR_SPOOL_DIR": "./spool",
                "COLLECTOR_AUTH_TOKEN": "from-env",
                "COLLECTOR_TLS_INSECURE": "true",
                "COLLECTOR_MAX_ATTEMPTS": "2",
            }
        )

        self.assertEqual(config.server_url, "https://remote.example")
        self.assertEqual(config.spool_dir, Path("spool"))
        self.assertEqual(config.bearer_token, "from-env")
        self.assertEqual(config.max_attempts, 2)
        context = config.tls_context()
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(context.check_hostname)

    def test_artifact_path_appends_digest_when_configured_as_a_directory(self) -> None:
        config = TransportConfig(
            server_url="http://localhost:8080",
            artifact_path="/api/artifacts",
        )
        digest = "a" * 64
        self.assertEqual(
            config.artifact_url(digest),
            f"http://localhost:8080/api/artifacts/{digest}",
        )

    def test_non_loopback_http_and_embedded_credentials_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-loopback HTTP"):
            TransportConfig(server_url="http://collector.example:8080")

        with self.assertRaisesRegex(ValueError, "embedded credentials"):
            TransportConfig(server_url="https://user:password@collector.example")

    def test_loopback_http_endpoints_remain_usable(self) -> None:
        for endpoint in (
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://[::1]:8000",
        ):
            with self.subTest(endpoint=endpoint):
                config = TransportConfig(server_url=endpoint)
                self.assertEqual(config.server_url, endpoint)
                self.assertFalse(config.is_https)


if __name__ == "__main__":
    unittest.main()
