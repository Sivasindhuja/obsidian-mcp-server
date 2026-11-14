from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from mcp_obsidian.errors import (
    AuthenticationError,
    InputValidationError,
    NotFoundError,
    PermissionDeniedError,
    TransientAPIError,
    UnsupportedFileTypeError,
)


async def test_tool_schemas_expose_enums_and_numeric_bounds(server):
    patch_tool = await server.mcp.get_tool("patch_content")
    patch_properties = patch_tool.parameters["properties"]
    assert patch_properties["operation"]["enum"] == [
        "append",
        "prepend",
        "replace",
        "delete",
    ]
    assert patch_properties["target_type"]["enum"] == [
        "heading",
        "block",
        "frontmatter",
    ]
    assert {"type": "null"} in patch_properties["target"]["anyOf"]

    search_tool = await server.mcp.get_tool("simple_search")
    search_properties = search_tool.parameters["properties"]
    assert search_properties["max_results"]["minimum"] == 1
    assert search_properties["max_results"]["maximum"] == server.MAX_SEARCH_RESULTS
    assert search_properties["context_length"]["maximum"] == 500

    batch_tool = await server.mcp.get_tool("batch_get_file_contents")
    batch_properties = batch_tool.parameters["properties"]
    assert batch_properties["max_files"]["minimum"] == 1
    assert batch_properties["max_files"]["maximum"] == server.MAX_BATCH_FILES


async def test_search_and_summarize_extracts_filenames_and_contexts(server, monkeypatch):
    async def fake_search(query, **_kwargs):
        return [
            {
                "filename": "a.md",
                "matches": [
                    {"context": "first"},
                    {"context": "second"},
                    {"context": "third"},
                ],
            },
            {"filename": "b.md", "matches": [{"context": "fourth"}]},
        ], 2

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.search_and_summarize(
        "query", max_sources=1, max_excerpts_per_source=2
    )

    data = result["data"]
    assert data["sources"] == [
        {"filename": "a.md", "excerpts": ["first", "second"]}
    ]
    assert data["returned_sources"] == 1
    assert data["source_count"] == 2
    assert data["files_truncated"] is True
    assert data["matches_truncated"] is False
    assert data["truncated"] is True
    assert data["summary"] == "a.md: first\n\na.md: second"
    assert "no AI summary" in data["note"]


async def test_search_and_summarize_ignores_malformed_results(server, monkeypatch):
    async def fake_search(_query, **_kwargs):
        return [
            None,
            "not-a-result",
            {
                "filename": 123,
                "matches": [
                    None,
                    "not-a-match",
                    {"context": 5},
                    {"context": {"nested": True}},
                    {"context": "valid"},
                ],
            },
        ], 1

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.search_and_summarize(
        "query", max_excerpts_per_source=10
    )
    data = result["data"]

    assert data["sources"] == [
        {"filename": "<unknown>", "excerpts": ["valid"]}
    ]
    assert data["summary"] == "<unknown>: valid"


async def test_search_and_summarize_handles_empty_results(server, monkeypatch):
    async def fake_search(_query, **_kwargs):
        return [], 0

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.search_and_summarize("query")

    assert result["data"]["sources"] == []
    assert result["data"]["source_count"] == 0
    assert result["data"]["total_matches"] == 0
    assert result["data"]["matches_returned"] == 0
    assert result["data"]["matches_truncated"] is False
    assert result["data"]["truncated"] is False


async def test_search_and_summarize_marks_unknown_total_at_limit_as_truncated(
    server, monkeypatch
):
    async def fake_search(_query, **_kwargs):
        return [{"filename": "a.md", "matches": [{"context": "first"}]}], None

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.search_and_summarize("query", max_sources=1)

    assert result["data"]["source_count"] == 1
    assert result["data"]["truncated"] is True


async def test_simple_search_clamps_limits(server, monkeypatch):
    captured = {}

    async def fake_search(_query, **kwargs):
        captured.update(kwargs)
        return [{"filename": "a.md"}], 100

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.simple_search(
        "query", max_results=1000, max_matches_per_file=1000
    )

    assert captured["max_results"] == server.MAX_SEARCH_RESULTS
    assert captured["max_matches_per_file"] == server.MAX_MATCHES_PER_FILE
    assert result["returned"] == 1
    assert result["truncated"] is True


