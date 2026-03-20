"""
main.py — LangGraph Orchestrator
=================================
This module is a THIN orchestrator. It owns:
  - FastAPI app setup, auth, and rate limiting
  - OpenTelemetry + Prometheus instrumentation
  - The LangGraph state machine (graph topology + routing logic)

All actual work is delegated via HTTP to isolated Docker containers:
  - investigation-agent:8004
  - analysis-agent:8005
  - ticket-agent:8006
"""
import os
import httpx
from typing import Optional
from typing_extensions import TypedDict
from dotenv import load_dotenv

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

resource = Resource.create({SERVICE_NAME: "langgraph-agent"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://phoenix:6006/v1/traces")
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

HTTPXClientInstrumentor().instrument()
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import FastAPI, HTTPException, Security, status, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from langgraph.graph import StateGraph, END


# URLs to the isolated agent containers
INVESTIGATION_API = os.getenv("INVESTIGATION_API", "http://investigation-agent:8004/run")
ANALYSIS_API = os.getenv("ANALYSIS_API", "http://analysis-agent:8005/run")
TICKET_API = os.getenv("TICKET_API", "http://ticket-agent:8006/run")


# ── FastAPI app ───────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="LangGraph Orchestrator")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)

# ── Auth ──────────────────────────────────────────────────────────────────────
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)


def get_api_key(api_key_header: Optional[str] = Security(api_key_header)) -> str:
    expected_key = os.getenv("APP_API_KEY")
    if not expected_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="APP_API_KEY is not configured on the server",
        )
    if api_key_header and api_key_header == expected_key:
        return api_key_header
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Could not validate credentials",
    )


# ══════════════════════════════════════════════════════════════════════════════
# LangGraph State Machine
# ══════════════════════════════════════════════════════════════════════════════

class InvestigationState(TypedDict):
    """Shared state threaded through every LangGraph node."""
    # Input: alert context
    host: str
    host_ip: str
    source_ip: str
    alert_description: str
    # Outputs: filled in by each agent node
    evidence: str          # raw tool output from investigation-agent
    analysis: str          # verdict + findings from analysis-agent
    ticket_result: str     # confirmation string from ticket-agent


# ── Node: Investigate ─────────────────────────────────────────────────────────
async def node_investigate(state: InvestigationState) -> dict:
    with tracer.start_as_current_span("langgraph.node.investigate") as span:
        span.set_attribute("host", state["host"])
        span.set_attribute("host_ip", state["host_ip"])
        
        payload = {
            "host": state["host"],
            "host_ip": state["host_ip"],
            "source_ip": state["source_ip"],
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(INVESTIGATION_API, json=payload, timeout=60.0)
                response.raise_for_status()
                data = response.json()
                evidence = data.get("evidence", "")
                span.set_attribute("evidence_length", len(evidence))
                return {"evidence": evidence}
            except Exception as e:
                span.record_exception(e)
                return {"evidence": f"Error during investigation: {e}"}


# ── Node: Analyse ─────────────────────────────────────────────────────────────
async def node_analyse(state: InvestigationState) -> dict:
    with tracer.start_as_current_span("langgraph.node.analyse") as span:
        payload = {
            "host": state["host"],
            "host_ip": state["host_ip"],
            "source_ip": state["source_ip"],
            "alert_description": state["alert_description"],
            "evidence": state.get("evidence", ""),
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(ANALYSIS_API, json=payload, timeout=60.0)
                response.raise_for_status()
                data = response.json()
                analysis = data.get("analysis", "")
                span.set_attribute("analysis", analysis[:500])
                return {"analysis": analysis}
            except Exception as e:
                span.record_exception(e)
                return {"analysis": f"VERDICT: ERROR - Analysis failed: {e}"}


# ── Node: Create ticket ───────────────────────────────────────────────────────
async def node_create_ticket(state: InvestigationState) -> dict:
    with tracer.start_as_current_span("langgraph.node.create_ticket") as span:
        payload = {
            "host": state["host"],
            "host_ip": state["host_ip"],
            "source_ip": state["source_ip"],
            "alert_description": state["alert_description"],
            "analysis": state.get("analysis", ""),
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(TICKET_API, json=payload, timeout=30.0)
                response.raise_for_status()
                data = response.json()
                ticket_result = data.get("ticket_result", "")
                span.set_attribute("ticket_result", ticket_result)
                return {"ticket_result": ticket_result}
            except Exception as e:
                span.record_exception(e)
                return {"ticket_result": f"Error creating ticket: {e}"}


# ── Routing ───────────────────────────────────────────────────────────────────
def route_after_analysis(state: InvestigationState) -> str:
    if "VERDICT: THREAT" in state.get("analysis", "").upper():
        return "create_ticket"
    return END


# ── Graph compilation ─────────────────────────────────────────────────────────
def _build_graph() -> StateGraph:
    g = StateGraph(InvestigationState)

    g.add_node("investigate", node_investigate)
    g.add_node("analyse", node_analyse)
    g.add_node("create_ticket", node_create_ticket)

    g.set_entry_point("investigate")
    g.add_edge("investigate", "analyse")
    g.add_conditional_edges(
        "analyse",
        route_after_analysis,
        {"create_ticket": "create_ticket", END: END},
    )
    g.add_edge("create_ticket", END)

    return g.compile()


orchestrator = _build_graph()


# ══════════════════════════════════════════════════════════════════════════════
# API Endpoints
# ══════════════════════════════════════════════════════════════════════════════

class AgentRequest(BaseModel):
    prompt: str


class AgentResponse(BaseModel):
    result: str


@app.get("/")
async def health_check():
    return {"status": "ok", "message": "LangGraph Orchestrator running"}


@app.post("/agent", response_model=AgentResponse)
async def run_agent(request: AgentRequest, api_key: str = Security(get_api_key)):
    try:
        payload = {
            "host": "n/a",
            "host_ip": "n/a",
            "source_ip": "n/a",
            "alert_description": request.prompt,
            "evidence": "",
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(ANALYSIS_API, json=payload, timeout=60.0)
            response.raise_for_status()
            data = response.json()
            return AgentResponse(result=data.get("analysis", ""))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhook")
@limiter.limit("5/minute")
async def handle_alert(request: Request, payload: dict):
    alert_status = payload.get("status", "unknown")
    alerts = payload.get("alerts", [])

    if alert_status != "firing" or not alerts:
        return {"status": "ignored"}

    alert = alerts[0]
    alert_desc = alert.get("annotations", {}).get("description", "No description")

    hostname = (
        alert.get("labels", {}).get("host")
        or alert.get("labels", {}).get("hostname")
        or alert.get("labels", {}).get("instance", "").split(":")[0]
        or "unknown"
    )
    host_ip = alert.get("labels", {}).get("host_ip", hostname)
    source_ip = alert.get("labels", {}).get("source_ip", "unknown")

    initial_state: InvestigationState = {
        "host": hostname,
        "host_ip": host_ip,
        "source_ip": source_ip,
        "alert_description": alert_desc,
        "evidence": "",
        "analysis": "",
        "ticket_result": "",
    }

    with tracer.start_as_current_span("langgraph.orchestration") as span:
        span.set_attribute("host", hostname)
        span.set_attribute("host_ip", host_ip)
        span.set_attribute("source_ip", source_ip)
        span.set_attribute("alert", alert_desc[:500])

        try:
            final = await orchestrator.ainvoke(initial_state)
            return {
                "status": "investigated",
                "hostname": hostname,
                "analysis": final.get("analysis", ""),
                "ticket": final.get("ticket_result", "No ticket created."),
            }
        except Exception as e:
            span.record_exception(e)
            return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
