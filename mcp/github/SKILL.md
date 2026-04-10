---
name: GitHub MCP Server
description: Interact with GitHub repositories — read files, search code, create issues, and manage PRs.
---

# GitHub MCP Server

### Connection
- **URL:** `http://github-mcp-server.ai-agent.svc.cluster.local:8080/sse`
- **Transport:** SSE (Server-Sent Events)

### Available Tools
- `get_file_contents` — Read a file or list a directory from a repository.
- `search_code` — Search for code across repositories.
- `create_or_update_file` — Create or update a file in a repository.
- `create_issue` — Open a new issue.
- `list_issues` — List issues in a repository.
- `push_files` — Push multiple files in a single commit.

### Common Usage
- Reading codebase context, configuration files (`values.yaml`, `.env`, manifests).
- Investigating failed GitHub Action workflow logs.
- Creating issues or updating PRs with triage summaries.
- Reading skills and agent definitions from the repository.

### Parameters
- `repo_name` — Format: `owner/repo` (e.g., `pizour/infra-agentic-incident-triage`)
- `file_path` — Path to the file within the repository.
- `branch` — Branch name (default: `main`).
