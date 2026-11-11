"""Structured JSON logging for the server.

Tool arguments, note contents, and API keys are never logged.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

_EXTRA_FIELDS = (
    "event",
    "tool",
    "path",
    "method",
    "status",
    "attempt",
    "duration_ms",
    "circuit_state",
    "failure_count",
)


class JsonFormatter(logging.Formatter):
    """Render log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _EXTRA_FIELDS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str | None = None) -> None:
    """Configure the root logger once, writing JSON to stderr."""

    resolved = (level or os.getenv("OBSIDIAN_LOG_LEVEL", "INFO")).upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, resolved, logging.INFO))
