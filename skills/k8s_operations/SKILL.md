---
name: Kubernetes Operations
description: Standard operating procedures for investigating and troubleshooting Kubernetes clusters.
---

# Kubernetes Operations

### Required MCPs
Refer to [`mcp/gcloud/SKILL.md`](mcp/gcloud/SKILL.md) for GCloud/GKE connection details and available tools.

When diagnosing an issue within a Kubernetes cluster (e.g., CrashLoopBackOff, OOMKilled, Pending pods), use the following standard commands via the `gke-oss` or `gcloud` MCP tools.

### 1. Check Pod Status
Alerts often trigger when pods are not in the 'Running' state.
- **Command:** `kubectl get pods -A`
- **Command:** `kubectl describe pod <pod-name> -n <namespace>`
- Look for `Events` at the bottom of the describe output for immediate errors.

### 2. Retrieve Pod Logs
If a pod is crashing or misbehaving.
- **Command:** `kubectl logs <pod-name> -n <namespace> --tail=100`
- If the pod has multiple containers, specify: `kubectl logs <pod-name> -c <container-name> -n <namespace>`
- For previously crashed containers: `kubectl logs <pod-name> -n <namespace> -p`

### 3. Check Node Resources
If multiple pods are failing or in 'Pending' state.
- **Command:** `kubectl get nodes`
- **Command:** `kubectl top nodes`
- **Command:** `kubectl describe node <node-name>`
- Look for `Conditions` (e.g., DiskPressure, MemoryPressure).

### 4. Investigate Events
To see cluster-wide issues.
- **Command:** `kubectl get events -A --sort-by='.lastTimestamp' | tail -n 50`

### 5. Check Services & Endpoints
If pods are running but not reachable.
- **Command:** `kubectl get svc -A`
- **Command:** `kubectl get endpoints <service-name> -n <namespace>`
