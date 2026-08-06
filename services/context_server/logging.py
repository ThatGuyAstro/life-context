"""Structured logging helpers for the context server."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


_HANDLER_MARKER = "_context_server_handler"
_STANDARD_RECORD_FIELDS = set(logging.LogRecord(None, 0, "", 0, "", (), None).__dict__)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


class JsonFormatter(logging.Formatter):
    """Render one log record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key in {"message", "asctime"}:
                continue
            payload[key] = _json_safe(value)

        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class PlainFormatter(logging.Formatter):
    """Readable fallback formatter for local development."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")


def configure_logging(
    level: str = "INFO",
    *,
    json_logs: bool = True,
    stream: Any = None,
    force: bool = False,
) -> logging.Logger:
    """Configure an idempotent root handler and return the context logger.

    Only handlers installed by this function are replaced when ``force`` is
    true; host applications and test runners retain ownership of their own
    handlers.
    """

    root = logging.getLogger()
    numeric_level = logging._nameToLevel.get(str(level).upper(), None)  # type: ignore[attr-defined]
    if numeric_level is None:
        raise ValueError(f"unknown log level: {level!r}")
    root.setLevel(numeric_level)

    managed_handlers = [
        handler
        for handler in root.handlers
        if getattr(handler, _HANDLER_MARKER, False)
    ]
    if force:
        for handler in managed_handlers:
            root.removeHandler(handler)
            handler.close()
        managed_handlers = []

    if managed_handlers:
        handler = managed_handlers[0]
    else:
        handler = logging.StreamHandler(stream or sys.stderr)
        setattr(handler, _HANDLER_MARKER, True)
        root.addHandler(handler)

    handler.setLevel(numeric_level)
    handler.setFormatter(JsonFormatter() if json_logs else PlainFormatter())
    return logging.getLogger("context_server")


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger below the foundation namespace."""

    return logging.getLogger(name or "context_server")


__all__ = ["JsonFormatter", "PlainFormatter", "configure_logging", "get_logger"]
