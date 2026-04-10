---
name: NetBox MCP Server
description: CMDB and asset management lookups via NetBox.
---

# NetBox MCP Server

### Connection
- **URL:** `http://netbox-mcp-server.ai-agent.svc.cluster.local:8002/sse`
- **Transport:** SSE (Server-Sent Events)

### Available Tools
- `search_devices` — Search for devices by name, IP, or site.
- `get_device` — Get detailed info about a specific device.
- `search_ip_addresses` — Look up IP address assignments.

### Common Usage
- Correlating hostnames from alerts to IP addresses.
- Looking up device information, rack location, and site details.
- Identifying which team owns a particular asset.
- Cross-referencing alert targets with the CMDB inventory.
