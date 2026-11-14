from __future__ import annotations

import pytest

from mcp_obsidian.errors import PathValidationError
from mcp_obsidian.validation import encode_vault_path, normalize_vault_path


@pytest.mark.parametrize(
    "path",
    [
        "../secret.md",
        "folder/../../secret.md",
        "/etc/passwd",
        "\\windows\\system32",
        "C:\\Users\\note.md",
        "%2e%2e/secret.md",
        "%252e%252e/secret.md",
        "folder/%2E%2E/secret.md",
        "bad\x00name.md",
    ],
)
def test_rejects_traversal_and_absolute_paths(path):
    with pytest.raises(PathValidationError):
        normalize_vault_path(path)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("folder/note.md", "folder/note.md"),
        ("./folder/note.md", "folder/note.md"),
        ("folder\\note.md", "folder/note.md"),
        ("folder//note.md", "folder/note.md"),
        ("  folder/note.md  ", "folder/note.md"),
    ],
)
def test_normalizes_valid_paths(raw, expected):
    assert normalize_vault_path(raw) == expected


def test_encoding_preserves_folders_and_encodes_specials():
    encoded = encode_vault_path("folder/My Note#one.md")
    assert encoded == "folder/My%20Note%23one.md"
    assert "/" in encoded


def test_literal_percent_sequences_are_not_decoded():
    # The raw path names a file whose name literally contains "%20".  The
    # percent sign must be encoded once for the URL, not decoded to a space.
    assert normalize_vault_path("folder/100%20.md") == "folder/100%20.md"
    assert encode_vault_path("folder/100%20.md") == "folder/100%2520.md"


def test_directory_encoding_has_single_trailing_slash():
    assert encode_vault_path("folder", is_dir=True) == "folder/"
