"""Typed errors shared by the Obsidian API wrapper and the MCP tools."""

from __future__ import annotations


class ObsidianMCPError(Exception):
    """Base class for expected, user-actionable failures.

    ``status`` is surfaced in the MCP tool response so clients can distinguish
    conflicts and validation failures from transient service failures.
    """

    status = "error"
    counts_as_circuit_failure = False
    http_status: int | None = None


class PathValidationError(ObsidianMCPError, ValueError):
    """A vault path is absolute, traverses upwards, or is otherwise invalid."""

    status = "invalid_path"


class InputValidationError(ObsidianMCPError, ValueError):
    """A tool argument is syntactically invalid."""

    status = "invalid_input"


class WriteConflictError(ObsidianMCPError):
    """A write could not be applied because it conflicts with current state."""

    status = "conflict"


class NoteAlreadyExistsError(WriteConflictError):
    """Safe-create was requested for a note that already exists."""

    status = "conflict"


class ConflictError(WriteConflictError):
    """The resource changed or is otherwise in a conflicting state (HTTP 409)."""

    status = "conflict"
    http_status = 409


class PreconditionFailedError(WriteConflictError):
    """A conditional write failed because the note changed or already exists."""

    status = "precondition_failed"
    http_status = 412


class UnsupportedFileTypeError(ObsidianMCPError):
    """A text operation was attempted against a binary or undecodable file."""

    status = "unsupported_file_type"


class NotFoundError(ObsidianMCPError):
    """The requested vault resource does not exist (HTTP 404)."""

    status = "not_found"
    http_status = 404


class AuthenticationError(ObsidianMCPError):
    """The API rejected the configured credentials (HTTP 401)."""

    status = "authentication_failed"
    http_status = 401


class PermissionDeniedError(ObsidianMCPError):
    """The credentials are valid but lack access to the requested resource (HTTP 403)."""

    status = "permission_denied"
    http_status = 403


class ProxyAuthenticationError(AuthenticationError):
    """A proxy requires authentication before the API can be reached (HTTP 407)."""

    status = "proxy_authentication_required"
    http_status = 407


class TransientAPIError(ObsidianMCPError):
    """Retries were exhausted while talking to the Obsidian API."""

    status = "service_unavailable"
    counts_as_circuit_failure = True


class RateLimitError(TransientAPIError):
    """The API kept returning HTTP 429 after retries."""

    status = "rate_limited"
    http_status = 429


class ServerError(TransientAPIError):
    """The API returned an HTTP 5xx response."""

    status = "server_error"
    counts_as_circuit_failure = True


class ServiceUnavailableError(ServerError):
    """The API kept returning HTTP 503 after retries."""

    status = "service_unavailable"
    http_status = 503


class CircuitOpenError(ObsidianMCPError):
    """The circuit breaker is open and rejected the request."""

    status = "circuit_open"
    counts_as_circuit_failure = False
