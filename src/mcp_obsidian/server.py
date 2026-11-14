"""MCP tools for the Obsidian Local REST API."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from functools import wraps
from typing import Annotated, Any, Literal

from dotenv import load_dotenv

# Must run before importing APIWrapper: APIWrapper resolves environment values
# when Obsidian is instantiated, and server configuration is read below.
load_dotenv()

from fastmcp import FastMCP  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from mcp_obsidian.APIWrapper import (  # noqa: E402
    Obsidian,
    is_circuit_failure,
    is_service_failure,
)
from mcp_obsidian.circuit_breaker import CircuitBreaker  # noqa: E402
from mcp_obsidian.errors import InputValidationError, ObsidianMCPError  # noqa: E402
from mcp_obsidian.limits import (  # noqa: E402
    DEFAULT_MAX_BATCH_BYTES,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_SEARCH_BYTES,
    DEFAULT_MAX_SUMMARY_BYTES,
    MAX_MAX_BATCH_BYTES,
    MAX_MAX_FILE_BYTES,
    MAX_MAX_SEARCH_BYTES,
    MAX_MAX_SUMMARY_BYTES,
    env_float,
    env_int,
    fit_json_to_bytes,
    json_bytes,
    json_characters,
    resolve_limit,
    truncate_utf8,
    utf8_len,
)
from mcp_obsidian.validation import normalize_vault_path  # noqa: E402
from mcp_obsidian.vault_stats import (  # noqa: E402
    compute_vault_stats,
    is_folder_entry,
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
port = env_int("OBSIDIAN_PORT", 27124, 1, 65535)

api = Obsidian(api_key=api_key, protocol=protocol, host=host, port=port)

circuit = CircuitBreaker(
    failure_threshold=env_int(
        "OBSIDIAN_CIRCUIT_FAILURE_THRESHOLD", 3, 1, 100
    ),
    recovery_time=env_float(
        "OBSIDIAN_CIRCUIT_RECOVERY_TIME", 10.0, 0.0, 3600.0
    ),
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


def _count_matches(results: Any) -> int:
    """Count match objects present in a simple-search result list."""

    total = 0
    if not isinstance(results, list):
        return total
    for item in results:
        if isinstance(item, dict) and isinstance(item.get("matches"), list):
            total += len(item["matches"])
    return total


def _read_limit(value: int | None) -> int:
    return resolve_limit(
        value,
        "OBSIDIAN_MAX_FILE_BYTES",
        DEFAULT_MAX_FILE_BYTES,
        MAX_MAX_FILE_BYTES,
    )


def _batch_limit(value: int | None) -> int:
    return resolve_limit(
        value,
        "OBSIDIAN_MAX_BATCH_BYTES",
        DEFAULT_MAX_BATCH_BYTES,
        MAX_MAX_BATCH_BYTES,
    )


def _search_limit(value: int | None) -> int:
    return resolve_limit(
        value,
        "OBSIDIAN_MAX_SEARCH_BYTES",
        DEFAULT_MAX_SEARCH_BYTES,
        MAX_MAX_SEARCH_BYTES,
    )


def _summary_limit(value: int | None) -> int:
    return resolve_limit(
        value,
        "OBSIDIAN_MAX_SUMMARY_BYTES",
        DEFAULT_MAX_SUMMARY_BYTES,
        MAX_MAX_SUMMARY_BYTES,
    )


async def _read_file(filepath: str, max_bytes: int | None):
    """Call the wrapper while remaining compatible with older test doubles."""

    if max_bytes is None:
        return await api.get_file_contents(filepath)
    try:
        return await api.get_file_contents(filepath, max_bytes=max_bytes)
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        return await api.get_file_contents(filepath)


async def _read_file_with_etag(filepath: str, max_bytes: int | None):
    """Call the etag wrapper while remaining compatible with older test doubles."""

    if max_bytes is None:
        return await api.get_file_contents_with_etag(filepath)
    try:
        return await api.get_file_contents_with_etag(
            filepath, max_bytes=max_bytes
        )
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        return await api.get_file_contents_with_etag(filepath)


async def _read_batch(
    filepaths: list[str],
    max_files: int,
    max_bytes: int | None,
    max_file_bytes: int | None,
):
    """Call the batch wrapper while remaining compatible with older fakes."""

    kwargs: dict[str, Any] = {"max_files": max_files}
    if max_bytes is not None:
        kwargs["max_bytes"] = max_bytes
    if max_file_bytes is not None:
        kwargs["max_file_bytes"] = max_file_bytes
    try:
        return await api.get_batch_file_contents(filepaths, **kwargs)
    except TypeError as exc:
        if len(kwargs) == 1 or "unexpected keyword argument" not in str(exc):
            raise
        return await api.get_batch_file_contents(filepaths, max_files=max_files)


def _search_metadata(
    results: Any, total_results: int | None, file_limit: int
) -> dict[str, Any]:
    """Summarise file-level and match-level truncation from simple search.

    Obsidian reports the number of files in the ``Total-Results`` header and
    the number of matches per file in ``totalMatches``. Both can be larger than
    what the API returned, so they are tracked independently.
    """

    result_list = results if isinstance(results, list) else []
    files_returned = len(result_list)
    if total_results is None:
        files_truncated = files_returned >= file_limit
    else:
        files_truncated = total_results > files_returned

    total_matches = 0
    matches_returned = 0
    for item in result_list:
        if not isinstance(item, dict):
            continue
        matches = item.get("matches")
        if not isinstance(matches, list):
            matches = []
        matches_returned += len(matches)

        raw_total = item.get("totalMatches")
        try:
            per_file_total = int(raw_total)
        except (TypeError, ValueError, OverflowError):
            per_file_total = len(matches)
        total_matches += max(len(matches), per_file_total)

    matches_truncated = total_matches > matches_returned
    return {
        "returned": files_returned,
        "files_truncated": files_truncated,
        "total_matches": total_matches,
        "matches_returned": matches_returned,
        "matches_truncated": matches_truncated,
        "truncated": files_truncated or matches_truncated,
    }


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
            if is_circuit_failure(exc):
                circuit.record_failure()
                circuit_outcome = "failure"
            else:
                # Local validation, auth/client errors, and write conflicts
                # are neutral: they neither prove nor disprove service health.
                circuit.record_neutral()
                circuit_outcome = "neutral"

            snapshot = circuit.snapshot()
            # Expected, typed failures are actionable without a traceback.
            # Unexpected exceptions get one so operators can diagnose them.
            unexpected = not isinstance(exc, ObsidianMCPError)
            logger.warning(
                "tool_failed",
                extra={
                    "event": "tool_failed",
                    "tool": func.__name__,
                    "status": getattr(exc, "status", "failed"),
                    "circuit_state": snapshot["state"],
                    "circuit_outcome": circuit_outcome,
                    "failure_count": snapshot["failure_count"],
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                },
                exc_info=exc if unexpected else None,
            )
            return error(
                {
                    "status": getattr(exc, "status", "failed"),
                    "reason": str(exc),
                    "circuit_state": snapshot["state"],
                    "failure_count": snapshot["failure_count"],
                }
            )

        if isinstance(result, dict) and result.get("success") is False:
            circuit.record_neutral()
            circuit_outcome = "neutral"
        else:
            circuit.record_success()
            circuit_outcome = "success"

        logger.info(
            "tool_completed",
            extra={
                "event": "tool_completed",
                "tool": func.__name__,
                "circuit_state": circuit.state,
                "circuit_outcome": circuit_outcome,
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
async def get_file_contents(
    input: FilePathInput,
    include_etag: bool = False,
    max_bytes: Annotated[int, Field(ge=1, le=MAX_MAX_FILE_BYTES)] | None = None,
) -> dict:
    """Read a UTF-8 text note. Binary attachments are rejected, not decoded as text.

    Set ``include_etag`` to receive the ETag/version needed for conditional writes.
    ``max_bytes`` bounds the returned text; truncation is reported explicitly.
    """

    filepath = normalize_vault_path(input.filepath)
    limit = _read_limit(max_bytes)
    if include_etag:
        content, etag = await _read_file_with_etag(filepath, max_bytes)
        data = {"filepath": filepath, "content": str(content), "etag": etag}
    else:
        content = await _read_file(filepath, max_bytes)
        data = str(content)

    bounded, post_truncated = truncate_utf8(str(content), limit)
    metadata = getattr(content, "metadata", {})
    bytes_truncated = (
        bool(metadata.get("bytes_truncated", metadata.get("truncated")))
        or post_truncated
    )
    if include_etag:
        data["content"] = bounded
    else:
        data = bounded
    return success(
        data,
        returned_bytes=utf8_len(bounded),
        returned_characters=len(bounded),
        max_bytes=limit,
        truncated=bytes_truncated,
        bytes_truncated=bytes_truncated,
    )


@mcp.tool()
@circuit_protected
async def batch_get_file_contents(
    filepaths: list[str],
    max_files: Annotated[int, Field(ge=1, le=MAX_BATCH_FILES)] = DEFAULT_BATCH_FILES,
    max_bytes: Annotated[int, Field(ge=1, le=MAX_MAX_BATCH_BYTES)] | None = None,
    max_file_bytes: Annotated[int, Field(ge=1, le=MAX_MAX_FILE_BYTES)] | None = None,
) -> dict:
    """Read multiple text notes in one call.

    Output is bounded by ``max_files`` and by the total UTF-8 byte limit.
    """

    limit = _clamp(max_files, 1, MAX_BATCH_FILES)
    selected = filepaths[:limit]
    normalized = [normalize_vault_path(path) for path in selected]
    total_limit = _batch_limit(max_bytes)
    file_limit = _read_limit(max_file_bytes)
    batch = await _read_batch(normalized, limit, max_bytes, max_file_bytes)

    content = str(batch)
    if len(filepaths) > len(selected):
        content += (
            f"# Batch truncated\n\nOnly the first {len(selected)} of "
            f"{len(filepaths)} requested files were read.\n\n---\n\n"
        )
    bounded, post_truncated = truncate_utf8(content, total_limit)
    metadata = getattr(batch, "metadata", {})
    count_truncated = len(filepaths) > len(selected)
    files_truncated = bool(metadata.get("files_truncated")) or count_truncated
    file_content_truncated = bool(metadata.get("file_content_truncated"))
    metadata_bytes = metadata.get("bytes_truncated")
    if metadata_bytes is None:
        metadata_bytes = bool(metadata.get("truncated")) and not bool(
            metadata.get("files_truncated")
        )
    bytes_truncated = (
        bool(metadata_bytes) or file_content_truncated or post_truncated
    )
    truncated = (
        bytes_truncated
        or bool(metadata.get("truncated"))
        or files_truncated
    )
    return success(
        bounded,
        returned_bytes=utf8_len(bounded),
        returned_characters=len(bounded),
        max_bytes=total_limit,
        max_file_bytes=file_limit,
        files_returned=int(metadata.get("files_returned", len(normalized))),
        max_files=limit,
        files_truncated=files_truncated,
        file_content_truncated=file_content_truncated,
        truncated=truncated,
        bytes_truncated=bytes_truncated,
    )


# ========================
# SEARCH
# ========================


@mcp.tool()
@circuit_protected
async def simple_search(
    query: str,
    context_length: Annotated[int, Field(ge=1, le=500)] = 100,
    max_results: Annotated[int, Field(ge=1, le=MAX_SEARCH_RESULTS)] = (
        DEFAULT_SEARCH_RESULTS
    ),
    max_matches_per_file: Annotated[
        int, Field(ge=1, le=MAX_MATCHES_PER_FILE)
    ] = DEFAULT_MATCHES_PER_FILE,
    max_bytes: Annotated[int, Field(ge=1, le=MAX_MAX_SEARCH_BYTES)] | None = None,
) -> dict:
    """Full-text search using Obsidian's built-in search.

    Results include filenames and match contexts. Limits are clamped to avoid
    returning an unbounded amount of text to the model. The returned data is
    also bounded by a total UTF-8 byte limit.
    """

    results_limit = _clamp(max_results, 1, MAX_SEARCH_RESULTS)
    matches_limit = _clamp(max_matches_per_file, 1, MAX_MATCHES_PER_FILE)
    results, total = await api.search_with_metadata(
        query,
        context_length=context_length,
        max_results=results_limit,
        max_matches_per_file=matches_limit,
    )
    metadata = _search_metadata(results, total, results_limit)
    limit = _search_limit(max_bytes)
    bounded, bytes_truncated = fit_json_to_bytes(results, limit)
    if not isinstance(bounded, list):
        bounded = []
    bounded_metadata = _search_metadata(bounded, total, results_limit)
    files_truncated = (
        metadata["files_truncated"]
        or bounded_metadata["returned"] < metadata["returned"]
    )
    matches_returned = bounded_metadata["matches_returned"]
    matches_truncated = (
        metadata["matches_truncated"]
        or matches_returned < metadata["matches_returned"]
    )
    truncated = files_truncated or matches_truncated or bytes_truncated
    return success(
        bounded,
        returned=bounded_metadata["returned"],
        max_results=results_limit,
        total_results=total,
        files_truncated=files_truncated,
        total_matches=metadata["total_matches"],
        matches_returned=matches_returned,
        matches_truncated=matches_truncated,
        returned_bytes=json_bytes(bounded),
        returned_characters=json_characters(bounded),
        max_bytes=limit,
        bytes_truncated=bytes_truncated,
        truncated=truncated,
    )


@mcp.tool()
@circuit_protected
async def complex_search(
    query: dict,
    max_results: Annotated[int, Field(ge=1, le=MAX_SEARCH_RESULTS)] = 50,
    max_bytes: Annotated[int, Field(ge=1, le=MAX_MAX_SEARCH_BYTES)] | None = None,
) -> dict:
    """Structured JSONLogic search against note metadata. Results are bounded."""

    limit = _clamp(max_results, 1, MAX_SEARCH_RESULTS)
    byte_limit = _search_limit(max_bytes)
    results = await api.search_json(query)
    if isinstance(results, list):
        selected = results[:limit]
        bounded, bytes_truncated = fit_json_to_bytes(selected, byte_limit)
        if not isinstance(bounded, list):
            bounded = []
        truncated = (
            len(results) > limit
            or len(bounded) < len(selected)
            or bytes_truncated
        )
        return success(
            bounded,
            returned=len(bounded),
            max_results=limit,
            returned_bytes=json_bytes(bounded),
            returned_characters=json_characters(bounded),
            max_bytes=byte_limit,
            bytes_truncated=bytes_truncated,
            truncated=truncated,
        )

    bounded, bytes_truncated = fit_json_to_bytes(results, byte_limit)
    return success(
        bounded,
        returned=None,
        max_results=limit,
        returned_bytes=json_bytes(bounded),
        returned_characters=json_characters(bounded),
        max_bytes=byte_limit,
        bytes_truncated=bytes_truncated,
        truncated=bytes_truncated,
    )


@mcp.tool()
@circuit_protected
async def search_and_summarize(
    query: str,
    max_sources: Annotated[int, Field(ge=1, le=MAX_SUMMARY_SOURCES)] = (
        DEFAULT_SUMMARY_SOURCES
    ),
    max_excerpts_per_source: Annotated[
        int, Field(ge=1, le=MAX_EXCERPTS_PER_SOURCE)
    ] = DEFAULT_EXCERPTS_PER_SOURCE,
    context_length: Annotated[int, Field(ge=1, le=500)] = 100,
    max_bytes: Annotated[int, Field(ge=1, le=MAX_MAX_SUMMARY_BYTES)] | None = None,
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
    metadata = _search_metadata(results, total, sources_limit)

    sources: list[dict[str, Any]] = []
    for item in results[:sources_limit] if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        filename_value = item.get("filename") or item.get("path")
        filename = (
            filename_value
            if isinstance(filename_value, str) and filename_value
            else "<unknown>"
        )
        raw_matches = item.get("matches")
        matches = raw_matches if isinstance(raw_matches, list) else []
        valid_matches = [
            match
            for match in matches
            if isinstance(match, dict)
            and isinstance(match.get("context"), str)
            and match["context"]
        ]
        excerpts = [
            match["context"] for match in valid_matches[:excerpts_limit]
        ]
        sources.append({"filename": filename, "excerpts": excerpts})

    source_count = total if total is not None else metadata["returned"]
    selected_matches = sum(len(source["excerpts"]) for source in sources)
    summary = "\n\n".join(
        f"{source['filename']}: {excerpt}"
        for source in sources
        for excerpt in source["excerpts"]
    )
    summary_character_truncated = len(summary) > 500
    summary = summary[:500]
    reported_limit = _summary_limit(max_bytes)
    priority = [
        "returned_bytes",
        "truncated",
        "max_bytes",
        "bytes_truncated",
        # Keep the size metadata and truncation flags ahead of the payloads
        # they describe so a tight byte budget removes excerpts before it
        # removes the reason they were shortened.
        "returned_characters",
        "summary_truncated",
        "files_truncated",
        "matches_truncated",
        "summary",
        "sources",
        "source_count",
        "returned_sources",
        "total_matches",
        "matches_returned",
        "query",
        "note",
    ]
    values = {
        "summary": summary,
        "query": query,
        "sources": sources,
        "source_count": source_count,
        "returned_sources": len(sources),
        "files_truncated": metadata["files_truncated"],
        "total_matches": metadata["total_matches"],
        "matches_returned": metadata["matches_returned"],
        "matches_truncated": metadata["matches_truncated"],
        "summary_truncated": summary_character_truncated,
        "returned_bytes": 0,
        "returned_characters": 0,
        "max_bytes": reported_limit,
        "bytes_truncated": False,
        "truncated": metadata["truncated"] or summary_character_truncated,
        "note": "Excerpts from Obsidian search; no AI summary was generated.",
    }
    data: dict[str, Any] = {key: values[key] for key in priority}
    core_keys = {
        "returned_bytes",
        "truncated",
    }

    def stabilize(payload: dict[str, Any]) -> dict[str, Any]:
        for _ in range(3):
            size = json_bytes(payload)
            characters = json_characters(payload)
            if (
                payload.get("returned_bytes") == size
                and payload.get("returned_characters") == characters
            ):
                break
            if "returned_bytes" in payload:
                payload["returned_bytes"] = size
            if "returned_characters" in payload:
                payload["returned_characters"] = characters
        return payload

    bytes_truncated = False
    summary_was_truncated = summary_character_truncated
    for _ in range(4):
        fitted, changed = fit_json_to_bytes(data, reported_limit)
        if not isinstance(fitted, dict):
            fitted = {}
        fitted_sources = fitted.get("sources")
        if not isinstance(fitted_sources, list):
            fitted_sources = []
        matches_returned = sum(
            len(source.get("excerpts", []))
            for source in fitted_sources
            if isinstance(source, dict)
            and isinstance(source.get("excerpts"), list)
        )
        files_truncated = (
            metadata["files_truncated"] or source_count > len(fitted_sources)
        )
        matches_truncated = (
            metadata["matches_truncated"] or matches_returned < selected_matches
        )
        reported_matches = metadata["matches_returned"]
        if matches_returned < reported_matches:
            reported_matches = matches_returned
        fitted_summary = fitted.get("summary")
        summary_truncated = (
            summary_character_truncated
            or not isinstance(fitted_summary, str)
            or fitted_summary != summary
        )
        summary_was_truncated = summary_was_truncated or summary_truncated
        bytes_truncated = bytes_truncated or changed
        updates = {
            "returned_sources": len(fitted_sources),
            "files_truncated": files_truncated,
            "matches_returned": reported_matches,
            "matches_truncated": matches_truncated,
            "summary_truncated": summary_truncated,
            "returned_bytes": 0,
            "returned_characters": 0,
            "max_bytes": reported_limit,
            "bytes_truncated": bytes_truncated,
            "truncated": (
                files_truncated
                or matches_truncated
                or bytes_truncated
                or summary_truncated
            ),
        }
        for key, value in updates.items():
            if key in fitted or key in core_keys:
                fitted[key] = value
        data = stabilize(fitted)
        if json_bytes(data) <= reported_limit:
            break
        bytes_truncated = True
        for key in reversed(priority):
            if key in core_keys:
                continue
            if key in data and json_bytes(data) > reported_limit:
                data.pop(key, None)
        data = stabilize(data)
        if json_bytes(data) <= reported_limit:
            break

    return success(
        data,
        returned_bytes=data.get("returned_bytes", json_bytes(data)),
        returned_characters=data.get(
            "returned_characters", json_characters(data)
        ),
        max_bytes=reported_limit,
        bytes_truncated=data.get("bytes_truncated", True),
        summary_truncated=data.get(
            "summary_truncated", summary_was_truncated
        ),
        truncated=data.get("truncated", True),
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
    operation: Literal["append", "prepend", "replace", "delete"],
    target_type: Literal["heading", "block", "frontmatter"],
    target: str | list[str] | None,
    content: str,
    if_match: str | None = None,
) -> dict:
    """Patch a heading, block reference, or frontmatter field.

    ``operation`` is one of append, prepend, replace, delete. ``target_type``
    is one of heading, block, frontmatter. For a heading, ``target`` may be a
    string or a list of heading texts from the top level down. Use ``[]`` or
    ``null`` to target the document root. For frontmatter
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
        raise InputValidationError("confirm must be true before deleting a note")

    await api.delete_file(filepath, if_match=if_match)
    return success(f"Deleted {filepath}")


