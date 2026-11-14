"""Pure helpers for wiki-link resolution and vault statistics.

Keeping this logic separate from the MCP layer makes it testable without a
running Obsidian instance or FastMCP server.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping

_FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*$")
_WIKI_LINK_RE = re.compile(r"(?<!!)\[\[([^\]]+)\]\]")
_TAG_RE = re.compile(r"(?<![\w/#])#([A-Za-z0-9_][\w/-]*)")

_MARKDOWN_EXTENSIONS = (".md", ".markdown")


def is_markdown(path: str) -> bool:
    return path.lower().endswith(_MARKDOWN_EXTENSIONS)


def is_folder_entry(path: str) -> bool:
    """Return True for directory entries in a vault listing.

    The Local REST API includes directories in its ``files`` array with a
    trailing slash (for example ``somedirectory/``).  They are not files and
    must not be counted as such.
    """

    return path.replace("\\", "/").endswith("/")


def strip_fenced_code(content: str) -> str:
    """Remove fenced code blocks so links/tags inside them are not counted.

    A closing fence may be longer than the opening fence, as allowed by
    Markdown; comparing the fence lengths rather than requiring an exact
    backreference handles both backtick and tilde fences.
    """

    kept: list[str] = []
    fence_character: str | None = None
    fence_length = 0

    for line in content.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if fence_character is None:
            opening = _FENCE_OPEN_RE.match(body)
            if opening:
                marker = opening.group(1)
                fence_character = marker[0]
                fence_length = len(marker)
                continue
            kept.append(line)
            continue

        closing = _FENCE_CLOSE_RE.match(body)
        if (
            closing
            and closing.group(1)[0] == fence_character
            and len(closing.group(1)) >= fence_length
        ):
            fence_character = None
            fence_length = 0

    return "".join(kept)


def extract_wiki_links(content: str) -> list[str]:
    """Return raw wiki-link targets, excluding embeds (``![[...]]``)."""

    return [match.group(1) for match in _WIKI_LINK_RE.finditer(content)]


def extract_tags(content: str) -> list[str]:
    """Extract hashtags, including hyphenated and nested tags."""

    return [match.group(1) for match in _TAG_RE.finditer(content)]


def _link_target(raw_target: str) -> str:
    """Strip alias/heading fragments and normalise a wiki-link target.

    A leading ``./`` or ``../`` is preserved: it changes how the link is
    resolved, so stripping it here would let a root-level note shadow the
    explicitly relative target.
    """

    target = raw_target.split("|", 1)[0].split("#", 1)[0].strip()
    # Obsidian writes wiki links literally.  Do not URL-decode the target here:
    # a note named ``100%20.md`` is linked as ``[[100%20]]``, not ``[[100 ]]``.
    return target.replace("\\", "/").strip()


def _strip_markdown_extension(path: str) -> str:
    lower = path.lower()
    for extension in _MARKDOWN_EXTENSIONS:
        if lower.endswith(extension):
            return path[: -len(extension)]
    return path


def build_path_index(paths: Iterable[str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Build case-insensitive exact-path and basename indexes."""

    exact: dict[str, str] = {}
    basenames: dict[str, list[str]] = defaultdict(list)

    for path in paths:
        if not is_markdown(path):
            continue
        normalised = path.replace("\\", "/")
        while normalised.startswith("./"):
            normalised = normalised[2:]
        exact[normalised.lower()] = path
        basename = _strip_markdown_extension(
            normalised.rsplit("/", 1)[-1]
        ).lower()
        basenames[basename].append(path)

    return exact, dict(basenames)


