---
name: Parse Grafana Alerts
description: How to parse and extract key information from Grafana webhook alert payloads.
---

# Parse Grafana Alerts

### Required MCPs
- [`mcp/netbox/SKILL.md`](mcp/netbox/SKILL.md) — for resolving hostnames to IP addresses when `host_ip` is not in the payload.

When you receive a webhook payload from Grafana, follow these steps to extract actionable information.

### 1. Check Alert Status
- Look at `status` field: only process alerts with `status: "firing"`.
- If `status: "resolved"`, log it and take no further action.

### 2. Extract Alert Metadata
From each item in the `alerts[]` array:

| Field | Path | Description |
|-------|------|-------------|
| Alert name | `alerts[].labels.alertname` | Name of the alert rule |
| Severity | `alerts[].labels.severity` | e.g., `critical`, `warning`, `info` |
| Description | `alerts[].annotations.description` | Human-readable description |
| Summary | `alerts[].annotations.summary` | Short summary |

### 3. Extract Target Host Information
Look for the target host in this order of priority:

1. `alerts[].labels.host_ip` — direct IP (preferred)
2. `alerts[].labels.host` — hostname
3. `alerts[].labels.hostname` — hostname (alternative)
4. `alerts[].labels.instance` — format is usually `ip:port`, extract the IP by splitting on `:`

### 4. Extract Attacker / Source Info
For security alerts:
- `alerts[].labels.source_ip` — the attacker or source IP
- `alerts[].labels.user` — the user account involved

### 5. Example Grafana Payload
```json
{
  "status": "firing",
  "alerts": [
    {
      "status": "firing",
      "labels": {
        "alertname": "SSHBruteForce",
        "severity": "critical",
        "host": "web-server-01",
        "host_ip": "10.0.1.15",
        "source_ip": "192.168.1.100"
      },
      "annotations": {
        "description": "Multiple failed SSH login attempts detected",
        "summary": "SSH brute force attack on web-server-01"
      }
    }
  ]
}
```

### 6. Output
After parsing, pass the extracted context to the next agent in the pipeline with:
- `hostname` — resolved target host
- `host_ip` — IP to SSH into
- `source_ip` — attacker IP (if applicable)
- `alert_description` — what triggered the alert
- `severity` — how urgent it is
