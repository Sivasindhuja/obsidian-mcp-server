"""Typed errors shared by the Obsidian API wrapper and the MCP tools."""

from __future__ import annotations


class ObsidianMCPError(Exception):
    """Base class for expected, user-actionable failures.

    ``status`` is surfaced in the MCP tool response so clients can distinguish
    conflicts and validation failures from transient service failures.
    """

    status = "error"
    counts_as_circuit_failure = False


class PathValidationError(ObsidianMCPError, ValueError):
    """A vault path is absolute, traverses upwards, or is otherwise invalid."""

    status = "invalid_path"


class InputValidationError(ObsidianMCPError, ValueError):
    """A tool argument is syntactically invalid."""

    status = "invalid_input"


class NoteAlreadyExistsError(ObsidianMCPError):
    """Safe-create was requested for a note that already exists."""

    status = "conflict"


class PreconditionFailedError(ObsidianMCPError):
    """A conditional write failed because the note changed or already exists."""

    status = "conflict"


class UnsupportedFileTypeError(ObsidianMCPError):
    """A text operation was attempted against a binary or undecodable file."""

    status = "unsupported_file_type"


class TransientAPIError(ObsidianMCPError):
    """Retries were exhausted while talking to the Obsidian API."""

    status = "service_unavailable"
    counts_as_circuit_failure = True


class CircuitOpenError(ObsidianMCPError):
    """The circuit breaker is open and rejected the request."""

    status = "circuit_open"
    counts_as_circuit_failure = False