async def test_simple_search_uses_known_total_for_truncation(server, monkeypatch):
    async def fake_search(_query, **_kwargs):
        return [{"filename": "a.md"}], 1

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.simple_search("query", max_results=1)

    assert result["returned"] == 1
    assert result["truncated"] is False


async def test_simple_search_reports_match_truncation(server, monkeypatch):
    async def fake_search(_query, **_kwargs):
        return [
            {
                "filename": "a.md",
                "matches": [{"context": "first"}],
                "totalMatches": 4,
            },
            {
                "filename": "b.md",
                "matches": [{"context": "second"}],
                "totalMatches": 1,
            },
        ], 2

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.simple_search("query", max_results=2)

    assert result["files_truncated"] is False
    assert result["total_matches"] == 5
    assert result["matches_returned"] == 2
    assert result["matches_truncated"] is True
    assert result["truncated"] is True


async def test_simple_search_tolerates_malformed_totals_and_matches(
    server, monkeypatch
):
    async def fake_search(_query, **_kwargs):
        return [
            {
                "filename": "a.md",
                "matches": [None, {"context": "first"}],
                "totalMatches": float("inf"),
            },
            "not-a-result",
        ], 1

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.simple_search("query")

    assert result["success"] is True
    assert result["total_matches"] == 2
    assert result["matches_returned"] == 2
    assert result["matches_truncated"] is False


async def test_simple_search_distinguishes_file_and_match_truncation(
    server, monkeypatch
):
    async def fake_search(_query, **_kwargs):
        return [
            {
                "filename": "a.md",
                "matches": [{"context": "first"}],
                "totalMatches": 1,
            }
        ], 5

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.simple_search("query", max_results=1)

    assert result["files_truncated"] is True
    assert result["matches_truncated"] is False
    assert result["truncated"] is True


async def test_simple_search_empty_results_are_not_truncated(server, monkeypatch):
    async def fake_search(_query, **_kwargs):
        return [], 0

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.simple_search("query")

    assert result["returned"] == 0
    assert result["files_truncated"] is False
    assert result["total_matches"] == 0
    assert result["matches_returned"] == 0
    assert result["matches_truncated"] is False
    assert result["truncated"] is False


async def test_search_and_summarize_reports_match_truncation(server, monkeypatch):
    async def fake_search(_query, **_kwargs):
        return [
            {
                "filename": "a.md",
                "matches": [
                    {"context": "first"},
                    {"context": "second"},
                ],
                "totalMatches": 5,
            }
        ], 1

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.search_and_summarize(
        "query", max_sources=5, max_excerpts_per_source=10
    )

    data = result["data"]
    assert data["files_truncated"] is False
    assert data["total_matches"] == 5
    assert data["matches_returned"] == 2
    assert data["matches_truncated"] is True
    assert data["truncated"] is True


async def test_search_and_summarize_preserves_summary_character_cap(
    server, monkeypatch
):
    async def fake_search(_query, **_kwargs):
        return [
            {
                "filename": "a.md",
                "matches": [{"context": "x" * 1000}],
                "totalMatches": 1,
            }
        ], 1

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.search_and_summarize(
        "query", max_sources=1, max_excerpts_per_source=1
    )

    data = result["data"]
    assert len(data["summary"]) == 500
    assert data["bytes_truncated"] is False
    assert data["summary_truncated"] is True
    assert data["truncated"] is True


async def test_complex_search_is_bounded(server, monkeypatch):
    async def fake_search(_query):
        return [{"path": "a.md"}, {"path": "b.md"}, {"path": "c.md"}]

    monkeypatch.setattr(server.api, "search_json", fake_search)

    result = await server.complex_search({}, max_results=2)

    assert result["data"] == [{"path": "a.md"}, {"path": "b.md"}]
    assert result["returned"] == 2
    assert result["truncated"] is True


