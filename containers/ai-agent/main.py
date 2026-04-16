import os
import httpx
import base64
from typing import Optional, List
from dotenv import load_dotenv

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
from opentelemetry.propagate import inject
from prometheus_fastapi_instrumentator import Instrumentator

# --- Langfuse OTLP Support ---
from langfuse import Langfuse

# Initialize TracerProvider with Service Name
resource = Resource.create({SERVICE_NAME: "ai-agent"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)

tracer = trace.get_tracer(__name__)

# Configure OTLP Exporter (sending to Phoenix)
endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://monitoring-phoenix.monitoring.svc.cluster.local:6006/v1/traces")
try:
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider.add_span_processor(BatchSpanProcessor(exporter))
except Exception as e:
    pass  # Will log after logger is imported

# Langfuse OTLP Export
langfuse_host = os.getenv("LANGFUSE_HOST", "http://langfuse.ai-agent.svc.cluster.local:3000")
langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")

if langfuse_public_key and langfuse_secret_key:
    auth_str = f"{langfuse_public_key}:{langfuse_secret_key}"
    encoded_auth = base64.b64encode(auth_str.encode()).decode()
    lf_headers = {"Authorization": f"Basic {encoded_auth}"}
    lf_endpoint = f"{langfuse_host}/api/public/otlp/v1/traces"
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=lf_endpoint, headers=lf_headers)))

# Configure Langfuse SpanProcessor (sending to Langfuse)
# If env vars LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST are set, it will auto-config
# Initialize Langfuse client (automatically registers with OTEL in v3+)
langfuse = Langfuse()

# Instrument frames and libraries
provider.add_span_processor(OpenInferenceSpanProcessor())
HTTPXClientInstrumentor().instrument()
# ---------------------------------------------

from fastapi import FastAPI, HTTPException, Security, status, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

# --- SlowAPI Setup ---
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="AI Agent API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- Shared config ---
GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL", "http://github-mcp-server:8080/sse")
GITHUB_REPO = os.getenv("GITHUB_REPO", "pizour/infra-agentic-incident-triage")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
MCP_API_KEY = os.getenv("MCP_API_KEY", "")

from pydantic_ai.models.google import GoogleModel
model_name = os.getenv("LLM_MODEL", "gemini-2.5-flash")
model = GoogleModel(model_name, provider="google-vertex")

DEFAULT_SYSTEM_PROMPT = (
    "You are an AI agent for cloud infrastructure operations. "
    "You receive tasks from the orchestrator with a specific system prompt that tells you what to do. "
    "Follow your system prompt instructions precisely. "
    "You have a 'github' tool to read skills and documentation from the repository. "
    "Provide a full summary of your findings."
)

agent = Agent(
    model,
    system_prompt=os.getenv("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
)


from loguru import logger

logger.info(f"Phoenix OTLP exporter initialized: {endpoint}")

@agent.tool
async def github(
    ctx: RunContext[None],
    action: str,
    path: str = None,
) -> str:
    """
    Read files from the GitHub repository.
    Actions:
      - read_skill: Read a skill/SOP or any file from GitHub (requires 'path')
    """
    logger.info(f"GITHUB TOOL CALL: action={action}, path={path}")
    with tracer.start_as_current_span(f"github.{action}") as span:
        span.set_attribute("action", action)

        if action == "read_skill":
            if not path: return "Error: 'path' required for read_skill"
            async with httpx.AsyncClient(timeout=30.0) as client:
                headers = {}
                if MCP_API_KEY:
                    headers["X-MCP-API-Key"] = MCP_API_KEY

                max_retries = 3
                for attempt in range(1, max_retries + 1):
                    try:
                        payload = {
                            "owner": GITHUB_REPO.split('/')[0],
                            "repo": GITHUB_REPO.split('/')[1],
                            "path": f"skills/{path}" if path and not path.startswith("skills/") and not path.startswith("mcp/") and not path.startswith("agents/") else path,
                            "branch": GITHUB_BRANCH
                        }
                        # Prefer Service name over FQDN if possible, but keep fallback
                        base_url = GITHUB_MCP_URL.split("/sse")[0]
                        api_endpoint = f"{base_url}/read_file"

                        resp = await client.post(api_endpoint, json=payload, headers=headers)
                        if resp.status_code == 200:
                            content = resp.json().get("content", "Empty file.")
                            span.set_attribute("content_length", len(content))
                            return content
                        elif attempt < max_retries:
                            logger.warning(f"GitHub MCP attempt {attempt}/{max_retries} failed: {resp.status_code}. Retrying...")
                            continue
                        else:
                            return f"Error reading GitHub skill (failed after {max_retries} attempts): {resp.status_code} - {resp.text}"
                    except Exception as e:
                        if attempt < max_retries:
                            logger.warning(f"GitHub MCP attempt {attempt}/{max_retries} exception: {str(e)}. Retrying...")
                            continue
                        else:
                            return f"Exception connecting to GitHub MCP (failed after {max_retries} attempts): {str(e)}"

        return f"Unknown action: {action}"

# Authentication Setup
API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

def get_api_key(api_key_header: str = Security(api_key_header)):
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

class AgentRequest(BaseModel):
    prompt: str

class AgentResponse(BaseModel):
    result: str

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Flushing OpenTelemetry spans to Phoenix...")
    provider.force_flush(timeout_millis=5000)

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "AI Agent API is running"}

@app.post("/agent", response_model=AgentResponse)
async def run_agent(request: AgentRequest, api_key: str = Security(get_api_key)):
    """Standard agent endpoint for manual queries."""
    logger.info(f"RUNNING AGENT: prompt='{request.prompt[:100]}...'")
    try:
        result = await agent.run(request.prompt)
        
        # Robustly handle result attribute (Pydantic-AI 0.x uses .data, 1.x uses .output)
        output = getattr(result, "output", getattr(result, "data", None))
        if output is None:
            raise AttributeError(f"AgentRunResult has neither 'output' nor 'data': {dir(result)}")

        logger.info(f"AGENT RUN COMPLETE: {str(output)[:200]}...")
        return AgentResponse(result=str(output))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error running agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ---------------------------------------------

@app.post("/webhook")
@limiter.limit("5/minute")
async def handle_webhook(request: Request, payload: dict):
    """Generic webhook receiver. Passes raw payload to the agent."""
    import json
    logger.info(f"WEBHOOK RECEIVED: {json.dumps(payload)[:200]}...")

    with tracer.start_as_current_span("ai-agent.webhook") as span:
        span.set_attribute("service.name", "ai-agent")
        span.set_attribute("payload.keys", str(list(payload.keys())))

        try:
            prompt = f"Process this incoming webhook payload:\n{json.dumps(payload, indent=2)}"
            result = await agent.run(prompt)
            output = str(result.output)
            logger.info(f"WEBHOOK COMPLETE: {output[:200]}...")
            return {"status": "processed", "result": output}
        except Exception as e:
            logger.error(f"Webhook error: {e}")
            span.record_exception(e)
            return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
