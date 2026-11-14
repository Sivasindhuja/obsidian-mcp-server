"""Shared test configuration.

Environment variables are set before any test module imports the server so the
server's import-time API-key check can run without a real vault.
"""

from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("OBSIDIAN_API_KEY", "test-api-key")
os.environ.setdefault("OBSIDIAN_HOST", "127.0.0.1")
os.environ.setdefault("OBSIDIAN_PROTOCOL", "https")
os.environ.setdefault("OBSIDIAN_PORT", "27124")
os.environ.setdefault("OBSIDIAN_VERIFY_SSL", "false")

from mcp_obsidian.APIWrapper import Obsidian  # noqa: E402


@pytest.fixture
async def api_client():
    client = Obsidian(
        api_key="test-api-key",
        protocol="https",
        host="127.0.0.1",
        port=27124,
        verify_ssl=False,
    )
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def server():
    import mcp_obsidian.server as server_module

    server_module.circuit.reset()
    yield server_module
    server_module.circuit.reset()


@pytest.fixture(scope="session", autouse=True)
def _close_server_client():
    yield
    import mcp_obsidian.server as server_module

    asyncio.run(server_module.api.aclose())