async def test_batch_limit_is_clamped(server, monkeypatch):
    captured = {}

    async def fake_batch(filepaths, max_files):
        captured["filepaths"] = filepaths
        captured["max_files"] = max_files
        return "result"

    monkeypatch.setattr(server.api, "get_batch_file_contents", fake_batch)

    await server.batch_get_file_contents(["a.md"] * 500, max_files=1000)

    assert captured["max_files"] == server.MAX_BATCH_FILES


async def test_vault_stats_reports_truncation_and_read_failures(server, monkeypatch):
    async def fake_list():
        return ["A.md", "B.md", "C.md", "image.png"]

    async def fake_contents(path):
        if path == "B.md":
            raise UnsupportedFileTypeError("unreadable")
        return {"A.md": "[[B]]", "C.md": "#tag"}[path]

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)
    monkeypatch.setattr(server.api, "get_file_contents", fake_contents)

    result = await server.get_vault_stats(max_files=2)

    data = result["data"]
    assert data["total_files"] == 4
    assert data["total_notes"] == 3
    assert data["notes_scanned"] == 2
    assert data["notes_read"] == 1
    assert data["read_failures"] == 1
    assert data["truncated"] is True


async def test_vault_stats_partial_reads_do_not_orphan_unread_notes(
    server, monkeypatch
):
    async def fake_list():
        return ["A.md", "B.md"]

    async def fake_contents(path):
        if path == "B.md":
            raise UnsupportedFileTypeError("unreadable")
        return "no links"

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)
    monkeypatch.setattr(server.api, "get_file_contents", fake_contents)

    result = await server.get_vault_stats(max_files=2)

    data = result["data"]
    assert data["notes_read"] == 1
    assert data["read_failures"] == 1
    assert data["unread_files_count"] == 1
    assert data["unread_files_sample"] == ["B.md"]
    assert data["orphaned_files_count"] == 1
    assert data["orphaned_files_sample"] == ["A.md"]


async def test_vault_stats_propagates_service_failures(server, monkeypatch):
    async def fake_list():
        return ["A.md", "B.md"]

    async def failing_read(_path):
        raise TransientAPIError("service down")

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)
    monkeypatch.setattr(server.api, "get_file_contents", failing_read)

    result = await server.get_vault_stats(max_files=2)

    assert result["success"] is False
    assert result["error"]["status"] == "service_unavailable"


async def test_vault_stats_stops_after_service_failure(server, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_STATS_CONCURRENCY", "1")
    calls = []

    async def fake_list():
        return [f"{index}.md" for index in range(100)]

    async def failing_read(path):
        calls.append(path)
        raise TransientAPIError("service down")

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)
    monkeypatch.setattr(server.api, "get_file_contents", failing_read)

    result = await server.get_vault_stats(max_files=100)

    assert result["success"] is False
    assert result["error"]["status"] == "service_unavailable"
    assert calls == ["0.md"]


async def test_vault_stats_continues_after_expected_per_file_failure(
    server, monkeypatch
):
    monkeypatch.setenv("OBSIDIAN_STATS_CONCURRENCY", "1")
    calls = []

    async def fake_list():
        return ["A.md", "B.md", "C.md"]

    async def fake_read(path):
        calls.append(path)
        if path == "B.md":
            raise UnsupportedFileTypeError("unreadable")
        return {"A.md": "#tag", "C.md": "[[A]]"}[path]

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)
    monkeypatch.setattr(server.api, "get_file_contents", fake_read)

    result = await server.get_vault_stats(max_files=3)

    assert result["success"] is True
    assert result["data"]["notes_read"] == 2
    assert result["data"]["read_failures"] == 1
    assert result["data"]["unread_files_sample"] == ["B.md"]
    assert calls == ["A.md", "B.md", "C.md"]


