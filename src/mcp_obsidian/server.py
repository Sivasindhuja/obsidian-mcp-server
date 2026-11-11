"""MCP tools for the Obsidian Local REST API."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from functools import wraps
from typing import Any

import httpx
from dotenv import load_dotenv

# Must run before importing APIWrapper: APIWrapper resolves environment values
# when Obsidian is instantiated, and server configuration is read below.
load_dotenv()

from fastmcp import FastMCP  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from mcp_obsidian.APIWrapper import Obsidian, is_service_failure  # noqa: E402
from mcp_obsidian.circuit_breaker import CircuitBreaker  # noqa: E402
from mcp_obsidian.validation import normalize_vault_path  # noqa: E402
from mcp_obsidian.vault_stats import (  # noqa: E402
    compute_vault_stats,
    is_markdown,
)

logger = logging.getLogger(__name__)

# ========================
# CONFIGURATION
# ========================

api_key = os.getenv("OBSIDIAN_API_KEY")
if not api_key:
    raise ValueError(
        "OBSIDIAN_API_KEY environment variable required; see .env.example"
    )

host = os.getenv("OBSIDIAN_HOST", "127.0.0.1")
protocol = os.getenv("OBSIDIAN_PROTOCOL", "https")
port = int(os.getenv("OBSIDIAN_PORT", "27124"))

api = Obsidian(api_key=api_key, protocol=protocol, host=host, port=port)

circuit = CircuitBreaker(
    failure_threshold=int(os.getenv("OBSIDIAN_CIRCUIT_FAILURE_THRESHOLD", "3")),
    recovery_time=float(os.getenv("OBSIDIAN_CIRCUIT_RECOVERY_TIME", "10")),
)

DEFAULT_SEARCH_RESULTS = 20
MAX_SEARCH_RESULTS = 100
DEFAULT_MATCHES_PER_FILE = 10
MAX_MATCHES_PER_FILE = 50
DEFAULT_BATCH_FILES = 20
MAX_BATCH_FILES = 100
DEFAULT_STATS_FILES = 500
MAX_STATS_FILES = 5000
DEFAULT_SUMMARY_SOURCES = 5
MAX_SUMMARY_SOURCES = 20
DEFAULT_EXCERPTS_PER_SOURCE = 3
MAX_EXCERPTS_PER_SOURCE = 20


@asynccontextmanager
async def lifespan(_server: FastMCP):
    """Close the shared HTTP client when the MCP server shuts down."""

    try:
        yield
    finally:
        await api.aclose()


mcp = FastMCP("obsidian-server", lifespan=lifespan)


# ========================
# RESPONSE HELPERS
# ========================


def success(data: Any, **extra: Any) -> dict:
    payload = {"success": True, "data": data}
    payload.update(extra)
    return payload


def error(message: Any) -> dict:
    return {"success": False, "error": message}


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(int(value), maximum))


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer environment setting without failing a tool call."""

    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return _clamp(value, minimum, maximum)


# ========================
# CIRCUIT BREAKER DECORATOR
# ========================


