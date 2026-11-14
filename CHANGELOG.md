# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/). This release is a beta.

## [0.3.0] - 2026-11-14

### Added

- Bounded text reads, batch reads, search results, and summaries with
  configurable UTF-8 byte budgets.
- Truncation metadata for files, batches, searches, and summaries.
- Typed error taxonomy for authentication, permissions, missing resources,
  conflicts, preconditions, rate limits, and server/service failures.
- Circuit-breaker handling for service failures with neutral handling for
  expected client and authentication errors.
- Structured JSON logging to stderr and graceful HTTP client shutdown.
- Vault analytics for note counts, link density, tags, and orphan detection.
- Conditional writes using `If-Match` and safe creation using `If-None-Match`.
- CI coverage for Python 3.11, 3.12, and 3.13.

### Fixed

- Vault listing now traverses subdirectories instead of returning only the
  vault root.
- MCP server metadata now advertises the package version instead of the
  FastMCP dependency version.
- Folder entries are no longer counted as files.
- Wiki-link resolution rejects unsafe path-like targets and no longer falls
  back to an unrelated basename.
- Redirect loops are no longer retried as transient network failures.
- Literal percent sequences in vault filenames are preserved and encoded once.
- UTF-8 BOMs are preserved across read/write round trips.
- Markdown closing fences longer than their opening fence are handled.
- Root-level PATCH targets are supported for heading operations.
- MCP tool schemas expose enums and numeric bounds.
- Invalid numeric environment settings fall back to safe defaults instead of
  crashing startup.
- Unexpected exceptions are logged with tracebacks; expected typed failures are
  not.
- Non-finite frontmatter JSON values are rejected instead of being serialized.
- Malformed search results are tolerated, and summary truncation metadata is
  retained where the byte budget allows.

### Security

- Path validation rejects absolute paths, drive-qualified paths, control
  characters, and encoded traversal.
- Binary attachments are not decoded as text or overwritten by text writes.
- Authentication and permission failures do not count as circuit-breaker
  failures.

### Verified

- GitHub Actions on Python 3.11, 3.12, and 3.13.
- Clean wheel install, `mcp-obsidian` console entry point, and MCP stdio
  lifecycle.
- Docker image non-root execution and MCP stdio lifecycle.
- Live disposable Obsidian vault with Local REST API 5.5.2: reads, search,
  create, update, append, PATCH, delete, and conditional writes.

### Known limitations

See the [Known limitations](README.md#known-limitations) section of the README.
