from __future__ import annotations

import json

import httpx
import pytest
import respx

from mcp_obsidian.APIWrapper import (
    Obsidian,
    _coerce_frontmatter_value,
    is_circuit_failure,
)
from mcp_obsidian.errors import (
    AuthenticationError,
    ConflictError,
    InputValidationError,
    NoteAlreadyExistsError,
    NotFoundError,
    PermissionDeniedError,
    PreconditionFailedError,
    ProxyAuthenticationError,
    RateLimitError,
    ServerError,
    ServiceUnavailableError,
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


async def test_invalid_numeric_environment_values_fall_back(monkeypatch):
    monkeypatch.setenv("OBSIDIAN_PORT", "not-a-port")
    monkeypatch.setenv("OBSIDIAN_TIMEOUT", "not-a-timeout")

    client = Obsidian(api_key="key")
    try:
        assert client.port == 27124
        assert client.client.timeout.read == 10.0
    finally:
        await client.aclose()


async def test_out_of_range_environment_values_are_clamped(monkeypatch):
    monkeypatch.setenv("OBSIDIAN_PORT", "99999")
    monkeypatch.setenv("OBSIDIAN_TIMEOUT", "0")

    client = Obsidian(api_key="key")
    try:
        assert client.port == 65535
        assert client.client.timeout.read == 0.1
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


@pytest.mark.parametrize(
    "content",
    ["NaN", "Infinity", "-Infinity", "1e999", '{"value": NaN}'],
)
def test_non_finite_frontmatter_json_falls_back_to_a_string(content):
    assert _coerce_frontmatter_value(content) == content


@respx.mock
async def test_patch_delete_omits_content(api_client):
    route = respx.patch(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(204)
    )

    await api_client.patch_content("note.md", "delete", "heading", "Old", "")

    payload = json.loads(route.calls[0].request.content)
    assert "content" not in payload
    assert "value" not in payload


@respx.mock
async def test_patch_root_heading_target_uses_empty_array(api_client):
    route = respx.patch(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(204)
    )

    await api_client.patch_content("note.md", "append", "heading", [], "x")
    await api_client.patch_content("note.md", "append", "heading", None, "y")

    first = json.loads(route.calls[0].request.content)
    second = json.loads(route.calls[1].request.content)
    assert first["target"] == []
    assert second["target"] == []


async def test_patch_rejects_empty_heading_string(api_client):
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
async def test_create_note_treats_409_as_conflict(api_client):
    respx.put(f"{BASE}/vault/existing.md").mock(return_value=httpx.Response(409))

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
async def test_binary_write_paths_are_rejected_without_request(api_client):
    put_route = respx.put(f"{BASE}/vault/image.png").mock(
        return_value=httpx.Response(204)
    )
    post_route = respx.post(f"{BASE}/vault/image.png").mock(
        return_value=httpx.Response(204)
    )
    patch_route = respx.patch(f"{BASE}/vault/image.png").mock(
        return_value=httpx.Response(204)
    )

    with pytest.raises(UnsupportedFileTypeError):
        await api_client.put_content("image.png", "text")
    with pytest.raises(UnsupportedFileTypeError):
        await api_client.append_content("image.png", "text")
    with pytest.raises(UnsupportedFileTypeError):
        await api_client.patch_content(
            "image.png", "append", "heading", "Log", "text"
        )
    with pytest.raises(UnsupportedFileTypeError):
        await api_client.create_note("image.png", "text")

    assert put_route.call_count == 0
    assert post_route.call_count == 0
    assert patch_route.call_count == 0


@respx.mock
async def test_binary_delete_remains_allowed(api_client):
    route = respx.delete(f"{BASE}/vault/image.png").mock(
        return_value=httpx.Response(204)
    )

    await api_client.delete_file("image.png")

    assert route.call_count == 1


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationError),
        (403, PermissionDeniedError),
        (407, ProxyAuthenticationError),
    ],
)
@respx.mock
async def test_auth_statuses_map_to_typed_errors(api_client, status, error_type):
    respx.get(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(status)
    )

    with pytest.raises(error_type):
        await api_client.get_file_contents("note.md")


@respx.mock
async def test_batch_reads_propagate_authentication_errors(api_client):
    respx.get(f"{BASE}/vault/a.md").mock(return_value=httpx.Response(401))

    with pytest.raises(AuthenticationError):
        await api_client.get_batch_file_contents(["a.md", "b.md"])


@respx.mock
async def test_invalid_utf8_is_rejected(api_client):
    respx.get(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(200, content=b"\xff\xfe")
    )

    with pytest.raises(UnsupportedFileTypeError):
        await api_client.get_file_contents("note.md")


@respx.mock
async def test_utf8_bom_is_preserved_for_round_trips(api_client):
    respx.get(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(200, content=b"\xef\xbb\xbfhello")
    )

    content = await api_client.get_file_contents("note.md")

    assert isinstance(content, str)
    assert content == "\ufeffhello"


@respx.mock
async def test_utf8_bom_round_trips_through_put_content(api_client):
    respx.get(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(200, content=b"\xef\xbb\xbfhello")
    )
    put_route = respx.put(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(204)
    )

    content = await api_client.get_file_contents("note.md")
    await api_client.put_content("note.md", content)

    assert put_route.calls[0].request.content == b"\xef\xbb\xbfhello"


@respx.mock
async def test_paths_are_percent_encoded(api_client):
    route = respx.get(url__regex=r".*/vault/folder/My%20Note%231\.md$").mock(
        return_value=httpx.Response(200, text="ok")
    )

    assert await api_client.get_file_contents("folder/My Note#1.md") == "ok"
    assert "%20" in str(route.calls[0].request.url)
    assert "%23" in str(route.calls[0].request.url)


