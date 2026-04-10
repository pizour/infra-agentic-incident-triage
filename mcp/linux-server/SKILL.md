---
name: Linux Server MCP
description: Execute commands on remote Linux hosts via SSH.
---

# Linux Server MCP

### Connection
- **URL:** `http://linux-mcp-server.ai-agent.svc.cluster.local:8001/sse`
- **Transport:** SSE (Server-Sent Events)

### Available Tools
- `execute_command` — Run a shell command on a remote host.

### Parameters
- `host` — IP address of the target VM (e.g., the `host_ip` from an alert).
- `command` — The shell command to execute.

### Common Usage
- Running diagnostic commands (`df -h`, `top -b -n 1`, `journalctl`, `dmesg`).
- Checking service status (`systemctl status <service>`).
- Investigating SSH logs (`grep sshd /var/log/auth.log`).
- Verifying connectivity (`uptime`, `uname -a`).

### Important
- Always pass the correct `host_ip` from the alert context as the `host` parameter.
- If connection fails, verify SSH daemon is running on the target and network access is available.