def resolve_wiki_link(
    raw_target: str,
    source_path: str,
    exact_paths: Mapping[str, str],
    basename_paths: Mapping[str, list[str]],
) -> set[str]:
    """Resolve one wiki-link target to zero or more vault paths.

    Resolution order:
    1. an explicit relative path (``./`` or ``../``) from the source folder;
    2. exact vault path (with or without a Markdown extension);
    3. a same-folder relative path;
    4. a unique/ambiguous basename match for a bare target.

    Ambiguous basename matches return every candidate rather than falsely
    reporting a note as orphaned.  Path-like targets never fall back to a
    basename match.  Remaining limitations are documented in the README.
    """

    target = _link_target(raw_target)
    if not target or target in {".", ".."}:
        return set()
    if (
        target.startswith("/")
        or re.match(r"^[A-Za-z]:", target)
        or any(ord(character) < 32 for character in target)
    ):
        return set()

    source_dir = posixpath.dirname(source_path.replace("\\", "/"))

    # An explicitly relative target (``./Sibling`` or ``../Other/Note``) must
    # not fall back to a same-named note elsewhere in the vault.
    if target.startswith(("./", "../")):
        relative = posixpath.normpath(posixpath.join(source_dir, target))
        if relative.startswith("../") or relative == "..":
            return set()

        relative_candidates = [relative.lower()]
        if not relative.lower().endswith(_MARKDOWN_EXTENSIONS):
            relative_candidates.extend(
                relative.lower() + extension for extension in _MARKDOWN_EXTENSIONS
            )
        for candidate in relative_candidates:
            if candidate in exact_paths:
                return {exact_paths[candidate]}
        return set()

    # A path-like target that contains an upward segment is malformed unless it
    # was handled as an explicit relative link above.  Do not normalise it and
    # then fall back to a same-named note elsewhere in the vault.
    if any(part == ".." for part in target.split("/")):
        return set()

    lowered = target.lower()
    candidates = [lowered]
    if not lowered.endswith(_MARKDOWN_EXTENSIONS):
        candidates.extend(lowered + extension for extension in _MARKDOWN_EXTENSIONS)

    for candidate in candidates:
        if candidate in exact_paths:
            return {exact_paths[candidate]}

    # Relative folder-qualified link (e.g. ``Sibling`` in the same folder).
    relative = posixpath.normpath(posixpath.join(source_dir, target))
    if not relative.startswith("../") and relative != "..":
        relative_candidates = [relative.lower()]
        if not relative.lower().endswith(_MARKDOWN_EXTENSIONS):
            relative_candidates.extend(
                relative.lower() + extension for extension in _MARKDOWN_EXTENSIONS
            )
        for candidate in relative_candidates:
            if candidate in exact_paths:
                return {exact_paths[candidate]}

    # Basename fallback is only safe for a bare link target.  A missing
    # folder-qualified target such as ``missing/Note`` must not silently
    # resolve to ``elsewhere/Note.md``.
    if "/" in target:
        return set()

    basename = _strip_markdown_extension(target).lower()
    matches = basename_paths.get(basename, [])
    return set(matches)


def compute_vault_stats(
    files: Iterable[str],
    contents: Mapping[str, str],
    *,
    max_files: int,
) -> dict:
    """Compute link density, tags, and orphans for scanned notes."""

    files = [path for path in files if not is_folder_entry(path)]
    notes = [path for path in files if is_markdown(path)]
    scanned = notes[: max(1, int(max_files))]
    read_notes = [note for note in scanned if isinstance(contents.get(note), str)]
    unread_notes = [note for note in scanned if not isinstance(contents.get(note), str)]

    exact_paths, basename_paths = build_path_index(notes)

    outgoing_links: dict[str, set[str]] = defaultdict(set)
    incoming_links: dict[str, set[str]] = defaultdict(set)
    raw_outgoing_counts: dict[str, int] = defaultdict(int)
    tag_counter: Counter[str] = Counter()

    for note in read_notes:
        content = contents[note]
        cleaned = strip_fenced_code(content)
        raw_links = extract_wiki_links(cleaned)
        raw_outgoing_counts[note] = len(raw_links)

        for raw_link in raw_links:
            for resolved in resolve_wiki_link(
                raw_link, note, exact_paths, basename_paths
            ):
                outgoing_links[note].add(resolved)
                incoming_links[resolved].add(note)

        tag_counter.update(extract_tags(cleaned))

    orphaned = []
    for note in read_notes:
        has_outgoing = bool(outgoing_links.get(note)) or raw_outgoing_counts.get(
            note, 0
        ) > 0
        has_incoming = bool(incoming_links.get(note))
        if not has_outgoing and not has_incoming:
            orphaned.append(note)

    total_links = sum(raw_outgoing_counts.values())
    avg_link_density = total_links / len(read_notes) if read_notes else 0.0

    return {
        "total_files": len(files),
        "total_notes": len(notes),
        "notes_scanned": len(scanned),
        "notes_read": len(read_notes),
        "read_failures": len(unread_notes),
        "unread_files_count": len(unread_notes),
        "unread_files_sample": unread_notes[:10],
        "truncated": len(notes) > len(scanned),
        "avg_link_density": round(avg_link_density, 2),
        "top_tags": tag_counter.most_common(10),
        "orphaned_files_count": len(orphaned),
        "orphaned_files_sample": orphaned[:10],
        "resolution_limitations": (
            "Embeds and links inside fenced code blocks are ignored. "
            "Inline code is not stripped. "
            "Ambiguous basename links mark all matching notes as linked. "
            "Frontmatter tags are not counted. Orphan detection only considers "
            "successfully read scanned notes. Unread notes are reported "
            "separately and are never classified as orphaned. Relative and "
            "folder-qualified links are resolved on a best-effort basis."
        ),
    }
