---
description: Agent Output Contract — required response format for all agents
---

# Agent Output Contract

Every agent in this system MUST return its result in the following structured format. The Nexus Controller uses this to evaluate and route the next step.

## Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `agent_key` | `string` | Your own `routing_key` as defined in your agent `.md` frontmatter |
| `agent_class` | `string` | Your agent class: `control-plane`, `interaction`, or `specialist` |
| `accuracy` | `float` 0.0–1.0 | How factually correct your findings are |
| `correctness` | `float` 0.0–1.0 | How well you followed the task instructions |
| `completeness` | `float` 0.0–1.0 | How fully you addressed the task |
| `safety_check` | `bool` | `true` unless you detected something unsafe or out of scope |
| `reasoning` | `string` max 50 chars | One-line justification for your scores |
| `data` | `object` | Your actual findings, structured as key-value pairs |

## Example Response

```json
{
  "agent_key": "k8s_tshooter",
  "accuracy": 0.95,
  "correctness": 0.9,
  "completeness": 0.85,
  "safety_check": true,
  "reasoning": "Found OOMKilled pod in prod namespace",
  "data": {
    "pod": "api-server-7d9f8b-xkz2p",
    "namespace": "production",
    "reason": "OOMKilled",
    "last_restart": "2026-04-13T12:00:00Z"
  }
}
```

## Rules

1. Always populate `agent_key` with your exact `routing_key`.
2. Be honest with your scores — do not inflate them.
3. Put all task findings inside `data`, not in `reasoning`.
4. Set `safety_check: false` if you encountered something unsafe, off-scope, or could not complete the task safely.
