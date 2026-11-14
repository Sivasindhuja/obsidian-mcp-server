from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from mcp_obsidian.errors import (
    RateLimitError,
    ServiceUnavailableError,
    TransientAPIError,
)

BASE = "https://127.0.0.1:27124"


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    monkeypatch.setattr("mcp_obsidian.APIWrapper.asyncio.sleep", AsyncMock())


@respx.mock
async def test_transient_503_is_retried(api_client):
    route = respx.get(f"{BASE}/vault/").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"files": ["a.md"]}),
        ]
    )

    assert await api_client.list_files_in_vault() == ["a.md"]
    assert route.call_count == 2


@respx.mock
async def test_network_error_is_retried(api_client):
    route = respx.get(f"{BASE}/vault/").mock(
        side_effect=[
            httpx.ConnectError("offline"),
            httpx.Response(200, json={"files": ["b.md"]}),
        ]
    )

    assert await api_client.list_files_in_vault() == ["b.md"]
    assert route.call_count == 2


@respx.mock
async def test_non_transient_error_is_not_retried(api_client):
    route = respx.get(f"{BASE}/vault/").mock(return_value=httpx.Response(400))

    with pytest.raises(httpx.HTTPStatusError):
        await api_client.list_files_in_vault()

    assert route.call_count == 1


@respx.mock
async def test_redirect_loop_is_not_retried(api_client):
    route = respx.get(f"{BASE}/vault/").mock(
        side_effect=httpx.TooManyRedirects("redirect loop")
    )

    with pytest.raises(httpx.TooManyRedirects):
        await api_client.list_files_in_vault()

    assert route.call_count == 1


@respx.mock
async def test_max_retries_is_respected(api_client):
    route = respx.get(f"{BASE}/vault/").mock(
        side_effect=[httpx.Response(503) for _ in range(4)]
    )

    with pytest.raises(TransientAPIError):
        await api_client.list_files_in_vault()

    # Initial attempt + three retries.
    assert route.call_count == 4


@respx.mock
async def test_success_stops_retrying_immediately(api_client):
    route = respx.get(f"{BASE}/vault/").mock(
        return_value=httpx.Response(200, json={"files": []})
    )

    assert await api_client.list_files_in_vault() == []
    assert route.call_count == 1


@respx.mock
async def test_mutating_writes_are_not_automatically_retried(api_client):
    route = respx.post(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(503)
    )

    with pytest.raises(ServiceUnavailableError):
        await api_client.append_content("note.md", "must not be duplicated")

    # Replaying an append after an ambiguous 503 could duplicate content.
    assert route.call_count == 1


@respx.mock
async def test_rate_limit_is_retried_then_reports_typed_status(api_client):
    route = respx.get(f"{BASE}/vault/").mock(
        return_value=httpx.Response(429)
    )

    with pytest.raises(RateLimitError) as exc_info:
        await api_client.list_files_in_vault()

    assert exc_info.value.status == "rate_limited"
    assert exc_info.value.http_status == 429
    assert route.call_count == 4
