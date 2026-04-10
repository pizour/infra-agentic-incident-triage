---
name: Investigate SSH
description: How to extract target VM IP addresses and investigate incidents via SSH.
---

# Investigate SSH

### Required MCPs
- [`mcp/linux-server/SKILL.md`](mcp/linux-server/SKILL.md) — for executing SSH commands on the target host.
- [`mcp/netbox/SKILL.md`](mcp/netbox/SKILL.md) — for resolving hostnames to IP addresses if `host_ip` is not provided.

When an alert triggers (e.g., from Grafana) regarding a specific Virtual Machine or remote host, the agent must investigate the target VM directly using SSH. 

Follow these steps to correctly access and investigate the target host:

1. **Extract the Target IP:**
   - Review the incident context or alert payload.
   - Look for the target VM's IP address, often passed as the `host_ip` parameter in the alert metadata.

2. **Execute Remote Commands:**
   - Use the `execute_command` tool provided by the `linux-server` MCP.
   - Pass the extracted `host_ip` as the `host` parameter for all remote tool calls.
   - Example command to verify connectivity: `uptime` or `uname -a`.

3. **Troubleshoot Connectivity:**
   - If the connection fails or hangs, verify that the SSH daemon is running on the target and that the AI agent's environment has network access to the `host_ip`.
   - Ensure the correct context is being used (e.g., VPN or bastion host routing if applicable).
