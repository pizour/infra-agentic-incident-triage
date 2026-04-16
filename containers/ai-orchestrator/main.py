import os
import json
import httpx
import time
import asyncio
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
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator
from openinference.instrumentation.langchain import LangChainInstrumentor
# from langfuse.opentelemetry import LangfuseExporter

resource = Resource.create({SERVICE_NAME: "ai-orchestrator"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

# Arize Phoenix OTLP Export
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://monitoring-phoenix.monitoring.svc.cluster.local:6006/v1/traces")
try:
    phoenix_exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(phoenix_exporter))
    logger.info(f"Phoenix OTLP exporter initialized: {endpoint}")
except Exception as e:
    logger.error(f"Failed to initialize Phoenix exporter: {e}")

# Langfuse native exporter (uses /api/public/ingestion, works with Langfuse v2+)
# langfuse_host = os.getenv("LANGFUSE_HOST", "http://langfuse.ai-agent.svc.cluster.local:3000")
# langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
# langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")
#
# if langfuse_public_key and langfuse_secret_key:
#     langfuse_exporter = LangfuseExporter(
#         public_key=langfuse_public_key,
#         secret_key=langfuse_secret_key,
#         host=langfuse_host,
#     )
#     provider.add_span_processor(BatchSpanProcessor(langfuse_exporter))
#     logger.info(f"Langfuse exporter initialized (native SDK) targeting {langfuse_host}")

# Instrument LangChain (which includes LangGraph)
LangChainInstrumentor().instrument()

HTTPXClientInstrumentor().instrument()
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import FastAPI, HTTPException, Security, status, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from langgraph.graph import StateGraph, END

# --- Configuration ---
NAMESPACE = os.getenv("NAMESPACE", "ai-agent")
ROUTER_API = os.getenv("ROUTER_API", f"http://router-agent.{NAMESPACE}.svc.cluster.local:8010/run")
GENERIC_AGENT_IMAGE = os.getenv("GENERIC_AGENT_IMAGE", "europe-west4-docker.pkg.dev/ai-incident-triage/gke-artifacts-dev/ai-agent:latest")

# Initialize Kubernetes client
try:
    config.load_incluster_config()
except:
    config.load_kube_config()

core_v1 = client.CoreV1Api()

# ── FastAPI app ───────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="AI Orchestrator")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
Instrumentator().instrument(app).expose(app)

@app.on_event("startup")
async def startup_event():
    setup_logging()
    logger.info("AI-Orchestrator refined logging and connectivity initialized")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Flushing OpenTelemetry spans to Phoenix...")
    provider.force_flush(timeout_millis=5000)

# ── Auth ──────────────────────────────────────────────────────────────────────
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def get_api_key(api_key_header: Optional[str] = Security(api_key_header)) -> str:
    expected_key = os.getenv("APP_API_KEY")
    if not expected_key:
        return "not-set"
    if api_key_header and api_key_header == expected_key:
        return api_key_header
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Could not validate credentials",
    )


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


# ── Kubernetes Pod Lifecycle Helper ───────────────────────────────────────────

