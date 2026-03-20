# AI Incident Triage Agent

> [!WARNING]
> **This project is currently under heavy development and has only been partially tested.**

An AI-powered security monitoring and incident response agent. It automatically investigates system alerts (e.g., from Grafana) using specialized MCP tools, troubleshoots remote infrastructure, and creates detailed incident tickets.

## 🚀 Project Overview

The architecture is a multi-agent orchestrated system (using **LangGraph**) designed to replace manual Level 1 support. It automatically handles webhooks (e.g., from Grafana), investigates affected systems, and documents findings using local LLMs.

**Key Components:**
- **Orchestration & Agents:** A LangGraph orchestrator routing tasks between specialized agents (`investigation-agent`, `analysis-agent`, `ticket-agent`) powered by **Ollama** (Llama 3).
- **Safety & Guardrails:** Core agent APIs are secured by **NeMo Guardrails**.
- **MCP Servers:** Extends agent tooling with:
  - `mcp-server` (`linux-server`) for remote SSH and diagnostic operations.
  - `github-mcp-server` for CI/CD and repository context.
  - `netbox-mcp-server` to query the infrastructure source of truth.
- **Monitoring & Observability:** Grafana, Prometheus, Loki, and Promtail for system alerting, and **Arize Phoenix** for LLM tracing.
- **Ticketing & IPAM:** **Zammad** for automated ticket creation and **NetBox** for infrastructure management.

## 🏗️ Infrastructure & Deployment

The project leverages a robust modern GitOps workflow:
- **Infrastructure as Code (IaC):** Cloud resources and fundamental infrastructure are managed via **Pulumi**.
- **Pipelines:** **GitHub Actions** handles continuous integration and automated secret loading.
- **GitOps:** Continuous application delivery is managed by **ArgoCD**.
- **Package Management:** Core services and agents are structured as **Helm** charts for repeatable, configuration-driven deployments on Kubernetes.
