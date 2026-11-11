from __future__ import annotations

from mcp_obsidian.errors import TransientAPIError, UnsupportedFileTypeError


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
    assert data["truncated"] is True
    assert data["summary"] == "a.md: first\n\na.md: second"
    assert "no AI summary" in data["note"]


async def test_search_and_summarize_handles_empty_results(server, monkeypatch):
    async def fake_search(_query, **_kwargs):
        return [], 0

    monkeypatch.setattr(server.api, "search_with_metadata", fake_search)

    result = await server.search_and_summarize("query")

    assert result["data"]["sources"] == []
    assert result["data"]["source_count"] == 0


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


async def test_check_connection_reports_health_and_circuit(server, monkeypatch):
    async def fake_list():
        return ["a.md", "b.md"]

    monkeypatch.setattr(server.api, "list_files_in_vault", fake_list)

    healthy = await server.check_connection()

    assert healthy["data"]["status"] == "healthy"
    assert healthy["data"]["file_count"] == 2
    assert healthy["data"]["circuit_state"] == "CLOSED"

    async def failing_list():
        raise RuntimeError("offline")

    monkeypatch.setattr(server.api, "list_files_in_vault", failing_list)

    unhealthy = await server.check_connection()

    assert unhealthy["success"] is False
    assert unhealthy["error"]["status"] == "unhealthy"
    assert unhealthy["error"]["circuit"]["failure_count"] == 1
