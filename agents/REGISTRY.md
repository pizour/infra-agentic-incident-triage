---
description: Agent registry — routing keys, descriptions, and file paths. Read this once; read individual agent files only when an agent is selected.
---

# Agent Registry

| routing_key | description | file |
|-------------|-------------|------|
| `input_guardrail` | Validates and normalises incoming alert payloads against the Unified Input Schema. | `agents/control-plane/input-guardrail.md` |
| `vm_tshooter` | SSH-based deep investigation of Linux VMs (Compute Engine): system health, security, services, networking. | `agents/specialists/vm-tshooter.md` |
| `k8s_tshooter` | Kubernetes (GKE) investigation: pod status, node resources, service health, cluster events. | `agents/specialists/k8s-tshooter.md` |
| `create_ticket` | Creates Zammad incident tickets from investigation evidence with priority classification. | `agents/interaction/ticket-agent.md` |
