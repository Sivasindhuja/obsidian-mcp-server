"""Centralised vault path validation and URL encoding.

Obsidian vault paths are always relative to the vault root and use ``/`` as the
separator.  Validation deliberately runs on repeatedly URL-decoded input so
that ``%2e%2e`` and ``%252e%252e`` cannot bypass the traversal checks.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote

from mcp_obsidian.errors import PathValidationError

_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _decoded_forms(value: str) -> list[str]:
    """Return the original value and up to three URL-decoded forms."""
    forms = [value]
    current = value
    for _ in range(3):
        decoded = unquote(current)
        if decoded == current:
            break
        forms.append(decoded)
        current = decoded
    return forms


def normalize_vault_path(path: str, *, allow_root: bool = False) -> str:
    """Validate and normalise a vault-relative path.

    Raises:
        PathValidationError: if the path is absolute, contains a ``..``
            segment (including encoded forms), contains NUL/control characters,
            or is empty when ``allow_root`` is false.
    """

    if not isinstance(path, str):
        raise PathValidationError("path must be a string")

    raw = path.strip()
    if not raw:
        if allow_root:
            return ""
        raise PathValidationError("path is required")

    for form in _decoded_forms(raw):
        if "\x00" in form or any(ord(ch) < 32 for ch in form):
            raise PathValidationError("path contains control characters")

        candidate = form.replace("\\", "/")

        if candidate.startswith("/") or candidate.startswith("//"):
            raise PathValidationError("absolute paths are not allowed")
        if _DRIVE_RE.match(candidate):
            raise PathValidationError("drive-qualified paths are not allowed")

        parts = [part for part in candidate.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise PathValidationError("path traversal is not allowed")

    # Use the once-decoded form so ordinary percent-escapes in filenames are
    # treated literally, while encoded traversal is still rejected above.
    normalized = unquote(raw).replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]

    if not parts and not allow_root:
        raise PathValidationError("path is required")

    return "/".join(parts)


def encode_vault_path(path: str, *, is_dir: bool = False) -> str:
    """Return a URL-safe path while preserving ``/`` folder separators."""

    normalized = normalize_vault_path(path, allow_root=is_dir)
    encoded = quote(normalized, safe="/")
    if is_dir:
        return encoded.rstrip("/") + "/"
    return encoded


def validate_filepath(path: str) -> str:
    """Validate a file path (no directory suffix)."""

    return normalize_vault_path(path)


def validate_dirpath(path: str) -> str:
    """Validate a directory path."""

    return normalize_vault_path(path)