async def test_vault_stats_cancels_pending_reads_after_service_failure(
    server, monkeypatch
):
    monkeypatch.setenv("OBSIDIAN_STATS_CONCURRENCY", "2")
    both_started = asyncio.Event()
    cancelled = asyncio.Event()
    calls = []

    async def fake_list():
        return ["A.md", "B.md", "C.md"]

    async def fake_read(path):
        calls.append(path)
        if path == "A.md":
            await both_started.wait()
            raise TransientAPIError("service down")
        both_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)
    monkeypatch.setattr(server.api, "get_file_contents", fake_read)

    result = await asyncio.wait_for(server.get_vault_stats(max_files=3), timeout=2)

    assert result["success"] is False
    assert result["error"]["status"] == "service_unavailable"
    assert set(calls) == {"A.md", "B.md"}
    assert cancelled.is_set()


async def test_vault_stats_cancellation_propagates(server, monkeypatch):
    started = asyncio.Event()

    async def fake_list():
        return ["A.md"]

    async def blocking_read(_path):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)
    monkeypatch.setattr(server.api, "get_file_contents", blocking_read)

    task = asyncio.create_task(server.get_vault_stats(max_files=1))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_vault_stats_propagates_authentication_failures(server, monkeypatch):
    async def fake_list():
        return ["A.md", "B.md"]

    async def unauthenticated(_path):
        raise AuthenticationError("bad API key")

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)
    monkeypatch.setattr(server.api, "get_file_contents", unauthenticated)

    result = await server.get_vault_stats(max_files=2)

    assert result["success"] is False
    assert result["error"]["status"] == "authentication_failed"
    assert server.circuit.state == "CLOSED"
    assert server.circuit.failures == 0


async def test_batch_get_file_contents_propagates_permission_failures(
    server, monkeypatch
):
    async def forbidden(_filepaths, max_files):
        raise PermissionDeniedError("no access")

    monkeypatch.setattr(server.api, "get_batch_file_contents", forbidden)

    result = await server.batch_get_file_contents(["a.md"], max_files=1)

    assert result["success"] is False
    assert result["error"]["status"] == "permission_denied"
    assert server.circuit.state == "CLOSED"
    assert server.circuit.failures == 0


async def test_invalid_stats_concurrency_env_does_not_fail(server, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_STATS_CONCURRENCY", "not-a-number")

    async def fake_list():
        return []

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)

    result = await server.get_vault_stats(max_files=1)

    assert result["success"] is True


async def test_list_note_titles_only_returns_markdown(server, monkeypatch):
    async def fake_list():
        return ["A.md", "folder/B.md", "image.png"]

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)

    result = await server.list_note_titles()

    assert result["data"] == ["A.md", "B.md"]


async def test_invalid_path_is_a_structured_tool_error(server):
    result = await server.mcp.call_tool(
        "get_file_contents", {"input": {"filepath": "../secret.md"}}
    )

    assert result.is_error is False
    assert "invalid_path" in result.content[0].text
    assert server.circuit.state == "CLOSED"


async def test_invalid_batch_path_is_rejected_before_reading(server, monkeypatch):
    called = []

    async def fake_batch(_filepaths, max_files):
        called.append(max_files)
        return "should not run"

    monkeypatch.setattr(server.api, "get_batch_file_contents", fake_batch)

    result = await server.batch_get_file_contents(
        ["a.md", "../secret.md"], max_files=5
    )

    assert result["success"] is False
    assert result["error"]["status"] == "invalid_path"
    assert called == []


async def test_batch_validates_only_selected_paths(server, monkeypatch):
    captured = {}

    async def fake_batch(filepaths, max_files):
        captured["filepaths"] = filepaths
        captured["max_files"] = max_files
        return "result"

    monkeypatch.setattr(server.api, "get_batch_file_contents", fake_batch)

    result = await server.batch_get_file_contents(
        ["a.md", "../not-selected.md", "also-not-selected.md"], max_files=1
    )

    assert result["success"] is True
    assert "Only the first 1 of 3 requested files" in result["data"]
    assert captured == {"filepaths": ["a.md"], "max_files": 1}


async def test_get_file_contents_can_include_etag(server, monkeypatch):
    async def fake_with_etag(path):
        return "body", '"v1"'

    monkeypatch.setattr(server.api, "get_file_contents_with_etag", fake_with_etag)

    result = await server.get_file_contents(
        server.FilePathInput(filepath="note.md"), include_etag=True
    )

    assert result["data"] == {
        "filepath": "note.md",
        "content": "body",
        "etag": '"v1"',
    }


