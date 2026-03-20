---
name: Use MCPs (Model Context Protocol)
description: Guidelines on how to leverage the available MCP servers for incident triage.
---

# Use MCPs

The `ai-incident-triage` project makes heavily utilizes MCP (Model Context Protocol) servers to extend the AI agent's capabilities. When performing triage, you should prioritize using MCP tools over raw CLI commands.

### 1. `github-mcp-server`
- **Purpose:** Direct interaction with GitHub repositories.
- **Common Usage:**
  - Reading codebase context or configuration files (`values.yaml`, `.env`, manifests).
  - Investigating failed GitHub Action workflow logs (`read_file`, filtering CI/CD outputs).
  - Creating issues or updating PRs with triage summaries.

### 2. `linux-server`
- **Purpose:** Executing commands on remote target infrastructure.
- **Common Usage:**
  - Running Linux diagnostic commands (`df`, `top`, `journalctl`) on remote VMs.
  - Requires the `host_ip` parameter to target the specific instance experiencing the incident.

### 3. `gcloud` and `gke-oss`
- **Purpose:** Interacting with Google Cloud Platform and Kubernetes resources.
- **Common Usage:**
  - Using `gke-oss` tools to list GKE clusters, read Pod logs, or check ArgoCD statuses within the cluster.
  - Using `gcloud` tools to check billing, project configurations, and broad GCP resource states.

**Best Practice:** Always chain the output of one MCP server into another. For example, read an alert's context from GitHub, identify the `host_ip`, and pass that to the `linux-server` MCP to troubleshoot.
