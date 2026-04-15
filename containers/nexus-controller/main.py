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
from mcp import ClientSession
from mcp.client.sse import sse_client

# from langfuse import Langfuse
# from langfuse.opentelemetry import LangfuseExporter

resource = Resource.create({SERVICE_NAME: "nexus-controller"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

# Arize Phoenix OTLP Export
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://monitoring-phoenix.monitoring.svc.cluster.local:6006/v1/traces")
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

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

provider.add_span_processor(OpenInferenceSpanProcessor())
HTTPXClientInstrumentor().instrument()

# --- Configuration ---
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-2.5-flash")
GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL", "http://github-mcp-server.ai-agent.svc.cluster.local:8080/sse")
GITHUB_REPO = os.getenv("GITHUB_REPO", "pizour/infra-agentic-incident-triage")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
MCP_API_KEY = os.getenv("MCP_API_KEY", "")

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
    Interact with the GitHub repository via MCP protocol (SSE).
    Actions:
      - list_files / list_directory_contents: List files in a directory
      - read_file / get_file_contents: Read content of a file
    """
    logger.info(f"GITHUB MCP CALL: action={action}, path={path}")
    try:
        params = {
            "owner": GITHUB_REPO.split('/')[0],
            "repo": GITHUB_REPO.split('/')[1],
            "path": path or "."
        }

        # Map caller actions to official GitHub MCP server tool names
        tool_map = {
            "list_files": "list_directory_contents",
            "read_file": "get_file_contents",
            "list_directory_contents": "list_directory_contents",
            "get_file_contents": "get_file_contents",
        }
        target_tool = tool_map.get(action, action)

        async with sse_client(GITHUB_MCP_URL) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                logger.debug(f"MCP tool='{target_tool}' params={params}")
                result = await session.call_tool(target_tool, arguments=params)

                if result.isError:
                    return f"MCP Error: {result.content}"

                parts = []
                for item in result.content:
                    if hasattr(item, 'text'):
                        parts.append(item.text or "")
                    elif isinstance(item, dict) and 'text' in item:
                        parts.append(item['text'] or "")
                    else:
                        parts.append(str(item))
                return "\n".join(parts) if parts else "No content returned."
    except Exception as e:
        logger.error(f"GitHub MCP call failed: {e}")
        return f"Exception during GitHub MCP call: {str(e)}"

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
