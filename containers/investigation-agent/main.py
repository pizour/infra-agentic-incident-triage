import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

# --- OpenTelemetry Setup ---
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from openinference.instrumentation.pydantic_ai import OpenInferenceSpanProcessor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from prometheus_fastapi_instrumentator import Instrumentator

resource = Resource.create({SERVICE_NAME: "investigation-agent"})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://phoenix:6006/v1/traces")
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))

provider.add_span_processor(OpenInferenceSpanProcessor())
HTTPXClientInstrumentor().instrument()
# ---------------------------

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# PydanticAI imports
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from mcp import ClientSession
from mcp.client.sse import sse_client

# --- Shared config ---
# _MODEL_NAME = os.getenv("LLM_MODEL", "llama3.1:8b")
# _OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434/v1")

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://linux-mcp-server:8001/sse")
MCP_API_KEY = os.getenv("MCP_API_KEY", "")

# def make_model() -> OpenAIChatModel:
#     return OpenAIChatModel(
#         _MODEL_NAME,
#         provider=OpenAIProvider(base_url=_OLLAMA_BASE_URL, api_key="ollama"),
#     )

from pydantic_ai.models.google import GoogleModel

def make_model() -> GoogleModel:
    return GoogleModel(os.getenv("LLM_MODEL", "gemini-2.5-flash"), provider="google-vertex")


async def mcp_exec(command: str, host: str, span=None) -> str:
    headers = {"X-MCP-API-Key": MCP_API_KEY}
    try:
        async with sse_client(MCP_SERVER_URL, headers=headers) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool(
                    "execute_command", arguments={"command": command, "host": host}
                )
                output = str(result.content[0].text) if result.content else "(no output)"
                if span:
                    span.set_attribute("output_length", len(output))
                return output
    except Exception as e:
        err = f"MCP error: {e}"
        if span:
            span.record_exception(e)
        return err


@dataclass
class InvestigationDeps:
    host_ip: str
    source_ip: str


# --- Investigation Agent Definition ---
investigation_agent: Agent[InvestigationDeps, str] = Agent(
    make_model(),
    instrument=True,
    deps_type=InvestigationDeps,
    result_type=str,
    system_prompt=(
        "You are a security data-collection agent. "
        "Your ONLY job is to call get_auth_logs, get_system_stats, and "
        "get_active_connections, then return ALL of their raw output "
        "concatenated into a single evidence block. "
        "DO NOT interpret, analyse, or summarise. Just collect and return everything."
    ),
)


@investigation_agent.tool
async def get_auth_logs(ctx: RunContext[InvestigationDeps], lines: int = 50) -> str:
    with tracer.start_as_current_span("investigation_agent.get_auth_logs") as span:
        span.set_attribute("service.name", "investigation-agent")
        span.set_attribute("tool.name", "get_auth_logs")
        span.set_attribute("tool.input.host_ip", ctx.deps.host_ip)
        span.set_attribute("tool.input.source_ip", ctx.deps.source_ip)

        if ctx.deps.source_ip and ctx.deps.source_ip != "unknown":
            cmd = f"grep '{ctx.deps.source_ip}' /var/log/auth.log | tail -n {lines}"
        else:
            cmd = f"tail -n {lines} /var/log/auth.log"

        output = await mcp_exec(cmd, ctx.deps.host_ip, span)
        span.set_attribute("tool.status", "success")
        return output


@investigation_agent.tool
async def get_system_stats(ctx: RunContext[InvestigationDeps]) -> str:
    with tracer.start_as_current_span("investigation_agent.get_system_stats") as span:
        span.set_attribute("service.name", "investigation-agent")
        span.set_attribute("tool.name", "get_system_stats")
        span.set_attribute("tool.input.host_ip", ctx.deps.host_ip)

        output = await mcp_exec("top -bn1 | head -n 20", ctx.deps.host_ip, span)
        span.set_attribute("tool.status", "success")
        return output


@investigation_agent.tool
async def get_active_connections(ctx: RunContext[InvestigationDeps]) -> str:
    with tracer.start_as_current_span("investigation_agent.get_active_connections") as span:
        span.set_attribute("service.name", "investigation-agent")
        span.set_attribute("tool.name", "get_active_connections")
        span.set_attribute("tool.input.host_ip", ctx.deps.host_ip)

        output = await mcp_exec("ss -tuln", ctx.deps.host_ip, span)
        span.set_attribute("tool.status", "success")
        return output


@investigation_agent.tool
async def run_remote_command(ctx: RunContext[InvestigationDeps], command: str) -> str:
    with tracer.start_as_current_span("investigation_agent.run_remote_command") as span:
        span.set_attribute("service.name", "investigation-agent")
        span.set_attribute("tool.name", "run_remote_command")
        span.set_attribute("tool.input.host_ip", ctx.deps.host_ip)
        span.set_attribute("tool.input.command", command)

        output = await mcp_exec(command, ctx.deps.host_ip, span)
        span.set_attribute("tool.status", "success")
        return output


# --- FastAPI Implementation ---
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Investigation Agent API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

FastAPIInstrumentor.instrument_app(app)
Instrumentator().instrument(app).expose(app)


class RunRequest(BaseModel):
    host: str
    host_ip: str
    source_ip: str


class RunResponse(BaseModel):
    evidence: str


@app.get("/")
def health_check():
    return {"status": "ok", "service": "investigation-agent"}


@app.post("/run", response_model=RunResponse)
async def run_agent(req: RunRequest):
    """
    Kicks off the PydanticAI investigation agent to gather evidence from the host.
    """
    with tracer.start_as_current_span("investigation_agent.run") as span:
        span.set_attribute("request.host", req.host)
        span.set_attribute("request.host_ip", req.host_ip)
        span.set_attribute("request.source_ip", req.source_ip)

        deps = InvestigationDeps(host_ip=req.host_ip, source_ip=req.source_ip)
        prompt = (
            f"Investigate a security alert on host {req.host} ({req.host_ip}). "
            f"Attacker IP: {req.source_ip}. "
            "Call get_auth_logs, get_system_stats, and get_active_connections. "
            "Return ALL raw tool output concatenated."
        )

        try:
            result = await investigation_agent.run(prompt, deps=deps)
            evidence = str(result.output)
            span.set_attribute("evidence_length", len(evidence))
            return RunResponse(evidence=evidence)
        except Exception as e:
            span.record_exception(e)
            raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8004)
