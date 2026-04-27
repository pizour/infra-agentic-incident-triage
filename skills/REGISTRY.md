---
name: Skills Registry
description: Skill registry — names, descriptions, and file paths. Read this once; read individual skill files only when a skill is needed.
---

# Skills Registry

| skill | description | file |
|-------|-------------|------|
| `agent_output_contract` | Required response format (fields, types, scoring) that every agent must follow when returning results. | `skills/agent_output_contract/skill.md` |
| `input_guardrail` | Input validation: injection detection, schema validation, topic checks, and PII masking. | `skills/input-guardrail/skill.md` |
| `nexus_routing` | Nexus Controller routing rules, quality thresholds, retry logic, and source-specific pipelines. | `skills/nexus_routing/skill.md` |
| `parse_grafana_alerts` | How to parse and extract key fields from Grafana webhook alert payloads. | `skills/parse_grafana_alerts/SKILL.md` |
| `investigate_ssh` | How to establish SSH connectivity to a target VM and verify reachability before running commands. | `skills/investigate_ssh/SKILL.md` |
| `linux_operations` | Standard diagnostic commands for Linux hosts: disk, CPU, memory, services, and logs. | `skills/linux_operations/SKILL.md` |
| `k8s_operations` | Standard procedures for investigating Kubernetes clusters: pods, nodes, services, and events. | `skills/k8s_operations/SKILL.md` |
| `ticket_creation` | SOP for building and filing Zammad incident tickets from investigation evidence, including priority mapping and error handling. | `skills/ticket_creation/SKILL.md` |