def circuit_protected(func):
    """Protect a tool call with the shared circuit breaker.

    The decorator must be applied *below* ``@mcp.tool()`` so FastMCP registers
    the wrapped function rather than the undecorated original.
    """

    @wraps(func)
    async def wrapper(*args, **kwargs):
        if not circuit.allow_request():
            snapshot = circuit.snapshot()
            logger.warning(
                "circuit_open",
                extra={
                    "event": "circuit_open",
                    "tool": func.__name__,
                    "circuit_state": snapshot["state"],
                    "failure_count": snapshot["failure_count"],
                },
            )
            return error(
                {
                    "status": "circuit_open",
                    "message": "Service unavailable. Skipping API call.",
                    "retry_after_sec": snapshot["retry_after_sec"],
                    "circuit_state": snapshot["state"],
                }
            )

        started = time.monotonic()
        try:
            result = await func(*args, **kwargs)
        except asyncio.CancelledError:
            # Do not leave a half-open probe slot permanently occupied when
            # the client disconnects or the server shuts down mid-request.
            circuit.record_cancelled()
            raise
        except Exception as exc:  # map expected errors into MCP responses
            counts_as_failure = getattr(exc, "counts_as_circuit_failure", True)
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and 400 <= exc.response.status_code < 500
                and exc.response.status_code != 429
            ):
                # A bad request is a client error, not evidence that the
                # service is down. 429 is retried and surfaces as
                # TransientAPIError instead.
                counts_as_failure = False
            if counts_as_failure:
                circuit.record_failure()
            else:
                # Conflicts and validation failures mean the service is healthy.
                circuit.record_success()

            snapshot = circuit.snapshot()
            logger.warning(
                "tool_failed",
                extra={
                    "event": "tool_failed",
                    "tool": func.__name__,
                    "status": getattr(exc, "status", "failed"),
                    "circuit_state": snapshot["state"],
                    "failure_count": snapshot["failure_count"],
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )
            return error(
                {
                    "status": getattr(exc, "status", "failed"),
                    "reason": str(exc),
                    "circuit_state": snapshot["state"],
                    "failure_count": snapshot["failure_count"],
                }
            )

        circuit.record_success()
        logger.info(
            "tool_completed",
            extra={
                "event": "tool_completed",
                "tool": func.__name__,
                "circuit_state": circuit.state,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            },
        )
        return result

    return wrapper


# ========================
# INPUT VALIDATION
# ========================


class FilePathInput(BaseModel):
    filepath: str = Field(description="Vault-relative path, for example 'folder/note.md'")


class DirPathInput(BaseModel):
    dirpath: str = Field(description="Vault-relative directory path, for example 'folder'")


# ========================
# FILE OPERATIONS
# ========================


@mcp.tool()
@circuit_protected
async def list_files_in_vault() -> dict:
    """List every file and folder in the vault."""

    return success(await api.list_files_in_vault())


@mcp.tool()
@circuit_protected
async def list_files_in_dir(input: DirPathInput) -> dict:
    """List files and folders inside one vault directory."""

    dirpath = normalize_vault_path(input.dirpath)
    return success(await api.list_files_in_dir(dirpath))


@mcp.tool()
@circuit_protected
async def list_note_titles() -> dict:
    """Return Markdown note basenames. Duplicate basenames in different folders are possible."""

    files = await api.list_files_in_vault()
    titles = [path.rsplit("/", 1)[-1] for path in files if is_markdown(path)]
    return success(titles)


@mcp.tool()
@circuit_protected
async def get_file_contents(input: FilePathInput, include_etag: bool = False) -> dict:
    """Read a UTF-8 text note. Binary attachments are rejected, not decoded as text.

    Set ``include_etag`` to receive the ETag/version needed for conditional writes.
    """

    filepath = normalize_vault_path(input.filepath)
    if include_etag:
        content, etag = await api.get_file_contents_with_etag(filepath)
        return success({"filepath": filepath, "content": content, "etag": etag})
    return success(await api.get_file_contents(filepath))


@mcp.tool()
@circuit_protected
async def batch_get_file_contents(
    filepaths: list[str], max_files: int = DEFAULT_BATCH_FILES
) -> dict:
    """Read multiple text notes in one call. Output is bounded by ``max_files``."""

    limit = _clamp(max_files, 1, MAX_BATCH_FILES)
    normalized = [normalize_vault_path(path) for path in filepaths]
    return success(await api.get_batch_file_contents(normalized, max_files=limit))


# ========================
# SEARCH
# ========================


@mcp.tool()
@circuit_protected
async def simple_search(
    query: str,
    context_length: int = 100,
    max_results: int = DEFAULT_SEARCH_RESULTS,
    max_matches_per_file: int = DEFAULT_MATCHES_PER_FILE,
) -> dict:
    """Full-text search using Obsidian's built-in search.

    Results include filenames and match contexts. Limits are clamped to avoid
    returning an unbounded amount of text to the model.
    """

    results_limit = _clamp(max_results, 1, MAX_SEARCH_RESULTS)
    matches_limit = _clamp(max_matches_per_file, 1, MAX_MATCHES_PER_FILE)
    results, total = await api.search_with_metadata(
        query,
        context_length=context_length,
        max_results=results_limit,
        max_matches_per_file=matches_limit,
    )
    returned = len(results) if isinstance(results, list) else 0
    truncated = total > returned if total is not None else returned >= results_limit
    return success(
        results,
        returned=returned,
        max_results=results_limit,
        total_results=total,
        truncated=truncated,
    )


@mcp.tool()
@circuit_protected
async def complex_search(query: dict, max_results: int = 50) -> dict:
    """Structured JSONLogic search against note metadata. Results are bounded."""

    limit = _clamp(max_results, 1, MAX_SEARCH_RESULTS)
    results = await api.search_json(query)
    if isinstance(results, list):
        truncated = len(results) > limit
        returned_results = results[:limit]
        return success(
            returned_results,
            returned=len(returned_results),
            max_results=limit,
            truncated=truncated,
        )
    return success(
        results, returned=None, max_results=limit, truncated=False
    )


@mcp.tool()
@circuit_protected
async def search_and_summarize(
    query: str,
    max_sources: int = DEFAULT_SUMMARY_SOURCES,
    max_excerpts_per_source: int = DEFAULT_EXCERPTS_PER_SOURCE,
    context_length: int = 100,
) -> dict:
    """Return relevant search excerpts grouped by source note.

    This is not an AI-generated summary: no LLM is called. The tool extracts
    filenames and matching contexts from Obsidian's simple-search response.
    """

    sources_limit = _clamp(max_sources, 1, MAX_SUMMARY_SOURCES)
    excerpts_limit = _clamp(
        max_excerpts_per_source, 1, MAX_EXCERPTS_PER_SOURCE
    )

    results, total = await api.search_with_metadata(
        query,
        context_length=context_length,
        max_results=sources_limit,
        max_matches_per_file=excerpts_limit,
    )

    sources: list[dict[str, Any]] = []
    for item in results[:sources_limit] if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename") or item.get("path") or "<unknown>"
        matches = item.get("matches") or []
        excerpts = [
            match.get("context", "")
            for match in matches[:excerpts_limit]
            if isinstance(match, dict) and match.get("context")
        ]
        sources.append({"filename": filename, "excerpts": excerpts})

    returned = len(results) if isinstance(results, list) else 0
    source_count = total if total is not None else returned
    truncated = source_count > len(sources) or (
        total is None and returned >= sources_limit
    )
    summary = "\n\n".join(
        f"{source['filename']}: {excerpt}"
        for source in sources
        for excerpt in source["excerpts"]
    )[:500]
    return success(
        {
            "summary": summary,
            "query": query,
            "sources": sources,
            "source_count": source_count,
            "returned_sources": len(sources),
            "truncated": truncated,
            "note": "Excerpts from Obsidian search; no AI summary was generated.",
        }
    )


# ========================
# WRITE / MODIFY
# ========================


@mcp.tool()
@circuit_protected
async def create_note(input: FilePathInput, content: str) -> dict:
    """Create a new note. Fails with a conflict if the note already exists.

    Use ``put_content`` when you intentionally want to replace an existing note.
    """

    filepath = normalize_vault_path(input.filepath)
    etag = await api.create_note(filepath, content)
    return success(f"Created note {filepath}", etag=etag)


@mcp.tool()
@circuit_protected
async def append_content(
    input: FilePathInput, content: str, if_match: str | None = None
) -> dict:
    """Append content to a note. Pass ``if_match`` for optimistic concurrency."""

    filepath = normalize_vault_path(input.filepath)
    etag = await api.append_content(filepath, content, if_match=if_match)
    return success(f"Appended content to {filepath}", etag=etag)


@mcp.tool()
@circuit_protected
async def patch_content(
    filepath: str,
    operation: str,
    target_type: str,
    target: str | list[str],
    content: str,
    if_match: str | None = None,
) -> dict:
    """Patch a heading, block reference, or frontmatter field.

    ``operation`` is one of append, prepend, replace, delete. ``target_type``
    is one of heading, block, frontmatter. For a heading, ``target`` may be a
    string or a list of heading texts from the top level down. For frontmatter
    append/prepend/replace, ``content`` is parsed as JSON when possible so
    values such as booleans, numbers, and lists are written with their native
    type.
    """

    normalized = normalize_vault_path(filepath)
    etag = await api.patch_content(
        normalized,
        operation=operation,
        target_type=target_type,
        target=target,
        content=content,
        if_match=if_match,
    )
    return success(f"Patched content in {normalized}", etag=etag)


@mcp.tool()
@circuit_protected
async def put_content(
    input: FilePathInput, content: str, if_match: str | None = None
) -> dict:
    """Create or replace a note. Pass ``if_match`` to avoid overwriting concurrent edits."""

    filepath = normalize_vault_path(input.filepath)
    etag = await api.put_content(filepath, content, if_match=if_match)
    return success(f"Updated file {filepath}", etag=etag)


@mcp.tool()
@circuit_protected
async def delete_file(
    input: FilePathInput, confirm: bool, if_match: str | None = None
) -> dict:
    """Delete a note. ``confirm`` must be true. Pass ``if_match`` for conditional deletion."""

    filepath = normalize_vault_path(input.filepath)
    if not confirm:
        return error(
            {
                "status": "invalid_input",
                "reason": "confirm must be true before deleting a note",
            }
        )

    await api.delete_file(filepath, if_match=if_match)
    return success(f"Deleted {filepath}")


# ========================
# VAULT ANALYTICS
# ========================


@mcp.tool()
@circuit_protected
async def get_vault_stats(max_files: int = DEFAULT_STATS_FILES) -> dict:
    """Summarise notes, link density, tags, and orphaned notes.

    Only Markdown notes are counted. The scan is bounded by ``max_files`` and
    reports ``truncated`` when the vault is larger. Embeds and links inside
    fenced code blocks are ignored.
    """

    limit = _clamp(max_files, 1, MAX_STATS_FILES)
    files = await api.list_files_in_vault()
    notes = [path for path in files if is_markdown(path)]
    scanned = notes[:limit]

    concurrency = _env_int("OBSIDIAN_STATS_CONCURRENCY", 10, 1, 50)
    semaphore = asyncio.Semaphore(concurrency)

    async def read_note(path: str) -> tuple[str, str | None | Exception]:
        async with semaphore:
            try:
                return path, await api.get_file_contents(path)
            except Exception as exc:
                if is_service_failure(exc):
                    return path, exc
                logger.warning(
                    "stats_file_read_failed",
                    extra={
                        "event": "stats_file_read_failed",
                        "path": path,
                        "status": getattr(exc, "status", type(exc).__name__),
                    },
                )
                return path, None

    pairs = await asyncio.gather(*(read_note(path) for path in scanned))
    errors = [value for _, value in pairs if isinstance(value, Exception)]
    if errors:
        raise errors[0]
    contents = {path: content for path, content in pairs if isinstance(content, str)}

    stats = compute_vault_stats(files, contents, max_files=limit)
    stats["notes_read"] = sum(
        1 for _, content in pairs if isinstance(content, str)
    )
    stats["read_failures"] = len(pairs) - stats["notes_read"]
    return success(stats)


# ========================
# HEALTH CHECK
# ========================


@mcp.tool()
async def check_connection() -> dict:
    """Check Obsidian connectivity, latency, and circuit-breaker state."""

    started = time.monotonic()

    try:
        files = await api.list_files_in_vault()
        latency = round((time.monotonic() - started) * 1000, 2)
        circuit.record_success()
        return success(
            {
                "status": "healthy",
                "latency_ms": latency,
                "file_count": len(files),
                "host": api.base_url,
                "circuit_state": circuit.state,
                "circuit": circuit.snapshot(),
            }
        )
    except Exception as exc:
        latency = round((time.monotonic() - started) * 1000, 2)
        circuit.record_failure()
        return error(
            {
                "status": "unhealthy",
                "latency_ms": latency,
                "reason": str(exc),
                "host": api.base_url,
                "circuit_state": circuit.state,
                "circuit": circuit.snapshot(),
            }
        )


# ========================
# RUN SERVER
# ========================

if __name__ == "__main__":
    mcp.run()
