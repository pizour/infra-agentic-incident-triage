import os
import json
import httpx
import base64
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from loguru import logger

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.google import GoogleModel

load_dotenv()

# --- OpenTelemetry / Arize Phoenix Setup ---
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from openinference.instrumentation.pydantic_ai import OpenInferenceSpanProcessor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator

from langfuse import Langfuse
resource = Resource.create({SERVICE_NAME: "nexus-controller"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

# Initialize Langfuse (v3+ auto-registers with the global provider set above)
langfuse = Langfuse()

endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://monitoring-phoenix.monitoring.svc.cluster.local:6006/v1/traces")
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

# Langfuse OTLP Export
langfuse_host = os.getenv("LANGFUSE_HOST", "http://langfuse.ai-agent.svc.cluster.local:3000")
langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")

if langfuse_public_key and langfuse_secret_key:
    # Langfuse OTLP requires Basic Auth: base64(public_key:secret_key)
    auth_str = f"{langfuse_public_key}:{langfuse_secret_key}"
    encoded_auth = base64.b64encode(auth_str.encode()).decode()
    lf_headers = {"Authorization": f"Basic {encoded_auth}"}
    lf_endpoint = f"{langfuse_host}/api/public/otlp/v1/traces"
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=lf_endpoint, headers=lf_headers)))
    logger.info(f"Langfuse OTLP exporter initialized targeting {lf_endpoint}")

provider.add_span_processor(OpenInferenceSpanProcessor())
HTTPXClientInstrumentor().instrument()

# --- Configuration ---
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-2.5-flash")
GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL", "http://github-mcp-server.ai-agent.svc.cluster.local:8080/sse")
GITHUB_REPO = os.getenv("GITHUB_REPO", "pizour/infra-agentic-incident-triage")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

app = FastAPI(title="Nexus-Controller (Pydantic-AI)")
FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
Instrumentator().instrument(app).expose(app)

model = GoogleModel(MODEL_NAME, provider="google-vertex")

class AgentValidationResult(BaseModel):
    agent_key: str
    agent_class: str  # "control-plane" | "interaction" | "specialist"
    accuracy: float
    correctness: float
    completeness: float
    safety_check: bool
    reasoning: str = Field(..., max_length=50)
    data: Optional[dict] = None

class NexusRoutingDecision(BaseModel):
    action: str
    feedback: str
    target_agent: Optional[str] = None

ROUTER_SYSTEM_PROMPT = (
    "You are the Nexus Controller. Your task is to evaluate the provided validation state and make a routing decision.\n"
    "CRITICAL: You MUST use your 'github' tool to read 'agents/control-plane/nexus-controller.md' and follow the operating procedures defined there to the detail.\n"
)

agent = Agent(
    model,
    output_type=NexusRoutingDecision,
    system_prompt=ROUTER_SYSTEM_PROMPT,
    instrument=True,
)

@agent.tool
async def github(
    ctx: RunContext[None],
    action: str,
    path: Optional[str] = None,
) -> str:
    """
    Interact with the GitHub repository.
    Actions:
      - list_files: List files in a directory (requires 'path')
      - read_file: Read content of a file (requires 'path')
    """
    logger.info(f"GITHUB TOOL CALL: action={action}, path={path}")
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            payload = {
                "owner": GITHUB_REPO.split('/')[0],
                "repo": GITHUB_REPO.split('/')[1],
                "path": path or ".",
                "branch": GITHUB_BRANCH
            }
            
            # Map action to endpoint
            endpoint_map = {
                "list_files": "/list_files",
                "read_file": "/read_file"
            }
            
            if action not in endpoint_map:
                return f"Error: Unknown action '{action}'"
                
            # Prefer Service name over FQDN if possible, but keep fallback
            base_url = GITHUB_MCP_URL.split("/sse")[0]
            api_endpoint = f"{base_url}{endpoint_map[action]}"
            
            logger.debug(f"Calling GitHub MCP endpoint: {api_endpoint}")
            resp = await client.post(api_endpoint, json=payload)
            
            if resp.status_code == 200:
                data = resp.json()
                if action == "list_files":
                    files = data.get("files", [])
                    return "\n".join([f"{f['raw_path']}" for f in files])
                return data.get("content", "Empty file.")
            
            return f"Error calling GitHub MCP ({action}): {resp.text}"
        except Exception as e:
            return f"Exception connecting to GitHub MCP: {str(e)}"

class RunRequest(BaseModel):
    input: str
    context_summary: str = ""
    latest_validation: Optional[Dict[str, Any]] = None

@app.post("/run")
async def run_nexus_controller(request: RunRequest):
    logger.info(f"NEXUS CONTROLLER REQUEST: {request.input[:100]}...")
    try:
        prompt = f"Goal: {request.input}\nContext: {request.context_summary}\nLatest Validation: {json.dumps(request.latest_validation or {})}"
        result = await agent.run(prompt)
        
        # Robustly handle result attribute (Pydantic-AI 0.x uses .data, 1.x uses .output)
        decision = getattr(result, "output", getattr(result, "data", None))
        if decision is None:
            raise AttributeError(f"AgentRunResult has neither 'output' nor 'data': {dir(result)}")

        logger.info(f"ROUTING DECISION COMPLETE: {decision.action}")
        return decision.model_dump()

    except Exception as e:
        logger.error(f"Controller execution failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Router-Agent Pydantic-AI running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010)
