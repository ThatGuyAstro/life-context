"""Errors shared by the context-server inference boundary.

The inference package deliberately keeps transport, model, contract, and queue
errors separate.  The worker uses the ``retryable`` flag to make a bounded
retry decision without having to know which model provider produced an error.
"""

from __future__ import annotations


class InferenceError(Exception):
    """Base class for expected inference failures."""


class ConfigurationError(InferenceError):
    """The configured model endpoint or inference contract is invalid."""

    retryable = False


class ModelError(InferenceError):
    """Base class for failures returned by or while talking to a model."""

    retryable = False


class ModelTransportError(ModelError):
    """A request could not be completed at the transport layer."""

    retryable = True


class ModelProtocolError(ModelError):
    """The provider returned a successful response with an invalid shape."""

    retryable = True


class ModelHTTPError(ModelError):
    """An OpenAI-compatible endpoint returned a non-success HTTP status."""

    def __init__(
        self,
        status_code: int,
        message: str = "model endpoint returned an HTTP error",
        *,
        body: str = "",
        retry_after_s: float | None = None,
    ) -> None:
        self.status_code = int(status_code)
        self.body = body
        self.retry_after_s = retry_after_s
        self.retryable = self.status_code in {408, 409, 425, 429} or self.status_code >= 500
        body_suffix = f": {body[:300]}" if body else ""
        super().__init__(f"{message} ({self.status_code}){body_suffix}")


class JsonExtractionError(InferenceError):
    """The model output was not one valid JSON value."""

    # A bounded retry is useful for an otherwise healthy model that occasionally
    # emits prose or malformed JSON.  The worker still dead-letters it after the
    # configured attempt budget.
    retryable = True


class DuplicateJsonKeyError(JsonExtractionError):
    """JSON contained a duplicate object member."""


class SchemaValidationError(JsonExtractionError):
    """The extracted JSON value did not satisfy the declared schema."""


def exception_code(error: BaseException) -> str:
    """Return a stable, low-cardinality code suitable for job records."""

    if isinstance(error, ModelHTTPError):
        return f"model_http_{error.status_code}"
    if isinstance(error, ModelTransportError):
        return "model_transport"
    if isinstance(error, ModelProtocolError):
        return "model_protocol"
    if isinstance(error, ConfigurationError):
        return "configuration"
    if isinstance(error, SchemaValidationError):
        return "json_schema_validation"
    if isinstance(error, DuplicateJsonKeyError):
        return "json_duplicate_key"
    if isinstance(error, JsonExtractionError):
        return "json_extraction"
    return error.__class__.__name__.lower()


def is_retryable(error: BaseException, *, retry_contract_errors: bool = True) -> bool:
    """Classify an expected exception for the worker retry policy."""

    if isinstance(error, JsonExtractionError):
        return retry_contract_errors
    return bool(getattr(error, "retryable", False))