# ========================
# VAULT ANALYTICS
# ========================


@mcp.tool()
@circuit_protected
async def get_vault_stats(
    max_files: Annotated[int, Field(ge=1, le=MAX_STATS_FILES)] = DEFAULT_STATS_FILES,
) -> dict:
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
    queue: asyncio.Queue[str] = asyncio.Queue()
    for path in scanned:
        queue.put_nowait(path)

    contents: dict[str, str] = {}

    async def read_worker() -> None:
        while True:
            try:
                path = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            try:
                try:
                    content = await api.get_file_contents(path)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if is_service_failure(exc):
                        raise
                    logger.warning(
                        "stats_file_read_failed",
                        extra={
                            "event": "stats_file_read_failed",
                            "path": path,
                            "status": getattr(exc, "status", type(exc).__name__),
                        },
                    )
                else:
                    contents[path] = content
            finally:
                queue.task_done()

    workers = [
        asyncio.create_task(read_worker())
        for _ in range(min(concurrency, len(scanned)))
    ]
    if workers:
        try:
            done, pending = await asyncio.wait(
                workers, return_when=asyncio.FIRST_EXCEPTION
            )
        except asyncio.CancelledError:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

        if pending:
            for worker in pending:
                worker.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        errors = [
            worker.exception()
            for worker in done
            if not worker.cancelled() and worker.exception() is not None
        ]
        if errors:
            raise errors[0]

    stats = compute_vault_stats(files, contents, max_files=limit)
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
        file_count = sum(
            1 for path in files if not is_folder_entry(path)
        )
        latency = round((time.monotonic() - started) * 1000, 2)
        circuit.record_success()
        return success(
            {
                "status": "healthy",
                "latency_ms": latency,
                "file_count": file_count,
                "host": api.base_url,
                "circuit_state": circuit.state,
                "circuit": circuit.snapshot(),
            }
        )
    except Exception as exc:
        latency = round((time.monotonic() - started) * 1000, 2)
        if is_circuit_failure(exc):
            circuit.record_failure()
        if not isinstance(exc, ObsidianMCPError):
            logger.warning(
                "health_check_failed",
                extra={
                    "event": "health_check_failed",
                    "status": getattr(exc, "status", type(exc).__name__),
                    "duration_ms": latency,
                },
                exc_info=exc,
            )
        # Auth and other client errors say nothing about service health, so
        # leave the breaker untouched rather than counting them as failures.
        return error(
            {
                "status": "unhealthy",
                "error_status": getattr(exc, "status", "failed"),
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
