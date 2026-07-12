# MCP Obsidian Server

An MCP (Model Context Protocol) server that enables AI assistants to securely interact with an Obsidian vault through the Obsidian Local REST API.

The server exposes a collection of tools for searching, reading, creating, modifying, and analyzing notes while incorporating resilience patterns such as retry mechanisms, circuit breakers, and input validation.

> **Status:** Published on PyPI | Engineering Prototype

---

## Why This Project?

Modern AI assistants become significantly more useful when they can access a user's personal knowledge base.

Obsidian stores valuable notes, documentation, and project information, but interacting with it programmatically often requires direct API integration.

This project implements an MCP server that allows any MCP-compatible AI client to safely interact with an Obsidian vault through a standardized tool interface.

Beyond providing file operations, the project also explores engineering practices required for building reliable AI infrastructure, including fault tolerance, validation, and service health monitoring.

---

## Features

### Vault Operations

- List vault files
- Browse directories
- Read notes
- Read multiple notes in batch

---

### Search

- Keyword search
- Structured JSON search
- Search and summarize notes

---

###  Note Management

- Create notes
- Update notes
- Append content
- Patch markdown sections
- Delete notes

---

###  Vault Analytics

- Total notes
- Average link density
- Most common tags
- Orphaned notes detection

---

###  Health Monitoring

- Connection health check
- Latency reporting
- Circuit breaker status

---

###  Reliability Features

- Exponential backoff retries
- Circuit breaker
- Input validation
- Standardized success/error responses

---

## Supported MCP Tools

| Category | Tools |
|----------|------|
| File Operations | list_files_in_vault, list_files_in_dir, get_file_contents, batch_get_file_contents |
| Search | simple_search, complex_search, search_and_summarize |
| Note Management | create_note, append_content, put_content, patch_content, delete_file |
| Analytics | get_vault_stats |
| Health | check_connection |

---

# Architecture

```text
                 MCP Client
                      │
                      ▼
              FastMCP Server
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
 Validation     Circuit Breaker    Retry Layer
        │             │             │
        └─────────────┼─────────────┘
                      ▼
             Obsidian API Wrapper
                      │
                      ▼
         Obsidian Local REST API
                      │
                      ▼
               Obsidian Vault
```

---

# Repository Structure

```text
MCP/

├── Dockerfile
├── pyproject.toml
├── requirements.txt
├── README.md
│
└── src/
    └── mcp_obsidian/
        ├── server.py
        ├── APIWrapper.py
        ├── main.py
        └── test_client.py
```

---

# Getting Started

## Clone the Repository

```bash
git clone https://github.com/<username>/MCP.git

cd MCP
```

---

## Install

```bash
pip install -r requirements.txt
```

or

```bash
pip install .
```

---

## Environment Variables

Create a `.env` file.

```env
OBSIDIAN_API_KEY=

OBSIDIAN_HOST=127.0.0.1

OBSIDIAN_PROTOCOL=https

OBSIDIAN_PORT=27124
```

---

## Run

```bash
python -m mcp_obsidian
```

---

## Docker

Build

```bash
docker build -t mcp-obsidian .
```

Run

```bash
docker run \
-e OBSIDIAN_API_KEY=... \
mcp-obsidian
```

---

# Tech Stack

| Category | Technology |
|----------|------------|
| Language | Python 3.11 |
| MCP Framework | FastMCP |
| HTTP Client | httpx |
| Validation | Pydantic |
| Configuration | python-dotenv |
| Packaging | PyPI |
| Containerization | Docker |

---

# Reliability Features

Unlike a basic API wrapper, this project includes several mechanisms designed to improve robustness.

### Retry with Exponential Backoff

Transient failures such as network interruptions or HTTP 429/503 responses are retried automatically using exponential backoff with jitter.

---

### Circuit Breaker

Repeated failures temporarily open the circuit to prevent unnecessary API requests until the service recovers.

---

### Input Validation

Pydantic models validate incoming requests before interacting with the Obsidian API.

---

### Health Checks

The server exposes a health endpoint that reports:

- connection status
- latency
- vault size
- circuit breaker state

---

# Current Limitations

- Requires the Obsidian Local REST API plugin
- Designed for a single vault
- No authentication beyond the Obsidian API key
- No caching layer
- No role-based permissions

---

# Future Improvements

- Vault indexing
- Semantic search
- Embedding support
- Streaming responses
- Metrics dashboard
- Multi-vault support
- Authentication middleware

---

# Published Package

The server is available as a PyPI package.

```bash
pip install mcp-obsidian-sindhutsundupalli
```

---

# Documentation

Detailed documentation covering the architecture, engineering decisions, and implementation details will be published separately.
