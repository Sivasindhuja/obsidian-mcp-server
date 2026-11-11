from __future__ import annotations

import json

import httpx
import pytest
import respx

from mcp_obsidian.APIWrapper import Obsidian
from mcp_obsidian.errors import (
    InputValidationError,
    NoteAlreadyExistsError,
    PreconditionFailedError,
    TransientAPIError,
    UnsupportedFileTypeError,
)

BASE = "https://127.0.0.1:27124"


async def test_aclose_closes_the_http_client():
    client = Obsidian(
        api_key="key",
        protocol="https",
        host="127.0.0.1",
        port=27124,
        verify_ssl=False,
    )

    assert client.client.is_closed is False
    await client.aclose()
    assert client.client.is_closed is True


async def test_environment_is_resolved_when_client_is_created(monkeypatch):
    monkeypatch.setenv("OBSIDIAN_PROTOCOL", "http")
    monkeypatch.setenv("OBSIDIAN_HOST", "10.0.0.5")
    monkeypatch.setenv("OBSIDIAN_PORT", "9999")
    monkeypatch.setenv("OBSIDIAN_VERIFY_SSL", "true")

    client = Obsidian(api_key="key")
    try:
        assert client.protocol == "http"
        assert client.host == "10.0.0.5"
        assert client.port == 9999
        assert client.verify_ssl is True
        assert client.base_url == "http://10.0.0.5:9999"
    finally:
        await client.aclose()


@respx.mock
async def test_patch_uses_current_json_contract(api_client):
    route = respx.patch(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(204, headers={"ETag": '"v2"'})
    )

    etag = await api_client.patch_content(
        "note.md", "append", "heading", "My Section", "new text"
    )

    request = route.calls[0].request
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "targetType": "heading",
        "target": ["My Section"],
        "operation": "append",
        "content": "new text",
    }
    assert etag == '"v2"'


@respx.mock
async def test_patch_nested_heading_and_frontmatter_targets(api_client):
    heading_route = respx.patch(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(204)
    )
    frontmatter_route = respx.patch(f"{BASE}/vault/other.md").mock(
        return_value=httpx.Response(204)
    )
    block_route = respx.patch(f"{BASE}/vault/block.md").mock(
        return_value=httpx.Response(204)
    )

    await api_client.patch_content(
        "note.md", "append", "heading", ["Work", "Meetings"], "new text"
    )
    await api_client.patch_content(
        "other.md", "replace", "frontmatter", "status", "done"
    )
    await api_client.patch_content(
        "block.md", "append", "block", "^2d9b4a", "Hello"
    )

    heading_payload = json.loads(heading_route.calls[0].request.content)
    frontmatter_payload = json.loads(frontmatter_route.calls[0].request.content)
    block_payload = json.loads(block_route.calls[0].request.content)
    assert heading_payload["target"] == ["Work", "Meetings"]
    assert frontmatter_payload["target"] == "status"
    assert frontmatter_payload["value"] == "done"
    assert block_payload["target"] == "2d9b4a"


@respx.mock
async def test_frontmatter_replace_uses_value_field(api_client):
    route = respx.patch(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(204)
    )

    await api_client.patch_content(
        "note.md", "replace", "frontmatter", "published", "true"
    )

    payload = json.loads(route.calls[0].request.content)
    assert payload["value"] is True
    assert "content" not in payload


@respx.mock
async def test_frontmatter_append_uses_value_field(api_client):
    route = respx.patch(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(204)
    )

    await api_client.patch_content(
        "note.md", "append", "frontmatter", "tags", '["project/active"]'
    )

    payload = json.loads(route.calls[0].request.content)
    assert payload["value"] == ["project/active"]
    assert "content" not in payload


@respx.mock
async def test_patch_delete_omits_content(api_client):
    route = respx.patch(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(204)
    )

    await api_client.patch_content("note.md", "delete", "heading", "Old", "")

    payload = json.loads(route.calls[0].request.content)
    assert "content" not in payload
    assert "value" not in payload


async def test_patch_rejects_empty_heading_target(api_client):
    with pytest.raises(InputValidationError):
        await api_client.patch_content("note.md", "append", "heading", [], "x")

    with pytest.raises(InputValidationError):
        await api_client.patch_content("note.md", "append", "heading", "  ", "x")


@respx.mock
async def test_conditional_update_sends_if_match(api_client):
    route = respx.put(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(204, headers={"ETag": '"v2"'})
    )

    await api_client.put_content("note.md", "updated", if_match='"v1"')

    assert route.calls[0].request.headers["if-match"] == '"v1"'


