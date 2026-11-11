"""Async wrapper around the Obsidian Local REST API.

The wrapper owns the retry policy, URL encoding, conditional-write headers, and
the small amount of response validation that the MCP layer needs.  It never
logs note contents or credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

import httpx

from mcp_obsidian.errors import (
    InputValidationError,
    NoteAlreadyExistsError,
    ObsidianMCPError,
    PreconditionFailedError,
    TransientAPIError,
    UnsupportedFileTypeError,
)
from mcp_obsidian.validation import encode_vault_path, normalize_vault_path

logger = logging.getLogger(__name__)

# Text extensions that are safe to round-trip through a text MCP tool.  Binary
# attachments are intentionally rejected rather than decoded as lossy text.
TEXT_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".mdx",
        ".txt",
        ".json",
        ".jsonc",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".csv",
        ".tsv",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".less",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".py",
        ".xml",
        ".svg",
        ".canvas",
        ".base",
        ".tex",
        ".bib",
        ".org",
        ".rst",
        ".log",
        ".sh",
        ".ps1",
        ".sql",
    }
)


def is_text_file(filepath: str) -> bool:
    """Return True when a path has a supported text extension."""

    normalized = filepath.replace("\\", "/").lower()
    _, dot, extension = normalized.rpartition(".")
    if not dot:
        return False
    return f".{extension}" in TEXT_EXTENSIONS


def is_service_failure(exc: Exception) -> bool:
    """Return True when an error should fail a batch or aggregate operation.

    Expected per-file failures (unsupported type, invalid path, 4xx) are
    reported inline. Service outages, transient errors, and unexpected
    exceptions propagate so the circuit breaker can see them.
    """

    if isinstance(exc, ObsidianMCPError):
        return exc.counts_as_circuit_failure
    if isinstance(exc, httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return True


def retry_with_backoff(max_retries: int = 3, base_delay: float = 0.5, max_delay: float = 5):
    """Retry transient HTTP/network failures with exponential backoff + jitter."""

    def decorator(func: Callable[..., Awaitable[Any]]):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Mutating writes opt out of automatic retries. Replaying an append
            # or PATCH after an ambiguous 503 could duplicate content.
            if not kwargs.get("retry", True):
                return await func(*args, **kwargs)

            attempt = 0
            while True:
                try:
                    return await func(*args, **kwargs)

                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status not in (429, 503):
                        raise
                    error_type = f"HTTP {status}"

                except httpx.RequestError:
                    error_type = "NETWORK_ERROR"

                except Exception:
                    raise

                if attempt >= max_retries:
                    raise TransientAPIError(
                        f"Max retries exceeded ({error_type})"
                    ) from None

                delay = min(max_delay, base_delay * (2**attempt))
                delay += random.uniform(0, delay * 0.1)

                logger.warning(
                    "retrying_api_request",
                    extra={
                        "event": "retrying_api_request",
                        "attempt": attempt + 1,
                        "status": error_type,
                        "duration_ms": round(delay * 1000, 2),
                    },
                )
                await asyncio.sleep(delay)
                attempt += 1

        return wrapper

    return decorator


def _coerce_frontmatter_value(content: str) -> Any:
    """Best-effort conversion of a tool string to a frontmatter JSON value."""

    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return content


class Obsidian:
    """Async client for the Obsidian Local REST API."""

    def __init__(
        self,
        api_key: str,
        protocol: str | None = None,
        host: str | None = None,
        port: int | str | None = None,
        verify_ssl: bool | None = None,
    ) -> None:
        # Resolve environment values here, not in default arguments.  Default
        # arguments are evaluated at import time, before load_dotenv() runs.
        self.api_key = api_key
        resolved_protocol = (protocol or os.getenv("OBSIDIAN_PROTOCOL", "https")).lower()
        self.protocol = "http" if resolved_protocol == "http" else "https"
        self.host = host or os.getenv("OBSIDIAN_HOST", "127.0.0.1")
        self.port = int(port if port is not None else os.getenv("OBSIDIAN_PORT", "27124"))

        if verify_ssl is None:
            verify_ssl = os.getenv("OBSIDIAN_VERIFY_SSL", "false").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        self.verify_ssl = verify_ssl

        ca_bundle = os.getenv("OBSIDIAN_CA_BUNDLE")
        self.base_url = f"{self.protocol}://{self.host}:{self.port}"

        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=float(os.getenv("OBSIDIAN_TIMEOUT", "10")),
            verify=ca_bundle or self.verify_ssl,
            headers={"User-Agent": "mcp-obsidian-sindhutsundupalli"},
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""

        await self.client.aclose()

    async def __aenter__(self) -> Obsidian:
        return self

    async def __aexit__(self, *_exc_info: Any) -> None:
        await self.aclose()

    def _get_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _url(self, filepath: str, *, is_dir: bool = False) -> str:
        encoded = encode_vault_path(filepath, is_dir=is_dir)
        return f"/vault/{encoded}"

    @staticmethod
    def _raise_for_status(response: httpx.Response, filepath: str) -> None:
        if response.status_code in (409, 412):
            raise PreconditionFailedError(
                f"conditional write failed for {filepath}; the note changed "
                "or already exists"
            )
        response.raise_for_status()

    # ========================
    # SAFE CALL
    # ========================

    @retry_with_backoff(max_retries=3, base_delay=0.5, max_delay=5)
    async def _safe_call(
        self,
        fn: Callable[..., Awaitable[Any]],
        *args: Any,
        retry: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Run a fresh request coroutine on every attempt.

        ``fn`` must be a callable that creates a new coroutine.  Passing an
        already-created coroutine would make retries impossible.  Mutating
        operations pass ``retry=False``.
        """

        return await fn(*args, **kwargs)

    # ========================
    # FILE OPERATIONS
    # ========================

    async def list_files_in_vault(self) -> Any:
        async def call():
            response = await self.client.get("/vault/", headers=self._get_headers())
            response.raise_for_status()
            return response.json()["files"]

        return await self._safe_call(call)

    async def list_files_in_dir(self, dirpath: str) -> Any:
        async def call():
            response = await self.client.get(
                self._url(dirpath, is_dir=True), headers=self._get_headers()
            )
            response.raise_for_status()
            return response.json()["files"]

        return await self._safe_call(call)

    async def _get_text_response(self, filepath: str) -> httpx.Response:
        normalized = normalize_vault_path(filepath)
        if not is_text_file(normalized):
            raise UnsupportedFileTypeError(
                f"{normalized} is not a supported text file; binary attachments "
                "are not returned through text tools"
            )

        async def call():
            response = await self.client.get(
                self._url(normalized), headers=self._get_headers()
            )
            response.raise_for_status()
            return response

        return await self._safe_call(call)

    async def get_file_contents(self, filepath: str) -> str:
        response = await self._get_text_response(filepath)
        try:
            return response.content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise UnsupportedFileTypeError(
                f"{filepath} is not valid UTF-8 text"
            ) from exc

    async def get_file_contents_with_etag(self, filepath: str) -> tuple[str, str | None]:
        response = await self._get_text_response(filepath)
        try:
            content = response.content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise UnsupportedFileTypeError(
                f"{filepath} is not valid UTF-8 text"
            ) from exc
        return content, response.headers.get("ETag")

    async def get_batch_file_contents(
        self, filepaths: list[str], max_files: int = 20
    ) -> str:
        """Read multiple notes, bounded by ``max_files``.

        Individual failures are reported inline so one bad path does not hide
        the successfully read notes.
        """

        max_files = max(1, int(max_files))
        selected = filepaths[:max_files]
        result: list[str] = []

        for filepath in selected:
            try:
                content = await self.get_file_contents(filepath)
                result.append(f"# {filepath}\n\n{content}\n\n---\n\n")
            except Exception as exc:  # report per-file failure without hiding others
                if is_service_failure(exc):
                    raise
                result.append(f"# {filepath}\n\nError: {exc}\n\n---\n\n")

        if len(filepaths) > len(selected):
            result.append(
                f"# Batch truncated\n\nOnly the first {len(selected)} of "
                f"{len(filepaths)} requested files were read.\n\n---\n\n"
            )

        return "".join(result)

    # ========================
    # SEARCH
    # ========================

    async def search_with_metadata(
        self,
        query: str,
        context_length: int = 100,
        max_results: int = 20,
        max_matches_per_file: int = 10,
    ) -> tuple[list[dict[str, Any]], int | None]:
        if not query or not query.strip():
            raise InputValidationError("query is required")

        context_length = max(1, min(int(context_length), 500))
        max_results = max(1, int(max_results))
        max_matches_per_file = max(1, int(max_matches_per_file))

        async def call():
            response = await self.client.post(
                "/search/simple/",
                headers=self._get_headers(),
                params={
                    "query": query,
                    "contextLength": context_length,
                    "maxResults": max_results,
                    "maxMatchesPerFile": max_matches_per_file,
                },
            )
            response.raise_for_status()
            return response

        response = await self._safe_call(call)
        total_header = response.headers.get("Total-Results")
        try:
            total = int(total_header) if total_header is not None else None
        except ValueError:
            total = None
        return response.json(), total

    async def search(
        self,
        query: str,
        context_length: int = 100,
        max_results: int = 20,
        max_matches_per_file: int = 10,
    ) -> Any:
        results, _ = await self.search_with_metadata(
            query,
            context_length=context_length,
            max_results=max_results,
            max_matches_per_file=max_matches_per_file,
        )
        return results

    async def search_json(self, query: dict) -> Any:
        if not isinstance(query, dict):
            raise InputValidationError("query must be a JSONLogic object")

        async def call():
            response = await self.client.post(
                "/search/",
                headers=self._get_headers()
                | {"Content-Type": "application/vnd.olrapi.jsonlogic+json"},
                json=query,
            )
            response.raise_for_status()
            return response.json()

        return await self._safe_call(call)

    # ========================
    # WRITE / MODIFY
    # ========================

    async def append_content(
        self, filepath: str, content: str, if_match: str | None = None
    ) -> str | None:
        normalized = normalize_vault_path(filepath)
        headers = self._get_headers() | {"Content-Type": "text/markdown"}
        if if_match:
            headers["If-Match"] = if_match

        async def call():
            response = await self.client.post(
                self._url(normalized), headers=headers, content=content
            )
            self._raise_for_status(response, normalized)
            return response.headers.get("ETag")

        return await self._safe_call(call, retry=False)

    async def patch_content(
        self,
        filepath: str,
        operation: str,
        target_type: str,
        target: str | list[str],
        content: str,
        if_match: str | None = None,
    ) -> str | None:
        normalized = normalize_vault_path(filepath)
        if operation not in {"append", "prepend", "replace", "delete"}:
            raise InputValidationError(f"unsupported patch operation: {operation}")
        if target_type not in {"heading", "block", "frontmatter"}:
            raise InputValidationError(f"unsupported patch target type: {target_type}")

        if target_type == "heading":
            # The current API addresses headings as an array of heading texts
            # from the top level down. Accept a plain string for compatibility
            # with the original single-heading tool interface.
            if isinstance(target, str):
                if not target.strip():
                    raise InputValidationError("target is required")
                target_value: str | list[str] = [target]
            elif (
                isinstance(target, list)
                and target
                and all(isinstance(part, str) and part.strip() for part in target)
            ):
                target_value = target
            else:
                raise InputValidationError(
                    "heading target must be a non-empty string or list of strings"
                )
        else:
            if not isinstance(target, str) or not target.strip():
                raise InputValidationError(
                    f"{target_type} target must be a non-empty string"
                )
            if target_type == "block":
                # The API takes the bare block id, without the leading caret.
                target_value = target[1:] if target.startswith("^") else target
                if not target_value.strip():
                    raise InputValidationError("block target must be a block id")
            else:
                target_value = target

        payload: dict[str, Any] = {
            "targetType": target_type,
            "target": target_value,
            "operation": operation,
        }
        if operation != "delete":
            if target_type == "frontmatter":
                payload["value"] = _coerce_frontmatter_value(content)
            else:
                payload["content"] = content

        headers = self._get_headers() | {"Content-Type": "application/json"}
        if if_match:
            headers["If-Match"] = if_match

        async def call():
            response = await self.client.patch(
                self._url(normalized), headers=headers, json=payload
            )
            self._raise_for_status(response, normalized)
            return response.headers.get("ETag")

        return await self._safe_call(call, retry=False)

    async def put_content(
        self,
        filepath: str,
        content: str,
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> str | None:
        normalized = normalize_vault_path(filepath)
        headers = self._get_headers() | {"Content-Type": "text/markdown"}
        if if_match:
            headers["If-Match"] = if_match
        if if_none_match:
            headers["If-None-Match"] = if_none_match

        async def call():
            response = await self.client.put(
                self._url(normalized), headers=headers, content=content
            )
            self._raise_for_status(response, normalized)
            return response.headers.get("ETag")

        return await self._safe_call(call, retry=False)

    async def create_note(self, filepath: str, content: str) -> str | None:
        """Create a note only when it does not already exist."""

        normalized = normalize_vault_path(filepath)
        try:
            return await self.put_content(
                normalized, content, if_none_match="*"
            )
        except PreconditionFailedError as exc:
            raise NoteAlreadyExistsError(
                f"{normalized} already exists; use put_content to replace it"
            ) from exc

    async def delete_file(self, filepath: str, if_match: str | None = None) -> None:
        normalized = normalize_vault_path(filepath)
        headers = self._get_headers()
        if if_match:
            headers["If-Match"] = if_match

        async def call():
            response = await self.client.delete(
                self._url(normalized), headers=headers
            )
            self._raise_for_status(response, normalized)

        return await self._safe_call(call, retry=False)

    # ========================
    # PERIODIC NOTES
    # ========================

    async def get_periodic_note(self, period: str, type: str = "content") -> Any:
        async def call():
            headers = self._get_headers()
            if type == "metadata":
                headers["Accept"] = "application/vnd.olrapi.note+json"

            response = await self.client.get(
                f"/periodic/{period}/", headers=headers
            )
            response.raise_for_status()
            return response.json() if type == "metadata" else response.text

        return await self._safe_call(call)

    async def get_recent_periodic_notes(
        self, period: str, limit: int = 5, include_content: bool = False
    ) -> Any:
        async def call():
            response = await self.client.get(
                f"/periodic/{period}/recent",
                headers=self._get_headers(),
                params={"limit": limit, "includeContent": include_content},
            )
            response.raise_for_status()
            return response.json()

        return await self._safe_call(call)

    async def get_recent_changes(self, limit: int = 10, days: int = 90) -> Any:
        async def call():
            query = "\n".join(
                [
                    "TABLE file.mtime",
                    f"WHERE file.mtime >= date(today) - dur({days} days)",
                    "SORT file.mtime DESC",
                    f"LIMIT {limit}",
                ]
            )

            response = await self.client.post(
                "/search/",
                headers=self._get_headers()
                | {"Content-Type": "application/vnd.olrapi.dataview.dql+txt"},
                content=query.encode("utf-8"),
            )
            response.raise_for_status()
            return response.json()

        return await self._safe_call(call)
