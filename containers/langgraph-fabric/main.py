import os
import json
import httpx
import time
import asyncio
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from typing_extensions import TypedDict
from dotenv import load_dotenv
from kubernetes import client, config

from loguru import logger
import logging
import base64

# --- Logging Filter to suppress /health logs ---
class HealthCheckFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "/health" not in record.getMessage()

def setup_logging():
    # Attempt to add filter to uvicorn access logger
    for name in ["uvicorn.access", "uvicorn"]:
        l = logging.getLogger(name)
        l.addFilter(HealthCheckFilter())

load_dotenv()

# ── OpenTelemetry ─────────────────────────────────────────────────────────────
from opentelemetry import trace, propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import attach, detach
from prometheus_fastapi_instrumentator import Instrumentator
from openinference.instrumentation.langchain import LangChainInstrumentor
ENVIRONMENT = os.getenv("DEPLOYMENT_ENVIRONMENT")

resource = Resource.create({
    SERVICE_NAME: "langgraph-fabric",
    "openinference.project.name": "ai-agent-triage",
    "deployment.environment": ENVIRONMENT,
})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

# W3C TraceContext propagator — enables traceparent header injection into outgoing httpx calls
propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()]))

# Instrument LangChain/LangGraph FIRST — adds OpenInference attributes before export
LangChainInstrumentor().instrument()

# Langfuse OTLP Export (via BatchSpanProcessor — non-blocking)
langfuse_host = os.getenv("LANGFUSE_HOST")
langfuse_pk = os.getenv("LANGFUSE_PUBLIC_KEY")
langfuse_sk = os.getenv("LANGFUSE_SECRET_KEY")
if langfuse_pk and langfuse_sk:
    langfuse_auth = base64.b64encode(f"{langfuse_pk}:{langfuse_sk}".encode()).decode()
    langfuse_exporter = OTLPSpanExporter(
        endpoint=f"{langfuse_host}/api/public/otel/v1/traces",
        headers={
            "Authorization": f"Basic {langfuse_auth}",
            "x-langfuse-ingestion-version": "4",
        },
    )
    provider.add_span_processor(BatchSpanProcessor(langfuse_exporter))
    logger.info(f"Langfuse OTLP exporter initialized: {langfuse_host}")
else:
    logger.warning("Langfuse credentials not set — OTLP export disabled")

HTTPXClientInstrumentor().instrument()
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from langgraph.graph import StateGraph, END

# --- Configuration ---
NAMESPACE = os.getenv("NAMESPACE")
NEXUS_API = os.getenv("NEXUS_API")
INPUT_GUARDRAIL_API = os.getenv("INPUT_GUARDRAIL_API")
GENERIC_AGENT_IMAGE = os.getenv("GENERIC_AGENT_IMAGE")

# Initialize Kubernetes client
try:
    config.load_incluster_config()
except:
    config.load_kube_config()

core_v1 = client.CoreV1Api()

# ── FastAPI app ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("LangGraph-Fabric refined logging and connectivity initialized")
    yield
    logger.info("Flushing OpenTelemetry spans...")
    provider.force_flush(timeout_millis=5000)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="LangGraph Fabric", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
Instrumentator().instrument(app).expose(app)

# ══════════════════════════════════════════════════════════════════════════════
# LangGraph State Machine
# ══════════════════════════════════════════════════════════════════════════════



class CompleteState(TypedDict):
    """Single shared state threaded through every LangGraph node."""
    input: str
    context: Optional[dict]
    results: Dict[str, Any]
    validation_history: List[dict]
    latest_validation: Optional[dict]
    next_action: str
    next_agent: Optional[str]
    next_instructions: str
    retry_count: int  # Track retries; max 3 attempts per agent
    agent_env_vars: Optional[dict]  # env_vars from agent .md frontmatter, set by nexus-controller
    spawned_pods: List[str]  # pod names to clean up at end of workflow


# ── Kubernetes Pod Lifecycle Helper ───────────────────────────────────────────