async def test_patch_tool_accepts_heading_list(server, monkeypatch):
    captured = {}

    async def fake_patch(path, **kwargs):
        captured["path"] = path
        captured.update(kwargs)
        return '"v2"'

    monkeypatch.setattr(server.api, "patch_content", fake_patch)

    result = await server.patch_content(
        "folder/note.md",
        "append",
        "heading",
        ["Work", "Meetings"],
        "new text",
    )

    assert result["success"] is True
    assert captured["path"] == "folder/note.md"
    assert captured["target"] == ["Work", "Meetings"]
    assert captured["operation"] == "append"


async def test_patch_tool_accepts_root_heading_target(server, monkeypatch):
    captured = []

    async def fake_patch(_path, **kwargs):
        captured.append(kwargs["target"])
        return '"v2"'

    monkeypatch.setattr(server.api, "patch_content", fake_patch)

    empty = await server.patch_content("note.md", "append", "heading", [], "x")
    null = await server.patch_content("note.md", "prepend", "heading", None, "y")

    assert empty["success"] is True
    assert null["success"] is True
    assert captured == [[], None]


async def test_delete_requires_confirmation(server, monkeypatch):
    called = []

    async def fake_delete(path, if_match=None):
        called.append((path, if_match))

    monkeypatch.setattr(server.api, "delete_file", fake_delete)

    denied = await server.delete_file(
        server.FilePathInput(filepath="note.md"), confirm=False
    )
    allowed = await server.delete_file(
        server.FilePathInput(filepath="note.md"), confirm=True
    )

    assert denied["success"] is False
    assert denied["error"]["status"] == "invalid_input"
    assert called == [("note.md", None)]
    assert allowed["success"] is True


async def test_check_connection_reports_health_and_circuit(
    server, monkeypatch, caplog
):
    async def fake_list():
        return ["a.md", "folder/", "b.md"]

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)

    healthy = await server.check_connection()

    assert healthy["data"]["status"] == "healthy"
    assert healthy["data"]["file_count"] == 2
    assert healthy["data"]["circuit_state"] == "CLOSED"

    async def failing_list():
        raise RuntimeError("offline")

    monkeypatch.setattr(server.api, "list_files_in_vault", failing_list)
    caplog.set_level(logging.WARNING, logger="mcp_obsidian.server")

    unhealthy = await server.check_connection()

    assert unhealthy["success"] is False
    assert unhealthy["error"]["status"] == "unhealthy"
    assert unhealthy["error"]["circuit"]["failure_count"] == 1
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "health_check_failed"
    )
    assert record.exc_info is not None


async def test_unexpected_tool_failure_logs_a_traceback(
    server, monkeypatch, caplog
):
    async def failing_list():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(server.api, "list_files_in_vault", failing_list)
    caplog.set_level(logging.WARNING, logger="mcp_obsidian.server")

    result = await server.list_files_in_vault()

    assert result["success"] is False
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "tool_failed"
    )
    assert record.exc_info is not None


async def test_expected_tool_failure_does_not_log_a_traceback(
    server, monkeypatch, caplog
):
    async def invalid_list():
        raise InputValidationError("invalid")

    monkeypatch.setattr(server.api, "list_files_in_vault", invalid_list)
    caplog.set_level(logging.WARNING, logger="mcp_obsidian.server")

    result = await server.list_files_in_vault()

    assert result["success"] is False
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "tool_failed"
    )
    assert record.exc_info is None


async def test_check_connection_auth_errors_do_not_increment_failures(
    server, monkeypatch
):
    async def unauthenticated():
        raise AuthenticationError("bad API key")

    monkeypatch.setattr(server.api, "list_files_in_vault", unauthenticated)

    for _ in range(5):
        result = await server.check_connection()
        assert result["success"] is False
        assert result["error"]["error_status"] == "authentication_failed"

    assert server.circuit.state == "CLOSED"
    assert server.circuit.failures == 0


