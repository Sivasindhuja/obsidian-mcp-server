# MCP Obsidian Server

An MCP (Model Context Protocol) server that lets AI assistants read, search,
create, and update notes in an Obsidian vault through the
[Obsidian Local REST API](https://github.com/coddingtonbear/obsidian-local-rest-api)
plugin.

> **Status: engineering prototype / beta.** The core operations and reliability
> paths are covered by automated tests, but this is not a production-hardened
> multi-user service. See [Known limitations](#known-limitations) before using it
> against important data. Release notes are in [CHANGELOG.md](CHANGELOG.md).

## Why this project?

The Obsidian Local REST API plugin now ships its own built-in MCP server at
`https://127.0.0.1:27124/mcp/`. That server is the best choice if you only need
Obsidian's built-in tools.

This project focuses on a **custom stdio MCP bridge** and a small set of
**vault analytics** (link density, tag counts, orphan detection) rather than
duplicating every built-in tool. It is useful when:

- your MCP client only supports stdio servers;
- you want a small, auditable Python server you can extend;
- you need the analytics helpers included here.

## Features

- Vault and directory listing
- Text note reads, including optional ETags for conditional writes
- Batch reads with output limits
- Simple and JSONLogic search with result limits
- Search excerpts grouped by source note (not AI summarization)
- Safe note creation, explicit replacement, append, PATCH, and delete
- Conditional writes with `If-Match` / `If-None-Match`
- Vault analytics: note count, link density, tags, orphan detection
- Retry with exponential backoff for transient read/search failures
- Testable circuit breaker
- Structured JSON logging to stderr
- Path validation and URL-safe path encoding
- Graceful HTTP client shutdown

## Requirements

- Python 3.11+
- Obsidian with the **Local REST API** community plugin enabled
- The plugin API key

## Install

```bash
git clone https://github.com/Sivasindhuja/obsidian-mcp-server.git
cd obsidian-mcp-server
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Or install the package without development dependencies:

```bash
pip install .
```

If you build a wheel, install it directly:

```bash
pip wheel . -w dist
pip install dist/mcp_obsidian_sindhutsundupalli-0.3.0-py3-none-any.whl
```

The `mcp-obsidian` console script and `python -m mcp_obsidian` both start the
stdio server.

## Configuration

Copy `.env.example` to `.env` and fill in the API key. The key is in Obsidian:
**Settings → Local REST API → API key**.

```env
OBSIDIAN_API_KEY=
OBSIDIAN_HOST=127.0.0.1
OBSIDIAN_PROTOCOL=https
OBSIDIAN_PORT=27124
OBSIDIAN_VERIFY_SSL=false
```

| Variable | Default | Purpose |
|---|---|---|
| `OBSIDIAN_API_KEY` | required | Bearer token from the Local REST API plugin |
| `OBSIDIAN_HOST` | `127.0.0.1` | Obsidian host |
| `OBSIDIAN_PROTOCOL` | `https` | `https` (port 27124) or `http` (port 27123) |
| `OBSIDIAN_PORT` | `27124` | REST API port |
| `OBSIDIAN_VERIFY_SSL` | `false` | Verify TLS. The plugin uses a self-signed certificate by default |
| `OBSIDIAN_CA_BUNDLE` | unset | Path to a trusted CA bundle; overrides `OBSIDIAN_VERIFY_SSL` |
| `OBSIDIAN_TIMEOUT` | `10` | HTTP timeout in seconds |
| `OBSIDIAN_LOG_LEVEL` | `INFO` | Log level |
| `OBSIDIAN_CIRCUIT_FAILURE_THRESHOLD` | `3` | Failures before the circuit opens |
| `OBSIDIAN_CIRCUIT_RECOVERY_TIME` | `10` | Seconds before a half-open probe |
| `OBSIDIAN_STATS_CONCURRENCY` | `10` | Parallel note reads for `get_vault_stats` |
| `OBSIDIAN_MAX_FILE_BYTES` | `1000000` | Per-file UTF-8 byte limit for text reads |
| `OBSIDIAN_MAX_BATCH_BYTES` | `2000000` | Total UTF-8 byte limit for one batch read |
| `OBSIDIAN_MAX_SEARCH_BYTES` | `1000000` | Total UTF-8 byte limit for search result data |
| `OBSIDIAN_MAX_SUMMARY_BYTES` | `1000000` | Total UTF-8 byte limit for summary data |

Do not commit `.env` or real API keys.

## Run

```bash
mcp-obsidian
# or
python -m mcp_obsidian
```

The server speaks MCP over stdio. It is not an HTTP server.

## MCP client configuration

### Claude Desktop / Cursor-style stdio config

Use the installed console script. Replace `<your-api-key>` locally; do not commit
the file.

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "mcp-obsidian",
      "env": {
        "OBSIDIAN_API_KEY": "<your-api-key>",
        "OBSIDIAN_HOST": "127.0.0.1",
        "OBSIDIAN_PROTOCOL": "https",
        "OBSIDIAN_PORT": "27124",
        "OBSIDIAN_VERIFY_SSL": "false"
      }
    }
  }
}
```

If the script is not on `PATH`, use:

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "python",
      "args": ["-m", "mcp_obsidian"],
      "env": { "OBSIDIAN_API_KEY": "<your-api-key>" }
    }
  }
}
```

### Docker

The `-i` flag is required: MCP stdio communication needs an attached stdin.

```bash
docker build -t mcp-obsidian .

