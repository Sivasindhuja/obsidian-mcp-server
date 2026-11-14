from __future__ import annotations

import json
import logging

from mcp_obsidian.logging_config import JsonFormatter


def test_json_formatter_includes_structured_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="mcp_obsidian.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="tool_completed",
        args=(),
        exc_info=None,
    )
    record.event = "tool_completed"
    record.tool = "get_file_contents"
    record.status = "ok"
    record.duration_ms = 12.5

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "INFO"
    assert payload["message"] == "tool_completed"
    assert payload["event"] == "tool_completed"
    assert payload["tool"] == "get_file_contents"
    assert payload["status"] == "ok"
    assert payload["duration_ms"] == 12.5
