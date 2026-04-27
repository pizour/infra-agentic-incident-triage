---
name: Ticket Creation SOP
description: Standard operating procedure for creating Zammad incident tickets from investigation evidence.
---

# Ticket Creation SOP

### Required MCP
Refer to [`mcp/zammad/SKILL.md`](mcp/zammad/SKILL.md) for connection details and available tools.

## Workflow

### 1. Connect to Zammad MCP
Use `discover_mcp` to connect to the Zammad MCP server, then use `create_ticket` to file the incident.

### 2. Determine Priority
Map findings to a Zammad priority:

| Condition | Priority |
|-----------|----------|
| Active exploit, data exfiltration, or critical service down | `3 high` |
| Confirmed threat, degraded service, or active anomaly | `2 normal` |
| Suspicious activity, benign finding, or investigation inconclusive | `1 low` |

### 3. Build the Ticket Body
Construct the `body` field using this template:

```
## Incident Summary
<One paragraph describing what happened, which host was affected, and the alert source.>

## Evidence
<Bullet-point list of key findings from the investigation data field.>

## Timeline
- Alert received: <timestamp from input>
- Investigation completed: <current timestamp>

## Recommended Actions
<List of follow-up steps if any are needed.>
```

### 4. Call `create_ticket`

```python
# Example parameters for the Zammad create_ticket tool
params = {
    "title": f"[{priority_label}] {alert_type} on {hostname}",
    "body": ticket_body,          # constructed from template above
    "group": "Security",          # always use "Security" for triage alerts
    "priority": priority_value,   # "1 low" | "2 normal" | "3 high"
}
```

### 5. Handle Errors

```python
# If create_ticket fails (e.g. MCP unreachable), retry once.
# If it fails again, return the ticket body in your data output
# so it can be handled upstream:
data = {
    "ticket_created": False,
    "ticket_body": ticket_body,
    "error": "<error message>",
}
```

### 6. Return Output
On success, include in your `data`:
```json
{
  "ticket_created": true,
  "ticket_id": "<id returned by create_ticket>",
  "ticket_title": "<title used>",
  "priority": "<priority used>"
}
```
