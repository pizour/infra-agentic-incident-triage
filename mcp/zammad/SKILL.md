---
name: Zammad MCP Server
description: Create and manage incident tickets in Zammad.
---

# Zammad MCP Server

### Connection
- **URL:** `http://zammad-mcp-server.ai-agent.svc.cluster.local:8006/sse`
- **Transport:** SSE (Server-Sent Events)

### Available Tools
- `create_ticket` — Create a new incident ticket.
- `update_ticket` — Update an existing ticket with new info.
- `search_tickets` — Search for existing tickets.

### Common Usage
- Creating incident tickets with investigation summaries and analysis reports.
- Updating existing tickets with new findings from ongoing investigations.
- Categorising incidents by risk level and severity.

### Parameters
- `title` — Short summary of the incident.
- `body` — Detailed description including evidence and analysis.
- `group` — Ticket group/queue for routing.
- `priority` — Incident priority level.
