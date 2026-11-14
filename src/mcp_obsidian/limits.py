"""Helpers for bounded MCP output.

Limits are expressed in UTF-8 bytes because that is what is actually sent over
the wire. Truncation always lands on a valid UTF-8 boundary so a multibyte
character is never split into an invalid replacement character.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

DEFAULT_MAX_FILE_BYTES = 1_000_000
MAX_MAX_FILE_BYTES = 10_000_000
DEFAULT_MAX_BATCH_BYTES = 2_000_000
MAX_MAX_BATCH_BYTES = 20_000_000
DEFAULT_MAX_SEARCH_BYTES = 1_000_000
MAX_MAX_SEARCH_BYTES = 10_000_000
DEFAULT_MAX_SUMMARY_BYTES = 1_000_000
MAX_MAX_SUMMARY_BYTES = 10_000_000

_MISSING = object()


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer environment setting without failing a tool call."""

    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    """Read a finite float environment setting without failing startup."""

    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(minimum, min(value, maximum))


def resolve_limit(
    value: int | None,
    env_name: str,
    default: int,
    maximum: int,
) -> int:
    """Resolve an explicit limit or the matching environment setting."""

    if value is None:
        value = env_int(env_name, default, 1, maximum)
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        resolved = default
    return max(1, min(resolved, maximum))


def utf8_len(value: str) -> int:
    """Return the UTF-8 byte length of a string."""

    return len(value.encode("utf-8"))


def trim_utf8_bytes(data: bytes, max_bytes: int) -> tuple[bytes, bool]:
    """Trim bytes to ``max_bytes`` without leaving a partial UTF-8 sequence."""

    if max_bytes < 0:
        max_bytes = 0
    if len(data) <= max_bytes:
        return data, False

    prefix = data[:max_bytes]
    while prefix:
        try:
            prefix.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data":
                prefix = prefix[:-1]
                continue
            # The source contained invalid UTF-8, not a truncation boundary.
            # Keep the bytes so the caller can report the decoding failure.
            break
        else:
            break
    return prefix, True


def truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate a string to at most ``max_bytes`` UTF-8 bytes."""

    if max_bytes < 0:
        max_bytes = 0
    data = value.encode("utf-8")
    if len(data) <= max_bytes:
        return value, False
    prefix, truncated = trim_utf8_bytes(data, max_bytes)
    return prefix.decode("utf-8", errors="ignore"), truncated


def json_text(value: Any) -> str:
    """Return compact, non-ASCII-escaped JSON for size accounting."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def json_bytes(value: Any) -> int:
    """Return the UTF-8 byte size of a JSON value."""

    return utf8_len(json_text(value))


def json_characters(value: Any) -> int:
    """Return the character count of a JSON value's compact representation."""

    return len(json_text(value))


def _shrink_string(value: str, budget: int) -> Any:
    if budget < 2:
        return _MISSING
    low, high = 0, len(value)
    best = ""
    while low <= high:
        middle = (low + high) // 2
        candidate = value[:middle]
        if json_bytes(candidate) <= budget:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best if json_bytes(best) <= budget else _MISSING


def _shrink(value: Any, budget: int) -> Any:
    if budget < 2:
        return _MISSING
    if json_bytes(value) <= budget:
        return value

    if isinstance(value, str):
        return _shrink_string(value, budget)

    if isinstance(value, list):
        result: list[Any] = []
        for item in value:
            if json_bytes(result + [item]) <= budget:
                result.append(item)
                continue
            remaining = budget - json_bytes(result) - 2 - (1 if result else 0)
            shrunk = _shrink(item, remaining)
            if shrunk is _MISSING:
                break
            if json_bytes(result + [shrunk]) <= budget:
                result.append(shrunk)
            else:
                break
        return result

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if json_bytes(result | {key: item}) <= budget:
                result[key] = item
                continue
            remaining = (
                budget
                - json_bytes(result)
                - 2
                - (1 if result else 0)
                - json_bytes(key)
                - 1
            )
            shrunk = _shrink(item, remaining)
            if shrunk is _MISSING:
                continue
            if json_bytes(result | {key: shrunk}) <= budget:
                result[key] = shrunk
        return result

    return value if json_bytes(value) <= budget else _MISSING


def fit_json_to_bytes(value: Any, max_bytes: int) -> tuple[Any, bool]:
    """Return a JSON-compatible value that fits in ``max_bytes``.

    The value is only changed when necessary. Lists and objects are reduced
    item-by-item, and long strings are cut on character boundaries. The second
    return value says whether anything was omitted or shortened.
    """

    if json_bytes(value) <= max_bytes:
        return value, False
    shrunk = _shrink(value, max_bytes)
    if shrunk is _MISSING:
        return None, True
    return shrunk, True