docker run -i --rm \
  -e OBSIDIAN_API_KEY="$OBSIDIAN_API_KEY" \
  -e OBSIDIAN_HOST=host.docker.internal \
  -e OBSIDIAN_PROTOCOL=https \
  -e OBSIDIAN_PORT=27124 \
  mcp-obsidian
```

- macOS/Windows: `host.docker.internal` normally reaches the host.
- Linux: add `--add-host=host.docker.internal:host-gateway`.
- If you enabled the plugin's plain HTTP endpoint, use
  `OBSIDIAN_PROTOCOL=http` and `OBSIDIAN_PORT=27123`.
- The container does not contain your notes; it talks to Obsidian running on
  the host.

Example MCP client entry for Docker:

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "OBSIDIAN_API_KEY=<your-api-key>",
        "-e", "OBSIDIAN_HOST=host.docker.internal",
        "-e", "OBSIDIAN_PROTOCOL=https",
        "-e", "OBSIDIAN_PORT=27124",
        "mcp-obsidian"
      ]
    }
  }
}
```

## MCP tools

`input` is a nested object generated by the Pydantic input models, for example
`{"input": {"filepath": "folder/note.md"}}`.

The generated tool schemas expose the allowed patch operations and target types
as enums, and numeric limits with minimum/maximum values. Direct Python calls to
the tool functions still clamp values defensively.

| Tool | Parameters | Description |
|---|---|---|
| `list_files_in_vault` | none | List every file and folder |
| `list_files_in_dir` | `input: {dirpath}` | List one directory |
| `list_note_titles` | none | Markdown basenames; duplicate basenames are possible |
| `get_file_contents` | `input: {filepath}`, `include_etag=false`, `max_bytes=None` | Read UTF-8 text; optionally return ETag |
| `batch_get_file_contents` | `filepaths: list[str]`, `max_files=20`, `max_bytes=None`, `max_file_bytes=None` | Read multiple text notes with count and byte limits |
| `simple_search` | `query`, `context_length=100`, `max_results=20`, `max_matches_per_file=10`, `max_bytes=None` | Obsidian full-text search |
| `complex_search` | `query: dict`, `max_results=50`, `max_bytes=None` | JSONLogic metadata search |
| `search_and_summarize` | `query`, `max_sources=5`, `max_excerpts_per_source=3`, `context_length=100`, `max_bytes=None` | Search excerpts grouped by source |
| `create_note` | `input: {filepath}`, `content` | Create only if absent; conflict if it exists |
| `append_content` | `input: {filepath}`, `content`, `if_match=None` | Append to a note |
| `patch_content` | `filepath`, `operation`, `target_type`, `target`, `content`, `if_match=None` | Patch heading/block/frontmatter |
| `put_content` | `input: {filepath}`, `content`, `if_match=None` | Explicit create or replace |
| `delete_file` | `input: {filepath}`, `confirm`, `if_match=None` | Delete; `confirm` must be true |
| `get_vault_stats` | `max_files=500` | Note count, link density, tags, orphans |
| `check_connection` | none | Health, latency, circuit state |