@respx.mock
async def test_literal_percent_in_filename_is_encoded_once(api_client):
    route = respx.get(url__regex=r".*/vault/100%2520\.md$").mock(
        return_value=httpx.Response(200, text="ok")
    )

    assert await api_client.get_file_contents("100%20.md") == "ok"
    assert "%2520" in str(route.calls[0].request.url)


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
    assert output.metadata["files_truncated"] is True
    assert output.metadata["bytes_truncated"] is False


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


@pytest.mark.parametrize(
    ("status", "error_type", "expected_status", "circuit_failure"),
    [
        (401, AuthenticationError, "authentication_failed", False),
        (403, PermissionDeniedError, "permission_denied", False),
        (404, NotFoundError, "not_found", False),
        (409, ConflictError, "conflict", False),
        (412, PreconditionFailedError, "precondition_failed", False),
        (429, RateLimitError, "rate_limited", True),
        (500, ServerError, "server_error", True),
        (501, ServerError, "server_error", True),
        (502, ServerError, "server_error", True),
        (503, ServiceUnavailableError, "service_unavailable", True),
        (504, ServerError, "server_error", True),
        (505, ServerError, "server_error", True),
        (599, ServerError, "server_error", True),
    ],
)
def test_http_status_taxonomy_is_typed_and_classified(
    status, error_type, expected_status, circuit_failure
):
    request = httpx.Request("GET", f"{BASE}/vault/note.md")
    response = httpx.Response(status, request=request)

    with pytest.raises(error_type) as exc_info:
        Obsidian._raise_for_status(response, "note.md")

    assert exc_info.value.status == expected_status
    assert exc_info.value.http_status == status
    assert is_circuit_failure(exc_info.value) is circuit_failure


def test_unexpected_http_status_remains_untyped_and_neutral():
    request = httpx.Request("GET", f"{BASE}/vault/note.md")
    response = httpx.Response(400, request=request)

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        Obsidian._raise_for_status(response, "note.md")

    assert is_circuit_failure(exc_info.value) is False


@respx.mock
async def test_large_file_is_truncated_on_a_utf8_boundary(api_client):
    respx.get(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(200, text="é" * 50)
    )

    content = await api_client.get_file_contents("note.md", max_bytes=5)

    assert content == "éé"
    assert content.metadata == {
        "returned_bytes": 4,
        "returned_characters": 2,
        "max_bytes": 5,
        "bytes_truncated": True,
        "truncated": True,
    }


@respx.mock
async def test_file_limit_is_configurable_by_environment(api_client, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_MAX_FILE_BYTES", "5")
    respx.get(f"{BASE}/vault/note.md").mock(
        return_value=httpx.Response(200, text="abcdefgh")
    )

    content = await api_client.get_file_contents("note.md")

    assert content == "abcde"
    assert content.metadata["max_bytes"] == 5
    assert content.metadata["truncated"] is True


@respx.mock
async def test_batch_output_is_bounded_by_total_bytes(api_client):
    respx.get(f"{BASE}/vault/a.md").mock(
        return_value=httpx.Response(200, text="A" * 20)
    )
    respx.get(f"{BASE}/vault/b.md").mock(
        return_value=httpx.Response(200, text="B" * 20)
    )

    output = await api_client.get_batch_file_contents(
        ["a.md", "b.md"], max_bytes=40, max_file_bytes=100
    )

    assert len(output.encode("utf-8")) <= 40
    assert output.metadata["returned_bytes"] == len(output.encode("utf-8"))
    assert output.metadata["max_bytes"] == 40
    assert output.metadata["files_returned"] == 1
    assert output.metadata["truncated"] is True


@respx.mock
async def test_batch_reports_per_file_content_truncation(api_client):
    respx.get(f"{BASE}/vault/a.md").mock(
        return_value=httpx.Response(200, text="é" * 20)
    )

    output = await api_client.get_batch_file_contents(
        ["a.md"], max_bytes=1000, max_file_bytes=5
    )

    assert "éé" in output
    assert output.metadata["file_content_truncated"] is True
    assert output.metadata["truncated"] is True


@respx.mock
async def test_list_files_in_vault_walks_subdirectories(api_client):
    root = respx.get(f"{BASE}/vault/").mock(
        return_value=httpx.Response(
            200, json={"files": ["Folder/", "Root.md"]}
        )
    )
    folder = respx.get(f"{BASE}/vault/Folder/").mock(
        return_value=httpx.Response(
            200, json={"files": ["Nested.md", "Sub/"]}
        )
    )
    sub = respx.get(f"{BASE}/vault/Folder/Sub/").mock(
        return_value=httpx.Response(200, json={"files": ["Deep.md"]})
    )

    assert await api_client.list_files_in_vault() == [
        "Folder/",
        "Root.md",
        "Folder/Nested.md",
        "Folder/Sub/",
        "Folder/Sub/Deep.md",
    ]
    assert root.call_count == 1
    assert folder.call_count == 1
    assert sub.call_count == 1


@respx.mock
async def test_batch_expected_404_is_reported_inline(api_client):
    respx.get(f"{BASE}/vault/missing.md").mock(
        return_value=httpx.Response(404)
    )
    respx.get(f"{BASE}/vault/present.md").mock(
        return_value=httpx.Response(200, text="present")
    )

    output = await api_client.get_batch_file_contents(
        ["missing.md", "present.md"]
    )

    assert "Error:" in output
    assert "present" in output
    assert output.metadata["truncated"] is False
