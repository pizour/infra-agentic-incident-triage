---
name: GCloud & GKE MCP
description: Interact with Google Cloud Platform and GKE Kubernetes clusters.
---

# GCloud & GKE MCP

### Connection
- **URL:** Built-in MCP servers — no external URL needed.
- **Transport:** Native integration.

### Available Tools (gcloud)
- `run_gcloud_command` — Execute any gcloud CLI command.

### Available Tools (gke-oss)
- `list_clusters` — List GKE clusters.
- `get_cluster` — Describe a GKE cluster.
- `get_kubeconfig` — Get kubeconfig for a cluster.
- `query_logs` — Query GCP logs using LQL.

### Common Usage
- Listing and inspecting GKE clusters.
- Reading pod logs and cluster events.
- Checking project configurations and billing.
- Querying Cloud Logging for application and audit logs.
