import os
import json
import httpx
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
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

resource = Resource.create({SERVICE_NAME: "router-agent"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://monitoring-phoenix.monitoring.svc.cluster.local:6006/v1/traces")
exporter = OTLPSpanExporter(endpoint=endpoint)
provider.add_span_processor(BatchSpanProcessor(exporter))
provider.add_span_processor(OpenInferenceSpanProcessor())
HTTPXClientInstrumentor().instrument()

# --- Configuration ---
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-1.5-flash")
GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL", "http://github-mcp-server.ai-agent.svc.cluster.local:8080/sse")
GITHUB_REPO = os.getenv("GITHUB_REPO", "pizour/infra-agentic-incident-triage")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

app = FastAPI(title="Router-Agent (Pydantic-AI)")
FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)

model = GoogleModel(MODEL_NAME, provider="google-vertex")

ROUTER_SYSTEM_PROMPT = (
    "You are the Router-Agent. Your task is to design an execution plan for incoming requests.\n"
    "CRITICAL: You MUST use your 'github' tool to read 'agents/router-agent.md' and follow the 'Routing Standard Operating Procedure' (SOP) defined there to the detail.\n"
    "You also need to discover available agents and skills in the repo by listing directories using the 'github' tool as instructed in the SOP.\n"
    "Your final answer MUST be the raw JSON plan as described in the SOP."
)

agent = Agent(
    model,
    instrument=True,
    system_prompt=ROUTER_SYSTEM_PROMPT,
)

@agent.tool
async def github(
    ctx: RunContext[None],
    action: str,
    path: str = None,
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
                
            api_endpoint = GITHUB_MCP_URL.replace("/sse", endpoint_map[action])
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
    context: Optional[dict] = None

@app.post("/run")
async def run_router(request: RunRequest):
    logger.info(f"ROUTER AGENT REQUEST: {request.input[:100]}...")
    try:
        prompt = f"Request: {request.input}\nContext: {json.dumps(request.context or {})}"
        result = await agent.run(prompt)
        
        output = str(result.data)
        logger.info("PLAN GENERATION COMPLETE")
        
        # Clean up output if it's wrapped in markdown
        if output.startswith("```json"):
            output = output.strip("```json").strip("```").strip()
        elif output.startswith("```"):
            output = output.strip("```").strip()
            
        try:
            return json.loads(output)
        except:
            return {"raw_output": output}

    except Exception as e:
        logger.error(f"Router execution failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Router-Agent Pydantic-AI running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010)
