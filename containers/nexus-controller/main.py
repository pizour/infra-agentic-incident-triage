import os
import json
import base64
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from loguru import logger

from _shared.github_mcp import get_oauth_token, call_mcp

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.google import GoogleModel

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

resource = Resource.create({
    SERVICE_NAME: "nexus-controller",
    "openinference.project.name": "ai-agent-triage",
    "deployment.environment": ENVIRONMENT,
})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

# Register W3C propagators so incoming traceparent headers are honoured
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
    logger.info(f"Langfuse OTLP exporter initialized: {langfuse_host}")
else:
    logger.warning("Langfuse credentials not set — OTLP export disabled")

HTTPXClientInstrumentor().instrument()

# --- Configuration ---
MODEL_NAME = os.getenv("MODEL_NAME")
GITHUB_MCP_URL = os.getenv("GITHUB_MCP_URL")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH")
MCP_API_KEY = os.getenv("MCP_API_KEY")
GH_PERSONAL_ACCESS_TOKEN = os.getenv("GH_PERSONAL_ACCESS_TOKEN")

# --- GitHub OAuth Setup ---
GH_OAUTH_APP_ID = os.getenv("GH_OAUTH_APP_ID")
GH_OAUTH_PRIVATE_KEY = os.getenv("GH_OAUTH_PRIVATE_KEY")
GH_OAUTH_INSTALLATION_ID = os.getenv("GH_OAUTH_INSTALLATION_ID")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await load_registries()
    yield
    logger.info("Flushing OpenTelemetry spans...")
    provider.force_flush(timeout_millis=5000)

