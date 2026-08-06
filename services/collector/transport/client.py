"""JSON batch and content-addressed artifact HTTP client."""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import TransportConfig
from .errors import HttpTransportError, RetryableTransportError
from .retry import RetryPolicy
from .spool import StoredArtifact


_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429})


def _is_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS_CODES or 500 <= status_code <= 599


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        # HTTP-date values are valid but are intentionally not converted here;
        # exponential backoff remains safe when a server sends one.
        return None


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode_body(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _header_dict(headers: object) -> dict[str, str]:
    if headers is None:
        return {}
    try:
        return {
            str(key): str(value) for key, value in headers.items()
        }  # type: ignore[attr-defined]
    except AttributeError:
        return {}


@dataclass(frozen=True, slots=True)
class ClientResponse:
    """Decoded HTTP response returned by the delivery client."""

    status_code: int
    body: Any
    headers: Mapping[str, str]
    attempts: int

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code <= 299

    @property
    def idempotent_conflict(self) -> bool:
        return self.status_code == 409


class JsonBatchClient:
    """Minimal HTTP client with injectable I/O for deterministic tests."""

    def __init__(
        self,
        config: TransportConfig | None = None,
        *,
        server_url: str | None = None,
        retry_policy: RetryPolicy | None = None,
        opener: Callable[..., object] = urlopen,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        retry_observer: Callable[[int, float, BaseException], None] | None = None,
    ) -> None:
        if config is None:
            config = TransportConfig(server_url=server_url or TransportConfig().server_url)
        elif server_url is not None:
            raise ValueError("pass either config or server_url, not both")
        self.config = config
        self.retry_policy = retry_policy or RetryPolicy(
            max_attempts=config.max_attempts,
            initial_delay_seconds=config.backoff_initial_seconds,
            max_delay_seconds=config.backoff_max_seconds,
            multiplier=config.backoff_multiplier,
            jitter=config.backoff_jitter,
        )
        self.opener = opener
        self.sleep = sleep
        self.random_value = random_value
        self.retry_observer = retry_observer
        self.tls_context = config.tls_context()

    def _open(self, request: Request) -> object:
        kwargs: dict[str, object] = {"timeout": self.config.request_timeout_seconds}
        if urlparse(request.full_url).scheme == "https":
            kwargs["context"] = self.tls_context
        return self.opener(request, **kwargs)

    @staticmethod
    def _response_status(response: object) -> int:
        status = getattr(response, "status", None)
        if status is None:
            getcode = getattr(response, "getcode", None)
            status = getcode() if callable(getcode) else None
        if not isinstance(status, int):
            raise RetryableTransportError("HTTP response did not contain a status code")
        return status

    @staticmethod
    def _read_response(response: object) -> bytes:
        read = getattr(response, "read", None)
        if not callable(read):
            raise RetryableTransportError("HTTP response did not provide a read method")
        value = read()
        if isinstance(value, str):
            return value.encode("utf-8")
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise RetryableTransportError("HTTP response body was not bytes")
        return bytes(value)

    @staticmethod
    def _close_response(response: object) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        accepted_statuses: frozenset[int] = frozenset(),
    ) -> ClientResponse:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "collector-transport/1",
        }
        if body is not None:
            request_headers["Content-Length"] = str(len(body))
        if self.config.bearer_token:
            request_headers["Authorization"] = f"Bearer {self.config.bearer_token}"
        if headers:
            request_headers.update(headers)

        seen_attempts = 0

        def operation(attempt: int) -> ClientResponse:
            nonlocal seen_attempts
            seen_attempts = attempt
            request = Request(
                url,
                data=body,
                headers=request_headers,
                method=method,
            )
            try:
                response = self._open(request)
            except HTTPError as error:
                response_headers = _header_dict(error.headers)
                try:
                    raw = self._read_response(error)
                finally:
                    self._close_response(error)
                status = int(error.code)
                decoded = _decode_body(raw)
                if 200 <= status <= 299 or status in accepted_statuses:
                    return ClientResponse(status, decoded, response_headers, attempt)
                raise HttpTransportError(
                    status,
                    decoded,
                    headers=response_headers,
                    retryable=_is_retryable_status(status),
                    retry_after_seconds=_retry_after(response_headers),
                    attempts=attempt,
                )
            except (URLError, TimeoutError, OSError) as error:
                retryable = RetryableTransportError(str(error) or error.__class__.__name__)
                retryable.attempts = attempt  # type: ignore[attr-defined]
                raise retryable from error

            try:
                status = self._response_status(response)
                response_headers = _header_dict(getattr(response, "headers", None))
                raw = self._read_response(response)
            finally:
                self._close_response(response)
            decoded = _decode_body(raw)
            if 200 <= status <= 299 or status in accepted_statuses:
                return ClientResponse(status, decoded, response_headers, attempt)
            raise HttpTransportError(
                status,
                decoded,
                headers=response_headers,
                retryable=_is_retryable_status(status),
                retry_after_seconds=_retry_after(response_headers),
                attempts=attempt,
            )

        try:
            return self.retry_policy.run(
                operation,
                sleep=self.sleep,
                random_value=self.random_value,
                retry_observer=self.retry_observer,
            )
        except BaseException as error:
            if seen_attempts:
                try:
                    error.attempts = seen_attempts  # type: ignore[attr-defined]
                except AttributeError:
                    pass
            raise

    def ingest_batch(
        self,
        records_or_payload: Iterable[Mapping[str, Any]] | Mapping[str, Any],
        *,
        batch_id: str | None = None,
    ) -> ClientResponse:
        """POST a generic JSON batch to the configured ingest endpoint."""

        if isinstance(records_or_payload, Mapping):
            payload: Mapping[str, Any] = dict(records_or_payload)
        else:
            records: list[dict[str, Any]] = []
            for record in records_or_payload:
                if not isinstance(record, Mapping):
                    raise TypeError("every batch record must be a mapping")
                records.append(dict(record))
            payload = {"records": records}
        headers = {"Content-Type": "application/json"}
        if batch_id:
            headers["Idempotency-Key"] = batch_id
        return self._request(
            "POST",
            self.config.ingest_url(),
            body=_json_bytes(payload),
            headers=headers,
            # A lost response after a successful idempotent ingest can leave
            # the server returning 409 for the replay. Treat that conflict as
            # an accepted delivery, just like content-addressed artifacts.
            accepted_statuses=frozenset({409}),
        )

    # ``send_batch`` is a convenient verb for callers that do not care about
    # the endpoint terminology.
    send_batch = ingest_batch
    ingest = ingest_batch

    def upload_artifact(
        self,
        artifact: StoredArtifact | str | os.PathLike[str],
        *,
        sha256: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ClientResponse:
        """PUT an artifact at its digest URL.

        A ``409 Conflict`` is returned to the caller as an accepted,
        idempotent result.  The spool layer can therefore mark it delivered
        without uploading the bytes twice when a prior response was lost.
        """

        expected_digest: str | None = None
        if isinstance(artifact, StoredArtifact):
            path = artifact.path
            expected_digest = artifact.sha256.lower()
            artifact_metadata = dict(artifact.metadata)
        else:
            path = Path(artifact)
            artifact_metadata = {}
        if sha256 is not None:
            supplied_digest = sha256.lower()
            if len(supplied_digest) != 64 or any(
                character not in "0123456789abcdef" for character in supplied_digest
            ):
                raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
            if expected_digest is not None and supplied_digest != expected_digest:
                raise ValueError("sha256 does not match the StoredArtifact identity")
            expected_digest = supplied_digest
        if metadata:
            artifact_metadata.update(metadata)
        if not path.is_file():
            raise FileNotFoundError(path)

        with path.open("rb") as stream:
            content = stream.read()
        digest = hashlib.sha256(content).hexdigest()
        if expected_digest is not None and digest != expected_digest:
            raise ValueError("artifact bytes do not match the requested SHA-256 digest")
        size = len(content)

        headers = {
            "Content-Type": str(
                artifact_metadata.get(
                    "content_type",
                    artifact_metadata.get("media_type", "application/octet-stream"),
                )
            ),
            "X-Artifact-SHA256": digest,
            "X-Artifact-Size": str(size),
            "Idempotency-Key": digest,
        }
        if artifact_metadata:
            headers["X-Artifact-Metadata"] = _json_bytes(artifact_metadata).decode("utf-8")
        return self._request(
            "PUT",
            self.config.artifact_url(digest),
            body=content,
            headers=headers,
            accepted_statuses=frozenset({409}),
        )

    upload = upload_artifact


BatchIngestClient = JsonBatchClient
