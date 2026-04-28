# AI Incident Triage Agent — Project Context

## Purpose

An AI-powered security monitoring and incident response system that replaces manual Level 1 support. It automatically investigates system alerts (e.g., from Grafana), troubleshoots remote infrastructure via SSH and Kubernetes, and creates detailed incident tickets in Zammad.

## Architecture Overview

Multi-agent orchestrated system using **LangGraph** with specialized agents routed by a central controller. Follows a "dumb executor + smart controller" pattern: agents execute tasks, the Nexus Controller evaluates outputs and decides the next step.

### Core Flow

```
Grafana Alert → LangGraph-Fabric → Input Guardrail (validation - not yet in the path)
    → Nexus Controller (routing) → Specialist Agent (investigation)
    → Ticket Agent (Zammad incident creation) → Done
```

## Components

### Control Plane

| Component | Port | Role |
|-----------|------|------|
| **langgraph-fabric** | 8009 | FastAPI + LangGraph workflow engine; spawns agent pods dynamically via K8s API |
| **nexus-controller** | 8010 | Pydantic-AI controller; evaluates agent outputs against quality thresholds (accuracy/correctness/completeness >= 0.8), decides next_agent/retry/finish |
| **input-guardrail** | 8000 | Prompt injection detection, schema validation, topic relevance, PII masking |

### Specialist Agents (dynamically spawned pods)

| Agent | Routing Key | Purpose |
|-------|-------------|---------|
| **vm-tshooter** | `vm_tshooter` | Linux VM troubleshooting via SSH (Linux MCP) |
| **k8s-tshooter** | `k8s_tshooter` | Kubernetes troubleshooting via GCloud MCP |
| **analysis-agent** | `analyse` | Threat verdict: THREAT or BENIGN |
| **deep-agent** | `deep_investigate` | Generic multi-SOP investigator for ambiguous alerts |
| **ticket-agent** | `create_ticket` | Creates Zammad incident tickets with risk categorization |

### MCP Servers (Tool Access Layer)

| Server | Port | Tools |
|--------|------|-------|
| **linux-mcp-server** | 8001 | `execute_command` — SSH remote execution on target hosts |
| **netbox-mcp-server** | 8002 | `lookup_device`, `list_devices` — CMDB/asset queries |
| **github-mcp-server** | 8080 | `get_file_contents`, `list_directory_contents`, `search_code` — skills/SOP access |

### Supporting Services

| Service | Role |
|---------|------|
| **Zammad** | Incident ticketing (PostgreSQL + Redis + Elasticsearch) |
| **NetBox** | Infrastructure CMDB and IP/device inventory |
| **Langfuse** | LLM observability and tracing |
| **Prometheus + Grafana** | Metrics and dashboards |
| **Loki + Promtail** | Log aggregation |
| **ArgoCD** | GitOps continuous deployment |

## Tech Stack

- **Language**: Python 3.12
- **Frameworks**: FastAPI, Pydantic-AI, LangGraph, Pydantic
- **LLM Provider**: Google Vertex AI (gemini-2.5-flash)
- **Protocol**: MCP (Model Context Protocol) over SSE
- **Observability**: OpenTelemetry → Langfuse; Prometheus → Grafana
- **Infrastructure**: GKE on GCP, Pulumi (IaC), Helm charts, ArgoCD (GitOps)
- **CI/CD**: GitHub Actions → Google Artifact Registry → ArgoCD
- **Auth**: X-API-Key (inter-service), X-MCP-API-Key (MCP), GCP Workload Identity

## Key Data Contracts

### Agent Output Contract

Every agent returns this structure; the Nexus Controller evaluates it for routing decisions:

```json
{
  "agent_key": "vm_tshooter",
  "agent_class": "specialist",
  "accuracy": 0.95,
  "correctness": 0.90,
  "completeness": 0.85,
  "safety_check": true,
  "reasoning": "Found OOMKilled pod",
  "data": { /* investigation findings */ }
}
```

### Grafana Alert Payload (Webhook Input)