### PATCH operations

`patch_content` uses the current JSON PATCH contract:

```json
{
  "targetType": "heading",
  "target": ["My Section"],
  "operation": "append",
  "content": "New text"
}
```

- `operation`: `append`, `prepend`, `replace`, or `delete`
- `target_type`: `heading`, `block`, or `frontmatter`
- For a `heading`, `target` is an array of heading texts from the top level down.
  The tool also accepts a plain string for a single heading, which is sent as a
  one-element array. Use an array such as `["Work", "Meetings"]` for nested
  headings. Use `[]` or `null` to target the document root.
- For `block` and `frontmatter`, `target` is a string (a block reference or a
  frontmatter key).
- For `frontmatter`, the payload is sent as the API's `value` field for
  `append`, `prepend`, and `replace`; `content` is parsed as JSON when possible
  (for example `"true"` becomes boolean `true`, `'["tag"]'` becomes a list).
  `delete` sends no payload.

### Conditional writes and ETags

```text
get_file_contents(include_etag=true) -> { content, etag }
put_content(..., if_match=etag)
append_content(..., if_match=etag)
patch_content(..., if_match=etag)
delete_file(..., if_match=etag)
```

`create_note` sends `If-None-Match: *`, so an existing note is never silently
overwritten. A failed precondition or HTTP 409 is converted to a `conflict`
error for create-note callers. These guarantees depend on the installed Obsidian
Local REST API version supporting conditional writes.

## Output limits

Large operations are bounded so they do not flood the model context, the
network, or process memory:

| Operation | Default | Maximum |
|---|---|---|
| Simple search results | 20 | 100 |
| Matches per search file | 10 | 50 |
| Batch file reads | 20 | 100 |
| Vault stats notes scanned | 500 | 5000 |
| Summary sources | 5 | 20 |
| Excerpts per source | 3 | 20 |
| JSONLogic results | 50 | 100 |
| Text returned by one file read | 1,000,000 bytes | 10,000,000 bytes |
| Total text returned by one batch | 2,000,000 bytes | 20,000,000 bytes |
| Search result data | 1,000,000 bytes | 10,000,000 bytes |
| Summary text | 500 characters | 500 characters |
| Summary data | 1,000,000 bytes | 10,000,000 bytes |

The byte limits are configurable with the `OBSIDIAN_MAX_*_BYTES` variables above
and can be overridden per call with `max_bytes` (and `max_file_bytes` for batch
reads). Limits apply to the returned `data` payload. Truncation is explicit:
responses include `returned_bytes`, `returned_characters`, `max_bytes`,
`bytes_truncated`, and `truncated`. File truncation lands on a valid UTF-8
boundary, so multibyte characters are never split. Text reads are streamed and
stop after the configured limit plus one sentinel byte instead of loading an
unbounded response into memory.

`simple_search` and `search_and_summarize` distinguish file truncation from
match truncation with `files_truncated`, `total_matches`, `matches_returned`,
and `matches_truncated`; `search_and_summarize` also reports
`summary_truncated`. `truncated` is true when files, matches, summary text, or
bytes were omitted. Batch responses also report `files_returned`, `max_files`,
`files_truncated`, and `file_content_truncated`. `get_vault_stats` reports
`notes_scanned`, `notes_read`, `read_failures`, `unread_files_count`,
`unread_files_sample`, and `truncated`. Malformed search entries are ignored by
`search_and_summarize` rather than being emitted as non-string excerpts.

For Python callers, `get_file_contents`, `get_file_contents_with_etag`, and
batch reads return a `str`-compatible `LimitedText` value. Its `.metadata`
attribute carries the truncation information; MCP tools convert it to plain
text in their responses. Existing string comparisons and slicing remain valid.

