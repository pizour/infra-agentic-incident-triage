import os
import json
import base64
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Dict

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from loguru import logger

from _shared.github_mcp import get_oauth_token, call_mcp

from pydantic_ai import Agent
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
    SERVICE_NAME: "input-guardrail",
    "openinference.project.name": "ai-agent-triage",
    "deployment.environment": ENVIRONMENT,
})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

propagate.set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()]))

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
GH_PERSONAL_ACCESS_TOKEN = os.getenv("GH_PERSONAL_ACCESS_TOKEN")
GH_OAUTH_APP_ID = os.getenv("GH_OAUTH_APP_ID")
GH_OAUTH_PRIVATE_KEY = os.getenv("GH_OAUTH_PRIVATE_KEY")
GH_OAUTH_INSTALLATION_ID = os.getenv("GH_OAUTH_INSTALLATION_ID")

# Pre-loaded content cache (filled at startup)
_skill_cache: Dict[str, str] = {}


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
    for block in content_blocks:
        if block.get("type") == "resource":
            return block.get("resource", {}).get("text", "")
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-load agent description and skills from GitHub at startup."""
    for key, path in [
        ("guardrail_agent", "agents/control-plane/input-guardrail.md"),
        ("guardrail_skill", "skills/input-guardrail/skill.md"),
        ("output_contract", "skills/agent_output_contract/skill.md"),
    ]:
        try:
            content = await _fetch_file_from_github(path)
            _skill_cache[key] = content
            logger.info(f"Loaded: {path} ({len(content)} chars)")
        except Exception as e:
            logger.error(f"Failed to load {path}: {e}")
            _skill_cache[key] = f"(unavailable: {e})"
    yield
    logger.info("Flushing OpenTelemetry spans...")
    provider.force_flush(timeout_millis=5000)


app = FastAPI(title="Input-Guardrail (Pydantic-AI)", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
Instrumentator().instrument(app).expose(app)

model = GoogleModel(MODEL_NAME, provider="google-vertex")


class InputGuardrailDecision(BaseModel):
    """Validation report only. Routing actions (finish/next_agent) are the
    Nexus Controller's responsibility — they are intentionally absent here."""
    safety_check: bool
    feedback: str = Field(..., max_length=500)
    masked_input: Optional[str] = None  # PII-scrubbed input when safety_check=true
    reasoning: str = Field(..., max_length=200)


BASE_GUARDRAIL_SYSTEM_PROMPT = (Path(__file__).parent / "system_prompt.md").read_text()


def build_system_prompt() -> str:
    return BASE_GUARDRAIL_SYSTEM_PROMPT.format(
        guardrail_agent=_skill_cache.get("guardrail_agent", "(not loaded)"),
        guardrail_skill=_skill_cache.get("guardrail_skill", "(not loaded)"),
        output_contract=_skill_cache.get("output_contract", "(not loaded)"),
    )


agent = Agent(
    model,
    output_type=InputGuardrailDecision,
    instrument=True,
)


@agent.system_prompt
def dynamic_system_prompt() -> str:
    return build_system_prompt()


class RunRequest(BaseModel):
    input: str
    context_summary: str = ""


@app.post("/run")
async def run_input_guardrail(run_request: RunRequest, raw_request: Request):
    logger.info(f"GUARDRAIL REQUEST: {run_request.input[:100]}...")
    upstream_ctx = propagate.extract(dict(raw_request.headers))
    token = attach(upstream_ctx)
    try:
        with tracer.start_as_current_span("input-guardrail.validation") as span:
            span.set_attribute("langfuse.observation.type", "span")
            span.set_attribute("langfuse.observation.input", run_request.input[:500])
            try:
                prompt = f"Validate this input.\n\nInput: {run_request.input}\nContext: {run_request.context_summary}"
                result = await asyncio.wait_for(agent.run(prompt), timeout=60.0)
                decision = getattr(result, "output", getattr(result, "data", None))
                if decision is None:
                    raise AttributeError(f"AgentRunResult has neither 'output' nor 'data': {dir(result)}")

                span.set_attribute("langfuse.observation.output", json.dumps(decision.model_dump()))
                logger.info(f"GUARDRAIL DECISION: safety_check={decision.safety_check}")
                return decision.model_dump()
            except asyncio.TimeoutError:
                span.set_attribute("langfuse.observation.level", "ERROR")
                logger.error("INPUT GUARDRAIL: agent.run exceeded 60s — failing closed")
                return InputGuardrailDecision(
                    safety_check=False,
                    feedback="Input validation timed out",
                    reasoning="Timeout",
                ).model_dump()
            except Exception as e:
                span.record_exception(e)
                logger.error(f"Guardrail execution failed: {e}")
                return InputGuardrailDecision(
                    safety_check=False,
                    feedback="Input validation failed",
                    reasoning=str(e)[:100],
                ).model_dump()
    finally:
        detach(token)


@app.get("/health")
async def health_check():
    return {"status": "ok", "message": "Input-Guardrail Pydantic-AI running"}