async def run_agent_pod(agent_id: str, prompt: str, env_vars: Dict[str, str], system_prompt: Optional[str] = None) -> tuple:
    pod_name = f"agent-{agent_id.replace('_', '-')}-{int(time.time())}"
    port = 8000
    logger.info(f"SPAWNING POD: {pod_name} for agent={agent_id}")
    
    # Forward env vars from the orchestrator to the spawned pod (mirrors Helm values.yaml env section)
    forwarded_keys = [
        "LLM_MODEL",
        "GUARDRAILS_URL", "ZAMMAD_URL", "LANGFUSE_HOST",
        "GITHUB_MCP_URL", "GITHUB_REPO",
        "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
    ]
    static_env = {k: v for k, v in ((k, os.getenv(k)) for k in forwarded_keys) if v is not None}
    if NAMESPACE is not None:
        static_env["NAMESPACE"] = NAMESPACE
    # Override/extend with caller-provided env vars
    static_env.update(env_vars)

    env = [client.V1EnvVar(name=k, value=str(v)) for k, v in static_env.items()]

    # Mount all secrets from ai-agent-secrets (same keys as Helm secrets section)
    secret_keys = [
        "MCP_API_KEY", "NETBOX_MCP_API_KEY",
        "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
        "ZAMMAD_TOKEN", "ZAMMAD_USER", "ZAMMAD_PASS",
        "GH_PERSONAL_ACCESS_TOKEN",
        "GH_OAUTH_APP_ID", "GH_OAUTH_CLIENT_ID", "GH_OAUTH_CLIENT_SECRET",
        "GH_OAUTH_PRIVATE_KEY", "GH_OAUTH_INSTALLATION_ID",
    ]
    for key in secret_keys:
        env.append(client.V1EnvVar(
            name=key,
            value_from=client.V1EnvVarSource(
                secret_key_ref=client.V1SecretKeySelector(name="ai-agent-secrets", key=key, optional=True)
            )
        ))

    # Service account with Workload Identity for GCP auth — must match the ai-agent Helm release SA
    # Run: kubectl get sa -n ai-agent  to find the correct name, then set AGENT_SERVICE_ACCOUNT
    agent_sa = os.getenv("AGENT_SERVICE_ACCOUNT")
    pod_spec_kwargs = {"service_account_name": agent_sa} if agent_sa else {}

    pod = client.V1Pod(
        api_version="v1",
        kind="Pod",
        metadata=client.V1ObjectMeta(name=pod_name, labels={"app": "ai-agent", "agent-type": agent_id}),
        spec=client.V1PodSpec(
            **pod_spec_kwargs,
            containers=[
                client.V1Container(
                    name="agent",
                    image=GENERIC_AGENT_IMAGE,
                    ports=[client.V1ContainerPort(container_port=port)],
                    env=env,
                    readiness_probe=client.V1Probe(
                        http_get=client.V1HTTPGetAction(path="/", port=port),
                        initial_delay_seconds=2,
                        period_seconds=1
                    )
                )
            ],
            restart_policy="Never"
        )
    )
    
    core_v1.create_namespaced_pod(namespace=NAMESPACE, body=pod)

    pod_ip = None
    for i in range(60):
        curr_pod = core_v1.read_namespaced_pod_status(name=pod_name, namespace=NAMESPACE)
        if curr_pod.status.pod_ip and any(c.ready for c in (curr_pod.status.container_statuses or [])):
            pod_ip = curr_pod.status.pod_ip
            break
        if i % 5 == 0:
            logger.debug(f"Waiting for pod {pod_name}...")
        await asyncio.sleep(1)

    if not pod_ip:
        raise Exception(f"Pod {pod_name} failed to become ready.")

    # Wait for app to fully initialize (Langfuse, model setup) after readiness probe passes
    logger.info(f"POD READY: {pod_name} — waiting 30s for app initialization...")
    await asyncio.sleep(30)

    agent_url = f"http://{pod_ip}:{port}/agent"
    logger.info(f"CALLING AGENT: {agent_id} at {agent_url}")
    body: Dict[str, Any] = {"prompt": prompt}
    if system_prompt:
        body["system_prompt"] = system_prompt
    async with httpx.AsyncClient(timeout=300.0) as client_http:
        resp = await client_http.post(agent_url, json=body)
        logger.info(f"AGENT {agent_id} HTTP {resp.status_code}")
        if not resp.is_success:
            logger.error(f"AGENT {agent_id} error body: {resp.text[:500]}")
            return pod_name, json.dumps({"agent_key": agent_id, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"})
        return pod_name, resp.json().get("result", "")


# ── Node: Input Guardrail ─────────────────────────────────────────────────────
async def node_input_guardrail(state: CompleteState) -> dict:
    """First node: validate raw input via the stationary input-guardrail service.
    On rejection, set next_action='finish' so node_nexus_controller short-circuits to cleanup."""
    logger.info("NODE: input_guardrail - validating input")
    with tracer.start_as_current_span("langgraph-fabric.node.input_guardrail") as span:
        span.set_attribute("langfuse.observation.type", "span")
        payload = {
            "input": state["input"],
            "context_summary": str(state.get("context", {})),
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)) as client:
            try:
                response = await client.post(INPUT_GUARDRAIL_API, json=payload)
                response.raise_for_status()
                data = response.json()
                safety_check = bool(data.get("safety_check"))
                feedback = data.get("feedback", "")
                masked_input = data.get("masked_input")

                span.set_attribute("langfuse.observation.output", json.dumps({
                    "safety_check": safety_check,
                }))
                logger.info(f"GUARDRAIL: safety_check={safety_check}")

                # Only the orchestrator translates a guardrail rejection into a
                # 'finish' action; the guardrail itself never specifies actions.
                if not safety_check:
                    return {
                        "next_action": "finish",
                        "next_instructions": feedback,
                    }
                # Pass: forward (optionally PII-masked) input to nexus_controller
                return {"input": masked_input if masked_input else state["input"]}
            except Exception as e:
                logger.error(f"INPUT GUARDRAIL: call failed: {e}. Failing closed.")
                span.record_exception(e)
                return {"next_action": "finish"}


# ── Node: Nexus Controller ──────────────────────────────────────────────────────────
async def node_nexus_controller(state: CompleteState) -> dict:
    # Short-circuit if upstream (input_guardrail) already decided to finish.
    if state.get("next_action") == "finish":
        logger.info("NODE: nexus_controller - upstream finish, passing through")
        return {}
    logger.info("NODE: nexus_controller - evaluating state")
    with tracer.start_as_current_span("langgraph-fabric.node.nexus_controller") as span:
        span.set_attribute("langfuse.observation.type", "span")
        latest = state.get("latest_validation")
        span.set_attribute("langfuse.observation.input", json.dumps({"latest_agent": latest.get("agent_key") if latest else None}))
        payload = {
            "input": state["input"],
            "context_summary": str(state.get("context", {})),
            "latest_validation": state.get("latest_validation")
        }
        # 90s total: nexus-controller does 2 LLM calls + 1 GitHub MCP call
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=200.0, write=10.0, pool=5.0)) as client:
            try:
                response = await client.post(NEXUS_API, json=payload)
                response.raise_for_status()
                data = response.json()
                action = data.get("action", "finish").lower()
                target_agent = data.get("target_agent")
                feedback = data.get("feedback", "")

                if action not in ["next_agent", "retry", "finish"]:
                    logger.warning(f"Unknown action from controller: {action}. Finishing.")
                    return {"next_action": "finish"}
                if "failed after 3 attempts" in feedback.lower():
                    logger.warning(f"Tool failure detected in feedback. Finishing.")
                    return {"next_action": "finish"}

                agent_env_vars = data.get("agent_env_vars") or {}
                span.set_attribute("langfuse.observation.output", json.dumps({"action": action, "target": target_agent}))
                logger.info(f"CONTROLLER DECISION: action={action}, target={target_agent}")
                return {"next_action": action, "next_agent": target_agent, "next_instructions": feedback, "agent_env_vars": agent_env_vars}
            except httpx.TimeoutException as e:
                logger.error(f"CONTROLLER TIMEOUT: nexus-controller exceeded timeout. Finishing workflow. {e}")
                span.set_attribute("timeout", True)
                return {"next_action": "finish"}
            except Exception as e:
                logger.error(f"Controller call failed: {e}")
                span.record_exception(e)
                return {"next_action": "finish"}