async def run_agent_pod(agent_id: str, prompt: str, env_vars: Dict[str, str], system_prompt: Optional[str] = None) -> str:
    pod_name = f"agent-{agent_id.replace('_', '-')}-{int(time.time())}"
    port = 8000
    logger.info(f"SPAWNING POD: {pod_name} for agent={agent_id}")
    
    # Static env vars (same as Helm values.yaml env section)
    static_env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://monitoring-phoenix.monitoring.svc.cluster.local:6006/v1/traces"),
        "PHOENIX_CLIENT_HEADERS": os.getenv("PHOENIX_CLIENT_HEADERS", "api_key=unused"),
        "LLM_MODEL": os.getenv("LLM_MODEL", "gemini-2.5-flash"),
        "GUARDRAILS_URL": os.getenv("GUARDRAILS_URL", "http://guardrails.ai-agent.svc.cluster.local:8080"),
        "ZAMMAD_URL": os.getenv("ZAMMAD_URL", "http://zammad.zammad.svc.cluster.local:8080"),
        "LANGFUSE_HOST": os.getenv("LANGFUSE_HOST", "http://langfuse.ai-agent.svc.cluster.local:3000"),
        "GITHUB_MCP_URL": os.getenv("GITHUB_MCP_URL", "http://github-mcp-server:8080/mcp"),
        "GITHUB_REPO": os.getenv("GITHUB_REPO", "pizour/infra-agentic-incident-triage"),
        "NAMESPACE": NAMESPACE,
    }
    # Override/extend with caller-provided env vars
    static_env.update(env_vars)

    env = [client.V1EnvVar(name=k, value=str(v)) for k, v in static_env.items()]

    # Mount all secrets from ai-agent-secrets (same keys as Helm secrets section)
    secret_keys = [
        "APP_API_KEY", "MCP_API_KEY", "NETBOX_MCP_API_KEY",
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

    pod = client.V1Pod(
        api_version="v1",
        kind="Pod",
        metadata=client.V1ObjectMeta(name=pod_name, labels={"app": "ai-agent", "agent-type": agent_id}),
        spec=client.V1PodSpec(
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
    
    try:
        pod_ip = None
        for i in range(60): 
            curr_pod = core_v1.read_namespaced_pod_status(name=pod_name, namespace=NAMESPACE)
            if curr_pod.status.pod_ip and any(c.ready for c in (curr_pod.status.container_statuses or [])):
                pod_ip = curr_pod.status.pod_ip
                logger.info(f"POD READY: {pod_name} at IP {pod_ip}")
                break
            if i % 5 == 0:
                logger.debug(f"Waiting for pod {pod_name}...")
            await asyncio.sleep(1)
            
        if not pod_ip:
            raise Exception(f"Pod {pod_name} failed to become ready.")
            
        agent_url = f"http://{pod_ip}:{port}/agent"
        logger.info(f"CALLING AGENT: {agent_id} at {agent_url}")
        agent_api_key = os.getenv("APP_API_KEY", "")
        body: Dict[str, Any] = {"prompt": prompt}
        if system_prompt:
            body["system_prompt"] = system_prompt
        async with httpx.AsyncClient(timeout=300.0) as client_http:
            resp = await client_http.post(agent_url, json=body, headers={"X-API-Key": agent_api_key})
            resp.raise_for_status()
            logger.info(f"AGENT {agent_id} RESPONSE RECEIVED")
            return resp.json().get("result", "")
            
    finally:
        try:
            logger.info(f"DELETING POD: {pod_name}")
            core_v1.delete_namespaced_pod(name=pod_name, namespace=NAMESPACE, grace_period_seconds=0)
        except Exception as e:
            logger.error(f"Error deleting pod {pod_name}: {e}")


# ── Node: Nexus Controller ──────────────────────────────────────────────────────────
async def node_nexus_controller(state: CompleteState) -> dict:
    logger.info("NODE: nexus_controller - evaluating state")
    with tracer.start_as_current_span("ai-orchestrator.node.nexus_controller") as span:
        payload = {
            "input": state["input"],
            "context_summary": str(state.get("context", {})),
            "latest_validation": state.get("latest_validation")
        }
        # 90s total: nexus-controller does 2 LLM calls + 1 GitHub MCP call
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=90.0, write=10.0, pool=5.0)) as client:
            try:
                response = await client.post(ROUTER_API, json=payload)
                response.raise_for_status()
                data = response.json()
                action = data.get("action", "finish")
                target_agent = data.get("target_agent")
                feedback = data.get("feedback", "")
                agent_env_vars = data.get("agent_env_vars") or {}
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

    # Post-response validation: if controller returned an error or tool failure pattern, finish
    try:
        if isinstance(data, dict):
            action = data.get("action", "").lower()
            feedback = data.get("feedback", "").lower()

            # Detect tool failure patterns - finish instead of continuing
            if "failed after 3 attempts" in feedback or "tool" in feedback and "fail" in feedback:
                logger.warning(f"Detected tool failure in feedback: {feedback}. Finishing workflow.")
                return {"next_action": "finish"}

            # If action is anything other than recognized actions, finish
            if action not in ["next_agent", "retry", "finish"]:
                logger.warning(f"Unknown action from controller: {action}. Finishing workflow.")
                return {"next_action": "finish"}
    except Exception as e:
        logger.error(f"Error validating controller response: {e}")
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
    with tracer.start_as_current_span(f"ai-orchestrator.node.execute.{agent_id}") as span:
        span.set_attribute("agent_id", str(agent_id))
        span.set_attribute("attempt", current_retry)
        prompt = str(state.get("next_instructions") or state["input"])

        agent_env_vars = dict(state.get("agent_env_vars") or {})
        system_prompt = agent_env_vars.pop("SYSTEM_PROMPT", None)

        try:
            result_str = await run_agent_pod(str(agent_id), prompt, agent_env_vars, system_prompt=system_prompt)
        except Exception as e:
            logger.error(f"Pod failed for agent {agent_id}: {e}")
            span.record_exception(e)
            result_str = json.dumps({"agent_key": str(agent_id), "error": str(e)[:200]})

        # Store raw result — no parsing, no scoring. Nexus-controller evaluates.
        history = list(state.get("validation_history") or [])
        results = dict(state.get("results") or {})  # type: ignore[arg-type]
        results[str(agent_id)] = result_str
        history.append({"agent_key": str(agent_id), "raw": result_str, "attempt": current_retry})

        return {
            "results": results,
            "latest_validation": {"agent_key": str(agent_id), "raw": result_str, "attempt": current_retry},
            "validation_history": history,
            "retry_count": current_retry,
        }

# ── Routing ───────────────────────────────────────────────────────────────────
def route_from_controller(state: CompleteState) -> str:
    """After nexus_controller decides: execute the suggested agent or finish."""
    action = state.get("next_action", "finish")
    if action == "finish":
        return END
    return "execute"

# ── Graph compilation ─────────────────────────────────────────────────────────
def _build_graph() -> StateGraph:
    g = StateGraph(CompleteState)
    g.add_node("nexus_controller", node_nexus_controller)
    g.add_node("execute", node_executor)

    g.set_entry_point("nexus_controller")
    g.add_conditional_edges(
        "nexus_controller",
        route_from_controller,
        {"execute": "execute", END: END},
    )
    # All agent results always go back to nexus_controller for re-evaluation
    g.add_edge("execute", "nexus_controller")

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
    return {"status": "ok", "message": "AI-Orchestrator Dynamic running"}

@app.post("/task")
async def run_task(request: TaskRequest):
    logger.info(f"TASK REQUEST: input='{request.input[:100]}...'")
    # Initialize LangGraph state
    initial_state: CompleteState = {
        "input": request.input,
        "context": request.context,
        "results": {},
        "validation_history": [],
        "latest_validation": None,
        "next_action": "",
        "next_agent": None,
        "next_instructions": "",
        "retry_count": 0
    }
    
    with tracer.start_as_current_span("ai-orchestrator.task.execution") as span:
        try:
            final = await workflow.ainvoke(initial_state)
            logger.info("TASK COMPLETED SUCCESSFULLY")
            return {
                "status": "completed",
                "results": final.get("results", {})
            }
        except Exception as e:
            logger.error(f"Task failed: {e}")
            span.record_exception(e)
            return {"status": "error", "message": str(e)}

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
        "retry_count": 0
    }

    with tracer.start_as_current_span("ai-orchestrator.alert.orchestration") as span:
        try:
            final = await workflow.ainvoke(initial_state)
            logger.info("ALERT ORCHESTRATION COMPLETED")
            return {"status": "completed", "results": final.get("results", {})}
        except Exception as e:
            logger.error(f"Alert orchestration failed: {e}")
            span.record_exception(e)
            return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8009)