Search and write responses keep their original `data` shape where possible and
add metadata as top-level fields:

```json
{
  "success": true,
  "data": [ ...search results... ],
  "returned": 20,
  "files_truncated": true,
  "total_matches": 140,
  "matches_returned": 80,
  "matches_truncated": true,
  "returned_bytes": 91234,
  "returned_characters": 90120,
  "max_bytes": 1000000,
  "bytes_truncated": false,
  "truncated": true
}
```

`simple_search` and `complex_search` return the result list in `data`;
`append_content`, `patch_content`, and `put_content` return their message string
in `data` and the new `etag` at the top level. `search_and_summarize` keeps
`summary`, `source_count`, and its byte metadata inside `data` and adds grouped
`sources`; the same metadata is also mirrored at the top level.

## Error handling

Tools return one of these shapes:

```json
{ "success": true, "data": ... }
```

```json
{
  "success": false,
  "error": {
    "status": "conflict",
    "reason": "...",
    "circuit_state": "CLOSED",
    "failure_count": 0
  }
}
```

Common statuses:

| Status | Meaning |
|---|---|
| `invalid_path` | Absolute path, traversal, or invalid characters |
| `invalid_input` | Invalid query, patch operation, or target |
| `conflict` | Note exists or the resource is in a conflicting state (HTTP 409) |
| `precondition_failed` | A conditional write failed because the note changed (HTTP 412) |
| `not_found` | The requested note or folder does not exist (HTTP 404) |
| `unsupported_file_type` | Binary or non-UTF-8 file requested as text, or a binary write target |
| `authentication_failed` | The API rejected the configured credentials (HTTP 401) |
| `permission_denied` | The API key cannot access the requested resource (HTTP 403) |
| `proxy_authentication_required` | A proxy requires authentication (HTTP 407) |
| `rate_limited` | HTTP 429 persisted after retries |
| `server_error` | An HTTP 5xx response other than 503 |
| `service_unavailable` | Network retries exhausted, or HTTP 503 persisted after retries |
| `circuit_open` | Circuit breaker rejected the call |
| `failed` | Other API or unexpected failure |

Tool-level failures are returned as structured payloads rather than raising MCP
errors, so clients should inspect `success`. Malformed arguments that fail the
MCP/Pydantic schema are rejected by the framework before the tool runs.

## Reliability behavior

- **Retries:** read/search requests retry 429 and 503 responses and transient
  network errors up to three times with exponential backoff and jitter. Other
  HTTP errors and redirect loops are not retried. Mutating writes are
  deliberately not retried
  automatically, because replaying an append or PATCH after an ambiguous 503
  could duplicate content; use ETags and retry explicitly when appropriate.
- **Circuit breaker:** three consecutive service failures open the circuit for
  ten seconds; then one half-open probe is allowed. Only a successful service
  response closes it. HTTP 429 and 5xx failures count as service failures.
  Local validation, authentication/client errors, HTTP 404, and write conflicts
  (409/412) are neutral: they release a half-open probe but do not reset the
  failure count or close the breaker.
- **Per-file errors:** batch reads report expected per-file failures
  (unsupported type, 4xx) inline, and vault statistics count them in
  `read_failures`. Batch validation happens after `max_files` selection, so
  unselected paths are not normalized. Authentication failures, service
  outages, and unexpected exceptions propagate so an aggregate cannot report
  `success: true` while the API is rejecting credentials. Vault statistics stop
  scheduling reads after a service failure to avoid a retry storm.
- **Path safety:** absolute paths, drive-qualified paths, `..`, and encoded
  traversal (`%2e%2e`, `%252e%252e`) are rejected. Paths are percent-encoded
  before being placed in URLs. Literal percent sequences in filenames are
  preserved, so a file named `100%20.md` is sent as `100%2520.md` and is not
  mistaken for `100 .md`.
- **Binary safety:** only known text extensions are read or written through text
  tools. Images, PDFs, and invalid UTF-8 content are rejected instead of being
  decoded into corrupted text or overwritten by text writes. A UTF-8 BOM is
  preserved as U+FEFF so a read/write round-trip does not silently remove it.
  `delete_file` remains able to remove binary attachments when explicitly
  confirmed.
