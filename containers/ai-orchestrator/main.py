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
from langfuse.opentelemetry import LangfuseExporter

resource = Resource.create({SERVICE_NAME: "ai-orchestrator"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

# Arize Phoenix OTLP Export
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://monitoring-phoenix.monitoring.svc.cluster.local:6006/v1/traces")
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

# Langfuse native exporter (uses /api/public/ingestion, works with Langfuse v2+)
langfuse_host = os.getenv("LANGFUSE_HOST", "http://langfuse.ai-agent.svc.cluster.local:3000")
langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")

if langfuse_public_key and langfuse_secret_key:
    langfuse_exporter = LangfuseExporter(
        public_key=langfuse_public_key,
        secret_key=langfuse_secret_key,
        host=langfuse_host,
    )
    provider.add_span_processor(BatchSpanProcessor(langfuse_exporter))
    logger.info(f"Langfuse exporter initialized (native SDK) targeting {langfuse_host}")

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


# ── Kubernetes Pod Lifecycle Helper ───────────────────────────────────────────

async def run_agent_pod(agent_id: str, prompt: str, env_vars: Dict[str, str]) -> str:
    pod_name = f"agent-{agent_id.replace('_', '-')}-{int(time.time())}"
    port = 8000
    logger.info(f"SPAWNING POD: {pod_name} for agent={agent_id}")
    
    # Build environment variables dynamically from router response
    env = []
    for k, v in env_vars.items():
        env.append(client.V1EnvVar(name=k, value=str(v)))

    # Ensure LLM_MODEL is present if not provided by router
    if "LLM_MODEL" not in env_vars:
        env.append(client.V1EnvVar(name="LLM_MODEL", value=os.getenv("LLM_MODEL", "gemini-2.0-flash")))

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
        async with httpx.AsyncClient(timeout=300.0) as client_http:
            resp = await client_http.post(agent_url, json={"prompt": prompt})
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
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(ROUTER_API, json=payload, timeout=60.0)
                response.raise_for_status()
                data = response.json()
                action = data.get("action", "finish")
                target_agent = data.get("target_agent")
                feedback = data.get("feedback", "")
                logger.info(f"CONTROLLER DECISION: action={action}, target={target_agent}")
                return {"next_action": action, "next_agent": target_agent, "next_instructions": feedback}
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

    logger.info(f"NODE: executor - forwarding to agent={agent_id}")
    with tracer.start_as_current_span(f"ai-orchestrator.node.execute.{agent_id}") as span:
        span.set_attribute("agent_id", str(agent_id))
        prompt = str(state.get("next_instructions") or state["input"])

        try:
            result_str = await run_agent_pod(str(agent_id), prompt, {})
        except Exception as e:
            logger.error(f"Pod failed for agent {agent_id}: {e}")
            span.record_exception(e)
            result_str = json.dumps({"agent_key": str(agent_id), "error": str(e)[:200]})

        # Store raw result — no parsing, no scoring. Nexus-controller evaluates.
        history = list(state.get("validation_history") or [])
        results = dict(state.get("results") or {})  # type: ignore[arg-type]
        results[str(agent_id)] = result_str
        history.append({"agent_key": str(agent_id), "raw": result_str})

        return {
            "results": results,
            "latest_validation": {"agent_key": str(agent_id), "raw": result_str},
            "validation_history": history,
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
        "next_instructions": ""
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
