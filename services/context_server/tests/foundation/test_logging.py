from __future__ import annotations

import io
import json
import logging

from services.context_server.logging import configure_logging


def test_configure_logging_emits_structured_json() -> None:
    stream = io.StringIO()
    configure_logging("INFO", json_logs=True, stream=stream, force=True)

    logging.getLogger("foundation-test").info(
        "started",
        extra={"component": "test", "attempt": 1},
    )

    payload = json.loads(stream.getvalue())
    assert payload["level"] == "INFO"
    assert payload["message"] == "started"
    assert payload["component"] == "test"
    assert payload["attempt"] == 1
