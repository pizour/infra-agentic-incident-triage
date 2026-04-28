import os
import json
import base64
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List
from dotenv import load_dotenv

from _shared.github_mcp import get_oauth_token, call_mcp

load_dotenv()

# --- OpenTelemetry / Langfuse Setup ---
from opentelemetry import trace, propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from openinference.instrumentation.pydantic_ai import OpenInferenceSpanProcessor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import attach, detach
from prometheus_fastapi_instrumentator import Instrumentator


ENVIRONMENT = os.getenv("DEPLOYMENT_ENVIRONMENT")

# Initialize TracerProvider with Service Name
resource = Resource.create({
    SERVICE_NAME: "ai-agent",
    "openinference.project.name": "ai-agent-triage",
    "deployment.environment": ENVIRONMENT,
})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

# W3C TraceContext propagator — extracts traceparent from incoming requests to join parent trace
propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()]))

# OpenInference enrichment FIRST — must run before exporters so spans have LLM attributes
provider.add_span_processor(OpenInferenceSpanProcessor())

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
else:
    pass  # Will log after logger is imported

HTTPXClientInstrumentor().instrument()
# ---------------------------------------------

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

# --- SlowAPI Setup ---
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    logger.info("Flushing OpenTelemetry spans...")
    provider.force_flush(timeout_millis=5000)

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="AI Agent API", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- Shared config ---
GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH")
MCP_API_KEY = os.getenv("MCP_API_KEY")
GH_PERSONAL_ACCESS_TOKEN = os.getenv("GH_PERSONAL_ACCESS_TOKEN")

# --- GitHub OAuth Setup ---
GH_OAUTH_APP_ID = os.getenv("GH_OAUTH_APP_ID")
GH_OAUTH_PRIVATE_KEY = os.getenv("GH_OAUTH_PRIVATE_KEY")
GH_OAUTH_INSTALLATION_ID = os.getenv("GH_OAUTH_INSTALLATION_ID")

from pydantic_ai.models.google import GoogleModel
model_name = os.getenv("LLM_MODEL")
model = GoogleModel(model_name, provider="google-vertex")

DEFAULT_SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text().strip()

agent = Agent(
    model,
    system_prompt=os.getenv("SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT,
)


from loguru import logger

if langfuse_pk and langfuse_sk:
    logger.info(f"Langfuse OTLP exporter initialized: {langfuse_host}")
else:
    logger.warning("Langfuse credentials not set — OTLP export disabled")

@agent.tool
async def github(
    ctx: RunContext[None],
    action: str,
    path: str = None,
) -> str:
    """
    Interact with the GitHub repository via MCP.
    Actions:
      - read_skill: Read a skill/SOP or any file from GitHub (requires 'path')
      - list_directory: List files in a directory (requires 'path', e.g., 'agents/', 'skills/')
    """
    logger.info(f"GITHUB TOOL CALL: tool={action}, path={path}")
    with tracer.start_as_current_span(f"github.{action}") as span:
        span.set_attribute("tool", action)

        if not path:
            return "Error: 'path' required"

        # read_skill auto-prefixes paths under skills/ unless they already point at another tree
        if action == "read_skill" and not path.startswith(("skills/", "mcp/", "agents/")):
            path = f"skills/{path}"

        owner, repo = GITHUB_REPO.split('/')
        gh_token = await get_oauth_token(GH_OAUTH_APP_ID, GH_OAUTH_PRIVATE_KEY, GH_OAUTH_INSTALLATION_ID)
        gh_token = gh_token or GH_PERSONAL_ACCESS_TOKEN

        return await call_mcp(
            url=GITHUB_MCP_URL,
            tool_name="get_file_contents",
            arguments={"owner": owner, "repo": repo, "path": path},
            gh_token=gh_token,
        )


class AgentRequest(BaseModel):
    prompt: str
    system_prompt: Optional[str] = None  # Injected by orchestrator from agent .md frontmatter

class AgentResponse(BaseModel):
    result: str

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "AI Agent API is running"}

@app.post("/agent", response_model=AgentResponse)
async def run_agent(request: AgentRequest, raw_request: Request):
    """Standard agent endpoint for manual queries."""
    logger.info(f"RUNNING AGENT: prompt='{request.prompt[:100]}...'")
    # Extract traceparent injected by orchestrator's HTTPXClientInstrumentor
    upstream_ctx = propagate.extract(dict(raw_request.headers))
    token = attach(upstream_ctx)
    try:
        with tracer.start_as_current_span("ai-agent.run") as span:
            span.set_attribute("langfuse.observation.type", "generation")
            span.set_attribute("langfuse.observation.input", request.prompt[:2000])
            if request.system_prompt:
                span.set_attribute("langfuse.observation.metadata.has_system_prompt", "true")
                logger.info("Using orchestrator-provided system prompt")
                effective_prompt = f"<system_prompt>{request.system_prompt}</system_prompt>\n\n{request.prompt}"
            else:
                effective_prompt = request.prompt

            result = await agent.run(effective_prompt)

            # Robustly handle result attribute (Pydantic-AI 0.x uses .data, 1.x uses .output)
            output = getattr(result, "output", getattr(result, "data", None))
            if output is None:
                raise AttributeError(f"AgentRunResult has neither 'output' nor 'data': {dir(result)}")

            span.set_attribute("langfuse.observation.output", str(output)[:2000])
            span.set_attribute("langfuse.observation.model.name", os.getenv("LLM_MODEL"))
            logger.info(f"AGENT RUN COMPLETE: {str(output)[:200]}...")
            return AgentResponse(result=str(output))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error running agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        detach(token)

# ---------------------------------------------

@app.post("/webhook")
@limiter.limit("5/minute")
async def handle_webhook(request: Request, payload: dict):
    """Generic webhook receiver. Passes raw payload to the agent."""
    import json
    logger.info(f"WEBHOOK RECEIVED: {json.dumps(payload)[:200]}...")

    # Extract upstream trace context so this webhook span joins any existing trace
    upstream_ctx = propagate.extract(dict(request.headers))
    token = attach(upstream_ctx)
    try:
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
    finally:
        detach(token)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