async def test_check_connection_client_errors_do_not_increment_failures(
    server, monkeypatch
):
    request = httpx.Request("GET", "https://127.0.0.1:27124/vault/")
    response = httpx.Response(400, request=request)

    async def bad_request():
        raise httpx.HTTPStatusError(
            "bad request", request=request, response=response
        )

    monkeypatch.setattr(server.api, "list_files_in_vault", bad_request)

    result = await server.check_connection()

    assert result["success"] is False
    assert result["error"]["error_status"] == "failed"
    assert server.circuit.state == "CLOSED"
    assert server.circuit.failures == 0


async def test_get_file_contents_bounds_output_and_reports_metadata(
    server, monkeypatch
):
    async def fake_contents(_path, **_kwargs):
        return "é" * 100

    monkeypatch.setattr(server.api, "get_file_contents", fake_contents)

    result = await server.get_file_contents(
        server.FilePathInput(filepath="note.md"), max_bytes=7
    )

    assert result["success"] is True
    assert result["data"] == "é" * 3
    assert result["returned_bytes"] == 6
    assert result["returned_characters"] == 3
    assert result["max_bytes"] == 7
    assert result["bytes_truncated"] is True
    assert result["truncated"] is True


async def test_get_file_contents_with_etag_keeps_shape_and_metadata(
    server, monkeypatch
):
    async def fake_with_etag(_path, **_kwargs):
        return "é" * 20, '"v1"'

    monkeypatch.setattr(server.api, "get_file_contents_with_etag", fake_with_etag)

    result = await server.get_file_contents(
        server.FilePathInput(filepath="note.md"),
        include_etag=True,
        max_bytes=5,
    )

    assert result["data"] == {
        "filepath": "note.md",
        "content": "é" * 2,
        "etag": '"v1"',
    }
    assert result["returned_bytes"] == 4
    assert result["max_bytes"] == 5
    assert result["truncated"] is True


async def test_get_file_contents_limit_can_come_from_environment(
    server, monkeypatch
):
    monkeypatch.setenv("OBSIDIAN_MAX_FILE_BYTES", "6")

    async def fake_contents(_path):
        return "abcdefgh"

    monkeypatch.setattr(server.api, "get_file_contents", fake_contents)

    result = await server.get_file_contents(
        server.FilePathInput(filepath="note.md")
    )

    assert result["data"] == "abcdef"
    assert result["max_bytes"] == 6
    assert result["truncated"] is True


async def test_batch_output_is_bounded_and_reports_metadata(server, monkeypatch):
    async def fake_batch(_filepaths, max_files, **_kwargs):
        return "x" * 100

    monkeypatch.setattr(server.api, "get_batch_file_contents", fake_batch)

    result = await server.batch_get_file_contents(["a.md"], max_bytes=20)

    assert len(result["data"].encode("utf-8")) <= 20
    assert result["returned_bytes"] == 20
    assert result["returned_characters"] == 20
    assert result["max_bytes"] == 20
    assert result["files_returned"] == 1
    assert result["truncated"] is True


async def test_batch_notice_stays_inside_total_byte_limit(server, monkeypatch):
    async def fake_batch(_filepaths, max_files, **_kwargs):
        return "x" * 100

    monkeypatch.setattr(server.api, "get_batch_file_contents", fake_batch)

    result = await server.batch_get_file_contents(
        ["a.md", "b.md"], max_files=1, max_bytes=25
    )

    assert len(result["data"].encode("utf-8")) <= 25
    assert result["files_truncated"] is True
    assert result["truncated"] is True