- **Lifecycle:** the shared `httpx.AsyncClient` is closed when the MCP server
  shuts down.

## Vault statistics and link resolution

`get_vault_stats` counts Markdown notes only and reports:

- total files (directory entries are excluded), total notes, notes scanned/read
- average link density, calculated only from successfully read notes
- most common tags (hyphenated and nested tags supported)
- orphaned notes and a sample, considering only successfully read notes
- unread notes separately via `unread_files_count` and `unread_files_sample`
- `truncated` when the vault is larger than `max_files`

Link resolution handles:

- `[[Note]]`, `[[Note.md]]`
- aliases: `[[Note|Alias]]`
- heading/block fragments: `[[Note#Heading]]`
- folder-qualified paths: `[[folder/Note]]`
- relative paths: `[[./Sibling]]`
- case-insensitive matching
- ambiguous basenames (all candidates are marked as linked)

Wiki-link targets are treated literally, so `[[100%20]]` resolves a note named
`100%20.md` rather than `100 .md`. A missing folder-qualified link such as
`[[missing/Note]]` does not fall back to an unrelated `Note.md`; basename
fallback is used only for bare targets. Fenced code blocks support closing
fences that are longer than the opening fence. The resolver intentionally
ignores embeds (`![[...]]`) and links inside fenced code blocks. See
[Known limitations](#known-limitations).

## Security considerations

- The API key grants full vault access. Treat it like a password.
- Keep `.env` out of version control. It is ignored by `.gitignore`.
- TLS verification is disabled by default because the Obsidian plugin uses a
  locally generated certificate. Prefer `OBSIDIAN_CA_BUNDLE` over disabling
  verification when possible.
- Logs include tool names, paths, status codes, and timings, but never API keys
  or note contents.
- This server has no multi-user authentication, role-based permissions, or rate
  limiting beyond the Obsidian API itself.

## Development

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
ruff check .
```

Tests use `respx` to mock the Obsidian API and do not require a real vault or
network access.

## Known limitations

- **Not production-ready.** It is a single-user, single-vault bridge.
- **No AI summarization.** `search_and_summarize` returns search excerpts; it
  does not call an LLM.
- **No caching or vault index.** Every operation talks to the Obsidian API.
- **Link resolution is best-effort.** Ambiguous basenames mark all candidates;
  frontmatter tags are not counted; inline code is not stripped; transclusions
  and Dataview-specific links are not modeled.
- **Statistics are bounded.** Large vaults are scanned up to `max_files`, and
  individual note reads stop at `OBSIDIAN_MAX_FILE_BYTES`; links or tags after
  that boundary are not counted.
- **Search responses are bounded after parsing.** File reads stream up to the
  configured limit, but JSON search responses must be parsed before their output
  can be byte-budgeted.
- **Tiny byte budgets cannot retain every metadata field.** A budget smaller
  than the minimal JSON envelope may omit some metadata; the top-level
  truncation flags remain the authoritative summary.
- **Invalid numeric environment values fall back to defaults.** Port, timeout,
  circuit, and concurrency settings are clamped rather than aborting startup;
  check logs if a configured value does not appear to take effect.
- **Text only.** Binary attachments are rejected; there is no upload/download
  tool.
- **No rate limiting or RBAC.**
- **Conditional writes depend on API support.** Older plugin versions may not
  implement `If-Match` / `If-None-Match`.
- **Legacy wrapper helpers are not exposed.** The periodic-note methods and the
  Dataview DQL `get_recent_changes` helper remain on the Python wrapper for
  compatibility, but they are not MCP tools and may not match newer plugin
  versions.
- **Tools return structured error payloads** rather than setting MCP `isError`;
  clients must inspect `success`.
- **Import package name.** The distribution is `mcp-obsidian-sindhutsundupalli`,
  but the import package is `mcp_obsidian`, which can collide with other
  Obsidian MCP packages installed in the same environment.

## License

MIT. See [LICENSE](LICENSE).
