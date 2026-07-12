# MCP Obsidian Server

An [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that allows LLMs to interact with your Obsidian vault via the [Local REST API](https://github.com/codemirror/obsidian-local-rest-api).

This server enables AI agents (like Claude Desktop and Cursor) to read, write, search, and analyze your personal knowledge base with built-in stability features like circuit breakers and exponential backoff.

## 🚀 Features
*   **Full File Management**: List, read, create, append, and patch notes.
*   **Powerful Search**: Supports both simple text queries and complex JSON-logic searches.
*   **Vault Analytics**: Tools to calculate link density, identify orphaned notes, and track top tags.
*   **Resilience**: 
    *   **Retry Logic**: Exponential backoff with jitter for transient network errors.
    *   **Circuit Breaker**: Automatically pauses requests if the Obsidian API becomes unreachable to prevent resource exhaustion.

## 📋 Prerequisites
1.  **Obsidian** must be open with the vault you wish to access.
2.  **Local REST API Plugin**:
    *   Install it via Obsidian Community Plugins.
    *   Enable the plugin and toggle "Enable Northbound" if required.
    *   Copy your **API Key** from the plugin settings.
    *   Note your port (default is `27124`).

## 🛠 Installation

### For Claude Desktop (Recommended)
The easiest way to run this server in Claude Desktop is using `uvx`. This automatically fetches the latest version directly from PyPI and runs it securely in an isolated environment.

1. Open your Claude Desktop configuration file:
   - **Mac:** `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
2. Add the following configuration (replace `your_api_key_here` with your actual Obsidian REST API key):
```json
{
  "mcpServers": {
    "mcp-obsidian": {
      "command": "uvx",
      "args": [
        "mcp-obsidian-sindhutsundupalli"
      ],
      "env": {
        "OBSIDIAN_API_KEY": "your_api_key_here",
        "OBSIDIAN_HOST": "127.0.0.1",
        "OBSIDIAN_PORT": "27124",
        "OBSIDIAN_PROTOCOL": "http"
      }
    }
  }
}
```
3. Completely restart Claude Desktop.

### For Cursor / Terminal (Global Installation)
If you want to use the server in Cursor or from your command line, you can install it globally via pip:
```bash
pip install mcp-obsidian-sindhutsundupalli
```

Once installed, you can configure it in Cursor Settings (Features > MCP > Add New) using this exact command:
```bash
uvx --env OBSIDIAN_API_KEY=your_key_here mcp-obsidian-sindhutsundupalli
```

## 🧰 Available Tools
| Tool | Description |
| :--- | :--- |
| `list_files_in_vault` | Returns a list of all files in your vault. |
| `get_file_contents` | Fetches the full text content of a specific note. |
| `simple_search` | Searches the vault for a specific string. |
| `create_note` | Creates a new note with specified content. |
| `get_vault_stats` | Provides statistics on links, tags, and orphaned files. |
| `append_content` | Adds text to the end of an existing note. |
| `check_connection` | Diagnostic tool to verify the connection to Obsidian. |

## 🛡️ Safety & Reliability
This server implements a **Circuit Breaker** pattern. If the connection to Obsidian fails 3 times consecutively, the circuit opens for 10 seconds, immediately rejecting further calls to allow the service to recover. This prevents the LLM from getting stuck in a loop of failed requests.

## 📝 License
MIT - Developed by Siva Sindhuja
```


---