async def test_batch_limit_can_come_from_environment(server, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_MAX_BATCH_BYTES", "15")

    async def fake_batch(_filepaths, max_files):
        return "x" * 100

    monkeypatch.setattr(server.api, "get_batch_file_contents", fake_batch)

    result = await server.batch_get_file_contents(["a.md"])

    assert len(result["data"].encode("utf-8")) <= 15
    assert result["max_bytes"] == 15
    assert result["truncated"] is True


async def test_simple_search_is_bounded_by_total_bytes(server, monkeypatch):
    async def fake_search(_query, **_kwargs):
        return [
            {
                "filename": "a.md",
                "matches": [{"context": "é" * 200}],
                "totalMatches": 1,
            }
        ], 1

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.simple_search("query", max_bytes=80)
    serialized = json.dumps(
        result["data"], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")

    assert len(serialized) <= 80
    assert result["returned_bytes"] == len(serialized)
    assert result["returned_characters"] == len(serialized.decode("utf-8"))
    assert result["max_bytes"] == 80
    assert result["bytes_truncated"] is True
    assert result["truncated"] is True


async def test_complex_search_is_bounded_by_total_bytes(server, monkeypatch):
    async def fake_search(_query):
        return [{"path": "a.md", "content": "é" * 200}]

    monkeypatch.setattr(server.api, "search_json", fake_search)

    result = await server.complex_search({}, max_bytes=60)
    serialized = json.dumps(
        result["data"], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")

    assert len(serialized) <= 60
    assert result["returned_bytes"] == len(serialized)
    assert result["max_bytes"] == 60
    assert result["bytes_truncated"] is True
    assert result["truncated"] is True


async def test_search_and_summarize_is_bounded_by_total_bytes(server, monkeypatch):
    async def fake_search(_query, **_kwargs):
        return [
            {
                "filename": "a.md",
                "matches": [{"context": "é" * 200}],
                "totalMatches": 1,
            }
        ], 1

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.search_and_summarize("query", max_bytes=150)
    data = result["data"]
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )

    assert len(serialized) <= 150
    assert data["returned_bytes"] == len(serialized)
    assert data["returned_characters"] == len(serialized.decode("utf-8"))
    assert data["max_bytes"] == 150
    assert data["bytes_truncated"] is True
    assert data["summary_truncated"] is True
    assert data["truncated"] is True
    assert result["summary_truncated"] is True


async def test_summary_limit_can_come_from_environment(server, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_MAX_SUMMARY_BYTES", "400")

    async def fake_search(_query, **_kwargs):
        return [{"filename": "a.md", "matches": [{"context": "é" * 200}]}], 1

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.search_and_summarize("query")
    data = result["data"]
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )

    assert len(serialized) <= 400
    assert data["max_bytes"] == 400
    assert data["truncated"] is True


async def test_search_limit_can_come_from_environment(server, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_MAX_SEARCH_BYTES", "70")

    async def fake_search(_query, **_kwargs):
        return [{"filename": "a.md", "matches": [{"context": "é" * 100}]}], 1

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.simple_search("query")
    serialized = json.dumps(
        result["data"], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")

    assert len(serialized) <= 70
    assert result["max_bytes"] == 70
    assert result["truncated"] is True


async def test_vault_stats_propagates_permission_denied(server, monkeypatch):
    async def fake_list():
        return ["A.md", "B.md"]

    async def forbidden(_path):
        raise PermissionDeniedError("no access")

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)
    monkeypatch.setattr(server.api, "get_file_contents", forbidden)

    result = await server.get_vault_stats(max_files=2)

    assert result["success"] is False
    assert result["error"]["status"] == "permission_denied"
    assert server.circuit.state == "CLOSED"
    assert server.circuit.failures == 0


async def test_batch_propagates_authentication_failures(server, monkeypatch):
    async def unauthenticated(_filepaths, max_files):
        raise AuthenticationError("bad API key")

    monkeypatch.setattr(server.api, "get_batch_file_contents", unauthenticated)

    result = await server.batch_get_file_contents(["a.md"], max_files=1)

    assert result["success"] is False
    assert result["error"]["status"] == "authentication_failed"
    assert server.circuit.state == "CLOSED"
    assert server.circuit.failures == 0


async def test_vault_stats_counts_not_found_as_read_failure(server, monkeypatch):
    async def fake_list():
        return ["A.md", "B.md"]

    async def missing(path):
        if path == "B.md":
            raise NotFoundError("missing")
        return "[[A]]"

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)
    monkeypatch.setattr(server.api, "get_file_contents", missing)

    result = await server.get_vault_stats(max_files=2)

    assert result["success"] is True
    assert result["data"]["read_failures"] == 1
    assert result["data"]["unread_files_sample"] == ["B.md"]
    assert server.circuit.failures == 0
