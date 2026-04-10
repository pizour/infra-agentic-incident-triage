import os
import httpx
import time
import asyncio
from typing import Optional, List, Annotated, Dict
from typing_extensions import TypedDict
from dotenv import load_dotenv
from kubernetes import client, config

from loguru import logger
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
from langfuse import Langfuse

resource = Resource.create({SERVICE_NAME: "ai-orchestrator"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://phoenix:6006/v1/traces")
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

# Initialize Langfuse client (automatically registers with OTEL in v3+)
langfuse = Langfuse()

HTTPXClientInstrumentor().instrument()
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import FastAPI, HTTPException, Security, status, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from langgraph.graph import StateGraph, END

# --- Configuration ---
NAMESPACE = os.getenv("NAMESPACE", "ai-agent")
ROUTER_API = os.getenv("ROUTER_API", "http://router-agent:8010/run")
GENERIC_AGENT_IMAGE = os.getenv("GENERIC_AGENT_IMAGE", "europe-west4-docker.pkg.dev/ai-incident-triage/gke-artifacts-dev/ai-agent:latest")

# Initialize Kubernetes client
try:
    config.load_incluster_config()
except:
    config.load_kube_config()

core_v1 = client.CoreV1Api()

# ── FastAPI app ───────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="AI-Orchestrator - Dynamic Multi-Agent Graph")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)

# --- Logging Suppression Middleware ---
@app.middleware("http")
async def suppress_health_logging(request: Request, call_next):
    if request.url.path == "/health":
        # Temporarily disable uvicorn access logging for this request
        import logging
        uvicorn_access = logging.getLogger("uvicorn.access")
        uvicorn_access.disabled = True
        try:
            return await call_next(request)
        finally:
            uvicorn_access.disabled = False
    return await call_next(request)

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

class AgentStep(TypedDict):
    agent_id: str
    skills: List[str]
    env_vars: Dict[str, str]
    output_key: str
    reasoning: str

class OrchestratorState(TypedDict):
    """Shared state threaded through every LangGraph node."""
    # Input: alert context
    input: str
    context: Optional[dict]
    
    # Plan
    plan: List[AgentStep]
    
    # Results accumulation
    results: Dict[str, str]


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


# ── Node: Router ──────────────────────────────────────────────────────────────
async def node_router(state: OrchestratorState) -> dict:
    logger.info("NODE: router - designing plan")
    with tracer.start_as_current_span("ai-orchestrator.node.router") as span:
        payload = {
            "input": state["input"],
            "context": state["context"]
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(ROUTER_API, json=payload, timeout=60.0)
                response.raise_for_status()
                data = response.json()
                plan = data.get("plan", [])
                logger.info(f"ROUTER PLAN RECEIVED: {len(plan)} steps")
                for i, step in enumerate(plan):
                    logger.info(f"  Step {i+1}: {step['agent_id']} ({step['reasoning']})")
                span.set_attribute("plan_steps", len(plan))
                return {"plan": plan}
            except Exception as e:
                logger.error(f"Router call failed: {e}")
                span.record_exception(e)
                return {"plan": []}


# ── Node: Executor ────────────────────────────────────────────────────────────
async def node_executor(state: OrchestratorState) -> dict:
    if not state["plan"]:
        logger.info("NODE: executor - plan exhausted")
        return {}
        
    # Get the next step
    plan = state["plan"].copy()
    step = plan.pop(0)
    agent_id = step["agent_id"]
    output_key = step.get("output_key", "evidence")
    
    logger.info(f"NODE: executor - running step for agent={agent_id}")
    with tracer.start_as_current_span(f"ai-orchestrator.node.execute.{agent_id}") as span:
        span.set_attribute("agent_id", agent_id)
        span.set_attribute("output_key", output_key)
        
        # Build the prompt for the agent dynamically using all results
        prompt = f"TASK: {state['input']}\n"
        if state["results"]:
            prompt += "\nRESULTS GATHERED SO FAR:\n"
            for k, v in state["results"].items():
                prompt += f"--- {k.upper()} ---\n{v}\n"
            
        prompt += f"\nYOUR SPECIFIC INSTRUCTIONS: {step['reasoning']}\n"
        prompt += f"Use these skills: {', '.join(step['skills'])}\n"

        try:
            result = await run_agent_pod(agent_id, prompt, step.get("env_vars", {}))
            logger.info(f"STEP COMPLETE: agent={agent_id} stored in key='{output_key}'")
            
            # Update shared results dictionary
            results = state["results"].copy()
            # If multiple agents write to the same key (e.g., 'evidence'), append it
            if output_key in results:
                results[output_key] = (results[output_key] + "\n\n" + result).strip()
            else:
                results[output_key] = result
                
            return {"plan": plan, "results": results}
        except Exception as e:
            logger.error(f"Step failed for agent {agent_id}: {e}")
            span.record_exception(e)
            results = state["results"].copy()
            error_msg = f"Error in {agent_id}: {e}"
            results["error"] = (results.get("error", "") + "\n" + error_msg).strip()
            return {"plan": plan, "results": results}


# ── Routing ───────────────────────────────────────────────────────────────────
def route_next(state: OrchestratorState) -> str:
    if state["plan"]:
        return "execute"
    return END


# ── Graph compilation ─────────────────────────────────────────────────────────
def _build_graph() -> StateGraph:
    g = StateGraph(OrchestratorState)

    g.add_node("router", node_router)
    g.add_node("execute", node_executor)

    g.set_entry_point("router")
    g.add_edge("router", "execute")
    g.add_conditional_edges(
        "execute",
        route_next,
        {"execute": "execute", END: END},
    )

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
    initial_state: OrchestratorState = {
        "input": request.input,
        "context": request.context or {},
        "plan": [],
        "results": {},
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
    alert_status = payload.get("status", "unknown")
    alerts = payload.get("alerts", [])

    if alert_status != "firing" or not alerts:
        logger.info(f"WEBHOOK IGNORED: status={alert_status}")
        return {"status": "ignored"}

    alert = alerts[0]
    alert_desc = alert.get("annotations", {}).get("description", "No description")
    logger.info(f"WEBHOOK RECEIVED: alert='{alert.get('labels', {}).get('alertname', 'unknown')}' status='firing'")

    hostname = (
        alert.get("labels", {}).get("host")
        or alert.get("labels", {}).get("hostname")
        or alert.get("labels", {}).get("instance", "").split(":")[0]
        or "unknown"
    )
    host_ip = alert.get("labels", {}).get("host_ip", hostname)
    source_ip = alert.get("labels", {}).get("source_ip", "unknown")

    initial_state: OrchestratorState = {
        "input": alert_desc,
        "context": {
            "host": hostname,
            "host_ip": host_ip,
            "source_ip": source_ip,
            "alert": alert
        },
        "plan": [],
        "results": {},
    }

    with tracer.start_as_current_span("ai-orchestrator.alert.orchestration") as span:
        try:
            final = await workflow.ainvoke(initial_state)
            logger.info("ALERT ORCHESTRATION COMPLETED")
            return {
                "status": "investigated",
                "hostname": hostname,
                "results": final.get("results", {})
            }
        except Exception as e:
            logger.error(f"Alert orchestration failed: {e}")
            span.record_exception(e)
            return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8009)
