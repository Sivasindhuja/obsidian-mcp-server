from __future__ import annotations

from mcp_obsidian.vault_stats import (
    build_path_index,
    compute_vault_stats,
    extract_tags,
    extract_wiki_links,
    resolve_wiki_link,
    strip_fenced_code,
)


def test_link_extraction_ignores_embeds_and_code_blocks():
    content = "[[A]] ![[Embedded]] `[[Inline]]`\n```\n[[Code]]\n```\n"
    cleaned = strip_fenced_code(content)

    # Fenced code is removed; inline code is intentionally not stripped in
    # this iteration and is documented as a limitation.
    assert extract_wiki_links(cleaned) == ["A", "Inline"]
    assert "Code" not in cleaned


def test_tag_extraction_supports_hyphens_and_nesting():
    tags = extract_tags("Text #simple #my-tag #nested/tag #123")

    assert tags == ["simple", "my-tag", "nested/tag", "123"]


def test_tag_extraction_ignores_url_fragments():
    assert extract_tags("https://example.com/#fragment #real") == ["real"]


def test_alias_heading_and_folder_links_resolve():
    exact, basenames = build_path_index(
        ["A.md", "B.md", "folder/C.md", "folder2/Note.md", "folder1/Note.md"]
    )

    assert resolve_wiki_link("B|Alias", "A.md", exact, basenames) == {"B.md"}
    assert resolve_wiki_link("B#Heading", "A.md", exact, basenames) == {"B.md"}
    assert resolve_wiki_link("folder/C", "A.md", exact, basenames) == {
        "folder/C.md"
    }
    assert resolve_wiki_link("folder/C.md", "A.md", exact, basenames) == {
        "folder/C.md"
    }
    # Ambiguous basenames mark every candidate as linked.
    assert resolve_wiki_link("Note", "A.md", exact, basenames) == {
        "folder1/Note.md",
        "folder2/Note.md",
    }


def test_relative_link_resolution():
    exact, basenames = build_path_index(["folder/A.md", "folder/B.md"])

    assert resolve_wiki_link("./B", "folder/A.md", exact, basenames) == {
        "folder/B.md"
    }


def test_explicit_relative_link_does_not_resolve_to_a_root_note():
    exact, basenames = build_path_index(
        ["Note.md", "folder/Note.md", "folder/A.md"]
    )

    assert resolve_wiki_link("./Note", "folder/A.md", exact, basenames) == {
        "folder/Note.md"
    }
    assert resolve_wiki_link("../Note", "folder/A.md", exact, basenames) == {
        "Note.md"
    }


def test_explicit_relative_link_missing_from_source_folder_is_unresolved():
    exact, basenames = build_path_index(["Note.md", "folder/A.md"])

    assert resolve_wiki_link("./Note", "folder/A.md", exact, basenames) == set()


def test_hidden_markdown_files_keep_their_leading_dot():
    exact, basenames = build_path_index([".hidden.md", "A.md"])

    assert resolve_wiki_link(".hidden", "A.md", exact, basenames) == {
        ".hidden.md"
    }


def test_stats_resolve_backlinks_and_do_not_report_false_orphans():
    files = ["A.md", "B.md", "C.md", "D.md", "folder/E.md"]
    contents = {
        "A.md": "[[B]]",
        "B.md": "[[C|Alias]] and [[folder/E#Heading]]",
        "C.md": "#tag #my-tag #nested/tag",
        "D.md": "```\n[[B]]\n```\n",
    }

    stats = compute_vault_stats(files, contents, max_files=10)

    assert stats["total_notes"] == 5
    assert stats["notes_scanned"] == 5
    assert stats["truncated"] is False
    assert stats["orphaned_files_count"] == 1
    assert stats["orphaned_files_sample"] == ["D.md"]
    assert ("tag", 1) in stats["top_tags"]
    assert ("my-tag", 1) in stats["top_tags"]
    assert ("nested/tag", 1) in stats["top_tags"]


def test_heavily_backlinked_note_is_not_orphaned():
    files = ["A.md", "B.md", "C.md"]
    contents = {
        "A.md": "[[B]]",
        "B.md": "no links",
        "C.md": "[[B]]",
    }

    stats = compute_vault_stats(files, contents, max_files=10)

    assert stats["orphaned_files_count"] == 0
    assert stats["orphaned_files_sample"] == []


def test_stats_scan_is_bounded_and_reports_truncation():
    files = ["A.md", "B.md", "C.md"]
    contents = {"A.md": "[[B]]", "B.md": "", "C.md": ""}

    stats = compute_vault_stats(files, contents, max_files=2)

    assert stats["total_notes"] == 3
    assert stats["notes_scanned"] == 2
    assert stats["truncated"] is True
