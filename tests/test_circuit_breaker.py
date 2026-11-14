from __future__ import annotations

import asyncio

import pytest

from mcp_obsidian.circuit_breaker import CircuitBreaker
from mcp_obsidian.errors import (
    AuthenticationError,
    ConflictError,
    NoteAlreadyExistsError,
    NotFoundError,
    PermissionDeniedError,
    PreconditionFailedError,
    RateLimitError,
    ServerError,
    ServiceUnavailableError,
)


def test_circuit_transitions_and_recovery():
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=3, recovery_time=10, clock=lambda: now[0]
    )

    assert breaker.allow_request() is True
    for _ in range(3):
        breaker.record_failure()

    assert breaker.state == "OPEN"
    assert breaker.allow_request() is False

    now[0] = 11.0
    assert breaker.allow_request() is True
    assert breaker.state == "HALF_OPEN"
    # Only one probe is allowed while half-open.
    assert breaker.allow_request() is False

    breaker.record_success()
    assert breaker.state == "CLOSED"
    assert breaker.failures == 0
    assert breaker.allow_request() is True


def test_cancelled_half_open_probe_releases_the_slot():
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_time=5, clock=lambda: now[0]
    )
    breaker.record_failure()
    now[0] = 6.0

    assert breaker.allow_request() is True
    assert breaker.allow_request() is False
    breaker.record_cancelled()
    assert breaker.allow_request() is True


def test_half_open_failure_reopens_circuit():
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_time=5, clock=lambda: now[0]
    )

    breaker.record_failure()
    assert breaker.state == "OPEN"

    now[0] = 6.0
    assert breaker.allow_request() is True
    breaker.record_failure()
    assert breaker.state == "OPEN"


def test_decorator_blocks_calls_when_open(server, monkeypatch):
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_time=10, clock=lambda: now[0]
    )
    breaker.record_failure()
    monkeypatch.setattr(server, "circuit", breaker)

    called = []

    async def should_not_run():
        called.append(True)
        return {"success": True}

    wrapped = server.circuit_protected(should_not_run)
    result = asyncio.run(wrapped())

    assert called == []
    assert result["success"] is False
    assert result["error"]["status"] == "circuit_open"


async def test_fastmcp_registers_the_protected_wrapper(server):
    tool = await server.mcp.get_tool("list_files_in_vault")
    assert getattr(tool.fn, "__wrapped__", None) is not None

    server.circuit.state = "OPEN"
    server.circuit.last_failure_time = server.circuit.clock()

    result = await server.mcp.call_tool("list_files_in_vault", {})
    assert "circuit_open" in result.content[0].text


def test_client_http_errors_do_not_open_the_circuit(server, monkeypatch):
    import httpx

    request = httpx.Request("GET", "https://127.0.0.1:27124/vault/")
    response = httpx.Response(400, request=request)

    async def bad_request():
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    monkeypatch.setattr(server.api, "list_files_in_vault", bad_request)

    for _ in range(5):
        result = asyncio.run(server.list_files_in_vault())
        assert result["success"] is False

    assert server.circuit.state == "CLOSED"
    assert server.circuit.failures == 0


def test_expected_conflicts_do_not_open_the_circuit(server, monkeypatch):
    from mcp_obsidian.errors import NoteAlreadyExistsError

    async def conflict(*_args, **_kwargs):
        raise NoteAlreadyExistsError("already there")

    monkeypatch.setattr(server.api, "create_note", conflict)

    result = asyncio.run(
        server.create_note(
            server.FilePathInput(filepath="note.md"), "content"
        )
    )

    assert result["success"] is False
    assert result["error"]["status"] == "conflict"
    assert server.circuit.state == "CLOSED"
    assert server.circuit.failures == 0


def test_neutral_outcome_preserves_failures_and_releases_half_open_probe():
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=2, recovery_time=5, clock=lambda: now[0]
    )

    breaker.record_failure()
    breaker.record_neutral()
    assert breaker.state == "CLOSED"
    assert breaker.failures == 1

    breaker.record_failure()
    assert breaker.state == "OPEN"

    now[0] = 6.0
    assert breaker.allow_request() is True
    assert breaker.state == "HALF_OPEN"

    breaker.record_neutral()
    assert breaker.state == "HALF_OPEN"
    assert breaker.failures == 2
    assert breaker.allow_request() is True


async def test_local_validation_is_neutral_in_closed_state(server, monkeypatch):
    breaker = CircuitBreaker(
        failure_threshold=3, recovery_time=10, clock=lambda: 0.0
    )
    breaker.failures = 2
    monkeypatch.setattr(server, "circuit", breaker)

    result = await server.get_file_contents(
        server.FilePathInput(filepath="../secret.md")
    )

    assert result["success"] is False
    assert result["error"]["status"] == "invalid_path"
    assert breaker.state == "CLOSED"
    assert breaker.failures == 2