```json
{
  "status": "firing",
  "alerts": [{
    "labels": { "alertname": "SSHBruteForce", "severity": "critical", "host": "web-server-01" },
    "annotations": { "description": "Multiple failed SSH login attempts" }
  }]
}
```

### Nexus Controller Routing Decision

```json
{
  "action": "next_agent|retry|finish",
  "feedback": "instruction or summary",
  "target_agent": "vm_tshooter"
}
```

## Directory Structure

```
ai-agent-triage/
├── agents/                    # Agent definitions (YAML frontmatter + system prompts)
│   ├── control-plane/         # nexus-controller.md, input-guardrail.md
│   ├── interaction/           # ticket-agent.md
│   └── specialists/           # vm-tshooter.md, k8s-tshooter.md, analysis-agent.md, deep-agent.md
├── containers/                # Source code + Dockerfiles
│   ├── ai-agent/              # Generic agent container (main.py)
│   ├── langgraph-fabric/       # LangGraph orchestrator (main.py)
│   ├── nexus-controller/      # Nexus Controller (main.py)
│   ├── linux-mcp-server/      # SSH MCP (server.py)
│   └── netbox-mcp-server/     # CMDB MCP (server.py)
├── services/                  # Helm charts for all K8s deployments
│   ├── langgraph-fabric/
│   ├── ai-agent/
│   ├── nexus-controller/
│   ├── linux-mcp-server/
│   ├── netbox-mcp-server/
│   ├── github-mcp-server/
│   ├── zammad/
│   ├── netbox/
│   ├── monitoring/            # Prometheus, Grafana, Loki, Promtail
│   ├── langfuse/
│   └── argocd-apps/
├── skills/                    # SOPs fetched by agents at runtime via GitHub MCP
│   ├── nexus_routing/
│   ├── input-guardrail/
│   ├── agent_output_contract/
│   ├── investigate_ssh/
│   ├── linux_operations/
│   ├── k8s_operations/
│   └── parse_grafana_alerts/
├── mcp/                       # MCP server documentation
├── infrastructure/            # Pulumi IaC (GKE, VPC, IAM, Artifact Registry)
└── .github/workflows/         # CI/CD pipelines
```

## Service Communication

```
┌─────────────┐   webhook    ┌──────────────────┐
│   Grafana    │────────────→│  LangGraph-Fabric  │
└─────────────┘              │  (LangGraph)      │
                             └────────┬─────────┘
                                      │ spawns pods / HTTP calls
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                  ▼
           ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
           │Input Guardrail│  │Nexus Controller│ │Specialist Agent│
           └──────────────┘  └──────────────┘  └───────┬──────┘
                                                        │ MCP (SSE)
                                          ┌─────────────┼─────────────┐
                                          ▼             ▼             ▼
                                    ┌──────────┐ ┌──────────┐ ┌──────────┐
                                    │Linux MCP │ │NetBox MCP│ │GitHub MCP│
                                    │(SSH)     │ │(CMDB)    │ │(Skills)  │
                                    └──────────┘ └──────────┘ └──────────┘
```

## Deployment

- **Cloud**: GCP (europe-west4 region)
- **Cluster**: GKE managed by Pulumi
- **Namespaces**: `ai-agent` (core), `monitoring`, `zammad`, `netbox`, `argocd`
- **Images**: Built via GitHub Actions → Google Artifact Registry
- **Delivery**: ArgoCD watches this repo, syncs Helm charts to GKE
- **Secrets**: GitHub Actions secrets → `kubectl create secret` (ai-agent-secrets, linux-mcp-server-secrets, etc.)

## Design Patterns

- **Skill-driven agents**: SOPs stored as markdown in `skills/`, fetched at runtime via GitHub MCP — update procedures without redeploying
- **Dynamic pod spawning**: Orchestrator creates/destroys agent pods on-demand via K8s API
- **Structured evaluation loop**: Nexus Controller applies quality thresholds and can retry or reroute agents
- **Multi-stage Docker builds**: Builder + runtime stages for minimal images
- **Full observability stack**: Every LLM call traced via OpenTelemetry to Langfuse