app = FastAPI(title="Nexus-Controller (Pydantic-AI)", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
Instrumentator().instrument(app).expose(app)

model = GoogleModel(MODEL_NAME, provider="google-vertex")

# --- Registry cache (loaded once at startup) ---
_registry_cache: Dict[str, str] = {}

async def _fetch_file_from_github(path: str) -> str:
    """Fetch a raw file from GitHub via MCP and return its decoded text content."""
    owner, repo = GITHUB_REPO.split('/')
    gh_token = await get_oauth_token(GH_OAUTH_APP_ID, GH_OAUTH_PRIVATE_KEY, GH_OAUTH_INSTALLATION_ID)
    gh_token = gh_token or GH_PERSONAL_ACCESS_TOKEN

    raw = await call_mcp(
        url=GITHUB_MCP_URL,
        tool_name="get_file_contents",
        arguments={"owner": owner, "repo": repo, "path": path},
        gh_token=gh_token,
    )
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(result, dict):
        return ""

    content_blocks = result.get("content", [])
    # Prefer resource blocks — they contain the actual file text
    for block in content_blocks:
        if block.get("type") == "resource":
            return block.get("resource", {}).get("text", "")
    # Fallback: text block with base64-encoded content (older MCP versions)
    for block in content_blocks:
        text = block.get("text", "")
        try:
            inner = json.loads(text)
            encoded = inner.get("content", "")
            if encoded:
                return base64.b64decode(encoded).decode("utf-8")
        except Exception:
            pass
    return ""


async def load_registries():
    """Fetch the agent description, routing skill, and registries from GitHub once and cache them."""
    for key, path in [
        ("nexus_agent", "agents/control-plane/nexus-controller.md"),
        ("nexus_routing", "skills/nexus_routing/skill.md"),
        ("agents", "agents/REGISTRY.md"),
        ("skills", "skills/REGISTRY.md"),
    ]:
        try:
            content = await _fetch_file_from_github(path)
            _registry_cache[key] = content
            logger.info(f"Loaded {path} ({len(content)} chars)")
        except Exception as e:
            logger.error(f"Failed to load {path}: {e}")
            _registry_cache[key] = f"(unavailable: {e})"

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
    action: Literal["next_agent", "retry", "finish"]
    feedback: str
    target_agent: Optional[str] = None
    agent_env_vars: Optional[dict] = None  # env_vars from agent .md frontmatter (e.g. SYSTEM_PROMPT)

BASE_NEXUS_SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text()

def build_system_prompt() -> str:
    return BASE_NEXUS_SYSTEM_PROMPT.format(
        nexus_agent=_registry_cache.get("nexus_agent", "(not loaded)"),
        routing_rules=_registry_cache.get("nexus_routing", "(not loaded)"),
        agent_registry=_registry_cache.get("agents", "(not loaded)"),
        skill_registry=_registry_cache.get("skills", "(not loaded)"),
    )

agent = Agent(
    model,
    output_type=NexusRoutingDecision,
    instrument=True,
)

@agent.system_prompt
def dynamic_system_prompt() -> str:
    """Returns the system prompt with registry content injected at call time."""
    return build_system_prompt()

@agent.tool
async def github(
    ctx: RunContext[None],
    tool: str,
    path: Optional[str] = None,
) -> str:
    """
    Call GitHub MCP tools. The agent specifies which tool to use.
    Examples:
      - tool='get_file_contents', path='/skills/nexus_routing/skill.md'
      - tool='get_file_contents', path='agents/'  (works for directories too)
    """
    logger.info(f"GITHUB TOOL CALL: tool={tool}, path={path}")

    if not path:
        return "Error: 'path' required"

    owner, repo = GITHUB_REPO.split('/')
    gh_token = await get_oauth_token(GH_OAUTH_APP_ID, GH_OAUTH_PRIVATE_KEY, GH_OAUTH_INSTALLATION_ID)
    gh_token = gh_token or GH_PERSONAL_ACCESS_TOKEN

    return await call_mcp(
        url=GITHUB_MCP_URL,
        tool_name="get_file_contents",
        arguments={"owner": owner, "repo": repo, "path": path},
        gh_token=gh_token,
    )

class RunRequest(BaseModel):
    input: str
    context_summary: str = ""
    latest_validation: Optional[Dict[str, Any]] = None

@app.post("/run")
async def run_nexus_controller(run_request: RunRequest, raw_request: Request):
    logger.info(f"NEXUS CONTROLLER REQUEST: {run_request.input[:100]}...")
    # Extract the traceparent from the orchestrator so all spans share one trace
    upstream_ctx = propagate.extract(dict(raw_request.headers))
    token = attach(upstream_ctx)
    try:
        latest_agent = (run_request.latest_validation or {}).get("agent_key", "none")
        with tracer.start_as_current_span("nexus-controller.routing") as span:
            span.set_attribute("langfuse.observation.type", "span")
            span.set_attribute("langfuse.observation.input", json.dumps({
                "goal": run_request.input[:300],
                "latest_agent": latest_agent,
            }))
            span.set_attribute("langfuse.observation.metadata.latest_agent", latest_agent)
            try:
                prompt = f"Goal: {run_request.input}\nContext: {run_request.context_summary}\nLatest Validation: {json.dumps(run_request.latest_validation or {})}"
                result = await asyncio.wait_for(
                    agent.run(prompt),
                    timeout=180.0,
                )

                decision = getattr(result, "output", getattr(result, "data", None))
                if decision is None:
                    raise AttributeError(f"AgentRunResult has neither 'output' nor 'data': {dir(result)}")

                span.set_attribute("langfuse.observation.output", json.dumps(decision.model_dump()))
                logger.info(f"ROUTING DECISION COMPLETE: {decision.action}")
                return decision.model_dump()
            except asyncio.TimeoutError:
                span.set_attribute("langfuse.observation.level", "ERROR")
                span.set_attribute("langfuse.observation.status_message", "Routing timed out")
                logger.error("NEXUS CONTROLLER: agent.run exceeded 180s timeout, returning finish")
                return NexusRoutingDecision(action="finish", feedback="Routing decision timed out", target_agent=None).model_dump()
            except Exception as e:
                span.record_exception(e)
                span.set_attribute("langfuse.observation.level", "ERROR")
                logger.error(f"Controller execution failed: {e}")
                raise HTTPException(status_code=500, detail=str(e))
    finally:
        detach(token)

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Nexus-Controller Pydantic-AI running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8010)