# ── Node: Executor ────────────────────────────────────────────────────────────
async def node_executor(state: CompleteState) -> dict:
    """Dumb executor: spawn the agent pod nexus-controller asked for, store raw result."""
    if state.get("next_action") not in ["next_agent", "retry"]:
        return {}

    agent_id = state.get("next_agent")
    if not agent_id:
        logger.error("No target_agent provided by Controller!")
        return {"next_action": "finish"}

    # Track retry attempts for this agent
    current_retry = state.get("retry_count", 0)
    last_agent = state.get("validation_history", [])[-1].get("agent_key") if state.get("validation_history") else None

    # Reset counter if this is a new agent request (not a retry)
    if agent_id != last_agent:
        current_retry = 0

    current_retry += 1

    # Check max retries (3 attempts per agent)
    if current_retry > 3:
        logger.warning(f"AGENT {agent_id} exceeded max retries (3). Finishing.")
        return {"next_action": "finish", "retry_count": current_retry}

    logger.info(f"NODE: executor - forwarding to agent={agent_id} (attempt {current_retry}/3)")
    with tracer.start_as_current_span(f"langgraph-fabric.node.execute.{agent_id}") as span:
        span.set_attribute("langfuse.observation.type", "span")
        span.set_attribute("langfuse.observation.metadata.agent_id", str(agent_id))
        span.set_attribute("langfuse.observation.metadata.attempt", str(current_retry))
        span.set_attribute("agent_id", str(agent_id))
        span.set_attribute("attempt", current_retry)
        prompt = str(state.get("next_instructions") or state["input"])

        agent_env_vars = dict(state.get("agent_env_vars") or {})
        system_prompt = agent_env_vars.pop("SYSTEM_PROMPT", None)

        pod_name = None
        try:
            pod_name, result_str = await run_agent_pod(str(agent_id), prompt, agent_env_vars, system_prompt=system_prompt)
        except Exception as e:
            logger.error(f"Pod failed for agent {agent_id}: {e}")
            span.record_exception(e)
            result_str = json.dumps({"agent_key": str(agent_id), "error": str(e)[:200]})

        # Store raw result — no parsing, no scoring. Nexus-controller evaluates.
        history = list(state.get("validation_history") or [])
        results = dict(state.get("results") or {})  # type: ignore[arg-type]
        pods = list(state.get("spawned_pods") or [])
        results[str(agent_id)] = result_str
        history.append({"agent_key": str(agent_id), "raw": result_str, "attempt": current_retry})
        if pod_name:
            pods.append(pod_name)

        return {
            "results": results,
            "latest_validation": {"agent_key": str(agent_id), "raw": result_str, "attempt": current_retry},
            "validation_history": history,
            "retry_count": current_retry,
            "spawned_pods": pods,
        }