@respx.mock
async def test_conditional_update_conflict_raises(api_client):
    respx.put(f"{BASE}/vault/note.md").mock(return_value=httpx.Response(412))

    with pytest.raises(PreconditionFailedError):
        await api_client.put_content("note.md", "updated", if_match='"old"')


@respx.mock
async def test_append_patch_and_delete_send_if_match(api_client):
    append_route = respx.post(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(204)
    )
    patch_route = respx.patch(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(204)
    )
    delete_route = respx.delete(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(204)
    )

    await api_client.append_content("note.md", "more", if_match='"v1"')
    await api_client.patch_content(
        "note.md", "append", "heading", "Log", "line", if_match='"v2"'
    )
    await api_client.delete_file("note.md", if_match='"v3"')

    assert append_route.calls[0].request.headers["if-match"] == '"v1"'
    assert patch_route.calls[0].request.headers["if-match"] == '"v2"'
    assert delete_route.calls[0].request.headers["if-match"] == '"v3"'


@respx.mock
async def test_create_note_uses_if_none_match(api_client):
    route = respx.put(f"{BASE}/vault/new.md").mock(
        return_value=httpx.Response(204)
    )

    await api_client.create_note("new.md", "content")

    assert route.calls[0].request.headers["if-none-match"] == "*"


@respx.mock
async def test_create_note_conflict_does_not_overwrite(api_client):
    respx.put(f"{BASE}/vault/existing.md").mock(return_value=httpx.Response(412))

    with pytest.raises(NoteAlreadyExistsError):
        await api_client.create_note("existing.md", "content")


@respx.mock
async def test_get_file_contents_returns_etag(api_client):
    respx.get(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(
            200, text="hello", headers={"ETag": '"v1"'}
        )
    )

    content, etag = await api_client.get_file_contents_with_etag("note.md")

    assert content == "hello"
    assert etag == '"v1"'


async def test_binary_file_is_rejected_without_request(api_client):
    with pytest.raises(UnsupportedFileTypeError):
        await api_client.get_file_contents("image.png")


@respx.mock
async def test_invalid_utf8_is_rejected(api_client):
    respx.get(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(200, content=b"\xff\xfe")
    )

    with pytest.raises(UnsupportedFileTypeError):
        await api_client.get_file_contents("note.md")


@respx.mock
async def test_paths_are_percent_encoded(api_client):
    route = respx.get(url__regex=r".*/vault/folder/My%20Note%231\.md$").mock(
        return_value=httpx.Response(200, text="ok")
    )

    assert await api_client.get_file_contents("folder/My Note#1.md") == "ok"
    assert "%20" in str(route.calls[0].request.url)
    assert "%23" in str(route.calls[0].request.url)


@respx.mock
async def test_search_sends_limits_and_reads_total_header(api_client):
    route = respx.post(f"{BASE}/search/simple/").mock(
        return_value=httpx.Response(
            200,
            json=[{"filename": "a.md", "matches": [{"context": "hit"}]}],
            headers={"Total-Results": "25"},
        )
    )

    results, total = await api_client.search_with_metadata(
        "query",
        context_length=1000,
        max_results=5,
        max_matches_per_file=2,
    )

    params = route.calls[0].request.url.params
    assert params["contextLength"] == "500"
    assert params["maxResults"] == "5"
    assert params["maxMatchesPerFile"] == "2"
    assert results[0]["filename"] == "a.md"
    assert total == 25


@respx.mock
async def test_batch_reads_are_bounded(api_client):
    respx.get(f"{BASE}/vault/a.md").mock(
        return_value=httpx.Response(200, text="A")
    )
    second = respx.get(f"{BASE}/vault/b.md").mock(
        return_value=httpx.Response(200, text="B")
    )

    output = await api_client.get_batch_file_contents(
        ["a.md", "b.md"], max_files=1
    )

    assert "A" in output
    assert "Batch truncated" in output
    assert second.call_count == 0


async def test_batch_reads_propagate_service_failures(api_client, monkeypatch):
    async def failing_read(_filepath):
        raise TransientAPIError("service down")

    monkeypatch.setattr(api_client, "get_file_contents", failing_read)

    with pytest.raises(TransientAPIError):
        await api_client.get_batch_file_contents(["a.md", "b.md"])


@respx.mock
async def test_search_json_uses_jsonlogic_content_type(api_client):
    route = respx.post(f"{BASE}/search/").mock(
        return_value=httpx.Response(200, json=[])
    )

    await api_client.search_json({"==": [{"var": "path"}, "a.md"]})

    assert (
        route.calls[0].request.headers["content-type"]
        == "application/vnd.olrapi.jsonlogic+json"
    )