async def test_local_validation_is_neutral_in_half_open_state(server, monkeypatch):
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_time=5, clock=lambda: now[0]
    )
    breaker.record_failure()
    now[0] = 6.0
    monkeypatch.setattr(server, "circuit", breaker)

    result = await server.get_file_contents(
        server.FilePathInput(filepath="../secret.md")
    )

    assert result["success"] is False
    assert result["error"]["status"] == "invalid_path"
    assert breaker.state == "HALF_OPEN"
    assert breaker.failures == 1
    assert breaker.allow_request() is True


async def test_denied_delete_is_neutral_in_half_open_state(server, monkeypatch):
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_time=5, clock=lambda: now[0]
    )
    breaker.record_failure()
    now[0] = 6.0
    monkeypatch.setattr(server, "circuit", breaker)

    result = await server.delete_file(
        server.FilePathInput(filepath="note.md"), confirm=False
    )

    assert result["success"] is False
    assert result["error"]["status"] == "invalid_input"
    assert breaker.state == "HALF_OPEN"
    assert breaker.failures == 1
    assert breaker.allow_request() is True


async def test_conflict_is_neutral_and_does_not_reset_failures(server, monkeypatch):
    breaker = CircuitBreaker(
        failure_threshold=3, recovery_time=10, clock=lambda: 0.0
    )
    breaker.failures = 2
    monkeypatch.setattr(server, "circuit", breaker)

    async def conflict(*_args, **_kwargs):
        raise NoteAlreadyExistsError("already there")

    monkeypatch.setattr(server.api, "create_note", conflict)

    result = await server.create_note(
        server.FilePathInput(filepath="note.md"), "content"
    )

    assert result["success"] is False
    assert result["error"]["status"] == "conflict"
    assert breaker.state == "CLOSED"
    assert breaker.failures == 2


async def test_successful_service_response_resets_failures(server, monkeypatch):
    breaker = CircuitBreaker(
        failure_threshold=3, recovery_time=10, clock=lambda: 0.0
    )
    breaker.failures = 2
    monkeypatch.setattr(server, "circuit", breaker)

    async def fake_list():
        return []

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)

    result = await server.list_files_in_vault()

    assert result["success"] is True
    assert breaker.state == "CLOSED"
    assert breaker.failures == 0


async def test_authentication_error_is_neutral_in_closed_state(server, monkeypatch):
    breaker = CircuitBreaker(
        failure_threshold=3, recovery_time=10, clock=lambda: 0.0
    )
    breaker.failures = 2
    monkeypatch.setattr(server, "circuit", breaker)

    async def unauthenticated():
        raise AuthenticationError("bad API key")

    monkeypatch.setattr(server.api, "list_files_in_vault", unauthenticated)

    result = await server.list_files_in_vault()

    assert result["success"] is False
    assert result["error"]["status"] == "authentication_failed"
    assert breaker.state == "CLOSED"
    assert breaker.failures == 2


async def test_authentication_error_is_neutral_in_half_open_state(server, monkeypatch):
    now = [0.0]
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_time=5, clock=lambda: now[0]
    )
    breaker.record_failure()
    now[0] = 6.0
    monkeypatch.setattr(server, "circuit", breaker)

    async def unauthenticated():
        raise AuthenticationError("bad API key")

    monkeypatch.setattr(server.api, "list_files_in_vault", unauthenticated)

    result = await server.list_files_in_vault()

    assert result["success"] is False
    assert result["error"]["status"] == "authentication_failed"
    assert breaker.state == "HALF_OPEN"
    assert breaker.failures == 1
    assert breaker.allow_request() is True


async def test_check_connection_auth_error_preserves_open_circuit(
    server, monkeypatch
):
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_time=100, clock=lambda: 0.0
    )
    breaker.record_failure()
    monkeypatch.setattr(server, "circuit", breaker)

    async def unauthenticated():
        raise AuthenticationError("bad API key")

    monkeypatch.setattr(server.api, "list_files_in_vault", unauthenticated)

    result = await server.check_connection()

    assert result["success"] is False
    assert result["error"]["error_status"] == "authentication_failed"
    assert breaker.state == "OPEN"
    assert breaker.failures == 1


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_failures"),
    [
        (AuthenticationError("bad key"), "authentication_failed", 0),
        (PermissionDeniedError("forbidden"), "permission_denied", 0),
        (NotFoundError("missing"), "not_found", 0),
        (ConflictError("conflict"), "conflict", 0),
        (PreconditionFailedError("changed"), "precondition_failed", 0),
        (RateLimitError("slow down"), "rate_limited", 1),
        (ServerError("server error"), "server_error", 1),
        (ServiceUnavailableError("unavailable"), "service_unavailable", 1),
    ],
)
async def test_http_error_categories_update_the_circuit_correctly(
    server, monkeypatch, error, expected_status, expected_failures
):
    async def failing_list():
        raise error

    monkeypatch.setattr(server.api, "list_files_in_vault", failing_list)

    result = await server.list_files_in_vault()

    assert result["success"] is False
    assert result["error"]["status"] == expected_status
    assert server.circuit.failures == expected_failures
    assert server.circuit.state == "CLOSED"