# ── Node: Cleanup ─────────────────────────────────────────────────────────────
async def node_cleanup(state: CompleteState) -> dict:
    """Delete all spawned agent pods at the end of the workflow."""
    pods = state.get("spawned_pods") or []
    for pod_name in pods:
        try:
            logger.info(f"CLEANUP: deleting pod {pod_name}")
            core_v1.delete_namespaced_pod(name=pod_name, namespace=NAMESPACE, grace_period_seconds=0)
        except Exception as e:
            logger.warning(f"CLEANUP: could not delete pod {pod_name}: {e}")
    return {}

# ── Routing ───────────────────────────────────────────────────────────────────
def route_from_controller(state: CompleteState) -> str:
    """After nexus_controller decides: execute the suggested agent or finish."""
    action = state.get("next_action", "finish")
    if action == "finish":
        return "cleanup"
    return "execute"

# ── Graph compilation ─────────────────────────────────────────────────────────
def _build_graph() -> StateGraph:
    g = StateGraph(CompleteState)
    g.add_node("input_guardrail", node_input_guardrail)
    g.add_node("nexus_controller", node_nexus_controller)
    g.add_node("execute", node_executor)
    g.add_node("cleanup", node_cleanup)

    g.set_entry_point("input_guardrail")
    # Static edge: guardrail's only forward path is to nexus_controller.
    # On rejection (safety_check=false), node_input_guardrail sets next_action='finish';
    # nexus_controller short-circuits and route_from_controller routes to cleanup.
    g.add_edge("input_guardrail", "nexus_controller")
    g.add_conditional_edges(
        "nexus_controller",
        route_from_controller,
        {"execute": "execute", "cleanup": "cleanup"},
    )
    g.add_edge("execute", "nexus_controller")
    g.add_edge("cleanup", END)

    return g.compile()

workflow = _build_graph()


# ══════════════════════════════════════════════════════════════════════════════
# API Endpoints
# ══════════════════════════════════════════════════════════════════════════════

class TaskRequest(BaseModel):
    input: str
    context: Optional[dict] = None

@app.get("/health")
async def health_check():
    return {"status": "ok", "message": "LangGraph-Fabric Dynamic running"}

