"""CLI-side client interfaces and a small stdlib HTTP adapter."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from services.context_server.query.models import QueryRequest, jsonable
from services.context_server.query.utils import call_compatible, get_callable, resolve


class ContextClientError(RuntimeError):
    """A user-facing CLI transport or service error."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = dict(headers or {})
        super().__init__(message)


def _is_loopback_host(value: str | None) -> bool:
    host = str(value or "").strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ContextClient(Protocol):
    def start(self, options: Mapping[str, Any] | None = None) -> Any: ...

    def pause(self) -> Any: ...

    def health(self) -> Any: ...

    def status(self) -> Any: ...

    def ingest_batch(
        self,
        payload: Any,
        *,
        batch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any: ...

    def upload_artifact(
        self,
        artifact: Any,
        *,
        sha256: str | None = None,
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any: ...

    def query(self, request: Mapping[str, Any] | QueryRequest) -> Any: ...

    def timeline(self, options: Mapping[str, Any] | None = None) -> Any: ...

    def purge(self, options: Mapping[str, Any]) -> Any: ...

    def config(self, operation: Mapping[str, Any] | None = None) -> Any: ...


@dataclass(slots=True)
class UrllibContextClient:
    """HTTP client with no required third-party runtime dependency."""

    base_url: str = "http://127.0.0.1:8000"
    timeout: float = 15.0
    bearer_token: str | None = None

    def __post_init__(self) -> None:
        # Treat a URL ending in /v1 as an API-root convenience while keeping
        # the canonical versioned paths below in one place.  This also avoids
        # accidentally producing /v1/v1/... when CONTEXT_SERVER_URL already
        # contains the API prefix.
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http:// or https:// URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not include embedded credentials")
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
            raise ValueError("non-loopback HTTP base_url is not allowed; use HTTPS")
        path = parsed.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[: -len("/v1")]
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")

    @classmethod
    def from_environment(cls) -> UrllibContextClient:
        return cls(
            os.environ.get("CONTEXT_SERVER_URL", "http://127.0.0.1:8000"),
            bearer_token=os.environ.get("CONTEXT_SERVER_TOKEN")
            or os.environ.get("CONTEXT_SERVER_AUTH_TOKEN"),
        )

    @staticmethod
    def _v1(path: str) -> str:
        return f"/v1/{path.lstrip('/')}"

    @staticmethod
    def _decode_body(raw: bytes) -> Any:
        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    @staticmethod
    def _header_dict(headers: Any) -> dict[str, str]:
        if headers is None:
            return {}
        try:
            return {str(key): str(value) for key, value in headers.items()}
        except AttributeError:
            return {}

    @staticmethod
    def _error_detail(body: Any) -> str:
        if isinstance(body, Mapping):
            detail = body.get("detail", body.get("message", body.get("error")))
            if detail is not None:
                if isinstance(detail, Mapping) and "message" in detail:
                    detail = detail["message"]
                if isinstance(detail, str):
                    return detail
                return json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)
            return json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
        if body in (None, ""):
            return "empty response body"
        return str(body)

    @classmethod
    def _http_error(
        cls,
        status_code: int,
        body: Any,
        headers: Mapping[str, str] | None = None,
    ) -> ContextClientError:
        detail = cls._error_detail(body)
        return ContextClientError(
            f"context server returned HTTP {status_code}: {detail}",
            status_code=status_code,
            body=body,
            headers=headers,
        )

    @staticmethod
    def _json_payload(value: Any) -> Any:
        if hasattr(value, "to_dict") and callable(value.to_dict):
            value = value.to_dict()
        elif hasattr(value, "model_dump") and callable(value.model_dump):
            try:
                value = value.model_dump(mode="json")
            except TypeError:
                value = value.model_dump()
        if isinstance(value, Mapping):
            return jsonable(dict(value))
        return jsonable(value)

    @staticmethod
    def _artifact_input(
        artifact: Any,
    ) -> tuple[bytes, str | None, dict[str, Any]]:
        """Read bytes plus optional identity/metadata from common artifact inputs."""

        if isinstance(artifact, (bytes, bytearray, memoryview)):
            return bytes(artifact), None, {}

        artifact_path = getattr(artifact, "path", None)
        artifact_sha256 = getattr(artifact, "sha256", None)
        artifact_metadata = getattr(artifact, "metadata", None)
        if artifact_path is not None:
            path = Path(artifact_path)
            expected = None if artifact_sha256 is None else str(artifact_sha256)
            metadata = dict(artifact_metadata or {})
        else:
            path = Path(artifact)
            expected = None
            metadata = {}
        if not path.is_file():
            raise FileNotFoundError(path)
        return path.read_bytes(), expected, metadata

    def _request(
        self,
        method: str,
        path: str,
        payload: Any = None,
        params: Mapping[str, Any] | None = None,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        if payload is not None and body is not None:
            raise ValueError("request cannot contain both a JSON payload and raw body")
        query = ""
        if params:
            query = "?" + urlencode(
                {key: value for key, value in params.items() if value is not None}, doseq=True
            )
        request_headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(
                self._json_payload(payload),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if body is not None and payload is None:
            request_headers.setdefault("Content-Type", "application/octet-stream")
        if headers is not None:
            request_headers.update({str(key): str(value) for key, value in headers.items()})
        # Authentication is applied last so endpoint-specific headers cannot
        # accidentally replace the configured bearer credential.
        if self.bearer_token:
            request_headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = Request(
            f"{self.base_url}{path if path.startswith('/') else '/' + path}{query}",
            data=body,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            response = urlopen(request, timeout=self.timeout)
            try:
                raw = response.read()
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                if not isinstance(raw, (bytes, bytearray, memoryview)):
                    raise ContextClientError("context server returned a non-byte response body")
                response_headers = self._header_dict(getattr(response, "headers", None))
                status = getattr(response, "status", None)
                if status is None:
                    getcode = getattr(response, "getcode", None)
                    status = getcode() if callable(getcode) else None
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except HTTPError as error:
            try:
                raw = error.read()
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
            finally:
                close = getattr(error, "close", None)
                if callable(close):
                    close()
            body_value = self._decode_body(bytes(raw))
            raise self._http_error(
                int(error.code),
                body_value,
                self._header_dict(error.headers),
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", None) or str(error)
            raise ContextClientError(
                f"could not reach context server at {self.base_url}: {reason}"
            ) from error
        body_value = self._decode_body(bytes(raw))
        if isinstance(status, int) and not 200 <= status <= 299:
            raise self._http_error(status, body_value, response_headers)  # type: ignore[arg-type]
        if not raw:
            return {"ok": True}
        if isinstance(body_value, str):
            # A stale dashboard, proxy, or unrelated service can return HTTP
            # 200 with HTML/plain text. Treat that as a transport failure so
            # lifecycle commands do not report false success.
            content_type = response_headers.get("Content-Type", "")
            suffix = f" (content-type: {content_type})" if content_type else ""
            raise ContextClientError(
                "context server returned a non-JSON response; "
                f"verify the base URL points to the context API{suffix}"
            )
        return body_value

    def health(self) -> Any:
        return self._request("GET", self._v1("health"))

    def start(self, options: Mapping[str, Any] | None = None) -> Any:
        return self._request("POST", self._v1("start"), options or {})

    def pause(self) -> Any:
        return self._request("POST", self._v1("pause"))

    def status(self) -> Any:
        return self._request("GET", self._v1("status"))

    def ingest_batch(
        self,
        payload: Any,
        *,
        batch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        if batch_id is not None and idempotency_key is not None and batch_id != idempotency_key:
            raise ValueError("batch_id and idempotency_key must match when both are provided")
        if isinstance(payload, Mapping) or hasattr(payload, "to_dict"):
            body_payload = payload
        else:
            try:
                records = list(payload)
            except TypeError as error:
                raise TypeError("ingest batch must be a mapping or iterable of mappings") from error
            if any(not isinstance(record, Mapping) for record in records):
                raise TypeError("every ingest batch record must be a mapping")
            body_payload = {"records": records}
        key = idempotency_key if idempotency_key is not None else batch_id
        request_headers = {"Idempotency-Key": key} if key else None
        return self._request(
            "POST",
            self._v1("ingest/batches"),
            body_payload,
            headers=request_headers,
        )

    ingest = ingest_batch
    send_batch = ingest_batch

    def upload_artifact(
        self,
        artifact: Any,
        *,
        sha256: str | None = None,
        media_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        content, artifact_digest, artifact_metadata = self._artifact_input(artifact)
        expected_digest = sha256 or artifact_digest
        if expected_digest is not None:
            expected_digest = str(expected_digest).strip().lower()
            if len(expected_digest) != 64 or any(
                character not in "0123456789abcdef" for character in expected_digest
            ):
                raise ValueError("sha256 must be a 64-character hexadecimal digest")
        digest = hashlib.sha256(content).hexdigest()
        if expected_digest is not None and expected_digest != digest:
            raise ValueError("artifact bytes do not match the requested SHA-256 digest")
        if metadata:
            artifact_metadata.update(dict(metadata))
        content_type = media_type or str(
            artifact_metadata.get(
                "content_type", artifact_metadata.get("media_type", "application/octet-stream")
            )
        )
        request_headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(content)),
            "X-Artifact-SHA256": digest,
            "X-Artifact-Size": str(len(content)),
            "Idempotency-Key": digest,
        }
        if artifact_metadata:
            request_headers["X-Artifact-Metadata"] = json.dumps(
                self._json_payload(artifact_metadata),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        return self._request(
            "PUT",
            self._v1(f"artifacts/{digest}"),
            body=content,
            headers=request_headers,
        )

    upload = upload_artifact

    def query(self, request: Mapping[str, Any] | QueryRequest) -> Any:
        payload = request if hasattr(request, "to_dict") else dict(request)
        return self._request("POST", self._v1("query"), payload)

    def timeline(self, options: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", self._v1("timeline"), params=options or {})

    def purge(self, options: Mapping[str, Any]) -> Any:
        return self._request("POST", self._v1("purge"), options)

    def config(self, operation: Mapping[str, Any] | None = None) -> Any:
        operation = dict(operation or {})
        if operation.get("operation", "get") == "get":
            return self._request("GET", self._v1("config"), params=operation)
        return self._request("POST", self._v1("config"), operation)


class ServiceContextClient:
    """Adapt injected service objects for deterministic CLI tests or embedding."""

    def __init__(self, services: Any) -> None:
        self.services = services

    def _target(self, names: tuple[str, ...]) -> Any:
        if isinstance(self.services, Mapping):
            for name in names:
                if name in self.services:
                    return self.services[name]
        for name in names:
            target = getattr(self.services, name, None)
            if target is not None:
                return target
        raise ContextClientError(f"service dependency not configured: {names[0]}")

    def _call(
        self,
        target_names: tuple[str, ...],
        method_names: tuple[str, ...],
        payload: Any = None,
        *,
        no_args: bool = False,
    ) -> Any:
        target = self._target(target_names)
        method = get_callable(target, method_names)
        if method is None:
            raise ContextClientError(f"service does not implement: {', '.join(method_names)}")
        if no_args:
            return jsonable(resolve(method()))
        request = payload
        options = payload if isinstance(payload, Mapping) else {}
        query = payload if isinstance(payload, str) else None
        return jsonable(resolve(call_compatible(method, request, query=query, options=options)))

    def start(self, options: Mapping[str, Any] | None = None) -> Any:
        return self._call(
            ("lifecycle", "controller"),
            ("start", "resume", "startup"),
            dict(options or {}),
        )

    def pause(self) -> Any:
        return self._call(
            ("lifecycle", "controller"),
            ("pause", "stop", "shutdown"),
            no_args=True,
        )

    def health(self) -> Any:
        target = self._target(("health_provider", "status_provider", "status", "lifecycle"))
        method = get_callable(
            target,
            ("health", "health_check", "status", "get_status", "snapshot"),
        )
        if method is None:
            raise ContextClientError("service does not implement health/status/get_status")
        return jsonable(resolve(method()))

    def status(self) -> Any:
        target = self._target(("status_provider", "status", "lifecycle"))
        method = get_callable(target, ("status", "health", "get_status", "snapshot"))
        if method is None:
            method = get_callable(getattr(target, "state", None), ("snapshot",))
        if method is None:
            raise ContextClientError("service does not implement status/health/get_status")
        return jsonable(resolve(method()))

    def query(self, request: Mapping[str, Any] | QueryRequest) -> Any:
        canonical = QueryRequest.from_value(request)
        target = self._target(("query_service", "query", "orchestrator"))
        method = get_callable(target, ("query", "aquery"))
        if method is None:
            raise ContextClientError("query service does not implement query/aquery")
        result = resolve(
            call_compatible(method, canonical, query=canonical.query, options=canonical.to_dict())
        )
        return result.to_dict() if hasattr(result, "to_dict") else jsonable(result)

    def timeline(self, options: Mapping[str, Any] | None = None) -> Any:
        return self._call(
            ("timeline_provider", "timeline", "context_store"),
            ("timeline", "list_timeline", "read_timeline"),
            dict(options or {}),
        )

    def purge(self, options: Mapping[str, Any]) -> Any:
        return self._call(
            ("purge_store", "purge", "context_store"),
            ("purge", "clear", "delete_before"),
            dict(options),
        )

    def config(self, operation: Mapping[str, Any] | None = None) -> Any:
        return self._call(
            ("config_provider", "config", "settings"),
            ("config", "get_config", "update_config", "get", "set"),
            dict(operation or {}),
        )


__all__ = [
    "ContextClient",
    "ContextClientError",
    "ServiceContextClient",
    "UrllibContextClient",
]
