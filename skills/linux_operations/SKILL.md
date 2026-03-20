---
name: Operate within Linux
description: Standard operating procedures for investigating and troubleshooting Linux endpoints.
---

# Operate within Linux

When diagnosing an issue on a Linux host (e.g., resource exhaustion, crashed services), use the following standard commands via the `linux-server` MCP `execute_command` tool.

### 1. Check Disk Space
Resource alerts often trigger due to full disks.
- **Command:** `df -h`
- Look for mounts (especially `/` and `/var`) that are at or near 100% capacity.
- To find the largest directories contributing to the issue, use: `du -ah -x / | sort -rh | head -20`

### 2. Check System Resources & Processes
If the alert is for high CPU or memory usage.
- **Command:** `top -b -n 1` or `ps aux --sort=-%mem | head -15`
- Identify the specific PID holding the resources.
- Run `free -h` to check available system memory.

### 3. Investigate Services
If a specific application or service has crashed.
- **Command:** `systemctl status <service-name>`
- Extract the immediate status and the last few lines of logs.
- To get deeper logs for the service: `journalctl -u <service-name> -n 100 --no-pager`

### 4. General System Logs
To check for kernel panics, OOM (Out of Memory) kills, or hardware errors.
- **Command:** `dmesg -T | tail -50`
- **Command:** `tail -n 100 /var/log/syslog` (or `/var/log/messages` on RHEL-based systems).