@app.post("/task")
async def run_task(task_request: TaskRequest, raw_request: Request):
    logger.info(f"TASK REQUEST: input='{task_request.input[:100]}...'")
    # Extract upstream trace context so all spans attach to the caller's trace
    upstream_ctx = propagate.extract(dict(raw_request.headers))
    token = attach(upstream_ctx)
    # Initialize LangGraph state
    initial_state: CompleteState = {
        "input": task_request.input,
        "context": task_request.context,
        "results": {},
        "validation_history": [],
        "latest_validation": None,
        "next_action": "",
        "next_agent": None,
        "next_instructions": "",
        "retry_count": 0,
        "agent_env_vars": None,
        "spawned_pods": [],
    }
    session_id = f"task-{int(time.time())}"
    try:
        with tracer.start_as_current_span("langgraph-fabric.task.execution") as span:
            # Langfuse trace attributes for grouping, filtering, and dashboard views
            span.set_attribute("langfuse.trace.name", "task-execution")
            span.set_attribute("langfuse.session.id", session_id)
            span.set_attribute("langfuse.trace.tags", json.dumps(["task", "manual"]))
            span.set_attribute("langfuse.trace.input", task_request.input[:500])
            span.set_attribute("langfuse.trace.metadata.source", "api")
            span.set_attribute("langfuse.trace.metadata.environment", ENVIRONMENT)
            try:
                final = await workflow.ainvoke(initial_state)
                span.set_attribute("langfuse.trace.output", json.dumps(final.get("results", {}))[:1000])
                logger.info("TASK COMPLETED SUCCESSFULLY")
                return {
                    "status": "completed",
                    "results": final.get("results", {})
                }
            except Exception as e:
                logger.error(f"Task failed: {e}")
                span.record_exception(e)
                return {"status": "error", "message": str(e)}
    finally:
        detach(token)

@app.post("/webhook")
@limiter.limit("5/minute")
async def handle_alert(request: Request, payload: dict):
    """Receive raw webhook payload, pass it to the graph. Zero parsing — nexus-controller handles it."""
    alert_status = payload.get("status", "unknown")
    alerts = payload.get("alerts", [])

    if alert_status != "firing" or not alerts:
        logger.info(f"WEBHOOK IGNORED: status={alert_status}")
        return {"status": "ignored"}

    logger.info(f"WEBHOOK RECEIVED: status='firing', alerts={len(alerts)}")

    # Extract upstream trace context (e.g. from Grafana or test clients)
    upstream_ctx = propagate.extract(dict(request.headers))
    token = attach(upstream_ctx)

    # Pass raw payload as-is — nexus-controller extracts fields via its skills
    initial_state: CompleteState = {
        "input": json.dumps(payload),
        "context": {"source": "grafana", "raw_payload": payload},
        "results": {},
        "validation_history": [],
        "latest_validation": None,
        "next_action": "",
        "next_agent": None,
        "next_instructions": "",
        "retry_count": 0,
        "agent_env_vars": None,
        "spawned_pods": [],
    }

    # Extract alert metadata for Langfuse trace enrichment
    first_alert = alerts[0] if alerts else {}
    alert_name = first_alert.get("labels", {}).get("alertname", "unknown")
    severity = first_alert.get("labels", {}).get("severity", "unknown")
    session_id = f"alert-{alert_name}-{int(time.time())}"

    try:
        with tracer.start_as_current_span("langgraph-fabric.alert.orchestration") as span:
            # Langfuse trace attributes for grouping, filtering, and dashboard views
            span.set_attribute("langfuse.trace.name", f"alert-{alert_name}")
            span.set_attribute("langfuse.session.id", session_id)
            span.set_attribute("langfuse.trace.tags", json.dumps(["alert", "grafana", severity]))
            span.set_attribute("langfuse.trace.input", json.dumps(payload)[:2000])
            span.set_attribute("langfuse.trace.metadata.source", "grafana")
            span.set_attribute("langfuse.trace.metadata.alert_name", alert_name)
            span.set_attribute("langfuse.trace.metadata.severity", severity)
            span.set_attribute("langfuse.trace.metadata.alert_count", str(len(alerts)))
            span.set_attribute("langfuse.trace.metadata.environment", ENVIRONMENT)
            try:
                final = await workflow.ainvoke(initial_state)
                span.set_attribute("langfuse.trace.output", json.dumps(final.get("results", {}))[:1000])
                logger.info("ALERT ORCHESTRATION COMPLETED")
                return {"status": "completed", "results": final.get("results", {})}
            except Exception as e:
                logger.error(f"Alert orchestration failed: {e}")
                span.record_exception(e)
                return {"status": "error", "message": str(e)}
    finally:
        detach(token)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8009)
