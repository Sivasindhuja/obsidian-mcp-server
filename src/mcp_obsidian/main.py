"""Console entry point for the MCP server."""

from __future__ import annotations


def main() -> None:
    from mcp_obsidian.logging_config import configure_logging

    configure_logging()

    from mcp_obsidian.server import mcp

    mcp.run()


if __name__ == "__main__":
    main()
